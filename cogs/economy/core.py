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

# [✅ DB 구조 확인] 실제 DB 테이블 이름을 상수로 정의합니다.
PROGRESS_TABLE = "user_progress"
ACTIVITY_PROGRESS_TABLE = "user_activity_progress" # 채팅 보상용 테이블

JST_MIDNIGHT = time(hour=0, minute=1, tzinfo=timezone(timedelta(hours=9))) # DB 쓰기 작업을 피하기 위해 1분 늦게 실행

class EconomyCore(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.coin_log_channel_id: Optional[int] = None
        self.admin_role_id: Optional[int] = None
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
        self.admin_role_id = get_id("role_admin_total")
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        logger.info("[EconomyCore Cog] 데이터베이스로부터 설정을 성공적으로 로드했습니다.")
        
    def cog_unload(self):
        self.reward_payout_loop.cancel()
        self.update_chat_progress_loop.cancel()
        self.daily_reset_loop.cancel()
    
    async def handle_level_up_event(self, user: discord.User, result_data: Dict):
        if not result_data or not result_data.get('leveled_up'):
            return

        level_up_data = result_data
        new_level = level_up_data.get('new_level')
        
        if new_level in [50, 100]:
            await save_config_to_db(f"job_advancement_request_{user.id}", {"level": new_level, "timestamp": time.time()})
            logger.info(f"유저 {user.display_name}(ID: {user.id})가 전직 가능 레벨({new_level})에 도달하여 DB에 요청을 기록했습니다.")

        await save_config_to_db(f"level_tier_update_request_{user.id}", {"level": new_level, "timestamp": time.time()})

    @tasks.loop(time=JST_MIDNIGHT)
    async def daily_reset_loop(self):
        logger.info("[일일 초기화] 모든 유저의 일일 퀘스트 진행도 초기화를 시작합니다.")
        try:
            await supabase.table(PROGRESS_TABLE).update({
                "daily_voice_minutes": 0,
                "daily_fish_count": 0
            }).gt("user_id", 0).execute()
            
            logger.info("[일일 초기화] 성공적으로 완료되었습니다.")
        except Exception as e:
            logger.error(f"[일일 초기화] 진행도 초기화 중 오류 발생: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or message.content.startswith('/'):
            return

        bucket = self._chat_cooldown.get_bucket(message)
        retry_after = bucket.update_rate_limit()
        if retry_after:
            return
        
        user_id = message.author.id
        async with self._cache_lock:
            self.chat_progress_cache[user_id] = self.chat_progress_cache.get(user_id, 0) + 1

    @tasks.loop(minutes=1)
    async def update_chat_progress_loop(self):
        await self.bot.wait_until_ready()
        
        async with self._cache_lock:
            if not self.chat_progress_cache:
                return
            
            data_to_update = self.chat_progress_cache.copy()
            self.chat_progress_cache.clear()

        try:
            user_updates_json = [
                {"user_id": str(uid), "chat_count": count}
                for uid, count in data_to_update.items()
            ]
            await supabase.rpc('batch_increment_chat_progress', {'p_user_updates': user_updates_json}).execute()
        except Exception as e:
            logger.error(f"채팅 활동 일괄 업데이트 중 DB 오류: {e}", exc_info=True)
            async with self._cache_lock:
                for user_update in user_updates_json:
                    uid = int(user_update['user_id'])
                    count = int(user_update['chat_count'])
                    self.chat_progress_cache[uid] = self.chat_progress_cache.get(uid, 0) + count

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot: return

        def is_valid_channel(state: discord.VoiceState):
            afk_id = member.guild.afk_channel.id if member.guild.afk_channel else None
            return state.channel and state.channel.id != afk_id

        def is_active_state(state: discord.VoiceState):
            return not state.self_deaf and not state.self_mute

        is_before_valid = is_valid_channel(before) and is_active_state(before)
        is_after_valid = is_valid_channel(after) and is_active_state(after)

        if not is_before_valid and is_after_valid:
            self.voice_sessions[member.id] = datetime.now(timezone.utc)

        elif is_before_valid and not is_after_valid:
            if member.id in self.voice_sessions:
                join_time = self.voice_sessions.pop(member.id)
                duration = datetime.now(timezone.utc) - join_time
                duration_minutes = duration.total_seconds() / 60.0

                if duration_minutes > 0:
                    try:
                        params = {'p_user_id': str(member.id), 'p_voice_minutes': duration_minutes}
                        await supabase.rpc('increment_user_progress', params).execute()
                    except Exception as e:
                        logger.error(f"음성 시간 DB 업데이트 중 오류: {e}", exc_info=True)
                        self.voice_sessions[member.id] = join_time

    @tasks.loop(minutes=1)
    async def reward_payout_loop(self):
        try:
            # --- 음성 보상 로직 ---
            voice_req_min_config = str(get_config("VOICE_TIME_REQUIREMENT_MINUTES", "10")).strip('"')
            voice_req_min = int(voice_req_min_config)
            voice_reward_range_config = str(get_config("VOICE_REWARD_RANGE", "[10, 15]"))
            voice_reward_range = eval(voice_reward_range_config)
            
            voice_response = await supabase.table(PROGRESS_TABLE).select('user_id, daily_voice_minutes').gte('daily_voice_minutes', voice_req_min).execute()

            if voice_response and voice_response.data:
                for record in voice_response.data:
                    user_id = int(record['user_id'])
                    member = self.bot.get_user(user_id)
                    if not member: continue
                    try:
                        reward = random.randint(voice_reward_range[0], voice_reward_range[1])
                        await update_wallet(member, reward)
                        await self.log_coin_activity(member, reward, "ボイスチャット活動報酬")
                        xp_to_add = int(str(get_config("XP_FROM_VOICE", "10")).strip('"'))
                        res = await supabase.rpc('add_xp', {'p_user_id': member.id, 'p_xp_to_add': xp_to_add, 'p_source': 'voice'}).execute()
                        if res and res.data:
                            await self.handle_level_up_event(member, res.data[0])
                    finally:
                        reset_params = {'p_user_id': str(member.id), 'p_reset_voice': True}
                        await supabase.rpc('reset_user_progress', reset_params).execute()

            # --- 채팅 보상 로직 ---
            chat_req_config = str(get_config("CHAT_MESSAGE_REQUIREMENT", "10")).strip('"')
            chat_req = int(chat_req_config)
            chat_reward_range_config = str(get_config("CHAT_REWARD_RANGE", "[5, 10]"))
            chat_reward_range = eval(chat_reward_range_config)
            
            chat_response = await supabase.table(ACTIVITY_PROGRESS_TABLE).select('user_id, chat_progress').gte('chat_progress', chat_req).execute()

            if chat_response and chat_response.data:
                for record in chat_response.data:
                    user_id = int(record['user_id'])
                    member = self.bot.get_user(user_id)
                    if not member: continue
                    try:
                        reward = random.randint(chat_reward_range[0], chat_reward_range[1])
                        await update_wallet(member, reward)
                        await self.log_coin_activity(member, reward, "チャット活動報酬")
                        xp_to_add = int(str(get_config("XP_FROM_CHAT", "5")).strip('"'))
                        res = await supabase.rpc('add_xp', {'p_user_id': member.id, 'p_xp_to_add': xp_to_add, 'p_source': 'chat'}).execute()
                        if res and res.data:
                            await self.handle_level_up_event(member, res.data[0])
                    finally:
                        reset_params = {'p_user_id': str(member.id), 'p_reset_chat': True}
                        await supabase.rpc('reset_user_progress', reset_params).execute()

        except Exception as e:
            logger.error(f"음성/채팅 보상 지급 루프 중 오류: {e}", exc_info=True)
        
    @reward_payout_loop.before_loop
    async def before_reward_payout_loop(self):
        await self.bot.wait_until_ready()
    
    async def log_coin_activity(self, user: discord.Member, amount: int, reason: str):
        if not self.coin_log_channel_id or not (log_channel := self.bot.get_channel(self.coin_log_channel_id)): return
        
        if embed_data := await get_embed_from_db("log_coin_gain"):
            formatted_embed_data = embed_data.copy()
            
            if reason == "チャット活動報酬":
                formatted_embed_data['title'] = "💬 チャット活動報酬"
                formatted_embed_data['description'] = f"{user.mention}さんがチャット活動でコインを獲得しました。"
            else: 
                formatted_embed_data['title'] = "🎙️ ボイスチャット活動報酬"
                formatted_embed_data['description'] = f"{user.mention}さんがVC活動でコインを獲得しました。"

            embed = format_embed_from_db(
                formatted_embed_data, 
                user_mention=user.mention, 
                amount=f"{amount:,}", 
                currency_icon=self.currency_icon
            )

            if user.display_avatar:
                embed.set_thumbnail(url=user.display_avatar.url)
            
            try: 
                await log_channel.send(embed=embed)
            except Exception as e: 
                logger.error(f"코인 활동 로그 전송 실패: {e}", exc_info=True)

    async def log_coin_transfer(self, sender: discord.Member, recipient: discord.Member, amount: int):
        if not self.coin_log_channel_id or not (log_channel := self.bot.get_channel(self.coin_log_channel_id)): return
        
        if embed_data := await get_embed_from_db("log_coin_transfer"):
            embed = format_embed_from_db(embed_data, sender_mention=sender.mention, recipient_mention=recipient.mention, amount=f"{amount:,}", currency_icon=self.currency_icon)
            try: await log_channel.send(embed=embed)
            except Exception as e: logger.error(f"코인 송금 로그 전송 실패: {e}", exc_info=True)
        
    async def log_admin_action(self, admin: discord.Member, target: discord.Member, amount: int, action: str):
        if not self.coin_log_channel_id or not (log_channel := self.bot.get_channel(self.coin_log_channel_id)): return
        
        if embed_data := await get_embed_from_db("log_coin_admin"):
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
        result = await update_wallet(user, amount)
        if result:
            await self.log_admin_action(interaction.user, user, amount, "付与")
            await interaction.followup.send(f"✅ {user.mention}さんへ `{amount:,}`{self.currency_icon}を付与しました。")
        else:
            await interaction.followup.send("❌ コイン付与中にエラーが発生しました。")
        
    @app_commands.command(name="コイン削減", description="[管理者専用] 特定のユーザーのコインを削減します。")
    @app_commands.checks.has_permissions(administrator=True)
    async def take_coin_command(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, None]):
        await interaction.response.defer(ephemeral=True)
        result = await update_wallet(user, -amount)
        if result:
            await self.log_admin_action(interaction.user, user, -amount, "削減")
            await interaction.followup.send(f"✅ {user.mention}さんの残高から `{amount:,}`{self.currency_icon}を削減しました。")
        else:
            await interaction.followup.send("❌ コイン削減中にエラーが発生しました。")

async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCore(bot))
