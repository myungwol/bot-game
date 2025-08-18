# cogs/games/fishing.py

import discord
from discord.ext import commands
from discord import app_commands, ui
import random
import asyncio
import logging
from typing import Optional, Set, Dict

from utils.database import (
    update_wallet, get_inventory, update_inventory, add_to_aquarium,
    get_user_gear, set_user_gear, save_panel_id, get_panel_id, get_id, 
    get_embed_from_db, get_panel_components_from_db,
    get_item_database, get_fishing_loot, get_config
)

logger = logging.getLogger(__name__)

# --- UI 클래스 (FishingGameView, FishingPanelView) ---
class FishingGameView(ui.View):
    def __init__(self, bot: commands.Bot, user: discord.Member, used_rod: str, used_bait: str, remaining_baits: Dict[str, int], active_fishers_set: Set[int]):
        super().__init__(timeout=35)
        self.bot = bot; self.player = user; self.message: Optional[discord.WebhookMessage] = None
        self.game_state = "waiting"; self.game_task: Optional[asyncio.Task] = None
        self.used_rod = used_rod; self.used_bait = used_bait; self.remaining_baits = remaining_baits
        self.active_fishers_set = active_fishers_set
        self.rod_bonus = get_item_database().get(self.used_rod, {}).get("good_fish_bonus", 0.0)
        self.bite_range = get_item_database().get(self.used_bait, {}).get("bite_time_range", (8.0, 15.0))
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
            if self.message: await self.message.edit(embed=embed, view=self)
            await asyncio.sleep(self.bite_reaction_time)
            if not self.is_finished() and self.game_state == "biting":
                embed = discord.Embed(title="💧 逃げられた…", description=f"{self.player.mention}さんは反応が遅れてしまいました。", color=discord.Color.greyple())
                await self._send_result(embed); self.stop()
        except asyncio.CancelledError: pass
        except Exception as e:
            logger.error(f"{self.player.display_name}의 낚시 게임 흐름 중 오류: {e}", exc_info=True)
            if not self.is_finished():
                error_embed = discord.Embed(title="❌ エラー発生", description="釣りの処理中に予期せぬエラーが発生しました。", color=discord.Color.red())
                await self._send_result(error_embed); self.stop()

    async def _handle_catch_logic(self) -> tuple[discord.Embed, bool, bool]:
        fishing_loot = get_fishing_loot()
        weights = [item['weight'] * (1 + self.rod_bonus if item.get('base_value') is not None else 1) for item in fishing_loot]
        catch_proto = random.choices(fishing_loot, weights=weights, k=1)[0]
        user_mention = self.player.mention; is_big_catch = log_publicly = False
        if catch_proto.get("min_size") is not None:
            log_publicly = True; size = round(random.uniform(catch_proto["min_size"], catch_proto["max_size"]), 1)
            await add_to_aquarium(str(self.player.id), {"name": catch_proto['name'], "size": size, "emoji": catch_proto['emoji']})
            is_big_catch = size >= self.big_catch_threshold
            title = "🏆 大物を釣り上げた！ 🏆" if is_big_catch else "🎉 釣り成功！ 🎉"
            desc = f"{user_mention}さんが、とてつもない大物を釣り上げました！" if is_big_catch else f"{user_mention}さんが釣りに成功し、魚を水槽に入れました。"
            color = discord.Color.gold() if is_big_catch else discord.Color.blue()
            embed = discord.Embed(title=title, description=desc, color=color)
            embed.add_field(name="魚", value=f"{catch_proto['emoji']} **{catch_proto['name']}**", inline=True); embed.add_field(name="サイズ", value=f"`{size}`cm", inline=True)
        else:
            value = catch_proto.get('value', 0)
            if value > 0: await update_wallet(self.player, value)
            log_publicly = catch_proto.get("log_publicly", False)
            embed = discord.Embed(title=catch_proto['title'], description=catch_proto['description'].format(user_mention=user_mention, value=value), color=discord.Color(int(catch_proto['color'])))
        return embed, log_publicly, is_big_catch
    
    @ui.button(label="待機中...", style=discord.ButtonStyle.secondary, custom_id="catch_fish_button", emoji="🎣")
    async def catch_button(self, interaction: discord.Interaction, button: ui.Button):
        if self.game_task: self.game_task.cancel()
        result_embed, log_publicly, is_big_catch = None, False, False
        if self.game_state == "waiting":
            await interaction.response.defer()
            result_embed = discord.Embed(title="❌ 早すぎ！", description=f"{interaction.user.mention}さんは焦ってしまい、魚に気づかれてしまいました…", color=discord.Color.dark_grey())
        elif self.game_state == "biting":
            await interaction.response.defer(); self.game_state = "finished"
            result_embed, log_publicly, is_big_catch = await self._handle_catch_logic()
        if result_embed:
            if self.player.display_avatar: result_embed.set_thumbnail(url=self.player.display_avatar.url)
            await self._send_result(result_embed, log_publicly, is_big_catch)
        self.stop()

    async def _send_result(self, embed: discord.Embed, log_publicly: bool = False, is_big_catch: bool = False):
        remaining_baits_config = get_config("FISHING_REMAINING_BAITS_DISPLAY", ["一般の釣りエサ", "高級釣りエサ"])
        footer_private_parts = [f"{bait_name}({self.remaining_baits.get(bait_name, 0)}個)" for bait_name in remaining_baits_config]
        footer_private = f"残りのエサ: {' / '.join(footer_private_parts)}"; footer_public = f"使用した装備: {self.used_rod} / {self.used_bait}"
        if log_publicly and (fishing_cog := self.bot.get_cog("Fishing")) and (log_ch_id := fishing_cog.fishing_log_channel_id) and (log_ch := self.bot.get_channel(log_ch_id)):
            public_embed = embed.copy(); public_embed.set_footer(text=footer_public)
            content = self.player.mention if is_big_catch else None
            allowed_mentions = discord.AllowedMentions(users=True) if is_big_catch else discord.AllowedMentions.none()
            try: await log_ch.send(content=content, embed=public_embed, allowed_mentions=allowed_mentions)
            except Exception as e: logger.error(f"공개 낚시 로그 전송 실패: {e}", exc_info=True)
        embed.set_footer(text=f"{footer_public}\n{footer_private}")
        if self.message:
            try: await self.message.edit(embed=embed, view=None)
            except (discord.NotFound, AttributeError, discord.HTTPException): pass

    async def on_timeout(self):
        if self.game_state != "finished":
            embed = discord.Embed(title="⏱️ 時間切れ", description=f"{self.player.mention}さんは時間内に反応がありませんでした。", color=discord.Color.darker_grey())
            await self._send_result(embed)
        self.stop()

    def stop(self):
        if self.game_task and not self.game_task.done(): self.game_task.cancel()
        self.active_fishers_set.discard(self.player.id); super().stop()

class FishingPanelView(ui.View):
    def __init__(self, bot: commands.Bot, cog_instance: 'Fishing'):
        super().__init__(timeout=None); self.bot = bot; self.fishing_cog = cog_instance
        self.user_locks: Dict[int, asyncio.Lock] = {}

    async def setup_buttons(self):
        self.clear_items()
        button_styles = get_config("DISCORD_BUTTON_STYLES_MAP", {})
        components_data = await get_panel_components_from_db('fishing')
        if not components_data:
            default_button = ui.Button(label="낚시하기", custom_id="start_fishing"); default_button.callback = self.start_fishing
            self.add_item(default_button); return
        for comp in components_data:
            if comp.get('component_type') == 'button' and comp.get('component_key'):
                style_key = comp.get('style', 'secondary')
                button = ui.Button(label=comp.get('label'), style=button_styles.get(style_key, discord.ButtonStyle.secondary), emoji=comp.get('emoji'), row=comp.get('row'), custom_id=comp.get('component_key'))
                if comp.get('component_key') == 'start_fishing': button.callback = self.start_fishing
                self.add_item(button)

    async def start_fishing(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        lock = self.user_locks.setdefault(user_id, asyncio.Lock())
        if lock.locked(): await interaction.response.send_message("現在、以前のリクエストを処理中です。しばらくお待ちください。", ephemeral=True); return
        async with lock:
            if user_id in self.fishing_cog.active_fishing_sessions_by_user:
                await interaction.response.send_message("すでに釣りを開始しています。", ephemeral=True); return
            await interaction.response.defer(ephemeral=True)
            self.fishing_cog.active_fishing_sessions_by_user.add(user_id)
            try:
                uid_str = str(user_id); gear, inventory = await asyncio.gather(get_user_gear(uid_str), get_inventory(uid_str))
                rod = gear.get('rod', '素手')
                if rod == "素手" or get_item_database().get(rod) is None: raise ValueError("「古い釣竿」以上の釣竿を商店で購入し、装備してください。")
                bait = gear.get('bait', 'エサなし')
                if bait != "エサなし":
                    if inventory.get(bait, 0) > 0: await update_inventory(uid_str, bait, -1); inventory[bait] = inventory.get(bait, 0) - 1
                    else: bait = "エサなし"; await set_user_gear(uid_str, bait="エサなし")
                rod_bonus = int(get_item_database().get(rod, {}).get("good_fish_bonus", 0.0) * 100)
                min_b, max_b = get_item_database().get(bait, {}).get("bite_time_range", (8.0, 15.0))
                desc = f"### ウキを投げました。\n**🎣 使用中の釣竿:** `{rod}` (`珍しい魚の確率 +{rod_bonus}%`)\n**🐛 使用中のエサ:** `{bait}` (`アタリ待機時間: {min_b}～{max_b}秒`)"
                embed = discord.Embed(title="🎣 釣りを開始しました！", description=desc, color=discord.Color.light_grey())
                view = FishingGameView(self.bot, interaction.user, rod, bait, inventory, self.fishing_cog.active_fishing_sessions_by_user)
                await view.start_game(interaction, embed)
            except Exception as e:
                self.fishing_cog.active_fishing_sessions_by_user.discard(user_id)
                logger.error(f"낚시 게임 시작 중 오류: {e}", exc_info=True)
                await interaction.followup.send(f"❌ 釣りの開始中にエラーが発生しました。\n`{e}`", ephemeral=True)

class Fishing(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_fishing_sessions_by_user: Set[int] = set()
        self.fishing_log_channel_id: Optional[int] = None
        self.view_instance = None
        logger.info("Fishing Cog가 성공적으로 초기화되었습니다.")

    async def register_persistent_views(self):
        self.view_instance = FishingPanelView(self.bot, self)
        await self.view_instance.setup_buttons()
        self.bot.add_view(self.view_instance)

    async def cog_load(self):
        await self.load_configs()

    async def load_configs(self):
        self.fishing_log_channel_id = get_id("fishing_log_channel_id")

    async def regenerate_panel(self, channel: discord.TextChannel):
        """요청에 의해 낚시터 패널을 재생성합니다."""
        panel_key = "fishing"
        embed_key = "panel_fishing"

        panel_info = get_panel_id(panel_key)
        if panel_info and (old_id := panel_info.get('message_id')):
            try:
                old_message = await channel.fetch_message(old_id)
                await old_message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
        
        embed_data = await get_embed_from_db(embed_key)
        if not embed_data:
            logger.error(f"DB에서 '{embed_key}' 임베드를 찾을 수 없어 패널 생성을 중단합니다.")
            return

        embed = discord.Embed.from_dict(embed_data)

        # [핵심 수정] View 인스턴스를 매번 새로 생성하고 봇에 등록합니다.
        self.view_instance = FishingPanelView(self.bot, self)
        await self.view_instance.setup_buttons()
        self.bot.add_view(self.view_instance)

        new_message = await channel.send(embed=embed, view=self.view_instance)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ 낚시터 패널을 성공적으로 새로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Fishing(bot))
