# cogs/games/fishing.py (임시 명령어 삭제 최종본)

import discord
from discord.ext import commands
from discord import ui
# [🔴 핵심 수정] app_commands import를 제거합니다.
import random
import asyncio
import logging
from typing import Optional, Set, Dict

from utils.database import (
    update_wallet, get_inventory, update_inventory, add_to_aquarium,
    get_user_gear, set_user_gear, save_panel_id, get_panel_id, get_id,
    get_embed_from_db, get_panel_components_from_db,
    get_item_database, get_fishing_loot, get_config, get_string,
    is_legendary_fish_available, set_legendary_fish_cooldown,
    BARE_HANDS, DEFAULT_ROD
)

logger = logging.getLogger(__name__)

class FishingGameView(ui.View):
    def __init__(self, bot: commands.Bot, user: discord.Member, used_rod: str, used_bait: str, remaining_baits: Dict[str, int], cog_instance: 'Fishing', location_type: str):
        super().__init__(timeout=35)
        self.bot = bot; self.player = user; self.message: Optional[discord.WebhookMessage] = None
        self.game_state = "waiting"; self.game_task: Optional[asyncio.Task] = None
        self.used_rod = used_rod; self.used_bait = used_bait; self.remaining_baits = remaining_baits
        self.fishing_cog = cog_instance
        self.location_type = location_type

        item_db = get_item_database()
        rod_data = item_db.get(self.used_rod, {})
        bait_data = item_db.get(self.used_bait, {})

        self.rod_bonus = rod_data.get("good_fish_bonus", 0.0)
        self.bite_range = bait_data.get("bite_time_range") if bait_data and bait_data.get("bite_time_range") else [10.0, 15.0]
        self.bite_reaction_time = get_config("FISHING_BITE_REACTION_TIME", 3.0)
        self.big_catch_threshold = get_config("FISHING_BIG_CATCH_THRESHOLD", 70.0)

    async def start_game(self, interaction: discord.Interaction, embed: discord.Embed):
        self.message = await interaction.followup.send(embed=embed, view=self, ephemeral=True)
        self.game_task = asyncio.create_task(self.game_flow())

    async def game_flow(self):
        try:
            await asyncio.sleep(random.uniform(*self.bite_range))
            if self.is_finished(): return
            self.game_state = "biting"
            if self.children and isinstance(catch_button := self.children[0], ui.Button):
                catch_button.style = discord.ButtonStyle.success; catch_button.label = "釣り上げる！"
            
            embed = discord.Embed(title="❗ アタリが来た！", description="今だ！ボタンを押して釣り上げよう！", color=discord.Color.red())
            if waiting_image_url := get_config("FISHING_WAITING_IMAGE_URL"):
                embed.set_image(url=waiting_image_url.strip('"'))

            if self.message: await self.message.edit(embed=embed, view=self)
            await asyncio.sleep(self.bite_reaction_time)
            if not self.is_finished() and self.game_state == "biting":
                embed = discord.Embed(title="💧 逃げられた…", description=f"{self.player.mention}さんは反応が遅れてしまいました。", color=discord.Color.greyple())
                await self._send_result(embed); self.stop()
        except asyncio.CancelledError: pass
        except Exception as e:
            logger.error(f"{self.player.display_name}의 낚시 게임 흐름 중 오류: {e}", exc_info=True)
            if not self.is_finished():
                await self._send_result(discord.Embed(title="❌ エラー発生", description="釣りの処理中に予期せぬエラーが発生しました。", color=discord.Color.red())); self.stop()

    async def _handle_catch_logic(self) -> tuple[discord.Embed, bool, bool, bool]:
        all_loot = get_fishing_loot()
        location_map = {"river": "강", "sea": "바다"}
        current_location_name = location_map.get(self.location_type, "강")
        
        # [수정] 쓰레기는 공통으로 잡히도록 로직 변경
        base_loot = [item for item in all_loot if item.get('location_type') == current_location_name or item.get('location_type') is None]

        is_legendary_available = self.used_rod == "伝説の釣竿" and await is_legendary_fish_available()
        loot_pool = [item for item in base_loot if item['name'] != 'クジラ'] # 고래는 따로 처리
        
        if is_legendary_available:
            if legendary_fish := next((item for item in base_loot if item['name'] == 'クジラ'), None):
                loot_pool.append(legendary_fish)
        
        if not loot_pool:
            logger.warning(f"'{current_location_name}' 장소에 대한 loot 테이블이 비어있습니다.")
            return (discord.Embed(title="エラー", description="この場所では何も釣れないようです。", color=discord.Color.red()), False, False, False)

        weights = [item['weight'] * (1 + self.rod_bonus if item.get('base_value') is not None or item.get('value', 0) > 0 else 1) for item in loot_pool]
        
        catch_proto = random.choices(loot_pool, weights=weights, k=1)[0]
        
        is_legendary_catch = catch_proto['name'] == 'クジラ'
        is_big_catch = log_publicly = False
        embed = discord.Embed()
        
        # [🔴 핵심 수정] min_size가 있는지 없는지로 물고기/아이템 구분
        if catch_proto.get("min_size") is not None:
            log_publicly = True
            size = round(random.uniform(catch_proto["min_size"], catch_proto["max_size"]), 1)
            if is_legendary_catch: await set_legendary_fish_cooldown()
            await add_to_aquarium(str(self.player.id), {"name": catch_proto['name'], "size": size}) # emoji 제외
            is_big_catch = size >= self.big_catch_threshold
            
            title = "🏆 大物を釣り上げた！ 🏆" if is_big_catch else "🎉 釣り成功！ 🎉"
            if is_legendary_catch: title = "👑 伝説の魚を釣り上げた！！ 👑"

            embed.title = title
            embed.description = f"{self.player.mention}さんが釣りに成功しました！"
            embed.color = discord.Color.gold() if is_legendary_catch else discord.Color.blue()
            embed.add_field(name="魚", value=f"**{catch_proto['name']}**", inline=True)
            embed.add_field(name="サイズ", value=f"`{size}`cm", inline=True)
        else: # 쓰레기 또는 복어
            log_publicly = catch_proto.get("log_publicly", False)
            value = catch_proto.get('value', 0)
            if value != 0: await update_wallet(self.player, value)
            
            embed.title = catch_proto['title']
            # description에 value가 음수일 경우를 대비
            embed.description = catch_proto['description'].format(user_mention=self.player.mention, value=abs(value))
            embed.color = int(catch_proto['color'], 16) if isinstance(catch_proto['color'], str) else catch_proto['color']

        if image_url := catch_proto.get('image_url'):
            embed.set_image(url=image_url)
            
        return embed, log_publicly, is_big_catch, is_legendary_catch

    @ui.button(label="待機中...", style=discord.ButtonStyle.secondary, custom_id="catch_fish_button")
    async def catch_button(self, interaction: discord.Interaction, button: ui.Button):
        if self.game_task: self.game_task.cancel()
        result_embed, log_publicly, is_big_catch, is_legendary = None, False, False, False
        if self.game_state == "waiting":
            await interaction.response.defer()
            result_embed = discord.Embed(title="❌ 早すぎ！", description=f"{interaction.user.mention}さんは焦ってしまい、魚に気づかれてしまいました…", color=discord.Color.dark_grey())
        elif self.game_state == "biting":
            await interaction.response.defer(); self.game_state = "finished"
            result_embed, log_publicly, is_big_catch, is_legendary = await self._handle_catch_logic()
        if result_embed:
            if self.player.display_avatar: result_embed.set_thumbnail(url=self.player.display_avatar.url)
            await self._send_result(result_embed, log_publicly, is_big_catch, is_legendary)
        self.stop()

    async def _send_result(self, embed: discord.Embed, log_publicly: bool = False, is_big_catch: bool = False, is_legendary: bool = False):
        remaining_baits_config = get_config("FISHING_REMAINING_BAITS_DISPLAY", ["一般の釣りエサ", "高級釣りエサ"])
        footer_private = f"残りのエサ: {' / '.join([f'{b}({self.remaining_baits.get(b, 0)}個)' for b in remaining_baits_config])}"
        footer_public = f"使用した装備: {self.used_rod} / {self.used_bait}"
        if log_publicly:
            if is_legendary:
                await self.bot.get_cog("Fishing").log_legendary_catch(self.player, embed)
            elif (fishing_cog := self.bot.get_cog("Fishing")) and (log_ch_id := fishing_cog.fishing_log_channel_id) and (log_ch := self.bot.get_channel(log_ch_id)):
                public_embed = embed.copy()
                public_embed.set_footer(text=footer_public)
                content = self.player.mention if is_big_catch else None
                try: await log_ch.send(content=content, embed=public_embed, allowed_mentions=discord.AllowedMentions(users=is_big_catch))
                except Exception as e: logger.error(f"공개 낚시 로그 전송 실패: {e}", exc_info=True)
        embed.set_footer(text=f"{footer_public}\n{footer_private}")
        if self.message:
            try:
                await self.message.edit(embed=embed, view=None)
                self.fishing_cog.last_result_messages[self.player.id] = self.message
            except (discord.NotFound, AttributeError, discord.HTTPException): pass

    async def on_timeout(self):
        if self.game_state != "finished":
            embed = discord.Embed(title="⏱️ 時間切れ", description=f"{self.player.mention}さんは時間内に反応がありませんでした。", color=discord.Color.darker_grey())
            await self._send_result(embed)
        self.stop()

    def stop(self):
        if self.game_task and not self.game_task.done(): self.game_task.cancel()
        self.fishing_cog.active_fishing_sessions_by_user.discard(self.player.id)
        super().stop()

class FishingPanelView(ui.View):
    def __init__(self, bot: commands.Bot, cog_instance: 'Fishing'):
        super().__init__(timeout=None)
        self.bot = bot
        self.fishing_cog = cog_instance
        self.user_locks: Dict[int, asyncio.Lock] = {}

    async def setup_buttons(self):
        self.clear_items()
        components_data = await get_panel_components_from_db('fishing')
        for comp in components_data:
            if comp.get('component_type') == 'button' and (key := comp.get('component_key')):
                style_str = comp.get('style', 'secondary')
                style = discord.ButtonStyle[style_str] if hasattr(discord.ButtonStyle, style_str) else discord.ButtonStyle.secondary
                button = ui.Button(label=comp.get('label'), style=style, emoji=comp.get('emoji'), row=comp.get('row'), custom_id=key)
                if key.startswith('start_fishing_'):
                    button.callback = self._start_fishing_callback
                self.add_item(button)

    # [🔴 핵심 수정] 동시 클릭으로 인한 상호작용 실패 방지 로직 적용
    async def _start_fishing_callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        lock = self.user_locks.setdefault(user_id, asyncio.Lock())

        # 만약 이 유저의 다른 낚시 작업이 이미 실행 중이라면,
        # 기다리지 않고 즉시 응답하여 상호작용 시간 초과를 방지합니다.
        if lock.locked():
            await interaction.response.send_message("現在、以前のリクエストを処理中です。しばらくお待ちください。", ephemeral=True, delete_after=5)
            return

        # 이제 안전하게 잠금을 획득하고 작업을 시작합니다.
        async with lock:
            # 작업 시작 전에 최신 상태를 다시 한번 확인
            if user_id in self.fishing_cog.active_fishing_sessions_by_user:
                await interaction.response.send_message("すでに釣りを開始しています。", ephemeral=True, delete_after=5)
                return

            await interaction.response.defer(ephemeral=True)
            
            # 이전 낚시 결과 메시지가 있다면 삭제
            if last_message := self.fishing_cog.last_result_messages.pop(user_id, None):
                try: await last_message.delete()
                except (discord.NotFound, discord.Forbidden): pass
            
            try:
                custom_id = interaction.data['custom_id']
                location_type = custom_id.split('_')[-1]
                
                uid_str = str(user_id)
                gear, inventory = await asyncio.gather(get_user_gear(uid_str), get_inventory(uid_str))
                rod = gear.get('rod', BARE_HANDS)
                item_db = get_item_database()

                if rod == BARE_HANDS:
                    has_any_rod = any('竿' in item_name for item_name in inventory if item_db.get(item_name, {}).get('category') == '釣り')
                    if not has_any_rod:
                        await interaction.followup.send(f"❌ 釣りをするには、まず商店で「{DEFAULT_ROD}」を購入してください。", ephemeral=True)
                        return
                    else:
                        await interaction.followup.send("❌ プロフィール画面から釣竿を装備してください。", ephemeral=True)
                        return
                
                self.fishing_cog.active_fishing_sessions_by_user.add(user_id)

                bait = gear.get('bait', 'エサなし')
                if bait != "エサなし":
                    if inventory.get(bait, 0) > 0:
                        await update_inventory(uid_str, bait, -1)
                        inventory[bait] = inventory.get(bait, 0) - 1
                    else:
                        bait = "エサなし"
                        await set_user_gear(uid_str, bait="エサなし")

                location_name = "川" if location_type == "river" else "海"
                desc = f"### {location_name}にウキを投げました。\n**🎣 使用中の釣竿:** `{rod}`\n**🐛 使用中のエサ:** `{bait}`"
                embed = discord.Embed(title=f"🎣 {location_name}釣りを開始しました！", description=desc, color=discord.Color.light_grey())
                
                if waiting_image_url := get_config("FISHING_WAITING_IMAGE_URL"):
                    embed.set_image(url=waiting_image_url.strip('"'))

                view = FishingGameView(self.bot, interaction.user, rod, bait, inventory, self.fishing_cog, location_type)
                await view.start_game(interaction, embed)
            except Exception as e:
                # 에러 발생 시 세션에서 유저를 확실히 제거
                self.fishing_cog.active_fishing_sessions_by_user.discard(user_id)
                logger.error(f"낚시 게임 시작 중 예측 못한 오류: {e}", exc_info=True)
                await interaction.followup.send(f"❌ 釣りの開始中に予期せぬエラーが発生しました。", ephemeral=True)


class Fishing(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_fishing_sessions_by_user: Set[int] = set()
        self.fishing_log_channel_id: Optional[int] = None
        self.view_instance = None
        self.last_result_messages: Dict[int, discord.Message] = {}
        logger.info("Fishing Cog가 성공적으로 초기화되었습니다.")

    async def register_persistent_views(self):
        self.view_instance = FishingPanelView(self.bot, self)
        await self.view_instance.setup_buttons()
        self.bot.add_view(self.view_instance)

    async def cog_load(self):
        await self.load_configs()

    async def load_configs(self):
        self.fishing_log_channel_id = get_id("fishing_log_channel_id")

    async def log_legendary_catch(self, user: discord.Member, result_embed: discord.Embed):
        if not self.fishing_log_channel_id or not (log_channel := self.bot.get_channel(self.fishing_log_channel_id)): return

        fish_field = next((f for f in result_embed.fields if f.name == "魚"), None)
        size_field = next((f for f in result_embed.fields if f.name == "サイズ"), None)
        if not all([fish_field, size_field]): return

        fish_name_raw = fish_field.value.replace('**', '')
        fish_data = next((loot for loot in get_fishing_loot() if loot['name'] == fish_name_raw), None)
        if not fish_data: return

        size_cm_str = size_field.value.strip('`cm`')
        size_cm = float(size_cm_str)
        value = int(fish_data.get("base_value", 0) + (size_cm * fish_data.get("size_multiplier", 0)))

        field_value = get_string("log_legendary_catch.field_value", emoji=fish_data.get('emoji','👑'), name=fish_name_raw, size=size_cm_str, value=f"{value:,}", currency_icon=get_config('CURRENCY_ICON', '🪙'))

        embed = discord.Embed(
            title=get_string("log_legendary_catch.title"),
            description=get_string("log_legendary_catch.description", user_mention=user.mention),
            color=int(get_string("log_legendary_catch.color", "0xFFD700").replace("0x", ""), 16)
        )
        embed.add_field(name=get_string("log_legendary_catch.field_name"), value=field_value)
        if user.display_avatar: embed.set_thumbnail(url=user.display_avatar.url)

        if image_url := fish_data.get('image_url'):
            embed.set_image(url=image_url)

        try:
            await log_channel.send(content="@here", embed=embed, allowed_mentions=discord.AllowedMentions(everyone=True))
        except Exception as e:
            logger.error(f"전설의 물고기 공지 전송 실패: {e}", exc_info=True)

    async def regenerate_panel(self, channel: discord.TextChannel):
        panel_key, embed_key = "fishing", "panel_fishing"
        if (panel_info := get_panel_id(panel_key)) and (old_id := panel_info.get('message_id')):
            try: await (await channel.fetch_message(old_id)).delete()
            except (discord.NotFound, discord.Forbidden): pass
        if not (embed_data := await get_embed_from_db(embed_key)):
            return logger.error(f"DB에서 '{embed_key}' 임베드를 찾을 수 없어 패널 생성을 중단합니다.")
        embed = discord.Embed.from_dict(embed_data)
        self.view_instance = FishingPanelView(self.bot, self)
        await self.view_instance.setup_buttons()
        self.bot.add_view(self.view_instance)
        new_message = await channel.send(embed=embed, view=self.view_instance)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ 낚시터 패널을 성공적으로 새로 생성했습니다. (채널: #{channel.name})")

    # [🔴 핵심 수정] /checkimages 명령어 전체를 삭제합니다.

async def setup(bot: commands.Bot):
    await bot.add_cog(Fishing(bot))
