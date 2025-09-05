# cogs/games/mining.py
import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
import time
import random
from typing import Optional, Dict

from utils.database import (
    get_inventory, update_inventory, get_user_gear, BARE_HANDS,
    save_panel_id, get_panel_id, get_id, get_embed_from_db,
    log_activity
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

MINING_PASS_NAME = "광산 입장권"
MINE_DURATION_SECONDS = 600  # 10분

# 곡괭이별 채굴 속도 (초)
PICKAXE_SPEEDS = {
    "나무 곡괭이": 30,
    "구리 곡괭이": 25,
    "철 곡괭이": 20,
    "금 곡괭이": 15,
    "다이아 곡괭이": 10,
}

# ⚠️ 중요: 아래 URL들을 실제 Supabase Storage 이미지 URL로 교체해주세요!
ORE_DATA = {
    "꽝":       {"weight": 40, "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/stone.jpg"},
    "구리 광석": {"weight": 30, "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/cooper.jpg"},
    "철 광석":   {"weight": 20, "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/Iron.jpg"},
    "금 광석":    {"weight": 8,  "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/gold.jpg"},
    "다이아몬드": {"weight": 2,  "image_url": "https://saewayvzcyzueviasftu.supabase.co/storage/v1/object/public/game_assets/diamond.jpg"}
}

class MiningGameView(ui.View):
    def __init__(self, cog_instance: 'Mining', user: discord.Member, thread: discord.Thread, pickaxe: str):
        super().__init__(timeout=MINE_DURATION_SECONDS)
        self.cog = cog_instance
        self.user = user
        self.thread = thread
        self.pickaxe = pickaxe
        self.mining_speed = PICKAXE_SPEEDS.get(pickaxe, 999)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("본인만 채굴할 수 있습니다.", ephemeral=True, delete_after=5)
            return False
        return True

    @ui.button(label="채굴하기", style=discord.ButtonStyle.primary, emoji="⛏️", custom_id="mine_ore_button")
    async def mine_button(self, interaction: discord.Interaction, button: ui.Button):
        button.disabled = True
        
        # 1. 채굴 중 상태로 변경
        mining_duration = self.mining_speed
        button.label = f"채굴 중... ({mining_duration}초)"
        original_embed = interaction.message.embeds[0]
        original_embed.description = f"**{self.pickaxe}**(으)로 열심히 광석을 캐는 중입니다..."
        await interaction.response.edit_message(embed=original_embed, view=self)

        # 2. 곡괭이 속도만큼 대기
        await asyncio.sleep(mining_duration)

        if self.is_finished() or self.user.id not in self.cog.active_sessions:
            return

        # 3. 채굴 완료 및 다음 광석 발견
        ores = list(ORE_DATA.keys())
        weights = [data['weight'] for data in ORE_DATA.values()]
        found_ore = random.choices(ores, weights=weights, k=1)[0]

        embed = interaction.message.embeds[0]
        embed.description = f"**{found_ore}**을(를) 발견했다!"
        embed.set_image(url=ORE_DATA[found_ore]['image_url'])

        if found_ore != "꽝":
            await update_inventory(self.user.id, found_ore, 1)
            embed.description += "\n\n인벤토리에 추가되었습니다."
            await log_activity(self.user.id, 'mining', amount=1)

        button.disabled = False
        button.label = "채굴하기"
        await interaction.message.edit(embed=embed, view=self)

    async def on_timeout(self):
        await self.cog.close_mine_session(self.user.id, self.thread, "시간이 다 되어")

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

        # 이미 세션이 있는지 확인
        if user.id in self.active_sessions:
            thread_id = self.active_sessions[user.id].get("thread_id")
            if thread := self.bot.get_channel(thread_id):
                await interaction.followup.send(f"이미 광산에 입장해 있습니다. {thread.mention}", ephemeral=True)
            else:
                await self.close_mine_session(user.id, None, "오류로 인해")
                await interaction.followup.send("이전 광산 정보를 찾을 수 없어 초기화했습니다. 다시 시도해주세요.", ephemeral=True)
            return

        # 인벤토리 및 장비 확인
        inventory, gear = await asyncio.gather(
            get_inventory(user),
            get_user_gear(user)
        )

        # 입장권 확인
        if inventory.get(MINING_PASS_NAME, 0) < 1:
            await interaction.followup.send(f"'{MINING_PASS_NAME}'이 부족합니다. 상점에서 구매해주세요.", ephemeral=True)
            return

        # 곡괭이 장착 확인
        pickaxe = gear.get('pickaxe', BARE_HANDS)
        if pickaxe == BARE_HANDS:
            await interaction.followup.send("❌ 곡괭이를 장착해야 광산에 입장할 수 있습니다.\n상점에서 구매 후 프로필에서 장착해주세요.", ephemeral=True)
            return

        # 입장권 소모
        await update_inventory(user.id, MINING_PASS_NAME, -1)

        # 비공개 스레드 생성
        try:
            thread = await interaction.channel.create_thread(
                name=f"⛏️｜{user.display_name}의 광산",
                type=discord.ChannelType.private_thread,
                invitable=False
            )
            await thread.add_user(user)
        except Exception as e:
            logger.error(f"광산 스레드 생성 실패: {e}", exc_info=True)
            await interaction.followup.send("❌ 광산을 여는 데 실패했습니다. 채널 권한을 확인해주세요.", ephemeral=True)
            await update_inventory(user.id, MINING_PASS_NAME, 1) # 입장권 환불
            return

        # 환영 메시지 및 게임 View 전송
        embed_data = await get_embed_from_db("mine_thread_welcome")
        if not embed_data:
            logger.error("DB에서 'mine_thread_welcome' 임베드를 찾을 수 없습니다.")
            await interaction.followup.send("❌ 광산 정보를 불러오는 데 실패했습니다.", ephemeral=True)
            return
        
        embed = format_embed_from_db(embed_data, user_name=user.display_name)
        embed.set_footer(text=f"사용 중인 장비: {pickaxe}")
        embed.set_image(url=ORE_DATA["꽝"]["image_url"])

        view = MiningGameView(self, user, thread, pickaxe)
        await thread.send(embed=embed, view=view)

        # 세션 시작
        session_task = asyncio.create_task(self.mine_timer(user.id, thread))
        self.active_sessions[user.id] = {"thread_id": thread.id, "task": session_task}

        await interaction.followup.send(f"광산에 입장했습니다! {thread.mention}", ephemeral=True)

    async def mine_timer(self, user_id: int, thread: discord.Thread):
        await asyncio.sleep(MINE_DURATION_SECONDS)
        await self.close_mine_session(user_id, thread, "10분이 지나")

    async def close_mine_session(self, user_id: int, thread: Optional[discord.Thread], reason: str):
        logger.info(f"{user_id}의 광산 세션을 '{reason}' 이유로 종료합니다.")
        session = self.active_sessions.pop(user_id, None)
        if session and not session["task"].done():
            session["task"].cancel()

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
