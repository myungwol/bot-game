# bot-game/cogs/economy/core.py

import discord
from discord.ext import commands, tasks
import random
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta, time as dt_time
from typing import Dict, Optional, List, Deque, Set
from collections import deque

from utils.database import (
    get_wallet, update_wallet, get_id, supabase, get_embed_from_db, get_config,
    save_config_to_db, get_all_user_stats, log_activity, get_cooldown, set_cooldown
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
JST_MONTHLY_RESET = dt_time(hour=0, minute=2, tzinfo=JST)
JST_MIDNIGHT_AGGREGATE = dt_time(hour=0, minute=5, tzinfo=JST)

class EconomyCore(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.currency_icon = "🪙"
        self._coin_reward_cooldown = commands.CooldownMapping.from_cooldown(1, 3.0, commands.BucketType.user)
        
        # [✅✅✅ 핵심 수정 ✅✅✅]
        # 퇴장 시 계산하는 방식 대신, 현재 활성 유저를 실시간으로 추적하는 Set을 사용합니다.
        self.active_voice_users: Set[int] = set()
        
        self.chat_cache: Deque[Dict] = deque()
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
        # [✅✅✅ 핵심 수정 ✅✅✅] 1분마다 음성 활동을 기록하는 새로운 루프를 시작합니다.
        self.voice_activity_tracker.start()
        self.update_market_prices.start()
        self.monthly_whale_reset.start()

        logger.info("EconomyCore Cog가 성공적으로 초기화되었습니다.")
        
    async def cog_load(self):
        await self.load_configs()
        if not self.log_sender_task or self.log_sender_task.done():
            self.log_sender_task = self.bot.loop.create_task(self.coin_log_sender())
        
    async def load_configs(self):
        game_config = get_config("GAME_CONFIG", {})
        self.currency_icon = game_config.get("CURRENCY_ICON", "🪙")
        self.voice_time_requirement_minutes = game_config.get("VOICE_TIME_REQUIREMENT_MINUTES", 10)
        self.voice_reward_range = game_config.get("VOICE_REWARD_RANGE", [10, 15])
        self.chat_message_requirement = game_config.get("CHAT_MESSAGE_REQUIREMENT", 20)
        self.chat_reward_range = game_config.get("CHAT_REWARD_RANGE", [10, 15])
        self.xp_from_chat = game_config.get("XP_FROM_CHAT", 5)
        self.xp_from_voice = game_config.get("XP_FROM_VOICE", 10)
        
    def cog_unload(self):
        self.activity_log_loop.cancel()
        # [✅✅✅ 핵심 수정 ✅✅✅] Cog가 언로드될 때 새로운 루프도 함께 중지합니다.
        self.voice_activity_tracker.cancel()
        self.update_market_prices.cancel()
        self.monthly_whale_reset.cancel()
        if self.log_sender_task:
            self.log_sender_task.cancel()
    
    async def coin_log_sender(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                async with self.log_sender_lock:
                    if self.coin_log_queue:
                        embed_to_send = self.coin_log_queue.popleft()
                        log_channel_id = get_id("coin_log_channel_id")
                        if log_channel_id and (log_channel := self.bot.get_channel(log_channel_id)):
                            await log_channel.send(embed=embed_to_send)
            except Exception as e:
                logger.error(f"코인 지급 로그 발송 중 오류: {e}", exc_info=True)
            await asyncio.sleep(2)
    
    @tasks.loop(minutes=1)
    async def activity_log_loop(self):
        await self.bot.wait_until_ready()
        async with self._cache_lock:
            if not self.chat_cache: return
            logs_to_process = list(self.chat_cache)
            self.chat_cache.clear()

        try:
            await supabase.table('user_activities').insert(logs_to_process).execute()
            
            user_chat_counts = {}
            for log in logs_to_process:
                user_id = log['user_id']
                user_chat_counts[user_id] = user_chat_counts.get(user_id, 0) + log['amount']

            for user_id, count in user_chat_counts.items():
                user = self.bot.get_user(user_id)
                if not user: continue
                
                xp_to_add = self.xp_from_chat * count
                if xp_to_add > 0:
                    xp_res = await supabase.rpc('add_xp', {'p_user_id': user_id, 'p_xp_to_add': xp_to_add, 'p_source': 'chat'}).execute()
                    if xp_res.data: await self.handle_level_up_event(user, xp_res.data)

                stats = await get_all_user_stats(user_id)
                daily_stats = stats.get('daily', {})
                if daily_stats.get('chat_count', 0) >= self.chat_message_requirement:
                    reward_res = await supabase.table('user_activities').select('id', count='exact').eq('user_id', user_id).eq('activity_type', 'reward_chat').gte('created_at', datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()).execute()
                    if reward_res.count == 0:
                        reward = random.randint(*self.chat_reward_range)
                        await update_wallet(user, reward)
                        await supabase.table('user_activities').insert({'user_id': user_id, 'activity_type': 'reward_chat', 'coin_earned': reward}).execute()
                        await self.log_coin_activity(user, reward, f"チャット{self.chat_message_requirement}回達成")

        except Exception as e:
            logger.error(f"활동 로그 루프 중 DB 오류: {e}", exc_info=True)
            async with self._cache_lock: self.chat_cache.extend(logs_to_process)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or not message.content or message.content.startswith('/'): return
        bucket = self._coin_reward_cooldown.get_bucket(message)
        if not bucket.update_rate_limit():
            xp_to_add = self.xp_from_chat
            async with self._cache_lock:
                self.chat_cache.append({'user_id': message.author.id, 'activity_type': 'chat', 'amount': 1, 'xp_earned': xp_to_add})

    # [✅✅✅ 핵심 수정 ✅✅✅]
    # 이 함수의 역할은 이제 '현재 누가 활성 상태인지'만 추적합니다.
    # 실제 시간 기록과 보상 지급은 1분 루프가 담당합니다.
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot or before.channel == after.channel:
            return

        afk_channel_id = member.guild.afk_channel.id if member.guild.afk_channel else None

        def is_active(state: discord.VoiceState):
            if not state.channel or state.channel.id == afk_channel_id: return False
            return not state.self_deaf and not state.self_mute

        was_active = is_active(before)
        is_now_active = is_active(after)

        if not was_active and is_now_active:
            self.active_voice_users.add(member.id)
            logger.info(f"[VOICE] {member.display_name} is now ACTIVE. Added to tracking set.")
        
        elif was_active and not is_now_active:
            self.active_voice_users.discard(member.id)
            logger.info(f"[VOICE] {member.display_name} is now INACTIVE. Removed from tracking set.")

    # [✅✅✅ 핵심 기능: 신규 추가 ✅✅✅]
    # 1분마다 실행되며, 현재 활성 상태인 유저들에게 1분의 활동 시간을 기록하고 보상을 확인합니다.
    @tasks.loop(minutes=1)
    async def voice_activity_tracker(self):
        if not self.active_voice_users:
            return

        active_users_copy = self.active_voice_users.copy()
        logger.info(f"[VOICE TRACKER] Running for {len(active_users_copy)} active users.")

        try:
            # 1. 활동 기록 및 경험치 지급 (일괄 처리)
            xp_per_minute = self.xp_from_voice
            logs_to_insert = [
                {'user_id': user_id, 'activity_type': 'voice', 'amount': 1, 'xp_earned': xp_per_minute}
                for user_id in active_users_copy
            ]
            
            if logs_to_insert:
                await supabase.table('user_activities').insert(logs_to_insert).execute()
                logger.info(f"[VOICE TRACKER] Logged 1 minute of activity for {len(logs_to_insert)} users.")

                xp_update_tasks = [
                    supabase.rpc('add_xp', {'p_user_id': user_id, 'p_xp_to_add': xp_per_minute, 'p_source': 'voice'})
                    for user_id in active_users_copy
                ]
                xp_results = await asyncio.gather(*xp_update_tasks, return_exceptions=True)
                
                for i, result in enumerate(xp_results):
                    if not isinstance(result, Exception) and result.data:
                        user = self.bot.get_user(list(active_users_copy)[i])
                        if user:
                            await self.handle_level_up_event(user, result.data)

            # 2. 10분 단위 코인 보상 확인 (개별 처리)
            for user_id in active_users_copy:
                user = self.bot.get_user(user_id)
                if not user:
                    continue
                
                stats = await get_all_user_stats(user_id)
                total_voice_minutes_today = stats.get('daily', {}).get('voice_minutes', 0)
                
                # 10분 단위가 되었고, 0분이 아닐 때 보상 확인
                if total_voice_minutes_today > 0 and total_voice_minutes_today % self.voice_time_requirement_minutes == 0:
                    today_str = datetime.now(JST).strftime('%Y-%m-%d')
                    # 동일한 시간대(예: 10분, 20분)에 중복 보상을 받지 않도록 확인
                    cooldown_key = f"voice_reward_{today_str}_{total_voice_minutes_today}m"
                    last_claimed = await get_cooldown(user_id, cooldown_key)

                    if last_claimed == 0:
                        logger.info(f"[VOICE TRACKER] User {user.display_name} reached {total_voice_minutes_today} minutes. Granting reward.")
                        reward = random.randint(*self.voice_reward_range)
                        await update_wallet(user, reward)
                        # 코인 보상에 대한 별도 활동 기록
                        await log_activity(user_id, 'reward_voice', coin_earned=reward)
                        await self.log_coin_activity(user, reward, f"ボイスチャンネルで{total_voice_minutes_today}分間活動")
                        # 중복 지급 방지를 위해 쿨다운 설정
                        await set_cooldown(user_id, cooldown_key)

        except Exception as e:
            logger.error(f"[VOICE TRACKER] Error in voice activity tracking loop: {e}", exc_info=True)

    @voice_activity_tracker.before_loop
    async def before_voice_activity_tracker(self):
        await self.bot.wait_until_ready()
        
    async def handle_level_up_event(self, user: discord.User, result_data: List[Dict]):
        if not result_data or not result_data[0].get('leveled_up'): return
        new_level = result_data[0].get('new_level')
        logger.info(f"유저 {user.display_name}(ID: {user.id})가 레벨 {new_level}(으)로 레벨업했습니다.")
        game_config = get_config("GAME_CONFIG", {})
        job_advancement_levels = game_config.get("JOB_ADVANCEMENT_LEVELS", [50, 100])
        if new_level in job_advancement_levels:
            await save_config_to_db(f"job_advancement_request_{user.id}", {"level": new_level, "timestamp": time.time()})
        await save_config_to_db(f"level_tier_update_request_{user.id}", {"level": new_level, "timestamp": time.time()})

    async def log_coin_activity(self, user: discord.Member, amount: int, reason: str):
        embed_data = await get_embed_from_db("log_coin_gain")
        if not embed_data: return
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
