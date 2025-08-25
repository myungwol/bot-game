# bot-game/cogs/economy/core.py

import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
import random
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta, time as dt_time
from typing import Dict, Optional, List, Deque
from collections import deque

from utils.database import (
    get_wallet, update_wallet, get_id, supabase, get_embed_from_db, get_config,
    save_config_to_db,
    log_user_activity, batch_log_chat_activity
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
JST_MIDNIGHT_AGGREGATE = dt_time(hour=0, minute=5, tzinfo=JST)
JST_MONTHLY_RESET = dt_time(hour=0, minute=2, tzinfo=JST)

class EconomyCore(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.coin_log_channel_id: Optional[int] = None
        self.currency_icon = "🪙"
        self._coin_reward_cooldown = commands.CooldownMapping.from_cooldown(1, 3.0, commands.BucketType.user)
        
        # [✅✅✅ 핵심 수정] 퇴장 시 정산을 위해 '언제 들어왔는지'만 기록하는 방식으로 변경
        self.voice_sessions: Dict[int, datetime] = {}
        
        self.all_chat_cache: Dict[int, int] = {}
        self.coin_chat_cache: Dict[int, int] = {}
        self._cache_lock = asyncio.Lock()

        self.voice_time_requirement_minutes = 10
        self.voice_reward_range = [10, 15]
        self.chat_message_requirement = 20
        self.chat_reward_range = [10, 15]
        self.xp_from_chat = 5
        self.xp_from_voice = 10

        self.coin_log_queue: Deque[discord.Embed] = deque()
        self.log_sender_task: Optional[asyncio.Task] = None
        self.log_sender_lock = asyncio.Lock()

        self.activity_log_loop.start()
        self.coin_reward_check_loop.start()
        self.update_market_prices.start()
        self.monthly_whale_reset.start()
        self.daily_maintenance_loop.start()
        
        logger.info("EconomyCore Cog가 성공적으로 초기화되었습니다.")
        
    async def cog_load(self):
        await self.load_configs()
        if not self.log_sender_task or self.log_sender_task.done():
            self.log_sender_task = self.bot.loop.create_task(self.coin_log_sender())
        
    async def load_configs(self):
        self.coin_log_channel_id = get_id("coin_log_channel_id")
        game_config = get_config("GAME_CONFIG", {})
        self.currency_icon = game_config.get("CURRENCY_ICON", "🪙")
        self.voice_time_requirement_minutes = game_config.get("VOICE_TIME_REQUIREMENT_MINUTES", 10)
        self.voice_reward_range = game_config.get("VOICE_REWARD_RANGE", [10, 15])
        self.chat_message_requirement = game_config.get("CHAT_MESSAGE_REQUIREMENT", 20)
        self.chat_reward_range = game_config.get("CHAT_REWARD_RANGE", [10, 15])
        self.xp_from_chat = game_config.get("XP_FROM_CHAT", 5)
        self.xp_from_voice = game_config.get("XP_FROM_VOICE", 10)
        logger.info("[EconomyCore Cog] 데이터베이스로부터 설정을 성공적으로 로드했습니다.")
        
    def cog_unload(self):
        self.activity_log_loop.cancel()
        self.coin_reward_check_loop.cancel()
        self.update_market_prices.cancel()
        self.monthly_whale_reset.cancel()
        self.daily_maintenance_loop.cancel()
        if self.log_sender_task:
            self.log_sender_task.cancel()
    
    async def coin_log_sender(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                async with self.log_sender_lock:
                    if self.coin_log_queue:
                        embed_to_send = self.coin_log_queue.popleft()
                        if self.coin_log_channel_id and (log_channel := self.bot.get_channel(self.coin_log_channel_id)):
                            await log_channel.send(embed=embed_to_send)
            except Exception as e:
                logger.error(f"코인 지급 로그 발송 중 오류: {e}", exc_info=True)
            await asyncio.sleep(5)
    
    @tasks.loop(time=JST_MIDNIGHT_AGGREGATE)
    async def daily_maintenance_loop(self):
        logger.info("[일일 유지보수] 어제 활동 기록 요약 및 오래된 로그 정리를 시작합니다.")
        try:
            yesterday_str = (datetime.now(JST) - timedelta(days=1)).strftime('%Y-%m-%d')
            await supabase.rpc('update_daily_activities', {'target_date': yesterday_str}).execute()
            logger.info(f"[일일 유지보수] {yesterday_str} 날짜의 활동 기록 요약을 성공적으로 완료했습니다.")
            seven_days_ago_utc = datetime.now(timezone.utc) - timedelta(days=7)
            await supabase.table('user_activity_logs').delete().lt('created_at', seven_days_ago_utc.isoformat()).execute()
            logger.info(f"[일일 유지보수] 일주일 이상 된 임시 활동 로그를 성공적으로 삭제했습니다.")
        except Exception as e:
            logger.error(f"[일일 유지보수] 작업 중 오류 발생: {e}", exc_info=True)

    @daily_maintenance_loop.before_loop
    async def before_daily_maintenance_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1)
    async def activity_log_loop(self):
        await self.bot.wait_until_ready()
        async with self._cache_lock:
            if not self.all_chat_cache: return
            data_to_update = self.all_chat_cache.copy()
            self.all_chat_cache.clear()
        try:
            chat_logs_to_insert: List[Dict] = [{'user_id': uid, 'activity_type': 'chat', 'amount': count} for uid, count in data_to_update.items()]
            await batch_log_chat_activity(chat_logs_to_insert)
        except Exception as e:
            logger.error(f"전체 채팅 로그 저장 중 DB 오류: {e}", exc_info=True)
            async with self._cache_lock:
                for log in chat_logs_to_insert:
                    uid, count = log['user_id'], log['amount']
                    self.all_chat_cache[uid] = self.all_chat_cache.get(uid, 0) + count

    @tasks.loop(minutes=1)
    async def coin_reward_check_loop(self):
        await self.bot.wait_until_ready()
        async with self._cache_lock:
            if not self.coin_chat_cache: return
            data_to_check = self.coin_chat_cache.copy()
            self.coin_chat_cache.clear()
        today_start_utc = (datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=9)).isoformat()
        for user_id, count in data_to_check.items():
            try:
                user = self.bot.get_user(user_id)
                if not user: continue
                if count > 0:
                    xp_to_add = self.xp_from_chat * count
                    xp_res = await supabase.rpc('add_xp', {'p_user_id': user_id, 'p_xp_to_add': xp_to_add, 'p_source': 'chat'}).execute()
                    if xp_res and xp_res.data: await self.handle_level_up_event(user, xp_res.data[0])
                reward_res = await supabase.table('user_activity_logs').select('id', count='exact').eq('user_id', user_id).eq('activity_type', 'coin_reward_chat').gte('created_at', today_start_utc).execute()
                if reward_res.count > 0: continue
                upsert_res = await supabase.rpc('upsert_and_increment_activity_log', {'p_user_id': user_id, 'p_activity_type': 'valid_chat_for_coin', 'p_amount': count, 'p_record_date_str': datetime.now(JST).strftime('%Y-%m-%d')}).execute()
                total_valid_chats = upsert_res.data if upsert_res.data else 0
                if total_valid_chats >= self.chat_message_requirement:
                    reward = random.randint(*self.chat_reward_range)
                    await update_wallet(user, reward)
                    await log_user_activity(user_id, 'coin_reward_chat', reward)
                    await self.log_coin_activity(user, reward, f"チャット{self.chat_message_requirement}回達成")
            except Exception as e:
                 logger.error(f"코인/경험치 보상 확인 중 오류 (유저: {user_id}): {e}", exc_info=True)
                 async with self._cache_lock: self.coin_chat_cache[user_id] = self.coin_chat_cache.get(user_id, 0) + count

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or not message.content or message.content.startswith('/'): return
        async with self._cache_lock: self.all_chat_cache[message.author.id] = self.all_chat_cache.get(message.author.id, 0) + 1
        coin_bucket = self._coin_reward_cooldown.get_bucket(message)
        if not coin_bucket.update_rate_limit():
            async with self._cache_lock: self.coin_chat_cache[message.author.id] = self.coin_chat_cache.get(message.author.id, 0) + 1

    # [✅✅✅ 핵심 수정] '퇴장 시 누적 및 정산' 방식으로 로직 전체 변경
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot or before.channel == after.channel:
            return

        afk_channel_id = member.guild.afk_channel.id if member.guild.afk_channel else None

        def is_active(state: discord.VoiceState):
            return state.channel is not None and state.channel.id != afk_channel_id and not state.self_deaf and not state.self_mute

        was_active = is_active(before)
        is_now_active = is_active(after)

        if not was_active and is_now_active:
            self.voice_sessions[member.id] = datetime.now(timezone.utc)
        
        elif was_active and not is_now_active:
            if join_time := self.voice_sessions.pop(member.id, None):
                duration_minutes = (datetime.now(timezone.utc) - join_time).total_seconds() / 60.0
                
                if duration_minutes >= 1:
                    rounded_minutes = round(duration_minutes)
                    try:
                        # 1. 활동 시간을 DB에 누적시키고, 오늘의 총 누적 시간을 반환받음
                        upsert_res = await supabase.rpc('upsert_and_increment_activity_log', {
                            'p_user_id': member.id,
                            'p_activity_type': 'voice_minutes',
                            'p_amount': rounded_minutes,
                            'p_record_date_str': datetime.now(JST).strftime('%Y-%m-%d')
                        }).execute()
                        total_voice_minutes_today = upsert_res.data if upsert_res.data else 0

                        # 2. 경험치 지급 (활동한 시간만큼)
                        xp_to_add = self.xp_from_voice * rounded_minutes
                        if xp_to_add > 0:
                            xp_res = await supabase.rpc('add_xp', {'p_user_id': member.id, 'p_xp_to_add': xp_to_add, 'p_source': 'voice'}).execute()
                            if xp_res and xp_res.data:
                                await self.handle_level_up_event(member, xp_res.data[0])

                        # 3. 코인 보상 확인 및 지급
                        today_start_utc = (datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=9)).isoformat()
                        reward_res = await supabase.table('user_activity_logs').select('id', count='exact').eq('user_id', member.id).eq('activity_type', 'coin_reward_voice').gte('created_at', today_start_utc).execute()
                        
                        rewards_given_today = reward_res.count

                        # 보상받지 않은 누적 시간이 보상 기준을 넘었는지 확인
                        unrewarded_minutes = total_voice_minutes_today - (rewards_given_today * self.voice_time_requirement_minutes)

                        if unrewarded_minutes >= self.voice_time_requirement_minutes:
                            reward = random.randint(*self.voice_reward_range)
                            await update_wallet(member, reward)
                            await log_user_activity(member.id, 'coin_reward_voice', reward) # 보상 지급 기록
                            await self.log_coin_activity(member, reward, f"ボイスチャンネルで{self.voice_time_requirement_minutes}分間活動")
                    
                    except Exception as e:
                        logger.error(f"음성 채널 활동 보상(누적 방식) 처리 중 오류 (유저: {member.id}): {e}", exc_info=True)


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

    async def log_coin_activity(self, user: discord.Member, amount: int, reason: str):
        if not (embed_data := await get_embed_from_db("log_coin_gain")): return
        embed = format_embed_from_db(embed_data, user_mention=user.mention, amount=f"{amount:,}", currency_icon=self.currency_icon, reason=reason)
        if user.display_avatar: embed.set_thumbnail(url=user.display_avatar.url)
        async with self.log_sender_lock: self.coin_log_queue.append(embed)

    @tasks.loop(time=JST_MONTHLY_RESET)
    async def monthly_whale_reset(self):
        now = datetime.now(JST)
        if now.day != 1: return
        logger.info("[월간 리셋] 고래 출현 공지 및 패널 재설치를 시작합니다.")
        try:
            sea_fishing_channel_id = get_id("sea_fishing_panel_channel_id")
            if not (sea_fishing_channel_id and (channel := self.bot.get_channel(sea_fishing_channel_id))): return
            fishing_cog = self.bot.get_cog("Fishing")
            if not fishing_cog: return
            if old_msg_id := get_config("whale_announcement_message_id"):
                try: await (await channel.fetch_message(int(old_msg_id))).delete()
                except (discord.NotFound, discord.Forbidden): pass
            if embed_data := await get_embed_from_db("embed_whale_reset_announcement"):
                announcement_embed = discord.Embed.from_dict(embed_data)
                announcement_msg = await channel.send(embed=announcement_embed)
                await save_config_to_db("whale_announcement_message_id", announcement_msg.id)
                await fishing_cog.regenerate_panel(channel, panel_key="panel_fishing_sea")
        except Exception as e:
            logger.error(f"[월간 리셋] 고래 공지 처리 중 오류 발생: {e}", exc_info=True)

    @monthly_whale_reset.before_loop
    async def before_monthly_whale_reset(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=JST_MIDNIGHT_AGGREGATE)
    async def update_market_prices(self):
        logger.info("[시장] 일일 아이템 및 물고기 가격 변동을 시작합니다.")
        try:
            item_res_task = supabase.table('items').select('*').gt('volatility', 0).execute()
            fish_res_task = supabase.table('fishing_loots').select('*').gt('volatility', 0).execute()
            item_res, fish_res = await asyncio.gather(item_res_task, fish_res_task)
            all_updates = []; announcements = []; fluctuation_data = []
            if item_res.data:
                item_updates = []
                for item in item_res.data:
                    current_price = item.get('current_price', item.get('price', 0))
                    new_price = self._calculate_new_price(current_price, item.get('volatility', 0), item.get('min_price'), item.get('max_price'))
                    item_updates.append({'id': item['id'], 'current_price': new_price})
                    if abs((new_price - current_price) / (current_price or 1)) > 0.3:
                        status = "暴騰 📈" if new_price > current_price else "暴落 📉"
                        announcement_text = f" - {item.get('name', 'N/A')}: `{current_price}` → `{new_price}`{self.currency_icon} ({status})"
                        announcements.append(announcement_text); fluctuation_data.append(announcement_text)
                if item_updates: all_updates.append(supabase.table('items').upsert(item_updates).execute())
            if fish_res.data:
                fish_updates = []
                for fish in fish_res.data:
                    current_price = fish.get('current_base_value', fish.get('base_value', 0))
                    new_price = self._calculate_new_price(current_price, fish.get('volatility', 0), fish.get('min_base_value'), fish.get('max_base_value'))
                    fish_updates.append({'id': fish['id'], 'current_base_value': new_price})
                    if abs((new_price - current_price) / (current_price or 1)) > 0.3:
                        status = "豊漁 📈" if new_price > current_price else "不漁 📉"
                        announcement_text = f" - {fish.get('name', 'N/A')} (基本価値): `{current_price}` → `{new_price}`{self.currency_icon} ({status})"
                        announcements.append(announcement_text); fluctuation_data.append(announcement_text)
                if fish_updates: all_updates.append(supabase.table('fishing_loots').upsert(fish_updates).execute())
            if all_updates: await asyncio.gather(*all_updates)
            await save_config_to_db("market_fluctuations", fluctuation_data)
            commerce_cog = self.bot.get_cog("Commerce")
            if commerce_cog:
                commerce_channel_id = get_id("commerce_panel_channel_id")
                if commerce_channel_id and (channel := self.bot.get_channel(commerce_channel_id)):
                    await commerce_cog.regenerate_panel(channel)
                    logger.info("상점 패널(panel_commerce)에 가격 변동 정보 업데이트를 요청했습니다.")
            if announcements and (log_channel_id := get_id("market_log_channel_id")):
                if log_channel := self.bot.get_channel(log_channel_id):
                    embed = discord.Embed(title="📢 今日の主な相場変動情報", description="\n".join(announcements), color=0xFEE75C)
                    await log_channel.send(embed=embed)
        except Exception as e:
            logger.error(f"[시장] 아이템 가격 업데이트 중 오류: {e}", exc_info=True)
    
    def _calculate_new_price(self, current, volatility, min_p, max_p):
        base_price = current
        change_percent = random.uniform(-volatility, volatility)
        new_price = int(base_price * (1 + change_percent))
        if min_p is not None: new_price = max(min_p, new_price)
        if max_p is not None: new_price = min(max_p, new_price)
        return new_price

    @update_market_prices.before_loop
    async def before_update_market_prices(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Cog):
    await bot.add_cog(EconomyCore(bot))
