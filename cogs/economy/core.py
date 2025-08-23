# bot-game/cogs/economy/core.py

import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
import random
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta, time as dt_time
from typing import Dict, Optional

from utils.database import (
    get_wallet, update_wallet,
    get_id, supabase, get_embed_from_db, get_config,
    save_config_to_db
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

PROGRESS_TABLE = "user_progress"

JST = timezone(timedelta(hours=9))
JST_MIDNIGHT_RESET = dt_time(hour=0, minute=1, tzinfo=JST)
JST_MONTHLY_RESET = dt_time(hour=0, minute=2, tzinfo=JST)

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
        self.update_market_prices.start()
        self.monthly_whale_reset.start()

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
        self.update_market_prices.cancel()
        self.monthly_whale_reset.cancel()
    
    async def handle_level_up_event(self, user: discord.User, result_data: Dict):
        if not result_data or not result_data.get('leveled_up'):
            return

        new_level = result_data.get('new_level')
        logger.info(f"유저 {user.display_name}(ID: {user.id})가 레벨 {new_level}(으)로 레벨업했습니다.")
        
        game_config = get_config("GAME_CONFIG", {})
        job_advancement_levels = game_config.get("JOB_ADVANCEMENT_LEVELS", [50, 100])
        
        if new_level in job_advancement_levels:
            await save_config_to_db(f"job_advancement_request_{user.id}", {"level": new_level, "timestamp": time.time()})
            logger.info(f"유저가 전직 가능 레벨({new_level})에 도달하여 DB에 요청을 기록했습니다.")

        await save_config_to_db(f"level_tier_update_request_{user.id}", {"level": new_level, "timestamp": time.time()})

    @tasks.loop(time=JST_MIDNIGHT_RESET)
    async def daily_reset_loop(self):
        logger.info("[일일 초기화] 모든 유저의 일일 퀘스트 진행도 초기화를 시작합니다.")
        try:
            await supabase.rpc('reset_daily_progress_all_users').execute()
            logger.info("[일일 초기화] 성공적으로 완료되었습니다.")
        except Exception as e:
            logger.error(f"[일일 초기화] 진행도 초기화 중 오류 발생: {e}", exc_info=True)
            
    @daily_reset_loop.before_loop
    async def before_daily_reset_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=JST_MIDNIGHT_RESET)
    async def update_market_prices(self):
        logger.info("[시장] 일일 아이템 가격 변동을 시작합니다.")
        try:
            response = await supabase.table('items').select('*').gt('volatility', 0).execute()
            if not response.data:
                logger.info("[시장] 가격 변동 대상 아이템이 없습니다.")
                return

            updates, announcements = [], []
            for item in response.data:
                base_price = item.get('base_price', item.get('price', 0))
                volatility = item.get('volatility', 0.0)
                current_price = item.get('current_price', base_price)
                min_price = item.get('min_price', int(base_price * 0.5))
                max_price = item.get('max_price', int(base_price * 2.0))
                change_percent = random.uniform(-volatility, volatility)
                new_price = max(min_price, min(max_price, int(base_price * (1 + change_percent))))
                updates.append({'id': item['id'], 'current_price': new_price})
                price_diff_ratio = (new_price - current_price) / current_price if current_price > 0 else 0
                if abs(price_diff_ratio) > 0.3:
                    status = "暴騰 📈" if price_diff_ratio > 0 else "暴落 📉"
                    announcements.append(f" - {item['name']}: `{current_price}` -> `{new_price}`{self.currency_icon} ({status})")
            
            await supabase.table('items').upsert(updates).execute()
            logger.info(f"[시장] {len(updates)}개 아이템의 가격을 업데이트했습니다.")
            
            if announcements and (log_channel_id := get_id("market_log_channel_id")):
                if log_channel := self.bot.get_channel(log_channel_id):
                    embed = discord.Embed(title="📢 今日の主な相場変動情報", description="\n".join(announcements), color=0xFEE75C)
                    await log_channel.send(embed=embed)
            
            if (game_db_cog := self.bot.get_cog("Commerce")) and hasattr(game_db_cog, "load_game_data_from_db"):
                 asyncio.create_task(game_db_cog.load_game_data_from_db())
                 logger.info("[시장] 게임 데이터 캐시 갱신을 요청했습니다.")
        except Exception as e:
            logger.error(f"[시장] 아이템 가격 업데이트 중 오류: {e}", exc_info=True)

    @update_market_prices.before_loop
    async def before_update_market_prices(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=JST_MONTHLY_RESET)
    async def monthly_whale_reset(self):
        now = datetime.now(JST)
        if now.day != 1:
            return

        logger.info("[월간 리셋] 고래 출현 공지 및 패널 재설치를 시작합니다.")
        try:
            sea_fishing_channel_id = get_id("sea_fishing_panel_channel_id")
            if not sea_fishing_channel_id:
                logger.warning("[월간 리셋] 바다 낚시터 채널이 설정되지 않아 공지를 보낼 수 없습니다.")
                return
            
            channel = self.bot.get_channel(sea_fishing_channel_id)
            if not isinstance(channel, discord.TextChannel):
                logger.warning(f"[월간 리셋] 채널 ID {sea_fishing_channel_id}를 찾을 수 없거나 텍스트 채널이 아닙니다.")
                return

            fishing_cog = self.bot.get_cog("Fishing")
            if not fishing_cog:
                logger.error("[월간 리셋] Fishing Cog를 찾을 수 없습니다.")
                return

            old_msg_id = get_config("whale_announcement_message_id")
            if old_msg_id:
                try:
                    old_msg = await channel.fetch_message(int(old_msg_id))
                    await old_msg.delete()
                    logger.info(f"[월간 리셋] 이전 고래 공지 메시지(ID: {old_msg_id})를 삭제했습니다.")
                except (discord.NotFound, discord.Forbidden): pass

            embed_data = await get_embed_from_db("embed_whale_reset_announcement")
            if not embed_data:
                logger.error("[월간 리셋] 고래 리셋 공지 임베드('embed_whale_reset_announcement')를 DB에서 찾을 수 없습니다.")
                return

            announcement_embed = discord.Embed.from_dict(embed_data)
            announcement_msg = await channel.send(embed=announcement_embed)

            await save_config_to_db("whale_announcement_message_id", announcement_msg.id)
            logger.info(f"[월간 리셋] 새로운 고래 공지 메시지(ID: {announcement_msg.id})를 전송하고 DB에 저장했습니다.")

            await fishing_cog.regenerate_panel(channel, panel_key="panel_fishing_sea")

        except Exception as e:
            logger.error(f"[월간 리셋] 고래 공지 처리 중 심각한 오류 발생: {e}", exc_info=True)

    @monthly_whale_reset.before_loop
    async def before_monthly_whale_reset(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or message.content.startswith('/'): return
        bucket = self._chat_cooldown.get_bucket(message)
        if bucket.update_rate_limit(): return
        async with self._cache_lock:
            self.chat_progress_cache[message.author.id] = self.chat_progress_cache.get(message.author.id, 0) + 1

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
            voice_req, voice_reward, voice_xp = game_config.get("VOICE_TIME_REQUIREMENT_MINUTES", 10), game_config.get("VOICE_REWARD_RANGE", [10, 15]), game_config.get("XP_FROM_VOICE", 10)
            await self.process_rewards('voice', voice_req, voice_reward, voice_xp, "ボイスチャット活動報酬")
            chat_req, chat_reward, chat_xp = game_config.get("CHAT_MESSAGE_REQUIREMENT", 20), game_config.get("CHAT_REWARD_RANGE", [5, 10]), game_config.get("XP_FROM_CHAT", 5)
            await self.process_rewards('chat', chat_req, chat_reward, chat_xp, "チャット活動報酬")
        except Exception as e:
            logger.error(f"활동 보상 지급 루프 중 오류: {e}", exc_info=True)

    async def process_rewards(self, reward_type: str, requirement: int, reward_range: list[int], xp_reward: int, reason: str):
        table, column = PROGRESS_TABLE, 'daily_voice_minutes' if reward_type == 'voice' else 'chat_progress'
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

async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCore(bot))
