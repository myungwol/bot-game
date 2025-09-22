# cogs/economy/core.py

import discord
from discord.ext import commands, tasks
import random
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta, time as dt_time
from typing import Dict, Optional, List, Deque, Set
from collections import deque, defaultdict

from utils.database import (
    get_wallet, update_wallet, get_id, supabase, get_embed_from_db, get_config,
    save_config_to_db, get_all_user_stats, log_activity, get_cooldown, set_cooldown,
    get_user_gear, load_all_data_from_db, ensure_user_gear_exists,
    load_bot_configs_from_db, delete_config_from_db, get_item_database, get_fishing_loot
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
KST_MONTHLY_RESET = dt_time(hour=0, minute=2, tzinfo=KST)
KST_MIDNIGHT_AGGREGATE = dt_time(hour=0, minute=5, tzinfo=KST)

class EconomyCore(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.currency_icon = "🪙"
        self._coin_reward_cooldown = commands.CooldownMapping.from_cooldown(1, 3.0, commands.BucketType.user)
        self.users_in_vc_last_minute: Set[int] = set()
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
        self.voice_activity_tracker.start()
        self.update_market_prices.start()
        self.monthly_whale_reset.start()
        self.unified_request_dispatcher.start()
        self.initial_setup_done = False
        logger.info("EconomyCore Cog가 성공적으로 초기화되었습니다.")

    @commands.Cog.listener()
    async def on_ready(self):
        if self.initial_setup_done:
            return
        logger.info("EconomyCore: 봇이 준비되었습니다. 데이터베이스 초기화를 시작합니다.")
        await load_all_data_from_db()
        logger.info("EconomyCore: 데이터베이스 설정 로딩 완료.")
        await self._ensure_all_members_have_gear()
        self.initial_setup_done = True

    async def cog_load(self):
        await self.load_configs()
        if not self.log_sender_task or self.log_sender_task.done():
            self.log_sender_task = self.bot.loop.create_task(self.coin_log_sender())

    async def _ensure_all_members_have_gear(self):
        logger.info("[초기화] 서버 멤버 장비 정보 확인 및 생성을 시작합니다.")
        server_id_str = get_config("SERVER_ID")
        if not server_id_str:
            logger.error("[초기화] DB에 'SERVER_ID'가 설정되지 않아 멤버 확인을 건너뜁니다.")
            return
        try:
            guild = self.bot.get_guild(int(server_id_str))
            if not guild:
                logger.error(f"[초기화] 설정된 SERVER_ID({server_id_str})에 해당하는 서버를 찾을 수 없습니다.")
                return
        except ValueError:
            logger.error(f"[초기화] DB의 SERVER_ID ('{server_id_str}')가 올바른 숫자가 아닙니다.")
            return
        logger.info(f"[초기화] 대상 서버: {guild.name} (ID: {guild.id})")
        tasks = [ensure_user_gear_exists(member.id) for member in guild.members if not member.bot]
        if tasks:
            logger.info(f"[초기화] 총 {len(tasks)}명의 멤버 정보를 확인 및 생성합니다.")
            await asyncio.gather(*tasks)
        logger.info("[초기화] 모든 멤버의 장비 정보 확인 작업이 완료되었습니다.")

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
        self.voice_activity_tracker.cancel()
        self.update_market_prices.cancel()
        self.monthly_whale_reset.cancel()
        if self.log_sender_task: self.log_sender_task.cancel()
        self.unified_request_dispatcher.cancel()

    @tasks.loop(seconds=10.0)
    async def unified_request_dispatcher(self):
        try:
            response = await supabase.table('bot_configs').select('config_key, config_value').like('config_key', '%_request%').execute()
            
            if not (response and response.data):
                return

            requests = response.data
            keys_to_delete = [req['config_key'] for req in requests]

            requests_by_prefix = defaultdict(list)
            for req in requests:
                # --- [로깅 추가 1] ---
                logger.info(f"[Dispatcher-Debug] 감지된 요청 키: {req['config_key']}")
                prefix_parts = req['config_key'].split('_request')
                if len(prefix_parts) > 1:
                    prefix = prefix_parts[0]
                    requests_by_prefix[prefix].append(req)
                    # --- [로깅 추가 2] ---
                    logger.info(f"[Dispatcher-Debug] '{req['config_key']}' -> 접두사 '{prefix}'로 분류됨.")

            if 'level_tier_update' in requests_by_prefix or 'job_advancement' in requests_by_prefix:
                if level_cog := self.bot.get_cog("LevelSystem"):
                    await level_cog.process_level_requests(requests_by_prefix)
            
            if 'farm_ui_update' in requests_by_prefix:
                if farm_cog := self.bot.get_cog("Farm"):
                    user_ids = {int(req['config_key'].split('_')[-1]) for req in requests_by_prefix['farm_ui_update']}
                    await farm_cog.process_ui_update_requests(user_ids)

            if 'kitchen_ui_update' in requests_by_prefix:
                if cooking_cog := self.bot.get_cog("Cooking"):
                    user_ids = {int(req['config_key'].split('_')[-1]) for req in requests_by_prefix['kitchen_ui_update']}
                    await cooking_cog.process_ui_update_requests(user_ids)
            
            if 'panel_regenerate' in requests_by_prefix:
                if panel_cog := self.bot.get_cog("PanelUpdater"):
                    await panel_cog.process_panel_regenerate_requests(requests_by_prefix['panel_regenerate'])

            if 'config_reload' in requests_by_prefix:
                logger.info("[CONFIG] 설정 새로고침 요청 감지...")
                await load_bot_configs_from_db()
                for cog in self.bot.cogs.values():
                    if hasattr(cog, 'load_configs'):
                        await cog.load_configs()
                logger.info("[CONFIG] 모든 설정 새로고침 완료.")

            if 'manual_update' in requests_by_prefix:
                logger.info("[수동 업데이트] 요청 감지...")
                if farm_cog := self.bot.get_cog("Farm"):
                    await farm_cog.daily_crop_update()
                await self.update_market_prices()
                logger.info("[수동 업데이트] 모든 수동 업데이트 완료.")
            
            if 'pet_levelup' in requests_by_prefix:
                if pet_cog := self.bot.get_cog("PetSystem"):
                    await pet_cog.process_levelup_requests(requests_by_prefix['pet_levelup'])

            if 'pet_admin_levelup' in requests_by_prefix:
                # --- [로깅 추가 3] ---
                logger.info(f"[Dispatcher-Debug] 'pet_admin_levelup' 접두사 감지! PetSystem으로 전달합니다.")
                if pet_cog := self.bot.get_cog("PetSystem"):
                    admin_requests = requests_by_prefix['pet_admin_levelup']
                    await pet_cog.process_levelup_requests(admin_requests, is_admin=True)

            if 'pet_evolution_check' in requests_by_prefix:
                 if pet_cog := self.bot.get_cog("PetSystem"):
                    user_ids = {int(req['config_key'].split('_')[-1]) for req in requests_by_prefix['pet_evolution_check']}
                    await pet_cog.check_and_process_auto_evolution(user_ids)
            
            # ▼▼▼▼▼ 여기에 아래 코드를 추가하세요 ▼▼▼▼▼
            if 'pet_level_set' in requests_by_prefix:
                if pet_cog := self.bot.get_cog("PetSystem"):
                    await pet_cog.process_level_set_requests(requests_by_prefix['pet_level_set'])
            # ▲▲▲▲▲ 여기까지 추가 ▲▲▲▲▲
            
            server_id_str = get_config("SERVER_ID")
            guild = self.bot.get_guild(int(server_id_str)) if server_id_str else None

            if guild:
                if 'coin_admin_update' in requests_by_prefix:
                    for req in requests_by_prefix['coin_admin_update']:
                        try:
                            user_id = int(req['config_key'].split('_')[-1])
                            user = guild.get_member(user_id)
                            amount = req['config_value'].get('amount')
                            if user and amount is not None:
                                await update_wallet(user, amount)
                                logger.info(f"[AdminBridge] {user.display_name}님에게 코인 {amount}를 처리했습니다.")
                        except Exception as e:
                            logger.error(f"[AdminBridge] 코인 요청 처리 중 오류: {e}", exc_info=True)

                if 'xp_admin_update' in requests_by_prefix:
                    level_cog = self.bot.get_cog("LevelSystem")
                    if level_cog:
                        for req in requests_by_prefix['xp_admin_update']:
                            try:
                                user_id = int(req['config_key'].split('_')[-1])
                                user = guild.get_member(user_id)
                                payload = req['config_value']
                                xp_to_add = payload.get('xp_to_add')
                                exact_level = payload.get('exact_level')
                                if user:
                                    if xp_to_add is not None:
                                        await level_cog.update_user_xp_and_level_from_admin(user, xp_to_add=xp_to_add)
                                    elif exact_level is not None:
                                        await level_cog.update_user_xp_and_level_from_admin(user, exact_level=exact_level)
                                    logger.info(f"[AdminBridge] {user.display_name}님의 XP/레벨을 처리했습니다.")
                            except Exception as e:
                                logger.error(f"[AdminBridge] XP/레벨 요청 처리 중 오류: {e}", exc_info=True)
            
            if keys_to_delete:
                await supabase.table('bot_configs').delete().in_('config_key', keys_to_delete).execute()

        except Exception as e:
            logger.error(f"통합 요청 처리기에서 오류 발생: {e}", exc_info=True)

    @unified_request_dispatcher.before_loop
    async def before_unified_dispatcher(self):
        await self.bot.wait_until_ready()

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
            for log in logs_to_process:
                log['user_id'] = str(log['user_id'])
            await supabase.table('user_activities').insert(logs_to_process).execute()

            user_chat_counts = defaultdict(int)
            for log in logs_to_process:
                user_id = int(log['user_id'])
                user_chat_counts[user_id] += log['amount']

            for user_id, count in user_chat_counts.items():
                user = self.bot.get_user(user_id)
                if not user: continue

                xp_to_add = self.xp_from_chat * count
                if xp_to_add > 0:
                    xp_res = await supabase.rpc('add_xp', {'p_user_id': str(user_id), 'p_xp_to_add': xp_to_add, 'p_source': 'chat'}).execute()
                    if xp_res.data: await self.handle_level_up_event(user, xp_res.data)
                    
                    pet_xp_res = await supabase.rpc('add_xp_to_pet', {'p_user_id': user_id, 'p_xp_to_add': xp_to_add}).execute()
                    if pet_xp_res.data and pet_xp_res.data[0].get('leveled_up'):
                        await save_config_to_db(f"pet_levelup_request_{user_id}", {
                            "new_level": pet_xp_res.data[0].get('new_level'),
                            "points_awarded": pet_xp_res.data[0].get('points_awarded')
                        })
                        await save_config_to_db(f"pet_evolution_check_request_{user_id}", time.time())

                stats = await get_all_user_stats(user_id)
                daily_stats = stats.get('daily', {})
                if daily_stats.get('chat_count', 0) >= self.chat_message_requirement:
                    reward_res = await supabase.table('user_activities').select('id', count='exact').eq('user_id', str(user_id)).eq('activity_type', 'reward_chat').gte('created_at', datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()).execute()
                    if reward_res.count == 0:
                        reward = random.randint(*self.chat_reward_range)
                        await update_wallet(user, reward)
                        await supabase.table('user_activities').insert({'user_id': str(user_id), 'activity_type': 'reward_chat', 'coin_earned': reward}).execute()
                        await self.log_coin_activity(user, reward, f"채팅 {self.chat_message_requirement}회 달성")

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

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        pass

    @tasks.loop(minutes=1)
    async def voice_activity_tracker(self):
        await self.bot.wait_until_ready()
        server_id_str = get_config("SERVER_ID")
        if not server_id_str: return
        guild = self.bot.get_guild(int(server_id_str))
        if not guild: return

        currently_active_users: Set[int] = set()
        afk_channel_id = guild.afk_channel.id if guild.afk_channel else None

        for channel in guild.voice_channels:
            if channel.id == afk_channel_id: continue
            for member in channel.members:
                if not member.bot: currently_active_users.add(member.id)

        users_to_reward = currently_active_users.intersection(self.users_in_vc_last_minute)
        if not users_to_reward:
            self.users_in_vc_last_minute = currently_active_users
            return

        try:
            xp_per_minute = self.xp_from_voice
            for user_id in users_to_reward:
                user = self.bot.get_user(user_id)
                if not user: continue
                stats = await get_all_user_stats(user_id)
                new_total_voice_minutes_today = stats.get('daily', {}).get('voice_minutes', 0) + 1
                if new_total_voice_minutes_today > 0 and new_total_voice_minutes_today % self.voice_time_requirement_minutes == 0:
                    today_str = datetime.now(KST).strftime('%Y-%m-%d')
                    cooldown_key = f"voice_reward_{today_str}_{new_total_voice_minutes_today}m"
                    if await get_cooldown(user_id, cooldown_key) == 0: # OK
                        reward = random.randint(*self.voice_reward_range)
                        await update_wallet(user, reward)
                        await log_activity(user_id, 'reward_voice', coin_earned=reward)
                        await self.log_coin_activity(user, reward, f"음성 채널에서 {new_total_voice_minutes_today}분 활동")
                        await set_cooldown(user_id, cooldown_key)
            
            logs_to_insert = [{'user_id': str(uid), 'activity_type': 'voice', 'amount': 1, 'xp_earned': xp_per_minute} for uid in users_to_reward]
            if logs_to_insert:
                await supabase.table('user_activities').insert(logs_to_insert).execute()
                xp_update_tasks = [supabase.rpc('add_xp', {'p_user_id': str(uid), 'p_xp_to_add': xp_per_minute, 'p_source': 'voice'}).execute() for uid in users_to_reward]
                pet_xp_tasks = [supabase.rpc('add_xp_to_pet', {'p_user_id': uid, 'p_xp_to_add': xp_per_minute}).execute() for uid in users_to_reward]
                
                xp_results, pet_xp_results = await asyncio.gather(
                    asyncio.gather(*xp_update_tasks, return_exceptions=True),
                    asyncio.gather(*pet_xp_tasks, return_exceptions=True)
                )

                for i, result in enumerate(xp_results):
                    if not isinstance(result, Exception) and hasattr(result, 'data') and result.data:
                        user = self.bot.get_user(list(users_to_reward)[i])
                        if user: await self.handle_level_up_event(user, result.data)
                
                for i, result in enumerate(pet_xp_results):
                    user_id_from_list = list(users_to_reward)[i]
                    if not isinstance(result, Exception) and hasattr(result, 'data') and result.data and result.data[0].get('leveled_up'):
                        await save_config_to_db(f"pet_levelup_request_{user_id_from_list}", {
                            "new_level": result.data[0].get('new_level'),
                            "points_awarded": result.data[0].get('points_awarded')
                        })
                        await save_config_to_db(f"pet_evolution_check_request_{user_id_from_list}", time.time())
        except Exception as e:
            logger.error(f"[음성 활동 추적] 순찰 중 오류 발생: {e}", exc_info=True)
        finally:
            self.users_in_vc_last_minute = currently_active_users
    
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

    @tasks.loop(time=KST_MONTHLY_RESET)
    async def monthly_whale_reset(self):
        now = datetime.now(KST)
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

    @tasks.loop(time=KST_MIDNIGHT_AGGREGATE)
    async def update_market_prices(self):
        logger.info("[시장] 일일 아이템 및 물고기 가격 변동을 시작합니다.")
        try:
            from utils.database import load_game_data_from_db
            await load_game_data_from_db()
            item_db, loot_db = get_item_database(), get_fishing_loot()
            items_to_update, announcements, fish_to_update = [], [], []
            for name, data in item_db.items():
                if data.get('volatility', 0) > 0:
                    old_price = data.get('current_price', data.get('price', 0))
                    new_price = self._calculate_new_price(old_price, data['volatility'], data.get('min_price'), data.get('max_price'))
                    if new_price != old_price:
                        item_update_payload = {**data, 'name': name, 'current_price': new_price}
                        items_to_update.append(item_update_payload)
                        if abs((new_price - old_price) / old_price) > 0.25:
                            status = "폭등 📈" if new_price > old_price else "폭락 📉"
                            announcements.append(f" - {name}: `{old_price}` → `{new_price}`{self.currency_icon} ({status})")
            for fish in loot_db:
                if fish.get('volatility', 0) > 0 and 'id' in fish:
                    old_price = fish.get('current_base_value', fish.get('base_value', 0))
                    new_price = self._calculate_new_price(old_price, fish['volatility'], fish.get('min_price'), fish.get('max_price'))
                    if new_price != old_price:
                        fish_update_payload = {**fish, 'current_base_value': new_price}
                        fish_to_update.append(fish_update_payload)
                        if abs((new_price - old_price) / old_price) > 0.20:
                            status = "풍어 📈" if new_price > old_price else "흉어 📉"
                            announcements.append(f" - {fish['name']} (기본 가치): `{old_price}` → `{new_price}`{self.currency_icon} ({status})")
            if items_to_update: await supabase.table('items').upsert(items_to_update, on_conflict="name").execute()
            if fish_to_update: await supabase.table('fishing_loots').upsert(fish_to_update, on_conflict="id").execute()
            if items_to_update or fish_to_update:
                await load_game_data_from_db()
            await save_config_to_db("market_fluctuations", announcements)
            if announcements and (log_channel_id := get_id("market_log_channel_id")) and (log_channel := self.bot.get_channel(log_channel_id)):
                embed = discord.Embed(title="📢 오늘의 주요 시세 변동 정보", description="\n".join(announcements), color=0xFEE75C)
                await log_channel.send(embed=embed)
            if (commerce_cog := self.bot.get_cog("Commerce")) and (commerce_channel_id := get_id("commerce_panel_channel_id")) and (channel := self.bot.get_channel(commerce_channel_id)):
                await commerce_cog.regenerate_panel(channel)
            logger.info("[시장] 가격 변동 처리가 완료되었습니다.")
        except Exception as e:
            logger.error(f"[시장] 아이템 가격 업데이트 중 오류: {e}", exc_info=True)
            
    def _calculate_new_price(self, current, volatility, min_p, max_p):
        new_price = int(current * (1 + random.uniform(-volatility, volatility)))
        return min(max_p, max(min_p, new_price)) if min_p is not None and max_p is not None else new_price
        
    @update_market_prices.before_loop
    async def before_update_market_prices(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCore(bot))
