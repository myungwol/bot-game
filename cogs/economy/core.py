# bot-game/cogs/core.py

import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
import random
import asyncio
import logging
import time
from typing import Optional, Dict, List
from datetime import datetime, timezone, timedelta, time

from utils.database import (
    get_wallet, update_wallet,
    get_id, supabase, get_embed_from_db, get_config,
    save_config_to_db
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

PROGRESS_TABLE = "user_progress"
ACTIVITY_PROGRESS_TABLE = "user_activity_progress"

JST = timezone(timedelta(hours=9))
JST_MIDNIGHT_RESET = time(hour=0, minute=1, tzinfo=JST)

class EconomyCore(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.coin_log_channel_id: Optional[int] = None
        self.currency_icon = "🪙"
        self._chat_cooldown = commands.CooldownMapping.from_cooldown(1, 3.0, commands.BucketType.user)
        self.voice_sessions: Dict[int, datetime] = {}
        
        self.chat_progress_cache: Dict[int, int] = {}
        self._cache_lock = asyncio.Lock()

        self.reward_payout_loop.start()
        self.update_chat_progress_loop.start()
        self.daily_reset_loop.start()

        logger.info("EconomyCore Cog가 성공적으로 초기화되었습니다.")
        
    async def cog_load(self):
        await self.load_configs()
        
    async def load_configs(self):
        self.coin_log_channel_id = get_id("coin_log_channel_id")
        self.currency_icon = get_config("GAME_CONFIG", {}).get("CURRENCY_ICON", "🪙")
        logger.info("[EconomyCore Cog] 데이터베이스로부터 설정을 성공적으로 로드했습니다.")
        
    def cog_unload(self):
        self.reward_payout_loop.cancel()
        self.update_chat_progress_loop.cancel()
        self.daily_reset_loop.cancel()
    
    # [✅ 구조 개선] 레벨업 이벤트 핸들러 중앙화
    async def handle_level_up_event(self, user: discord.User, result_data: Dict):
        if not result_data or not result_data.get('leveled_up'):
            return

        new_level = result_data.get('new_level')
        logger.info(f"유저 {user.display_name}(ID: {user.id})가 레벨 {new_level}(으)로 레벨업했습니다.")
        
        # 특정 레벨 도달 시 전직 요청
        job_advancement_levels = get_config("GAME_CONFIG", {}).get("JOB_ADVANCEMENT_LEVELS", [50, 100])
        if new_level in job_advancement_levels:
            await save_config_to_db(f"job_advancement_request_{user.id}", {"level": new_level, "timestamp": time.time()})
            logger.info(f"유저가 전직 가능 레벨({new_level})에 도달하여 DB에 요청을 기록했습니다.")

        # 레벨 등급 역할 업데이트 요청
        await save_config_to_db(f"level_tier_update_request_{user.id}", {"level": new_level, "timestamp": time.time()})

    @tasks.loop(time=JST_MIDNIGHT_RESET)
    async def daily_reset_loop(self):
        logger.info("[일일 초기화] 모든 유저의 일일 퀘스트 진행도 초기화를 시작합니다.")
        try:
            await supabase.rpc('reset_daily_progress_all_users').execute()
            logger.info("[일일 초기화] 성공적으로 완료되었습니다.")
        except Exception as e:
            logger.error(f"[일일 초기화] 진행도 초기화 중 오류 발생: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or message.content.startswith('/'):
            return

        bucket = self._chat_cooldown.get_bucket(message)
        if bucket.update_rate_limit(): return
        
        user_id = message.author.id
        async with self._cache_lock:
            self.chat_progress_cache[user_id] = self.chat_progress_cache.get(user_id, 0) + 1

    @tasks.loop(minutes=1)
    async def update_chat_progress_loop(self):
        await self.bot.wait_until_ready()
        async with self._cache_lock:
            if not self.chat_progress_cache: return
            data_to_update = self.chat_progress_cache.copy()
            self.chat_progress_cache.clear()
        try:
            user_updates_json = [{"user_id": str(uid), "chat_count": count} for uid, count in data_to_update.items()]
            await supabase.rpc('batch_increment_chat_progress', {'p_user_updates': user_updates_json}).execute()
        except Exception as e:
            logger.error(f"채팅 활동 일괄 업데이트 중 DB 오류: {e}", exc_info=True)
            async with self._cache_lock:
                for user_update in user_updates_json:
                    uid, count = int(user_update['user_id']), int(user_update['chat_count'])
                    self.chat_progress_cache[uid] = self.chat_progress_cache.get(uid, 0) + count

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot: return
        is_valid = lambda state: state.channel and state.channel.id != member.guild.afk_channel.id if member.guild.afk_channel else True
        is_active = lambda state: not state.self_deaf and not state.self_mute
        
        if not (is_valid(before) and is_active(before)) and (is_valid(after) and is_active(after)):
            self.voice_sessions[member.id] = datetime.now(timezone.utc)
        elif (is_valid(before) and is_active(before)) and not (is_valid(after) and is_active(after)):
            if join_time := self.voice_sessions.pop(member.id, None):
                duration_minutes = (datetime.now(timezone.utc) - join_time).total_seconds() / 60.0
                if duration_minutes > 0.1:
                    try:
                        await supabase.rpc('increment_user_progress', {'p_user_id': str(member.id), 'p_voice_minutes': duration_minutes}).execute()
                    except Exception as e:
                        logger.error(f"음성 시간 DB 업데이트 중 오류: {e}", exc_info=True)
                        self.voice_sessions[member.id] = join_time

    @tasks.loop(minutes=5)
    async def reward_payout_loop(self):
        game_config = get_config("GAME_CONFIG", {})
        try:
            # 음성 보상
            voice_req = game_config.get("VOICE_TIME_REQUIREMENT_MINUTES", 10)
            voice_reward = game_config.get("VOICE_REWARD_RANGE", [10, 15])
            voice_xp = game_config.get("XP_FROM_VOICE", 10)
            await self.process_rewards('voice', voice_req, voice_reward, voice_xp, "ボイスチャット活動報酬")

            # 채팅 보상
            chat_req = game_config.get("CHAT_MESSAGE_REQUIREMENT", 20)
            chat_reward = game_config.get("CHAT_REWARD_RANGE", [5, 10])
            chat_xp = game_config.get("XP_FROM_CHAT", 5)
            await self.process_rewards('chat', chat_req, chat_reward, chat_xp, "チャット活動報酬")

        except Exception as e:
            logger.error(f"활동 보상 지급 루프 중 오류: {e}", exc_info=True)

    async def process_rewards(self, reward_type: str, requirement: int, reward_range: List[int], xp_reward: int, reason: str):
        table, column = (PROGRESS_TABLE, 'daily_voice_minutes') if reward_type == 'voice' else (ACTIVITY_PROGRESS_TABLE, 'chat_progress')
        
        response = await supabase.table(table).select('user_id').gte(column, requirement).execute()
        if not (response and response.data): return

        for record in response.data:
            user_id = int(record['user_id'])
            if not (member := self.bot.get_user(user_id)): continue
            
            try:
                reward = random.randint(reward_range[0], reward_range[1])
                await update_wallet(member, reward)
                await self.log_coin_activity(member, reward, reason)
                
                res = await supabase.rpc('add_xp', {'p_user_id': member.id, 'p_xp_to_add': xp_reward, 'p_source': reward_type}).execute()
                if res and res.data: await self.handle_level_up_event(member, res.data[0])

            except Exception as e:
                logger.error(f"{reason} 처리 중 오류 (유저: {user_id}): {e}", exc_info=True)
            finally:
                reset_params = {'p_user_id': str(user_id), f'p_reset_{reward_type}': True}
                await supabase.rpc('reset_user_progress', reset_params).execute()

    @reward_payout_loop.before_loop
    async def before_reward_payout_loop(self):
        await self.bot.wait_until_ready()
    
    async def log_coin_activity(self, user: discord.Member, amount: int, reason: str):
        if not self.coin_log_channel_id or not (log_channel := self.bot.get_channel(self.coin_log_channel_id)): return
        if not (embed_data := await get_embed_from_db("log_coin_gain")): return
        embed = format_embed_from_db(embed_data, user_mention=user.mention, amount=f"{amount:,}", currency_icon=self.currency_icon, reason=reason)
        if user.display_avatar: embed.set_thumbnail(url=user.display_avatar.url)
        try: await log_channel.send(embed=embed)
        except Exception as e: logger.error(f"코인 활동 로그 전송 실패: {e}", exc_info=True)

    async def log_admin_action(self, admin: discord.Member, target: discord.Member, amount: int, action: str):
        if not self.coin_log_channel_id or not (log_channel := self.bot.get_channel(self.coin_log_channel_id)): return
        if not (embed_data := await get_embed_from_db("log_coin_admin")): return
        action_color = 0x3498DB if amount > 0 else 0xE74C3C
        amount_str = f"+{amount:,}" if amount > 0 else f"{amount:,}"
        embed = format_embed_from_db(embed_data, action=action, target_mention=target.mention, amount=amount_str, currency_icon=self.currency_icon, admin_mention=admin.mention)
        embed.color = discord.Color(action_color)
        try: await log_channel.send(embed=embed)
        except Exception as e: logger.error(f"관리자 코인 조작 로그 전송 실패: {e}", exc_info=True)
        
    @app_commands.command(name="コイン付与", description="[管理者専用] 特定のユーザーにコインを付与します。")
    @app_commands.checks.has_permissions(administrator=True)
    async def give_coin_command(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, None]):
        await interaction.response.defer(ephemeral=True)
        if await update_wallet(user, amount):
            await self.log_admin_action(interaction.user, user, amount, "付与")
            await interaction.followup.send(f"✅ {user.mention}さんへ `{amount:,}`{self.currency_icon}を付与しました。")
        else: await interaction.followup.send("❌ コイン付与中にエラーが発生しました。")
        
    @app_commands.command(name="コイン削減", description="[管理者専用] 特定のユーザーのコインを削減します。")
    @app_commands.checks.has_permissions(administrator=True)
    async def take_coin_command(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, None]):
        await interaction.response.defer(ephemeral=True)
        if await update_wallet(user, -amount):
            await self.log_admin_action(interaction.user, user, -amount, "削減")
            await interaction.followup.send(f"✅ {user.mention}さんの残高から `{amount:,}`{self.currency_icon}を削減しました。")
        else: await interaction.followup.send("❌ コイン削減中にエラーが発生しました。")

async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCore(bot))
