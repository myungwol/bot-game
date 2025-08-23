# cogs/games/fishing.py

import discord
from discord.ext import commands
from discord import ui
import random
import asyncio
import logging
import time
import json
from typing import Optional, Set, Dict, List

from utils.database import (
    update_wallet, get_inventory, update_inventory, add_to_aquarium,
    get_user_gear, set_user_gear, save_panel_id, get_panel_id, get_id,
    get_embed_from_db, supabase, get_item_database, get_fishing_loot, 
    get_config, get_string, save_config_to_db,
    is_whale_available, set_whale_caught,
    BARE_HANDS, DEFAULT_ROD,
    increment_progress,
    get_user_abilities
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

INTERMEDIATE_ROD_NAME = "鉄の釣竿"

class FishingGameView(ui.View):
    def __init__(self, bot: commands.Bot, user: discord.Member, used_rod: str, used_bait: str, remaining_baits: Dict[str, int], cog_instance: 'Fishing', location_type: str, bite_range: List[float]):
        super().__init__(timeout=35)
        self.bot = bot; self.player = user; self.message: Optional[discord.WebhookMessage] = None
        self.game_state = "waiting"; self.game_task: Optional[asyncio.Task] = None
        self.used_rod = used_rod; self.used_bait = used_bait; self.remaining_baits = remaining_baits
        self.fishing_cog = cog_instance
        self.location_type = location_type
        
        item_db = get_item_database()
        self.rod_data = item_db.get(self.used_rod, {})
        
        self.bite_range = bite_range
        game_config = get_config("GAME_CONFIG", {})
        self.bite_reaction_time = game_config.get("FISHING_BITE_REACTION_TIME", 3.0)
        self.big_catch_threshold = game_config.get("FISHING_BIG_CATCH_THRESHOLD", 70.0)

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
            if self.message: await self.message.edit(embed=embed, view=self)
            await asyncio.sleep(self.bite_reaction_time)
            if not self.is_finished() and self.game_state == "biting":
                embed = discord.Embed(title="💧 逃げられた…", description=f"{self.player.mention}さんは反応が遅れてしまいました。", color=discord.Color.greyple())
                await self._send_result(embed); self.stop()
        except asyncio.CancelledError: pass
        except Exception as e:
            logger.error(f"{self.player.display_name}의낚시 게임 흐름 중 오류: {e}", exc_info=True)
            if not self.is_finished():
                await self._send_result(discord.Embed(title="❌ エラー発生", description="釣りの処理中に予期せぬエラーが発生しました。", color=discord.Color.red())); self.stop()

    async def _handle_catch_logic(self) -> tuple[discord.Embed, bool, bool, bool]:
        all_loot = get_fishing_loot()
        location_map = {"river": "川", "sea": "海"}
        current_location_name = location_map.get(self.location_type, "川")
        base_loot = [item for item in all_loot if item.get('location_type') == current_location_name or item.get('location_type') is None]

        rod_data = self.rod_data
        rod_tier = rod_data.get('tier', 0)
        rod_bonus = rod_data.get('loot_bonus', 0.0)
        
        loot_pool = []
        is_whale_catchable = await is_whale_available()

        for item in base_loot:
            if item.get('name') == 'クジラ':
                if rod_tier >= 5 and is_whale_catchable:
                    loot_pool.append(item)
            else:
                loot_pool.append(item)

        if not loot_pool:
            return (discord.Embed(title="エラー", description="この場所では何も釣れないようです。", color=discord.Color.red()), False, False, False)
        
        user_abilities = await get_user_abilities(self.player.id) or []
        rare_up_bonus = 0.2 if 'fish_rare_up_2' in user_abilities else 0.0
        size_multiplier = 1.2 if 'fish_size_up_2' in user_abilities else 1.0
        
        weights = []
        for item in loot_pool:
            weight = item['weight']
            base_value = item.get('base_value')
            if base_value is None: base_value = 0
            if base_value > 100:
                weight *= (1.0 + rod_bonus + rare_up_bonus)
            else:
                weight *= (1.0 + rod_bonus)
            weights.append(weight)

        catch_proto = random.choices(loot_pool, weights=weights, k=1)[0]
        is_whale_catch = catch_proto.get('name') == 'クジラ'
        is_big_catch, log_publicly = False, False
        
        embed = discord.Embed()
        if catch_proto.get("min_size") is not None:
            log_publicly = True
            min_s, max_s = catch_proto["min_size"] * size_multiplier, catch_proto["max_size"] * size_multiplier
            size = round(random.uniform(min_s, max_s), 1)

            if is_whale_catch:
                await set_whale_caught()

            await add_to_aquarium(str(self.player.id), {"name": catch_proto['name'], "size": size, "emoji": catch_proto.get('emoji', '🐠')})
            is_big_catch = size >= self.big_catch_threshold
            await increment_progress(self.player.id, fish_count=1)

            xp_to_add = get_config("GAME_CONFIG", {}).get("XP_FROM_FISHING", 20)
            res = await supabase.rpc('add_xp', {'p_user_id': self.player.id, 'p_xp_to_add': xp_to_add, 'p_source': 'fishing'}).execute()
            if res and res.data and (core_cog := self.bot.get_cog("EconomyCore")):
                await core_cog.handle_level_up_event(self.player, res.data[0])

            title = "🏆 大物を釣り上げた！ 🏆" if is_big_catch else "🎉 釣り成功！ 🎉"
            if is_whale_catch: title = "🐋 今月のヌシ、クジラを釣り上げた！！ 🐋"
            embed.title, embed.description, embed.color = title, f"{self.player.mention}さんが釣りに成功しました！", discord.Color.blue()
            embed.add_field(name="魚", value=f"{catch_proto.get('emoji', '🐠')} **{catch_proto['name']}**", inline=True)
            embed.add_field(name="サイズ", value=f"`{size}`cm", inline=True)
        else:
            value = catch_proto.get('value') or 0
            if value != 0: await update_wallet(self.player, value)
            embed.title, embed.description, embed.color = catch_proto['title'], catch_proto['description'].format(user_mention=self.player.mention, value=abs(value)), int(catch_proto['color'], 16) if isinstance(catch_proto['color'], str) else catch_proto['color']
        
        if image_url := catch_proto.get('image_url'):
            embed.set_thumbnail(url=image_url)
            
        return embed, log_publicly, is_big_catch, is_whale_catch

    @ui.button(label="待機中...", style=discord.ButtonStyle.secondary, custom_id="catch_fish_button")
    async def catch_button(self, interaction: discord.Interaction, button: ui.Button):
        if self.game_task: self.game_task.cancel()
        result_embed, log_publicly, is_big_catch, is_whale = None, False, False, False
        if self.game_state == "waiting":
            await interaction.response.defer()
            result_embed = discord.Embed(title="❌ 早すぎ！", description=f"{interaction.user.mention}さんは焦ってしまい、魚に気づかれてしまいました…", color=discord.Color.dark_grey())
        elif self.game_state == "biting":
            await interaction.response.defer(); self.game_state = "finished"
            result_embed, log_publicly, is_big_catch, is_whale = await self._handle_catch_logic()
        if result_embed:
            if self.player.display_avatar and not result_embed.thumbnail: 
                result_embed.set_thumbnail(url=self.player.display_avatar.url)
            await self._send_result(result_embed, log_publicly, is_big_catch, is_whale)
        self.stop()

    async def _send_result(self, embed: discord.Embed, log_publicly: bool = False, is_big_catch: bool = False, is_whale: bool = False):
        remaining_baits_config = get_config("FISHING_REMAINING_BAITS_DISPLAY", ['普通の釣りエサ', '高級釣りエサ'])
        footer_private = f"残りのエサ: {' / '.join([f'{b}({self.remaining_baits.get(b, 0)}個)' for b in remaining_baits_config])}"
        footer_public = f"使用した装備: {self.used_rod} / {self.used_bait}"
        if log_publicly:
            if is_whale:
                await self.fishing_cog.log_whale_catch(self.player, embed)
            elif (log_ch_id := self.fishing_cog.fishing_log_channel_id) and (log_ch := self.bot.get_channel(log_ch_id)):
                public_embed = embed.copy(); public_embed.set_footer(text=footer_public)
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
    def __init__(self, bot: commands.Bot, cog_instance: 'Fishing', panel_key: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.fishing_cog = cog_instance
        self.panel_key = panel_key
        self.user_locks: Dict[int, asyncio.Lock] = {}

        if panel_key == "panel_fishing_river":
            river_button = ui.Button(label="川で釣りをする", style=discord.ButtonStyle.primary, emoji="🏞️", custom_id="start_fishing_river")
            river_button.callback = self._start_fishing_callback
            self.add_item(river_button)
        elif panel_key == "panel_fishing_sea":
            sea_button = ui.Button(label="海で釣りをする", style=discord.ButtonStyle.primary, emoji="🌊", custom_id="start_fishing_sea")
            sea_button.callback = self._start_fishing_callback
            self.add_item(sea_button)
    
    async def _start_fishing_callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        lock = self.user_locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            await interaction.response.send_message("現在、以前のリクエストを処理中です。しばらくお待ちください。", ephemeral=True)
            return

        async with lock:
            if user_id in self.fishing_cog.active_fishing_sessions_by_user:
                await interaction.response.send_message("すでに釣りを開始しています。", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)
            
            if last_message := self.fishing_cog.last_result_messages.pop(user_id, None):
                try:
                    if last_message.channel:
                        msg_to_delete = await last_message.channel.fetch_message(last_message.id)
                        await msg_to_delete.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            
            try:
                location_type = interaction.data['custom_id'].split('_')[-1]
                user = interaction.user
                
                gear, inventory, user_abilities = await asyncio.gather(
                    get_user_gear(user), 
                    get_inventory(user),
                    get_user_abilities(user.id)
                )
                user_abilities = user_abilities or []
                
                rod, item_db = gear.get('rod', BARE_HANDS), get_item_database()
                if rod == BARE_HANDS:
                    if any('竿' in item_name for item_name in inventory if item_db.get(item_name, {}).get('category') == '装備'):
                        await interaction.followup.send("❌ プロフィール画面から釣竿を装備してください。", ephemeral=True)
                    else:
                        await interaction.followup.send(f"❌ 釣りをするには、まず商店で「{DEFAULT_ROD}」を購入してください。", ephemeral=True)
                    return
                
                game_config = get_config("GAME_CONFIG", {})
                if location_type == 'sea':
                    rod_data = item_db.get(rod, {})
                    req_tier = game_config.get("FISHING_SEA_REQ_TIER", 3)
                    if rod_data.get('tier', 0) < req_tier:
                        await interaction.followup.send(f"❌ 海の釣りには「{INTERMEDIATE_ROD_NAME}」(等級{req_tier})以上の性能を持つ釣竿を**装備**する必要があります。", ephemeral=True)
                        return

                self.fishing_cog.active_fishing_sessions_by_user.add(user.id)
                bait = gear.get('bait', 'エサなし')
                
                bait_saved = False
                if bait != "エサなし" and 'fish_bait_saver_1' in user_abilities:
                    if random.random() < 0.2:
                        bait_saved = True

                if bait != "エサなし" and not bait_saved:
                    if inventory.get(bait, 0) > 0:
                        await update_inventory(str(user.id), bait, -1)
                        inventory[bait] = max(0, inventory.get(bait, 0) - 1)
                    else:
                        bait = "エサなし"
                        await set_user_gear(str(user.id), bait="エサなし")

                location_name = "川" if location_type == "river" else "海"
                
                rod_data = item_db.get(rod, {})
                loot_bonus = rod_data.get('loot_bonus', 0.0)
                
                default_times = { "エサなし": [10.0, 15.0], "普通の釣りエサ": [7.0, 12.0], "高級釣りエサ": [5.0, 10.0] }
                bite_times_config_raw = game_config.get("FISHING_BITE_TIMES_BY_BAIT", default_times)
                
                bite_times_config = default_times
                if isinstance(bite_times_config_raw, str):
                    try: bite_times_config = json.loads(bite_times_config_raw)
                    except json.JSONDecodeError: pass
                elif isinstance(bite_times_config_raw, dict):
                    bite_times_config = bite_times_config_raw

                bite_range = bite_times_config.get(bait, bite_times_config.get("エサなし", [10.0, 15.0]))
                
                if 'fish_bite_time_down_1' in user_abilities:
                    bite_range = [max(0.5, t - 2.0) for t in bite_range]

                desc_lines = [ f"### {location_name}にウキを投げました。", f"**🎣 使用中の釣竿:** `{rod}` (+{loot_bonus:.0%})", f"**🐛 使用中のエサ:** `{bait}` (⏱️ `{bite_range[0]:.1f}`～`{bite_range[1]:.1f}`秒)" ]
                if bait_saved:
                    desc_lines.append("✨ 能力効果でエサを消費しませんでした！")

                desc = "\n".join(desc_lines)
                embed = discord.Embed(title=f"🎣 {location_name}での釣りを開始しました！", description=desc, color=discord.Color.light_grey())
                
                # [✅✅✅ 핵심 수정] GAME_CONFIG 내부가 아닌, 전체 설정에서 직접 이미지 URL을 가져오도록 수정합니다.
                if image_url := get_config("FISHING_WAITING_IMAGE_URL"):
                    embed.set_thumbnail(url=str(image_url))
                
                view = FishingGameView(self.bot, interaction.user, rod, bait, inventory, self.fishing_cog, location_type, bite_range)
                await view.start_game(interaction, embed)
            except Exception as e:
                self.fishing_cog.active_fishing_sessions_by_user.discard(user_id)
                logger.error(f"낚시 게임 시작 중 예측 못한 오류: {e}", exc_info=True)
                await interaction.followup.send(f"❌ 釣りの開始中に予期せぬエラーが発生しました。", ephemeral=True)

class Fishing(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_fishing_sessions_by_user: Set[int] = set()
        self.fishing_log_channel_id: Optional[int] = None
        self.last_result_messages: Dict[int, discord.Message] = {}
        logger.info("Fishing Cog가 성공적으로 초기화되었습니다.")
    
    async def cog_load(self): await self.load_configs()
    async def load_configs(self): self.fishing_log_channel_id = get_id("fishing_log_channel_id")

    async def register_persistent_views(self):
        self.bot.add_view(FishingPanelView(self.bot, self, "panel_fishing_river"))
        self.bot.add_view(FishingPanelView(self.bot, self, "panel_fishing_sea"))

    async def log_whale_catch(self, user: discord.Member, result_embed: discord.Embed):
        """고래를 낚았을 때 공개적으로 알리고, 출현 공지를 삭제합니다."""
        
        announcement_msg_id = get_config("whale_announcement_message_id")
        sea_fishing_channel_id = get_id("sea_fishing_panel_channel_id")

        if announcement_msg_id and sea_fishing_channel_id:
            if channel := self.bot.get_channel(sea_fishing_channel_id):
                try:
                    msg_to_delete = await channel.fetch_message(int(announcement_msg_id))
                    await msg_to_delete.delete()
                    logger.info(f"고래가 잡혀서 공지 메시지(ID: {announcement_msg_id})를 삭제했습니다.")
                    await save_config_to_db("whale_announcement_message_id", None)
                except (discord.NotFound, discord.Forbidden):
                    logger.warning(f"고래 공지 메시지(ID: {announcement_msg_id})를 찾거나 삭제할 수 없습니다.")
                except Exception as e:
                    logger.error(f"고래 공지 메시지 삭제 중 오류: {e}", exc_info=True)

        if not self.fishing_log_channel_id or not (log_channel := self.bot.get_channel(self.fishing_log_channel_id)): return
        
        fish_field = next((f for f in result_embed.fields if f.name == "魚"), None)
        size_field = next((f for f in result_embed.fields if f.name == "サイズ"), None)
        if not all([fish_field, size_field]): return

        fish_name_raw = fish_field.value.split('**')[1] if '**' in fish_field.value else fish_field.value
        fish_data = next((loot for loot in get_fishing_loot() if loot['name'] == fish_name_raw), None)
        if not fish_data: return

        size_cm = float(size_field.value.strip('`cm`'))
        base_value = fish_data.get("base_value") or 0
        size_multiplier = fish_data.get("size_multiplier") or 0
        value = int(base_value + (size_cm * size_multiplier))
        
        embed_data = await get_embed_from_db("log_whale_catch") or {}

        embed = format_embed_from_db(
            embed_data, 
            user_mention=user.mention,
            emoji=fish_data.get('emoji','🐋'), 
            name=fish_name_raw, 
            size=size_cm, 
            value=f"{value:,}", 
            currency_icon=get_config('GAME_CONFIG', {}).get('CURRENCY_ICON', '🪙')
        )
        if user.display_avatar: embed.set_thumbnail(url=user.display_avatar.url)
        if image_url := fish_data.get('image_url'): embed.set_image(url=image_url)
        
        try:
            await log_channel.send(content="@here", embed=embed, allowed_mentions=discord.AllowedMentions(everyone=True))
        except Exception as e:
            logger.error(f"고래 공지 전송 실패: {e}", exc_info=True)

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str):
        if panel_key not in ["panel_fishing_river", "panel_fishing_sea"]: return
        if (panel_info := get_panel_id(panel_key)):
            if (old_ch_id := panel_info.get("channel_id")) and (old_ch := self.bot.get_channel(old_ch_id)):
                try:
                    async for message in old_ch.history(limit=10):
                        if message.id == panel_info.get('message_id'):
                            await message.delete()
                            break
                except (discord.NotFound, discord.Forbidden): pass
        
        embed_data = await get_embed_from_db(panel_key)
        if not embed_data: return
        embed = discord.Embed.from_dict(embed_data)
        view = FishingPanelView(self.bot, self, panel_key)
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。 (チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Fishing(bot))
