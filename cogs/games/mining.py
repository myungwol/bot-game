# cogs/games/mining.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
import time
import random
import json
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone, timedelta

from utils.database import (
    get_inventory, update_inventory, get_user_gear, BARE_HANDS,
    save_panel_id, get_panel_id, get_id, get_embed_from_db,
    log_activity, get_user_abilities, supabase, get_item_database
)
from utils.helpers import format_embed_from_db, format_timedelta_minutes_seconds, coerce_item_emoji

logger = logging.getLogger(__name__)

MINING_PASS_NAME = "鉱山入場券"
DEFAULT_MINE_DURATION_SECONDS = 600
MINING_COOLDOWN_SECONDS = 10

PICKAXE_LUCK_BONUS = {
    "木のツルハシ": 1.0, "銅のツルハシ": 1.1, "鉄のツルハシ": 1.25,
    "金のツルハシ": 1.5, "ダイヤのツルハシ": 2.0,
}

ORE_DATA = {
    "ハズレ":       {"weight": 40, "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/stone.jpg"},
    "銅鉱石": {"weight": 30, "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/cooper.jpg"},
    "鉄鉱石":   {"weight": 20, "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/Iron.jpg"},
    "金鉱石":    {"weight": 8,  "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/gold.jpg"},
    "ダイヤモンド": {"weight": 2,  "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/diamond.jpg"}
}

ORE_XP_MAP = { "銅鉱石": 10, "鉄鉱石": 15, "金鉱石": 30, "ダイヤモンド": 75 }

class MiningGameView(ui.View):
    def __init__(self, cog_instance: 'Mining', user: discord.Member, pickaxe: str, duration: int, end_time: datetime, duration_doubled: bool):
        super().__init__(timeout=duration + 30)
        self.cog = cog_instance
        self.user = user
        self.pickaxe = pickaxe
        self.end_time = end_time
        self.duration_doubled = duration_doubled
        self.mined_ores: Dict[str, int] = {}
        self.luck_bonus = PICKAXE_LUCK_BONUS.get(pickaxe, 1.0)
        self.time_reduction = 0
        self.can_double_yield = False
        self.state = "idle"
        self.discovered_ore: Optional[str] = None
        self.last_result_text: Optional[str] = None
        self.message: Optional[discord.Message] = None
        self.on_cooldown = False
        self.ui_lock = asyncio.Lock()
        self.ui_update_task = self.cog.bot.loop.create_task(self.ui_updater())
        self.initial_load_task = self.cog.bot.loop.create_task(self.load_initial_data())

        action_button = ui.Button(label="鉱石を探す", style=discord.ButtonStyle.secondary, emoji="🔍", custom_id="mine_action_button")
        action_button.callback = self.dispatch_callback
        self.add_item(action_button)

    async def load_initial_data(self):
        user_abilities = await get_user_abilities(self.user.id)
        self.cog.active_abilities_cache[self.user.id] = user_abilities
        if 'mine_time_down_1' in user_abilities: self.time_reduction = 3
        if 'mine_double_yield_2' in user_abilities: self.can_double_yield = True
        if 'mine_rare_up_2' in user_abilities: self.luck_bonus += 0.5

    def stop(self):
        if hasattr(self, 'ui_update_task') and not self.ui_update_task.done(): self.ui_update_task.cancel()
        if hasattr(self, 'initial_load_task') and not self.initial_load_task.done(): self.initial_load_task.cancel()
        super().stop()

    async def ui_updater(self):
        while not self.is_finished():
            async with self.ui_lock:
                try:
                    if self.message and self.state == "idle":
                        embed = self.build_embed()
                        await self.message.edit(embed=embed)
                except (discord.NotFound, discord.Forbidden): self.stop(); break
                except Exception as e: logger.error(f"Mining UI 업데이트 중 오류: {e}", exc_info=True)
            await asyncio.sleep(10)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("本人のみ採掘できます。", ephemeral=True, delete_after=5); return False
        if self.user.id not in self.cog.active_sessions:
            if self.message: await self.message.edit(content="期限切れの鉱山です。", view=None, embed=None)
            self.stop(); return False
        return True
        
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: ui.Item[Any]) -> None:
        logger.error(f"MiningGameView에서 오류 발생 (Item: {item.custom_id}): {error}", exc_info=True)

    async def dispatch_callback(self, interaction: discord.Interaction):
        if not interaction.response.is_done(): await interaction.response.defer()
        
        if self.on_cooldown:
            return await interaction.followup.send("⏳ まだ周りを見渡しています。", ephemeral=True, delete_after=5)
        
        await self.handle_action_button(interaction, self.children[0])

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"{self.user.display_name}さんの鉱山採掘", color=0x607D8B)
        item_db = get_item_database()
        if self.state == "idle":
            description_parts = ["## 前へ進んで鉱物を探そう"]
            if self.last_result_text: description_parts.append(f"## 採掘結果\n{self.last_result_text}")
            remaining_time = self.end_time - datetime.now(timezone.utc)
            timer_str = f"鉱山が閉まるまで: **{discord.utils.format_dt(self.end_time, 'R')}**" if remaining_time.total_seconds() > 0 else f"鉱山が閉まるまで: **終了**"
            description_parts.append(timer_str)
            active_abilities = []
            if self.duration_doubled: active_abilities.append("> ✨ 集中探査 (時間2倍)")
            if self.time_reduction > 0: active_abilities.append("> ⚡ 迅速な採掘 (クールタイム減少)")
            if self.can_double_yield: active_abilities.append("> 💰 豊富な鉱脈 (数量2倍確率)")
            if 'mine_rare_up_2' in self.cog.active_abilities_cache.get(self.user.id, []): active_abilities.append("> 💎 大当たり発見 (レア鉱物確率増加)")
            if active_abilities: description_parts.append(f"**--- 発動中の能力 ---**\n" + "\n".join(active_abilities))
            description_parts.append(f"**使用中の装備:** {self.pickaxe}")
            embed.description = "\n\n".join(description_parts)
        elif self.state == "discovered":
            ore_info = item_db.get(self.discovered_ore, {})
            ore_emoji = str(coerce_item_emoji(ore_info.get('emoji', '💎')))
            desc_text = f"### {ore_emoji} {self.discovered_ore}を発見した！" if self.discovered_ore != "ハズレ" else "### 何も見つからなかった..."
            embed.description = desc_text
            embed.set_image(url=ORE_DATA[self.discovered_ore]['image_url'])
        elif self.state == "mining":
            ore_info = item_db.get(self.discovered_ore, {})
            ore_emoji = str(coerce_item_emoji(ore_info.get('emoji', '💎')))
            embed.description = f"**{self.pickaxe}**で一生懸命**{ore_emoji} {self.discovered_ore}**を掘っています..."
            embed.set_image(url=ORE_DATA[self.discovered_ore]['image_url'])
        return embed

    async def handle_action_button(self, interaction: discord.Interaction, button: ui.Button):
        async with self.ui_lock:
            if self.state == "idle":
                self.last_result_text = None
                button.disabled = True; button.label = "探索中..."
                await interaction.edit_original_response(embed=self.build_embed(), view=self)
                try:
                    await asyncio.sleep(1)
                    ores, weights = zip(*[(k, v['weight']) for k, v in ORE_DATA.items()])
                    new_weights = [w * self.luck_bonus if o != "ハズレ" else w for o, w in zip(ores, weights)]
                    self.discovered_ore = random.choices(ores, weights=new_weights, k=1)[0]
                    if self.discovered_ore == "ハズレ": self.state = "discovered"; button.label = "もう一度探す"; button.emoji = "🔍"
                    else: self.state = "discovered"; button.label = "採掘する"; button.style = discord.ButtonStyle.primary; button.emoji = "⛏️"
                finally:
                    button.disabled = False
                    await interaction.edit_original_response(embed=self.build_embed(), view=self)
            elif self.state == "discovered":
                if self.discovered_ore == "ハズレ":
                    self.on_cooldown = True; button.disabled = True
                    await interaction.edit_original_response(view=self)
                    await asyncio.sleep(MINING_COOLDOWN_SECONDS - self.time_reduction)
                    self.on_cooldown = False
                    if self.is_finished(): return
                    self.state = "idle"; self.last_result_text = "### 何も見つからなかった..."
                    button.label = "鉱石を探す"; button.emoji = "🔍"; button.disabled = False
                    await interaction.edit_original_response(embed=self.build_embed(), view=self)
                else:
                    self.state = "mining"; button.disabled = True
                    mining_duration = max(3, MINING_COOLDOWN_SECONDS - self.time_reduction)
                    button.label = f"採掘中... ({mining_duration}秒)"
                    await interaction.edit_original_response(embed=self.build_embed(), view=self)
                    await asyncio.sleep(mining_duration)
                    if self.is_finished(): return
                    quantity = 2 if self.can_double_yield and random.random() < 0.20 else 1
                    xp_earned = ORE_XP_MAP.get(self.discovered_ore, 0) * quantity
                    self.mined_ores[self.discovered_ore] = self.mined_ores.get(self.discovered_ore, 0) + quantity
                    try:
                        session_res = await supabase.table('mining_sessions').select('mined_ores_json').eq('user_id', str(self.user.id)).maybe_single().execute()
                        if session_res and session_res.data:
                            current_ores_raw = session_res.data.get('mined_ores_json'); current_ores = {}
                            if isinstance(current_ores_raw, str):
                                try: current_ores = json.loads(current_ores_raw)
                                except json.JSONDecodeError: pass
                            elif isinstance(current_ores_raw, dict): current_ores = current_ores_raw
                            current_ores[self.discovered_ore] = current_ores.get(self.discovered_ore, 0) + quantity
                            await supabase.table('mining_sessions').update({'mined_ores_json': current_ores}).eq('user_id', str(self.user.id)).execute()
                    except Exception as db_error: logger.error(f"광산 채굴량 DB 업데이트 중 오류 발생: {db_error}", exc_info=True)
                    await update_inventory(self.user.id, self.discovered_ore, quantity)
                    await log_activity(self.user.id, 'mining', amount=quantity, xp_earned=xp_earned)
                    ore_info = get_item_database().get(self.discovered_ore, {}); ore_emoji = str(coerce_item_emoji(ore_info.get('emoji', '💎')))
                    self.last_result_text = f"✅ {ore_emoji} **{self.discovered_ore}** {quantity}個獲得しました！ (`+{xp_earned} XP`)"
                    if quantity > 1: self.last_result_text += f"\n\n✨ **豊富な鉱脈**の能力で鉱石を2個獲得しました！"
                    if xp_earned > 0:
                        res = await supabase.rpc('add_xp', {'p_user_id': self.user.id, 'p_xp_to_add': xp_earned, 'p_source': 'mining'}).execute()
                        if res.data and (level_cog := self.cog.bot.get_cog("LevelSystem")):
                            await level_cog.handle_level_up_event(self.user, res.data)
                    self.state = "idle"
                    button.label = "鉱石を探す"; button.style = discord.ButtonStyle.secondary; button.emoji = "🔍"; button.disabled = False
                    try: await interaction.edit_original_response(embed=self.build_embed(), view=self)
                    except discord.NotFound: self.stop()

class MiningPanelView(ui.View):
    def __init__(self, cog_instance: 'Mining'):
        super().__init__(timeout=None)
        self.cog = cog_instance
        
        enter_button = ui.Button(label="入場する", style=discord.ButtonStyle.secondary, emoji="⛏️", custom_id="enter_mine")
        enter_button.callback = self.dispatch_callback
        self.add_item(enter_button)

    async def dispatch_callback(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer()
        
        await self.cog.handle_enter_mine(interaction)

class Mining(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_sessions: Dict[int, Dict] = {}
        self.active_abilities_cache: Dict[int, List[str]] = {}
        self.check_expired_mines_from_db.start()
        
    def cog_unload(self):
        self.check_expired_mines_from_db.cancel()

    @tasks.loop(minutes=1)
    async def check_expired_mines_from_db(self):
        now = datetime.now(timezone.utc)
        res = await supabase.table('mining_sessions').select('*').lte('end_time', now.isoformat()).execute()
        if not (res and res.data): return
        
        for session in res.data:
            user_id = int(session['user_id'])
            if user_id not in self.active_sessions:
                logger.warning(f"DB에서 방치된 광산 세션(유저: {user_id})을 발견하여 안전장치로 종료합니다.")
                await self.close_mine_session(user_id)

    @check_expired_mines_from_db.before_loop
    async def before_check_expired_mines(self):
        await self.bot.wait_until_ready()

    async def handle_enter_mine(self, interaction: discord.Interaction):
        user = interaction.user

        if user.id in self.active_sessions:
            thread_id = self.active_sessions[user.id].get("thread_id")
            if thread := self.bot.get_channel(thread_id):
                await interaction.followup.send(f"すでに鉱山に入場しています。{thread.mention}", ephemeral=True)
            else:
                await self.close_mine_session(user.id)
                await interaction.followup.send("以前の鉱山情報を強制的に初期化しました。もう一度お試しください。", ephemeral=True)
            return

        inventory, gear, user_abilities = await asyncio.gather(get_inventory(user), get_user_gear(user), get_user_abilities(user.id))
        
        if inventory.get(MINING_PASS_NAME, 0) < 1: return await interaction.followup.send(f"'{MINING_PASS_NAME}'が不足しています。", ephemeral=True)
        pickaxe = gear.get('pickaxe', BARE_HANDS)
        if pickaxe == BARE_HANDS: return await interaction.followup.send("❌ ツルハシを装着する必要があります。", ephemeral=True)

        try: thread = await interaction.channel.create_thread(name=f"⛏️｜{user.display_name}の鉱山", type=discord.ChannelType.private_thread)
        except Exception: return await interaction.followup.send("❌ 鉱山を開くのに失敗しました。", ephemeral=True)
        
        await update_inventory(user.id, MINING_PASS_NAME, -1)
        await thread.add_user(user)

        duration = DEFAULT_MINE_DURATION_SECONDS
        duration_doubled = 'mine_duration_up_1' in user_abilities and random.random() < 0.15
        if duration_doubled: duration *= 2
        end_time = datetime.now(timezone.utc) + timedelta(seconds=duration)
        
        await supabase.table('mining_sessions').upsert({
            "user_id": str(user.id), "thread_id": str(thread.id), "end_time": end_time.isoformat(), "pickaxe_name": pickaxe, "mined_ores_json": "{}"
        }, on_conflict="user_id").execute()
        
        view = MiningGameView(self, user, pickaxe, duration, end_time, duration_doubled)
        
        session_task = self.bot.loop.create_task(self.mine_session_timer(user.id, thread, duration))
        self.active_sessions[user.id] = {"thread_id": thread.id, "view": view, "task": session_task}

        embed = view.build_embed()
        message = await thread.send(embed=embed, view=view)
        view.message = message
        
        await interaction.followup.send(f"鉱山に入場しました！ {thread.mention}", ephemeral=True)

    async def mine_session_timer(self, user_id: int, thread: discord.Thread, duration: int):
        try:
            if duration > 60:
                await asyncio.sleep(duration - 60)
                if user_id in self.active_sessions:
                    try: await thread.send("⚠️ 1分後に鉱山が閉まります...", delete_after=59)
                    except (discord.Forbidden, discord.HTTPException): pass
                else: return
                await asyncio.sleep(60)
            else:
                await asyncio.sleep(duration)
            
            if user_id in self.active_sessions:
                 await self.close_mine_session(user_id)
        except asyncio.CancelledError: pass
            
    async def close_mine_session(self, user_id: int):
        res = await supabase.table('mining_sessions').select('*').eq('user_id', str(user_id)).maybe_single().execute()
        session_data = res.data if res and res.data else None
        
        if in_memory_session := self.active_sessions.pop(user_id, None):
            if task := in_memory_session.get("task"): task.cancel()
            if view := in_memory_session.get("view"): view.stop()
        
        if not session_data:
            logger.warning(f"[{user_id}] 종료할 광산 세션이 DB에 없습니다 (이미 처리됨).")
            return

        thread_id = int(session_data['thread_id'])
        logger.info(f"[{user_id}] 광산 세션(스레드: {thread_id}) 종료 시작.")
        await supabase.table('mining_sessions').delete().eq('user_id', str(user_id)).execute()

        user = self.bot.get_user(user_id)
        if user:
            mined_ores_raw = session_data.get('mined_ores_json', "{}")
            mined_ores = {}
            if isinstance(mined_ores_raw, str):
                try: mined_ores = json.loads(mined_ores_raw)
                except json.JSONDecodeError: pass
            elif isinstance(mined_ores_raw, dict):
                mined_ores = mined_ores_raw

            item_db = get_item_database()
            mined_ores_lines = []
            for ore, qty in mined_ores.items():
                ore_info = item_db.get(ore, {})
                ore_emoji = str(coerce_item_emoji(ore_info.get('emoji', '💎')))
                mined_ores_lines.append(f"> {ore_emoji} {ore}: {qty}個")

            mined_ores_text = "\n".join(mined_ores_lines) or "> 採掘した鉱物はありません。"
            
            embed_data = await get_embed_from_db("log_mining_result") or {"title": "⛏️ 鉱山探査結果", "color": 0x607D8B}
            log_embed = format_embed_from_db(embed_data, user_mention=user.mention, pickaxe_name=session_data.get('pickaxe_name'), mined_ores=mined_ores_text)
            if user.display_avatar: log_embed.set_thumbnail(url=user.display_avatar.url)
            
            panel_channel_id = get_id("mining_panel_channel_id")
            if panel_channel_id and (panel_channel := self.bot.get_channel(panel_channel_id)):
                await self.regenerate_panel(panel_channel, last_log=log_embed)
        
        try:
            thread = self.bot.get_channel(thread_id) or await self.bot.fetch_channel(thread_id)
            await thread.add_user(self.bot.user)
            await thread.send("**鉱山が閉まりました。**", delete_after=10)
            await asyncio.sleep(1)
            await thread.delete()
        except (discord.NotFound, discord.Forbidden): pass

    async def register_persistent_views(self):
        self.bot.add_view(MiningPanelView(self))

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_mining", last_log: Optional[discord.Embed] = None):
        if last_log:
            try: await channel.send(embed=last_log)
            except discord.HTTPException as e: logger.error(f"광산 로그 전송 실패: {e}")
        
        panel_name = panel_key.replace("panel_", "")
        if panel_info := get_panel_id(panel_name):
            try:
                if old_channel := self.bot.get_channel(panel_info['channel_id']):
                    msg = await old_channel.fetch_message(panel_info['message_id'])
                    await msg.delete()
            except (discord.NotFound, discord.Forbidden): pass
        embed_data = await get_embed_from_db(panel_key)
        if not embed_data: return logger.error(f"DB에서 '{panel_key}' 임베드를 찾을 수 없습니다.")
        
        embed = format_embed_from_db(embed_data)
        
        view = MiningPanelView(self)
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_name, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。(チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Mining(bot))
