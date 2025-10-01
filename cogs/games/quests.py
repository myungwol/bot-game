# cogs/games/quests.py

import discord
from discord.ext import commands
from discord import ui
import logging
import time
from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta
import asyncio

from utils.database import (
    get_all_user_stats, 
    get_config,
    save_panel_id, get_panel_id, get_embed_from_db,
    update_wallet, set_cooldown, get_cooldown, log_activity,
    supabase, get_id
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

def get_current_week_start_end_utc() -> (str, str):
    """현재 KST 기준의 주(월요일 시작)의 시작과 끝 시간을 UTC ISO 형식으로 반환합니다."""
    now_kst = datetime.now(KST)
    start_of_week_kst = now_kst - timedelta(days=now_kst.weekday())
    start_of_week_kst = start_of_week_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    
    end_of_week_kst = start_of_week_kst + timedelta(days=7)
    
    start_of_week_utc = start_of_week_kst.astimezone(timezone.utc).isoformat()
    end_of_week_utc = end_of_week_kst.astimezone(timezone.utc).isoformat()
    
    return start_of_week_utc, end_of_week_utc

# ▼▼▼ [핵심 수정 1] 퀘스트 보상 구조 변경 ▼▼▼
# 개별 보상은 XP만 지급하고, 코인 보상은 '전체 완료' 시에만 지급하도록 구조를 단순화합니다.
QUEST_REWARDS = {
    "daily": {
        "attendance": {"xp": 10},
        "chat": {"xp": 10},
        "voice": {"xp": 50},
        "slot": {"xp": 15},
        "dice": {"xp": 15},
        "all_complete": {"coin": 300, "xp": 100}  # 전체 완료 시 코인 300 지급
    },
    "weekly": {
        "attendance": {"xp": 50},
        "chat": {"xp": 50},
        "voice": {"xp": 150},
        "fishing": {"xp": 75},
        "slot": {"xp": 45},
        "dice": {"xp": 45},
        "all_complete": {"coin": 1000, "xp": 500} # 전체 완료 시 코인 1000 지급
    }
}
# ▲▲▲ [핵심 수정 1] 완료 ▲▲▲

# ▼▼▼ [핵심 수정 2] 퀘스트 목표 변경 ▼▼▼
DAILY_QUESTS = {
    "attendance": {"name": "출석 체크하기", "goal": 1},
    "chat": {"name": "채팅 5회 하기", "goal": 5},
    "voice": {"name": "음성 채널에 60분 참가하기", "goal": 60},
    "slot": {"name": "슬롯 머신 1회 플레이", "goal": 1},
    "dice": {"name": "주사위 게임 1회 플레이", "goal": 1},
}
WEEKLY_QUESTS = {
    "attendance": {"name": "출석 체크 5회 하기", "goal": 5},
    "chat": {"name": "채팅 20회 하기", "goal": 20},
    "voice": {"name": "음성 채널에 180분 참가하기", "goal": 180},
    "fishing": {"name": "물고기 10마리 낚기", "goal": 10},
    "slot": {"name": "슬롯 머신 3회 플레이", "goal": 3},
    "dice": {"name": "주사위 게임 3회 플레이", "goal": 3},
}
# ▲▲▲ [핵심 수정 2] 완료 ▲▲▲

class TaskBoardView(ui.View):
    # ... (이 부분은 수정 없음) ...
    def __init__(self, cog_instance: 'Quests'):
        super().__init__(timeout=None)
        self.cog = cog_instance

        check_in_button = ui.Button(label="출석 체크", style=discord.ButtonStyle.success, emoji="✅", custom_id="task_board_daily_check")
        check_in_button.callback = self.check_in_callback
        self.add_item(check_in_button)

        quest_button = ui.Button(label="퀘스트 확인", style=discord.ButtonStyle.primary, emoji="📜", custom_id="task_board_open_quests")
        quest_button.callback = self.open_quest_view
        self.add_item(quest_button)

    async def check_in_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        
        stats = await get_all_user_stats(user.id)
        if stats.get('daily', {}).get('check_in_count', 0) > 0:
            await interaction.followup.send("❌ 오늘은 이미 출석 체크를 완료했습니다.", ephemeral=True)
            return

        reward_str = get_config("DAILY_CHECK_REWARD", "100").strip('"')
        attendance_reward = int(reward_str)
        
        await log_activity(user.id, 'daily_check_in', coin_earned=attendance_reward, xp_earned=0)
        await update_wallet(user, attendance_reward)
        
        await interaction.followup.send(f"✅ 출석 체크 완료! **`{attendance_reward}`**{self.cog.currency_icon}을(를) 획득했습니다.", ephemeral=True)

        log_embed = None
        if embed_data := await get_embed_from_db("log_daily_check"):
            log_embed = format_embed_from_db(embed_data, user_mention=user.mention, reward=attendance_reward, currency_icon=self.cog.currency_icon)
        
        if log_embed:
            try: await interaction.channel.send(embed=log_embed)
            except Exception as e: logger.error(f"출석체크 공개 로그 메시지 전송 실패 (채널: {interaction.channel.id}): {e}")

            if self.cog.log_channel_id and self.cog.log_channel_id != interaction.channel.id:
                if log_channel := self.cog.bot.get_channel(self.cog.log_channel_id):
                    try: await log_channel.send(embed=log_embed)
                    except Exception as e: logger.error(f"별도 출석체크 로그 채널로 전송 실패: {e}")
        
        await self.cog.regenerate_panel(interaction.channel)

    async def open_quest_view(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        view = QuestView(interaction.user, self.cog)
        embed = await view.build_embed()
        await view.update_components()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

class QuestView(ui.View):
    def __init__(self, user: discord.Member, cog_instance: 'Quests'):
        super().__init__(timeout=180)
        self.user = user
        self.cog = cog_instance
        self.current_tab = "daily"
        self.weekly_progress_cache: Optional[Dict] = None
        self.cache_timestamp: float = 0.0

    async def update_view(self, interaction: discord.Interaction):
        if not interaction.response.is_done(): await interaction.response.defer()
        embed = await self.build_embed()
        await self.update_components()
        await interaction.edit_original_response(embed=embed, view=self)

    async def _get_weekly_progress(self) -> Dict[str, int]:
        if self.weekly_progress_cache and time.time() - self.cache_timestamp < 30:
            return self.weekly_progress_cache

        start_utc, end_utc = get_current_week_start_end_utc()
        
        # ▼▼▼ [핵심 수정 3] 주간 퀘스트 집계에 필요한 RPC 호출 추가 ▼▼▼
        tasks = {
            'attendance': supabase.rpc('count_activity_in_range', {'p_user_id': str(self.user.id), 'p_activity_type': 'daily_check_in', 'p_start_time': start_utc, 'p_end_time': end_utc}).execute(),
            'chat': supabase.rpc('sum_activity_in_range', {'p_user_id': str(self.user.id), 'p_activity_type': 'chat', 'p_start_time': start_utc, 'p_end_time': end_utc}).execute(),
            'voice': supabase.rpc('sum_activity_in_range', {'p_user_id': str(self.user.id), 'p_activity_type': 'voice', 'p_start_time': start_utc, 'p_end_time': end_utc}).execute(),
            'fishing': supabase.rpc('sum_activity_in_range', {'p_user_id': str(self.user.id), 'p_activity_type': 'fishing_catch', 'p_start_time': start_utc, 'p_end_time': end_utc}).execute(),
            'slot': supabase.rpc('count_activity_in_range', {'p_user_id': str(self.user.id), 'p_activity_type': 'game_slot', 'p_start_time': start_utc, 'p_end_time': end_utc}).execute(),
            'dice': supabase.rpc('count_activity_in_range', {'p_user_id': str(self.user.id), 'p_activity_type': 'game_dice', 'p_start_time': start_utc, 'p_end_time': end_utc}).execute(),
        }
        
        results = await asyncio.gather(*tasks.values())
        res_map = dict(zip(tasks.keys(), results))
        
        self.weekly_progress_cache = {
            "check_in_count": res_map['attendance'].data if res_map['attendance'].data is not None else 0,
            "chat_count": res_map['chat'].data if res_map['chat'].data is not None else 0,
            "voice_minutes": res_map['voice'].data if res_map['voice'].data is not None else 0,
            "fishing_count": res_map['fishing'].data if res_map['fishing'].data is not None else 0,
            "slot_count": res_map['slot'].data if res_map['slot'].data is not None else 0,
            "dice_count": res_map['dice'].data if res_map['dice'].data is not None else 0,
        }
        # ▲▲▲ [핵심 수정 3] 완료 ▲▲▲
        self.cache_timestamp = time.time()
        return self.weekly_progress_cache

    async def build_embed(self) -> discord.Embed:
        if self.current_tab == "daily":
            summary = await get_all_user_stats(self.user.id)
            stats_to_show = summary.get("daily", {})
        else:
            stats_to_show = await self._get_weekly_progress()
        
        embed = discord.Embed(color=0x2ECC71)
        embed.set_author(name=f"{self.user.display_name}님의 퀘스트", icon_url=self.user.display_avatar.url if self.user.display_avatar else None)
        
        quests_to_show = DAILY_QUESTS if self.current_tab == "daily" else WEEKLY_QUESTS
        rewards = QUEST_REWARDS[self.current_tab]

        # ▼▼▼ [핵심 수정 4] 새로운 퀘스트 종류에 맞는 DB 키 매핑 ▼▼▼
        progress_key_map = {
            "attendance": "check_in_count", 
            "chat": "chat_count",
            "voice": "voice_minutes", 
            "fishing": "fishing_count",
            "slot": "slot_count",
            "dice": "dice_count",
        }
        # ▲▲▲ [핵심 수정 4] 완료 ▲▲▲
        
        embed.title = "📅 일일 퀘스트" if self.current_tab == "daily" else "🗓️ 주간 퀘스트"
        all_complete = True
        for key, quest in quests_to_show.items():
            db_key = progress_key_map[key]
            current = stats_to_show.get(db_key, 0)
            goal = quest["goal"]
            reward_xp = rewards.get(key, {}).get("xp", 0) # 코인 보상은 전체 완료 시에만 있으므로 XP만 표시
            is_complete = current >= goal
            if not is_complete: all_complete = False
            emoji = "✅" if is_complete else "❌"
            field_name = f"{emoji} {quest['name']}"
            field_value = f"> ` {min(current, goal)} / {goal} `\n> **보상:** `{reward_xp:,}` XP"
            embed.add_field(name=field_name, value=field_value, inline=False)
        
        if all_complete:
            all_in_reward_coin = rewards['all_complete'].get("coin", 0)
            all_in_reward_xp = rewards['all_complete'].get("xp", 0)
            embed.set_footer(text=f"🎉 모든 퀘스트 완료! 추가 보상: {all_in_reward_coin:,}{self.cog.currency_icon} + {all_in_reward_xp:,} XP")
        else:
            embed.set_footer(text="퀘스트를 완료하고 보상을 받으세요!")
        return embed

    async def update_components(self):
        for item in self.children:
            if isinstance(item, ui.Button) and item.custom_id.startswith("tab_"):
                item.style = discord.ButtonStyle.primary if item.custom_id == f"tab_{self.current_tab}" else discord.ButtonStyle.secondary
                item.disabled = item.custom_id == f"tab_{self.current_tab}"

        claim_button = next((child for child in self.children if isinstance(child, ui.Button) and child.custom_id == "claim_rewards_button"), None)
        if not claim_button: return

        quests_to_check = DAILY_QUESTS if self.current_tab == "daily" else WEEKLY_QUESTS
        
        if self.current_tab == "daily":
            summary = await get_all_user_stats(self.user.id)
            stats_to_check = summary.get("daily", {})
        else:
            stats_to_check = await self._get_weekly_progress()
        
        progress_key_map = {
            "attendance": "check_in_count", 
            "chat": "chat_count",
            "voice": "voice_minutes", 
            "fishing": "fishing_count",
            "slot": "slot_count",
            "dice": "dice_count",
        }

        all_quests_complete = True
        for key, quest in quests_to_check.items():
            db_key = progress_key_map[key]
            if stats_to_check.get(db_key, 0) < quest["goal"]:
                all_quests_complete = False
                break
        
        today_str = datetime.now(KST).strftime('%Y-%m-%d')
        week_start_str = (datetime.now(KST) - timedelta(days=datetime.now(KST).weekday())).strftime('%Y-%m-%d')
        period_str = today_str if self.current_tab == "daily" else week_start_str
        cooldown_key = f"quest_claimed_{self.current_tab}_all_{period_str}"
        already_claimed = await get_cooldown(self.user.id, cooldown_key) > 0

        if already_claimed:
            claim_button.label = "오늘의 보상을 받았습니다" if self.current_tab == "daily" else "이번 주 보상을 받았습니다"
            claim_button.style = discord.ButtonStyle.secondary
            claim_button.disabled = True
        elif all_quests_complete:
            claim_button.label = "모든 퀘스트 완료 보상 받기" # 버튼 라벨 변경
            claim_button.style = discord.ButtonStyle.success
            claim_button.disabled = False
        else:
            claim_button.label = "모든 퀘스트를 완료해주세요"
            claim_button.style = discord.ButtonStyle.secondary
            claim_button.disabled = True
    
    @ui.button(label="일일", style=discord.ButtonStyle.primary, custom_id="tab_daily", disabled=True)
    async def daily_tab_button(self, interaction: discord.Interaction, button: ui.Button):
        self.current_tab = "daily"
        await self.update_view(interaction)

    @ui.button(label="주간", style=discord.ButtonStyle.secondary, custom_id="tab_weekly")
    async def weekly_tab_button(self, interaction: discord.Interaction, button: ui.Button):
        self.current_tab = "weekly"
        await self.update_view(interaction)
    
    @ui.button(label="보상 받기", style=discord.ButtonStyle.success, emoji="💰", custom_id="claim_rewards_button", row=1)
    async def claim_rewards_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        rewards = QUEST_REWARDS[self.current_tab]
        
        # ▼▼▼ [핵심 수정 5] 보상 지급 로직 수정 ▼▼▼
        # 개별 퀘스트 XP와 전체 완료 보상을 합산하여 한 번에 지급합니다.
        total_coin_reward, total_xp_reward = 0, 0
        reward_details = []

        # 1. 개별 퀘스트 XP 합산
        quests = DAILY_QUESTS if self.current_tab == 'daily' else WEEKLY_QUESTS
        for key, quest in quests.items():
            reward_info = rewards.get(key, {})
            xp = reward_info.get("xp", 0)
            total_xp_reward += xp
            reward_details.append(f"・ {quest['name']}: `+{xp:,}` XP")
        
        # 2. 전체 완료 보너스 합산
        all_complete_reward = rewards.get("all_complete", {})
        all_coin, all_xp = all_complete_reward.get("coin", 0), all_complete_reward.get("xp", 0)
        total_coin_reward += all_coin
        total_xp_reward += all_xp
        reward_details.append(f"・ 모든 퀘스트 완료 보너스: `{all_coin:,}`{self.cog.currency_icon} + `{all_xp:,}` XP")
        # ▲▲▲ [핵심 수정 5] 완료 ▲▲▲
        
        today_str = datetime.now(KST).strftime('%Y-%m-%d')
        week_start_str = (datetime.now(KST) - timedelta(days=datetime.now(KST).weekday())).strftime('%Y-%m-%d')
        period_str = today_str if self.current_tab == "daily" else week_start_str
        cooldown_key = f"quest_claimed_{self.current_tab}_all_{period_str}"

        if total_coin_reward > 0 or total_xp_reward > 0:
            await log_activity(self.user.id, f"quest_claim_{self.current_tab}_all", coin_earned=total_coin_reward, xp_earned=total_xp_reward)
            if total_coin_reward > 0: await update_wallet(self.user, total_coin_reward)
            if total_xp_reward > 0:
                xp_res = await supabase.rpc('add_xp', {'p_user_id': self.user.id, 'p_xp_to_add': total_xp_reward, 'p_source': 'quest'}).execute()
                if xp_res.data and (level_cog := self.cog.bot.get_cog("LevelSystem")): await level_cog.handle_level_up_event(self.user, xp_res.data)
            await set_cooldown(self.user.id, cooldown_key)
            details_text = "\n".join(reward_details)
            await interaction.followup.send(f"🎉 **모든 {('일일' if self.current_tab == 'daily' else '주간')} 퀘스트 보상을 받았습니다!**\n{details_text}\n\n**합계:** `{total_coin_reward:,}`{self.cog.currency_icon} 와 `{total_xp_reward:,}` XP", ephemeral=True)
        else: await interaction.followup.send("❌ 받을 수 있는 보상이 없습니다.", ephemeral=True)
        await self.update_view(interaction)

class Quests(commands.Cog):
    # ... (이 부분은 수정 없음) ...
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.currency_icon = "🪙"
        self.log_channel_id: Optional[int] = None
    async def cog_load(self): await self.load_configs()
    async def load_configs(self):
        game_config = get_config("GAME_CONFIG", {})
        self.currency_icon = game_config.get("CURRENCY_ICON", "🪙")
        self.log_channel_id = get_id("log_daily_check_channel_id")
    async def register_persistent_views(self): self.bot.add_view(TaskBoardView(self))
    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_tasks", **kwargs):
        if panel_info := get_panel_id(panel_key):
            if (old_channel := self.bot.get_channel(panel_info['channel_id'])) and (old_message_id := panel_info.get('message_id')):
                try: await (await channel.fetch_message(old_message_id)).delete()
                except (discord.NotFound, discord.Forbidden): pass
        if not (embed_data := await get_embed_from_db(panel_key)): return logger.error(f"DB에서 '{panel_key}' 임베드를 찾을 수 없어 패널을 생성할 수 없습니다.")
        embed = discord.Embed.from_dict(embed_data)
        view = TaskBoardView(self)
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} 패널을 성공적으로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Quests(bot))
