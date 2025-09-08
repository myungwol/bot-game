# cogs/games/mining.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
import time
import random
from typing import Optional, Dict, List
from datetime import datetime, timezone, timedelta

from utils.database import (
    get_inventory, update_inventory, get_user_gear, BARE_HANDS,
    save_panel_id, get_panel_id, get_id, get_embed_from_db,
    log_activity, get_user_abilities, supabase
)
from utils.helpers import format_embed_from_db, format_timedelta_minutes_seconds

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

ORE_XP_MAP = {
    "구리 광석": 10,
    "철 광석": 15,
    "금 광석": 30,
    "다이아몬드": 75
}

class MiningGameView(ui.View):
    def __init__(self, cog_instance: 'Mining', user: discord.Member, thread: discord.Thread, pickaxe: str, user_abilities: List[str], duration: int, end_time: datetime, duration_doubled: bool):
        super().__init__(timeout=duration + 15)
        self.cog = cog_instance
        self.user = user
        self.thread = thread
        self.pickaxe = pickaxe
        self.user_abilities = user_abilities
        self.duration_doubled = duration_doubled
        self.end_time = end_time
        
        self.luck_bonus = PICKAXE_LUCK_BONUS.get(pickaxe, 1.0)
        if 'mine_rare_up_2' in self.user_abilities: self.luck_bonus += 0.5
        
        self.time_reduction = 3 if 'mine_time_down_1' in self.user_abilities else 0
        self.can_double_yield = 'mine_double_yield_2' in self.user_abilities

        self.state = "idle"
        self.discovered_ore: Optional[str] = None
        self.last_result_text: Optional[str] = None
        self.message: Optional[discord.Message] = None
        self.on_cooldown = False

        self.ui_update_task = self.cog.bot.loop.create_task(self.ui_updater())

    def stop(self):
        if hasattr(self, 'ui_update_task') and not self.ui_update_task.done():
            self.ui_update_task.cancel()
        super().stop()

    async def ui_updater(self):
        while not self.is_finished():
            try:
                if self.message and self.state == "idle":
                    embed = self.build_embed()
                    await self.message.edit(embed=embed)
            except (discord.NotFound, discord.Forbidden):
                self.stop()
                break
            except Exception as e:
                logger.error(f"Mining UI 업데이트 중 오류: {e}", exc_info=True)
            
            await asyncio.sleep(10)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.on_cooldown:
            await interaction.response.send_message("⏳ 아직 주변을 살피고 있습니다. 잠시 후에 다시 시도해주세요.", ephemeral=True, delete_after=5)
            return False
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("본인만 채굴할 수 있습니다.", ephemeral=True, delete_after=5)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"{self.user.display_name}님의 광산 채굴", color=0x607D8B)

        if self.state == "idle":
            description_parts = ["## 앞으로 나아가 광물을 찾아보자"]
            if self.last_result_text:
                description_parts.append(f"## 채굴 결과\n{self.last_result_text}")
            
            remaining_time = self.end_time - datetime.now(timezone.utc)
            description_parts.append(f"광산 닫힘까지: **{format_timedelta_minutes_seconds(remaining_time)}**")

            active_abilities = []
            if self.duration_doubled: active_abilities.append("> ✨ 집중 탐사 (시간 2배)")
            if self.time_reduction > 0: active_abilities.append("> ⚡ 신속한 채굴 (쿨타임 감소)")
            if self.can_double_yield: active_abilities.append("> 💰 풍부한 광맥 (수량 2배 확률)")
            if 'mine_rare_up_2' in self.user_abilities: active_abilities.append("> 💎 노다지 발견 (희귀 광물 확률 증가)")
            if active_abilities:
                description_parts.append(f"**--- 활성화된 능력 ---**\n" + "\n".join(active_abilities))
            description_parts.append(f"**사용 중인 장비:** {self.pickaxe}")
            embed.description = "\n\n".join(description_parts)
            embed.set_image(url=None)
        
        elif self.state == "discovered":
            desc_text = f"### {self.discovered_ore}을(를) 발견했다!" if self.discovered_ore != "꽝" else "### 아무것도 발견하지 못했다..."
            embed.description = desc_text
            embed.set_image(url=ORE_DATA[self.discovered_ore]['image_url'])
            embed.set_footer(text=f"사용 중인 장비: {self.pickaxe}")
            
        elif self.state == "mining":
            embed.description = f"**{self.pickaxe}**(으)로 열심히 **{self.discovered_ore}**을(를) 캐는 중입니다..."
            embed.set_image(url=ORE_DATA[self.discovered_ore]['image_url'])
            embed.set_footer(text=f"사용 중인 장비: {self.pickaxe}")
        
        return embed

    @ui.button(label="광석 찾기", style=discord.ButtonStyle.secondary, emoji="🔍", custom_id="mine_action_button")
    async def action_button(self, interaction: discord.Interaction, button: ui.Button):
        if self.user.id not in self.cog.active_sessions:
            button.disabled = True
            await interaction.response.edit_message(content="이미 만료된 광산입니다.", view=self, embed=None)
            return
        
        if self.state == "idle":
            self.last_result_text = None
            button.disabled = True; button.label = "탐색 중..."
            embed = discord.Embed(title=f"{self.user.display_name}님의 광산 채굴", description="더 깊이 들어가서 찾아보자...", color=0x607D8B)
            await interaction.response.edit_message(embed=embed, view=self)
            
            ores = list(ORE_DATA.keys())
            original_weights = [data['weight'] for data in ORE_DATA.values()]
            new_weights = [w * self.luck_bonus if o != "꽝" else w for o, w in zip(ores, original_weights)]
            self.discovered_ore = random.choices(ores, weights=new_weights, k=1)[0]
            
            if self.discovered_ore == "꽝":
                self.state = "discovered"
                button.label = "다시 찾아보기"
                button.emoji = "🔍"
            else:
                self.state = "discovered"
                button.label = "채굴하기"; button.style = discord.ButtonStyle.primary; button.emoji = "⛏️"
            
            embed = self.build_embed()
            button.disabled = False
            await interaction.edit_original_response(embed=embed, view=self)

        elif self.state == "discovered":
            if self.discovered_ore == "꽝":
                self.on_cooldown = True
                button.disabled = True
                await interaction.response.edit_message(view=self)
                cooldown = MINING_COOLDOWN_SECONDS - self.time_reduction
                await asyncio.sleep(cooldown)
                self.on_cooldown = False
                if self.is_finished() or self.user.id not in self.cog.active_sessions: return
                
                self.state = "idle"
                self.last_result_text = "### 아무것도 발견하지 못했다..."
                button.label = "광석 찾기"
                button.emoji = "🔍"
                button.disabled = False
                embed = self.build_embed()
                await interaction.edit_original_response(embed=embed, view=self)

            else:
                self.state = "mining"
                button.disabled = True
                mining_duration = max(3, MINING_COOLDOWN_SECONDS - self.time_reduction)
                button.label = f"채굴 중... ({mining_duration}초)"
                embed = self.build_embed()
                await interaction.response.edit_message(embed=embed, view=self)

                await asyncio.sleep(mining_duration)
                if self.is_finished() or self.user.id not in self.cog.active_sessions: return

                quantity = 2 if self.can_double_yield and random.random() < 0.20 else 1
                xp_earned = ORE_XP_MAP.get(self.discovered_ore, 0) * quantity

                await update_inventory(self.user.id, self.discovered_ore, quantity)
                await log_activity(self.user.id, 'mining', amount=quantity, xp_earned=xp_earned)
                
                self.last_result_text = f"✅ **{self.discovered_ore}** {quantity}개를 획득했습니다! (`+{xp_earned} XP`)"
                if quantity > 1: self.last_result_text += f"\n\n✨ **풍부한 광맥** 능력으로 광석을 2개 획득했습니다!"
                
                if xp_earned > 0:
                    res = await supabase.rpc('add_xp', {'p_user_id': self.user.id, 'p_xp_to_add': xp_earned, 'p_source': 'mining'}).execute()
                    if res.data and (level_cog := self.cog.bot.get_cog("LevelSystem")):
                        await level_cog.handle_level_up_event(self.user, res.data)
                
                self.state = "idle"
                embed = self.build_embed()
                button.label = "광석 찾기"; button.style = discord.ButtonStyle.secondary; button.emoji = "🔍"
                button.disabled = False
                
                try: await interaction.edit_original_response(embed=embed, view=self)
                except discord.NotFound: self.stop()

    async def on_timeout(self):
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
        self.check_stale_sessions.start()

    def cog_unload(self):
        self.check_stale_sessions.cancel()

    @tasks.loop(seconds=60.0)
    async def check_stale_sessions(self):
        now = datetime.now(timezone.utc)
        stale_user_ids = [
            uid for uid, session in self.active_sessions.items()
            if now >= session.get('end_time', now)
        ]
        for user_id in stale_user_ids:
            session_data = self.active_sessions.get(user_id)
            if session_data:
                logger.warning(f"오래된 광산 세션(유저: {user_id})을 안전장치 루프를 통해 종료합니다.")
                await self.close_mine_session(user_id, "시간 초과 (안전장치)")
    
    @check_stale_sessions.before_loop
    async def before_check_stale_sessions(self):
        await self.bot.wait_until_ready()

    async def handle_enter_mine(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user

        if user.id in self.active_sessions:
            thread_id = self.active_sessions[user.id].get("thread_id")
            if thread := self.bot.get_channel(thread_id):
                await interaction.followup.send(f"이미 광산에 입장해 있습니다. {thread.mention}", ephemeral=True)
            else:
                await self.close_mine_session(user.id, "오류로 인한 강제 종료")
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
        
        end_time = datetime.now(timezone.utc) + timedelta(seconds=duration)
        
        session_task = self.bot.loop.create_task(self.mine_session_timer(user.id, duration))
        
        self.active_sessions[user.id] = {
            "thread_id": thread.id,
            "end_time": end_time,
            "session_task": session_task
        }
        
        view = MiningGameView(self, user, thread, pickaxe, user_abilities, duration, end_time, duration_doubled)
        
        embed = view.build_embed()
        embed.title = f"⛏️ {user.display_name}님의 광산 채굴"
        
        message = await thread.send(embed=embed, view=view)
        view.message = message
        
        await interaction.followup.send(f"광산에 입장했습니다! {thread.mention}", ephemeral=True)

    async def mine_session_timer(self, user_id: int, duration: int):
        try:
            if duration > 60:
                await asyncio.sleep(duration - 60)
                if session := self.active_sessions.get(user_id):
                    if thread := self.bot.get_channel(session['thread_id']):
                        try: await thread.send("⚠️ 1분 후 광산이 닫힙니다...", delete_after=59)
                        except (discord.Forbidden, discord.HTTPException): pass
                else: return
                await asyncio.sleep(60)
            else:
                await asyncio.sleep(duration)
            
            if user_id in self.active_sessions:
                 await self.close_mine_session(user_id, "시간이 다 되어")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"광산 세션 타이머(유저: {user_id}) 중 오류: {e}", exc_info=True)
            
    # ▼▼▼ [핵심 수정] 함수 시그니처와 내부 로직이 변경되었습니다. ▼▼▼
    async def close_mine_session(self, user_id: int, reason: str):
        session_data = self.active_sessions.pop(user_id, None)
        if not session_data:
            return
        
        logger.info(f"[{user_id}] 광산 세션을 '{reason}' 이유로 종료 시작.")

        if session_task := session_data.get("session_task"):
            if not session_task.done():
                session_task.cancel()

        thread_id = session_data.get("thread_id")
        if not thread_id:
            logger.error(f"[{user_id}] 세션 데이터에 thread_id가 없어 스레드를 종료할 수 없습니다.")
            return

        thread = None
        try:
            thread = self.bot.get_channel(thread_id) or await self.bot.fetch_channel(thread_id)
            logger.info(f"[{user_id}] 스레드 객체(ID: {thread_id})를 성공적으로 찾았습니다.")
        except discord.NotFound:
            logger.warning(f"[{user_id}] 종료할 스레드(ID: {thread_id})를 찾을 수 없습니다. 이미 삭제되었을 수 있습니다.")
            return
        except discord.Forbidden:
            logger.error(f"[{user_id}] 스레드 정보(ID: {thread_id})를 가져올 권한이 없습니다. 이 문제는 서버 관리자가 해결해야 합니다.")
            return
        except Exception as e:
            logger.error(f"[{user_id}] 스레드(ID: {thread_id})를 가져오는 중 예기치 않은 오류 발생: {e}", exc_info=True)
            return

        try:
            # 멤버십 보장을 위해 봇 스스로를 추가 시도
            await thread.add_user(self.bot.user)
            await thread.send(f"**광산이 닫혔습니다.** ({reason})", delete_after=10)
            
            await asyncio.sleep(0.5) # 메시지 전송 후 아주 짧은 딜레이
            
            logger.info(f"[{user_id}] 스레드(ID: {thread.id}) 삭제를 시도합니다...")
            await thread.delete()
            logger.info(f"[{user_id}] 스레드(ID: {thread.id})를 성공적으로 삭제했습니다.")

        except discord.Forbidden as e:
            logger.error(f"[{user_id}] 최종 권한 부족으로 스레드(ID: {thread.id}) 처리 실패. '스레드 관리' 권한을 확인해주세요. 상세 오류: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"[{user_id}] 스레드 처리(메시지 전송 또는 삭제) 중 예기치 않은 오류 발생: {e}", exc_info=True)


    async def register_persistent_views(self):
        self.bot.add_view(MiningPanelView(self))

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_mining"):
        if panel_info := get_panel_id(panel_key):
            try:
                if old_channel := self.bot.get_channel(panel_info['channel_id']):
                    msg = await old_channel.fetch_message(panel_info['message_id'])
                    await msg.delete()
            except (discord.NotFound, discord.Forbidden): pass

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
