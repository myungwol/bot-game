# cogs/games/mining.py
import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
import time
import random
from typing import Optional, Dict, List
from datetime import timedelta

from utils.database import (
    get_inventory, update_inventory, get_user_gear, BARE_HANDS,
    save_panel_id, get_panel_id, get_id, get_embed_from_db,
    log_activity, get_user_abilities
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

MINING_PASS_NAME = "광산 입장권"
DEFAULT_MINE_DURATION_SECONDS = 600
MINING_COOLDOWN_SECONDS = 10

PICKAXE_LUCK_BONUS = {
    "나무 곡괭이": 1.0,
    "구리 곡괭이": 1.1,
    "철 곡괭이": 1.25,
    "금 곡괭이": 1.5,
    "다이아 곡괭이": 2.0,
}

ORE_DATA = {
    "꽝":       {"weight": 40, "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/stone.jpg"},
    "구리 광석": {"weight": 30, "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/cooper.jpg"},
    "철 광석":   {"weight": 20, "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/Iron.jpg"},
    "금 광석":    {"weight": 8,  "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/gold.jpg"},
    "다이아몬드": {"weight": 2,  "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/diamond.jpg"}
}

class MiningGameView(ui.View):
    def __init__(self, cog_instance: 'Mining', user: discord.Member, thread: discord.Thread, pickaxe: str, user_abilities: List[str], duration: int, duration_doubled: bool):
        super().__init__(timeout=duration)
        self.cog = cog_instance
        self.user = user
        self.thread = thread
        self.pickaxe = pickaxe
        self.user_abilities = user_abilities
        self.duration_doubled = duration_doubled
        
        self.luck_bonus = PICKAXE_LUCK_BONUS.get(pickaxe, 1.0)
        if 'mine_rare_up_2' in self.user_abilities: self.luck_bonus += 0.5
        
        self.time_reduction = 3 if 'mine_time_down_1' in self.user_abilities else 0
        self.can_double_yield = 'mine_double_yield_2' in self.user_abilities

        self.state = "finding"
        self.discovered_ore: Optional[str] = None
        self.last_result_text: Optional[str] = None
        
        self.end_time = discord.utils.utcnow() + timedelta(seconds=duration)
        self.warning_task: Optional[asyncio.Task] = None
        self.last_action_time = 0

    async def start(self):
        if self.timeout is not None and self.timeout > 60:
            self.warning_task = asyncio.create_task(self.send_warning())

    async def send_warning(self):
        try:
            await asyncio.sleep(self.timeout - 60)
            if not self.is_finished() and self.thread:
                await self.thread.send("⚠️ 곧 광산이 닫힙니다...", delete_after=60)
        except asyncio.CancelledError:
            pass

    def stop(self):
        if self.warning_task and not self.warning_task.done():
            self.warning_task.cancel()
        super().stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("본인만 채굴할 수 있습니다.", ephemeral=True, delete_after=5)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"{self.user.display_name}님의 광산 채굴", color=0x607D8B)

        if self.state == "finding":
            description_parts = ["주변을 다시 둘러보자. 어떤 광석이 나올까?"]
            if self.last_result_text:
                description_parts.append(f"## 채굴 결과\n{self.last_result_text}")
            embed.description = "\n\n".join(description_parts)
            embed.set_image(url=None)
            embed.set_footer(text=f"사용 중인 장비: {self.pickaxe} | 광산 닫힘: {discord.utils.format_dt(self.end_time, style='R')}")
        
        elif self.state == "discovered":
            embed.description = f"### {self.discovered_ore}을(를) 발견했다!"
            embed.set_image(url=ORE_DATA[self.discovered_ore]['image_url'])
            embed.set_footer(text=f"사용 중인 장비: {self.pickaxe}")
            
        elif self.state == "mining":
            embed.description = f"**{self.pickaxe}**(으)로 열심히 **{self.discovered_ore}**을(를) 캐는 중입니다..."
            embed.set_image(url=ORE_DATA[self.discovered_ore]['image_url'])
            embed.set_footer(text=f"사용 중인 장비: {self.pickaxe}")
        
        elif self.state == "searching":
            embed.description = "더 깊이 들어가서 찾아보자..."
            embed.set_image(url=None)
            embed.set_footer(text=f"사용 중인 장비: {self.pickaxe}")

        return embed

    @ui.button(label="광석 찾기", style=discord.ButtonStyle.secondary, emoji="🔍", custom_id="mine_action_button")
    async def action_button(self, interaction: discord.Interaction, button: ui.Button):
        
        if self.state == "finding":
            now = time.time()
            cooldown = MINING_COOLDOWN_SECONDS - self.time_reduction
            if now - self.last_action_time < cooldown:
                remaining = cooldown - (now - self.last_action_time)
                await interaction.response.send_message(f"⏳ 아직 주변을 살피고 있습니다. {remaining:.1f}초 후에 다시 시도해주세요.", ephemeral=True, delete_after=5)
                return

            self.state = "searching"
            self.last_result_text = None
            button.disabled = True
            button.label = "탐색 중..."
            embed = self.build_embed()
            await interaction.response.edit_message(embed=embed, view=self)
            
            await asyncio.sleep(2) # 시각적 피드백을 위한 짧은 딜레이
            if self.is_finished(): return

            ores = list(ORE_DATA.keys())
            original_weights = [data['weight'] for data in ORE_DATA.values()]
            new_weights = [w * self.luck_bonus if o != "꽝" else w for o, w in zip(ores, original_weights)]
            self.discovered_ore = random.choices(ores, weights=new_weights, k=1)[0]
            self.last_action_time = time.time()
            
            if self.discovered_ore == "꽝":
                self.last_result_text = "### 아무것도 발견하지 못했다..."
                self.state = "finding"
                button.label = "광석 찾기"
            else: # 광석 발견
                self.state = "discovered"
                button.label = "채굴하기"; button.style = discord.ButtonStyle.primary; button.emoji = "⛏️"
            
            embed = self.build_embed()
            button.disabled = False
            await interaction.edit_original_response(embed=embed, view=self)

        elif self.state == "discovered":
            self.state = "mining"
            button.disabled = True
            mining_duration = max(3, MINING_COOLDOWN_SECONDS - self.time_reduction)
            button.label = f"채굴 중... ({mining_duration}초)"
            embed = self.build_embed()
            await interaction.response.edit_message(embed=embed, view=self)

            await asyncio.sleep(mining_duration)
            if self.is_finished() or self.user.id not in self.cog.active_sessions: return

            if self.discovered_ore != "꽝":
                quantity = 2 if self.can_double_yield and random.random() < 0.20 else 1
                await update_inventory(self.user.id, self.discovered_ore, quantity)
                await log_activity(self.user.id, 'mining', amount=quantity)
                
                self.last_result_text = f"✅ **{self.discovered_ore}** {quantity}개를 획득했습니다!"
                if quantity > 1: self.last_result_text += f"\n\n✨ **풍부한 광맥** 능력으로 광석을 2개 획득했습니다!"
            
            self.state = "finding"
            self.last_action_time = time.time()
            embed = self.build_embed()
            button.label = "광석 찾기"; button.style = discord.ButtonStyle.secondary; button.emoji = "🔍"
            button.disabled = False
            
            try: await interaction.edit_original_response(embed=embed, view=self)
            except discord.NotFound: self.stop()

    async def on_timeout(self):
        await self.cog.close_mine_session(self.user.id, self.thread, "시간이 다 되어")
        self.stop()

class MiningPanelView(ui.View):
    def __init__(self, cog_instance: 'Mining'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    @ui.button(label="입장하기", style=discord.ButtonStyle.secondary, emoji="⛏️", custom_id="enter_mine")
    async def enter_mine_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.handle_enter_mine(interaction)

class Mining(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_sessions: Dict[int, Dict] = {}

    async def handle_enter_mine(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user

        if user.id in self.active_sessions:
            thread_id = self.active_sessions[user.id].get("thread_id")
            if thread := self.bot.get_channel(thread_id):
                await interaction.followup.send(f"이미 광산에 입장해 있습니다. {thread.mention}", ephemeral=True)
            else:
                await self.close_mine_session(user.id, None, "오류로 인해")
                await interaction.followup.send("이전 광산 정보를 찾을 수 없어 초기화했습니다. 다시 시도해주세요.", ephemeral=True)
            return

        inventory, gear, user_abilities = await asyncio.gather(
            get_inventory(user),
            get_user_gear(user),
            get_user_abilities(user.id)
        )

        if inventory.get(MINING_PASS_NAME, 0) < 1:
            await interaction.followup.send(f"'{MINING_PASS_NAME}'이 부족합니다. 상점에서 구매해주세요.", ephemeral=True)
            return

        pickaxe = gear.get('pickaxe', BARE_HANDS)
        if pickaxe == BARE_HANDS:
            await interaction.followup.send("❌ 곡괭이를 장착해야 광산에 입장할 수 있습니다.\n상점에서 구매 후 프로필에서 장착해주세요.", ephemeral=True)
            return

        await update_inventory(user.id, MINING_PASS_NAME, -1)

        try:
            thread = await interaction.channel.create_thread(
                name=f"⛏️｜{user.display_name}의 광산", type=discord.ChannelType.private_thread, invitable=False
            )
            await thread.add_user(user)
        except Exception as e:
            logger.error(f"광산 스레드 생성 실패: {e}", exc_info=True)
            await interaction.followup.send("❌ 광산을 여는 데 실패했습니다. 채널 권한을 확인해주세요.", ephemeral=True)
            await update_inventory(user.id, MINING_PASS_NAME, 1)
            return
        
        duration = DEFAULT_MINE_DURATION_SECONDS
        duration_doubled = 'mine_duration_up_1' in user_abilities and random.random() < 0.15
        if duration_doubled:
            duration *= 2
        
        view = MiningGameView(self, user, thread, pickaxe, user_abilities, duration, duration_doubled)
        
        embed = view.build_embed()
        embed.title = f"⛏️ {user.display_name}님의 광산 채굴"
        
        await thread.send(embed=embed, view=view)
        await view.start()
        
        self.active_sessions[user.id] = {"thread_id": thread.id}
        await interaction.followup.send(f"광산에 입장했습니다! {thread.mention}", ephemeral=True)

    async def close_mine_session(self, user_id: int, thread: Optional[discord.Thread], reason: str):
        if user_id not in self.active_sessions:
            return
        logger.info(f"{user_id}의 광산 세션을 '{reason}' 이유로 종료합니다.")
        self.active_sessions.pop(user_id, None)

        if thread:
            try:
                await thread.send(f"**광산이 닫혔습니다.** ({reason})")
                await asyncio.sleep(10)
                await thread.delete()
            except (discord.NotFound, discord.Forbidden) as e:
                logger.warning(f"광산 스레드(ID: {thread.id}) 삭제/메시지 전송 실패: {e}")

    async def register_persistent_views(self):
        self.bot.add_view(MiningPanelView(self))

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_mining"):
        if panel_info := get_panel_id(panel_key):
            try:
                if old_channel := self.bot.get_channel(panel_info['channel_id']):
                    msg = await old_channel.fetch_message(panel_info['message_id'])
                    await msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

        embed_data = await get_embed_from_db(panel_key)
        if not embed_data:
            logger.error(f"DB에서 '{panel_key}' 임베드를 찾을 수 없어 패널을 생성할 수 없습니다.")
            return

        embed = discord.Embed.from_dict(embed_data)
        view = MiningPanelView(self)

        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} 패널을 성공적으로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Mining(bot))
