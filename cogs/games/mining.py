# cogs/games/mining.py
import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
import time
import random
from typing import Optional, Dict, List

from utils.database import (
    get_inventory, update_inventory, get_user_gear, BARE_HANDS,
    save_panel_id, get_panel_id, get_id, get_embed_from_db,
    log_activity, get_user_abilities
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

MINING_PASS_NAME = "광산 입장권"
DEFAULT_MINE_DURATION_SECONDS = 600  # 10분
MINING_COOLDOWN_SECONDS = 10 # 고정 채굴 시간

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
    def __init__(self, cog_instance: 'Mining', user: discord.Member, thread: discord.Thread, pickaxe: str, user_abilities: List[str], duration: int):
        super().__init__(timeout=duration)
        self.cog = cog_instance
        self.user = user
        self.thread = thread
        self.pickaxe = pickaxe
        self.user_abilities = user_abilities
        
        # 능력 적용
        self.luck_bonus = PICKAXE_LUCK_BONUS.get(pickaxe, 1.0)
        if 'mine_rare_up_2' in self.user_abilities:
            self.luck_bonus += 0.5 # 전문 광부 능력: 희귀 광물 확률 50% 추가 증가
        
        self.time_reduction = 3 if 'mine_time_down_1' in self.user_abilities else 0
        self.can_double_yield = 'mine_double_yield_2' in self.user_abilities

        self.state = "finding"
        self.discovered_ore: Optional[str] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("본인만 채굴할 수 있습니다.", ephemeral=True, delete_after=5)
            return False
        return True

    @ui.button(label="광석 찾기", style=discord.ButtonStyle.secondary, emoji="🔍", custom_id="mine_action_button")
    async def action_button(self, interaction: discord.Interaction, button: ui.Button):
        
        if self.state == "finding":
            button.disabled = True
            await interaction.response.edit_message(view=self)

            ores = list(ORE_DATA.keys())
            original_weights = [data['weight'] for data in ORE_DATA.values()]
            
            new_weights = []
            for ore, weight in zip(ores, original_weights):
                if ore != "꽝":
                    new_weights.append(weight * self.luck_bonus)
                else:
                    new_weights.append(weight)
            
            self.discovered_ore = random.choices(ores, weights=new_weights, k=1)[0]

            embed = interaction.message.embeds[0]
            embed.description = f"**{self.discovered_ore}**을(를) 발견했다!"
            embed.set_image(url=ORE_DATA[self.discovered_ore]['image_url'])

            button.label = "채굴하기"
            button.style = discord.ButtonStyle.primary
            button.emoji = "⛏️"
            button.disabled = False
            self.state = "discovered"
            
            await interaction.edit_original_response(embed=embed, view=self)

        elif self.state == "discovered":
            button.disabled = True
            
            mining_duration = max(3, MINING_COOLDOWN_SECONDS - self.time_reduction)
            button.label = f"채굴 중... ({mining_duration}초)"
            button.style = discord.ButtonStyle.secondary
            
            original_embed = interaction.message.embeds[0]
            original_embed.description = f"**{self.pickaxe}**(으)로 열심히 **{self.discovered_ore}**을(를) 캐는 중입니다..."
            await interaction.response.edit_message(embed=original_embed, view=self)

            await asyncio.sleep(mining_duration)

            if self.is_finished() or self.user.id not in self.cog.active_sessions:
                return

            if self.discovered_ore != "꽝":
                quantity = 1
                double_yield_success = False
                if self.can_double_yield and random.random() < 0.20: # 20% 확률로 2배
                    quantity = 2
                    double_yield_success = True

                await update_inventory(self.user.id, self.discovered_ore, quantity)
                await log_activity(self.user.id, 'mining', amount=quantity)
                
                success_msg = f"✅ **{self.discovered_ore}** {quantity}개를 획득했습니다!"
                if double_yield_success:
                    success_msg += "\n✨ **풍부한 광맥** 능력으로 광석을 2개 획득했습니다!"

                await interaction.followup.send(success_msg, ephemeral=True)

            embed = interaction.message.embeds[0]
            embed.description = "다시 주변을 둘러보자. 어떤 광석이 나올까?"
            embed.set_image(url=ORE_DATA["꽝"]["image_url"])

            button.label = "광석 찾기"
            button.style = discord.ButtonStyle.secondary
            button.emoji = "🔍"
            button.disabled = False
            self.state = "finding"
            self.discovered_ore = None
            
            await interaction.edit_original_response(embed=embed, view=self)

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
                name=f"⛏️｜{user.display_name}의 광산",
                type=discord.ChannelType.private_thread,
                invitable=False
            )
            await thread.add_user(user)
        except Exception as e:
            logger.error(f"광산 스레드 생성 실패: {e}", exc_info=True)
            await interaction.followup.send("❌ 광산을 여는 데 실패했습니다. 채널 권한을 확인해주세요.", ephemeral=True)
            await update_inventory(user.id, MINING_PASS_NAME, 1)
            return

        embed_data = await get_embed_from_db("mine_thread_welcome")
        if not embed_data:
            logger.error("DB에서 'mine_thread_welcome' 임베드를 찾을 수 없습니다.")
            await interaction.followup.send("❌ 광산 정보를 불러오는 데 실패했습니다.", ephemeral=True)
            return
        
        duration = DEFAULT_MINE_DURATION_SECONDS
        duration_doubled = False
        if 'mine_duration_up_1' in user_abilities and random.random() < 0.15: # 15% 확률
            duration *= 2
            duration_doubled = True

        embed = format_embed_from_db(embed_data, user_name=user.display_name)
        embed.description = "광산에 들어왔다. 어떤 광석이 있을지 찾아보자!"
        if duration_doubled:
            embed.description += "\n\n✨ **집중 탐사** 능력 발동! 광산이 20분 동안 열립니다!"
        
        embed.set_footer(text=f"사용 중인 장비: {pickaxe}")
        embed.set_image(url=ORE_DATA["꽝"]["image_url"])

        view = MiningGameView(self, user, thread, pickaxe, user_abilities, duration)
        await thread.send(embed=embed, view=view)

        session_task = asyncio.create_task(self.mine_timer(user.id, thread, duration))
        self.active_sessions[user.id] = {"thread_id": thread.id, "task": session_task}

        await interaction.followup.send(f"광산에 입장했습니다! {thread.mention}", ephemeral=True)

    async def mine_timer(self, user_id: int, thread: discord.Thread, duration: int):
        await asyncio.sleep(duration)
        reason = f"{duration // 60}분이 지나"
        await self.close_mine_session(user_id, thread, reason)

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
