# bot-game/cogs/economy/core.py

import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
import random
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta, time as dt_time
from typing import Dict, Optional, List

from utils.database import (
    get_wallet, update_wallet, get_id, supabase, get_embed_from_db, get_config,
    save_config_to_db, 
    # [✅ 수정] 새로운 활동 기록 함수들을 가져옵니다.
    log_user_activity, batch_log_chat_activity
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
# [❌ 삭제] 일일/주간 리셋 로직은 더 이상 필요 없습니다.
# JST_MIDNIGHT_RESET = dt_time(hour=0, minute=1, tzinfo=JST) 
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

        # [❌ 삭제] 활동 보상 루프는 삭제합니다. 활동은 실시간으로 기록됩니다.
        # self.reward_payout_loop.start()
        self.update_chat_progress_loop.start()
        # [❌ 삭제] 일일 리셋 루프는 삭제합니다.
        # self.daily_reset_loop.start()
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
        # [❌ 삭제] 루프 2개 삭제
        # self.reward_payout_loop.cancel()
        self.update_chat_progress_loop.cancel()
        # self.daily_reset_loop.cancel()
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

    # [❌ 삭제] daily_reset_loop는 더 이상 필요 없습니다.
    # @tasks.loop(time=JST_MIDNIGHT_RESET)
    # async def daily_reset_loop(self): ...

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

        # [✅ 수정] 새로운 일괄 기록 함수를 사용합니다.
        try:
            # DB에 보낼 데이터 형식에 맞게 변환
            chat_logs_to_insert: List[Dict] = [
                {'user_id': uid, 'activity_type': 'chat', 'amount': count}
                for uid, count in data_to_update.items()
            ]
            await batch_log_chat_activity(chat_logs_to_insert)
        except Exception as e:
            logger.error(f"채팅 활동 일괄 업데이트 중 DB 오류: {e}", exc_info=True)
            # 실패 시 데이터를 캐시에 다시 추가하여 유실 방지
            async with self._cache_lock:
                for log in chat_logs_to_insert:
                    uid, count = log['user_id'], log['amount']
                    self.chat_progress_cache[uid] = self.chat_progress_cache.get(uid, 0) + count

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot: return
        # AFK 채널 확인 로직 개선
        afk_channel_id = member.guild.afk_channel.id if member.guild.afk_channel else None
        
        is_valid = lambda state: state.channel and state.channel.id != afk_channel_id
        is_active = lambda state: not state.self_deaf and not state.self_mute

        was_active = is_valid(before) and is_active(before)
        is_now_active = is_valid(after) and is_active(after)

        if not was_active and is_now_active:
            # 활동 시작
            self.voice_sessions[member.id] = datetime.now(timezone.utc)
        elif was_active and not is_now_active:
            # 활동 종료
            if join_time := self.voice_sessions.pop(member.id, None):
                duration_minutes = (datetime.now(timezone.utc) - join_time).total_seconds() / 60.0
                if duration_minutes >= 1: # 최소 1분 이상 참여했을 때만 기록
                    # [✅ 수정] 활동 로그 기록
                    await log_user_activity(member.id, 'voice', round(duration_minutes))

    async def log_coin_activity(self, user: discord.Member, amount: int, reason: str):
        if not self.coin_log_channel_id or not (log_channel := self.bot.get_channel(self.coin_log_channel_id)): return
        if not (embed_data := await get_embed_from_db("log_coin_gain")): return
        embed = format_embed_from_db(embed_data, user_mention=user.mention, amount=f"{amount:,}", currency_icon=self.currency_icon, reason=reason)
        if user.display_avatar: embed.set_thumbnail(url=user.display_avatar.url)
        try: await log_channel.send(embed=embed)
        except Exception as e: logger.error(f"코인 활동 로그 전송 실패: {e}", exc_info=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCore(bot))
