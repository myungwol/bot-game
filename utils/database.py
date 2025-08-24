# utils/database.py (최종 완전 통합본)
"""
Supabase 데이터베이스와의 모든 상호작용을 관리하는 중앙 파일입니다.
이 파일은 서버 관리 봇과 게임 봇 모두에서 안전하게 사용할 수 있습니다.
"""
import os
import asyncio
import logging
import time
from functools import wraps
from datetime import datetime, timezone, timedelta

from typing import Dict, Callable, Any, List, Optional
import discord

from supabase import create_client, AsyncClient
from postgrest.exceptions import APIError

try:
    from .ui_defaults import (
        UI_EMBEDS, UI_PANEL_COMPONENTS, UI_ROLE_KEY_MAP, 
        SETUP_COMMAND_MAP, JOB_SYSTEM_CONFIG, AGE_ROLE_MAPPING, GAME_CONFIG,
        ONBOARDING_CHOICES
    )
except ImportError:
    UI_EMBEDS, UI_PANEL_COMPONENTS, UI_ROLE_KEY_MAP = {}, [], {}
    SETUP_COMMAND_MAP, JOB_SYSTEM_CONFIG, AGE_ROLE_MAPPING, GAME_CONFIG, ONBOARDING_CHOICES = {}, {}, {}, {}, {}


logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# 1. 클라이언트 초기화 및 캐시
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
supabase: AsyncClient = None
try:
    url: str = os.environ.get("SUPABASE_URL")
    key: str = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("SUPABASE_URL 또는 SUPABASE_KEY 환경 변수가 설정되지 않았습니다.")
    supabase = AsyncClient(supabase_url=url, supabase_key=key)
    logger.info("✅ Supabase 비동기 클라이언트가 성공적으로 생성되었습니다.")
except Exception as e:
    logger.critical(f"❌ Supabase 클라이언트 생성 실패: {e}", exc_info=True)
    supabase = None

_bot_configs_cache: Dict[str, Any] = {}
_channel_id_cache: Dict[str, int] = {}
_user_abilities_cache: Dict[int, tuple[List[str], float]] = {}
_item_database_cache: Dict[str, Dict[str, Any]] = {}
_fishing_loot_cache: List[Dict[str, Any]] = []

# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# 2. DB 오류 처리 데코레이터
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
def supabase_retry_handler(retries: int = 3, delay: int = 2):
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not supabase:
                logger.error(f"❌ Supabase 클라이언트가 초기화되지 않아 '{func.__name__}' 함수를 실행할 수 없습니다.")
                return None
            
            last_exception = None
            for attempt in range(retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    logger.warning(f"⚠️ '{func.__name__}' 함수 실행 중 오류 발생 (시도 {attempt + 1}/{retries}): {e}")
                    last_exception = e
                
                if attempt < retries - 1:
                    await asyncio.sleep(delay * (attempt + 1))
            
            logger.error(f"❌ '{func.__name__}' 함수가 모든 재시도({retries}번)에 실패했습니다. 마지막 오류: {last_exception}", exc_info=True)
            
            return_type = func.__annotations__.get("return")
            if return_type:
                type_str = str(return_type).lower()
                if "dict" in type_str: return {}
                if "list" in type_str: return []
            return None
        return wrapper
    return decorator

# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# 3. 데이터 로드 및 동기화 (서버 관리 봇 전용)
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
async def sync_defaults_to_db():
    logger.info("------ [ 기본값 DB 동기화 시작 (서버 관리 봇) ] ------")
    try:
        if not UI_ROLE_KEY_MAP:
            logger.info("ui_defaults가 로드되지 않아 DB 동기화를 건너뜁니다 (게임 봇에서는 정상입니다).")
            return

        role_name_map = {key: info["name"] for key, info in UI_ROLE_KEY_MAP.items()}
        await save_config_to_db("ROLE_KEY_MAP", role_name_map)
        
        prefix_hierarchy = sorted(
            [info["name"] for info in UI_ROLE_KEY_MAP.values() if info.get("is_prefix")],
            key=lambda name: next((info.get("priority", 0) for info in UI_ROLE_KEY_MAP.values() if info["name"] == name), 0),
            reverse=True
        )
        await save_config_to_db("NICKNAME_PREFIX_HIERARCHY", prefix_hierarchy)
        
        await asyncio.gather(
            *[save_embed_to_db(key, data) for key, data in UI_EMBEDS.items()],
            *[save_panel_component_to_db(comp) for comp in UI_PANEL_COMPONENTS],
            save_config_to_db("SETUP_COMMAND_MAP", SETUP_COMMAND_MAP),
            save_config_to_db("JOB_SYSTEM_CONFIG", JOB_SYSTEM_CONFIG),
            save_config_to_db("AGE_ROLE_MAPPING", AGE_ROLE_MAPPING),
            save_config_to_db("GAME_CONFIG", GAME_CONFIG),
            save_config_to_db("ONBOARDING_CHOICES", ONBOARDING_CHOICES)
        )

        all_role_keys = list(UI_ROLE_KEY_MAP.keys())
        all_channel_keys = [info['key'] for info in SETUP_COMMAND_MAP.values()]
        
        placeholder_records = [{"channel_key": key, "channel_id": "0"} for key in set(all_role_keys + all_channel_keys)]
        
        if placeholder_records:
            await supabase.table('channel_configs').upsert(placeholder_records, on_conflict="channel_key", ignore_duplicates=True).execute()

        logger.info(f"✅ 설정, 임베드({len(UI_EMBEDS)}개), 컴포넌트({len(UI_PANEL_COMPONENTS)}개) 등 동기화 완료.")

    except Exception as e:
        logger.error(f"❌ 기본값 DB 동기화 중 치명적 오류 발생: {e}", exc_info=True)
    logger.info("------ [ 기본값 DB 동기화 완료 ] ------")

async def load_all_data_from_db():
    logger.info("------ [ 모든 DB 데이터 캐시 로드 시작 ] ------")
    await asyncio.gather(load_bot_configs_from_db(), load_channel_ids_from_db(), load_game_data_from_db())
    logger.info("------ [ 모든 DB 데이터 캐시 로드 완료 ] ------")

# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# 4. 설정 (bot_configs) 관련 함수
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
@supabase_retry_handler()
async def load_bot_configs_from_db():
    global _bot_configs_cache
    response = await supabase.table('bot_configs').select('config_key, config_value').execute()
    if response and response.data:
        _bot_configs_cache = {item['config_key']: item['config_value'] for item in response.data}
        logger.info(f"✅ {len(_bot_configs_cache)}개의 봇 설정을 DB에서 캐시로 로드했습니다.")

@supabase_retry_handler()
async def save_config_to_db(key: str, value: Any):
    global _bot_configs_cache
    await supabase.table('bot_configs').upsert({"config_key": key, "config_value": value}).execute()
    _bot_configs_cache[key] = value

def get_config(key: str, default: Any = None) -> Any:
    return _bot_configs_cache.get(key, default)

# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# 5. ID (channel_configs) 관련 함수
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
@supabase_retry_handler()
async def load_channel_ids_from_db():
    global _channel_id_cache
    response = await supabase.table('channel_configs').select('channel_key, channel_id').execute()
    if response and response.data:
        _channel_id_cache = {item['channel_key']: int(item['channel_id']) for item in response.data if item.get('channel_id') and item['channel_id'] != '0'}
        logger.info(f"✅ {len(_channel_id_cache)}개의 유효한 채널/역할 ID를 DB에서 캐시로 로드했습니다.")

@supabase_retry_handler()
async def save_id_to_db(key: str, object_id: int) -> bool:
    global _channel_id_cache
    try:
        response = await supabase.table('channel_configs').upsert({"channel_key": key, "channel_id": str(object_id)}, on_conflict="channel_key").execute()
        if response and response.data:
            _channel_id_cache[key] = object_id
            return True
        return False
    except Exception as e:
        logger.error(f"❌ '{key}' ID 저장 중 예외 발생: {e}", exc_info=True)
        return False

def get_id(key: str) -> Optional[int]:
    return _channel_id_cache.get(key)

async def save_panel_id(panel_name: str, message_id: int, channel_id: int):
    await save_id_to_db(f"panel_{panel_name}_message_id", message_id)
    await save_id_to_db(f"panel_{panel_name}_channel_id", channel_id)

def get_panel_id(panel_name: str) -> Optional[Dict[str, int]]:
    message_id = get_id(f"panel_{panel_name}_message_id")
    channel_id = get_id(f"panel_{panel_name}_channel_id")
    return {"message_id": message_id, "channel_id": channel_id} if message_id and channel_id else None

# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# 6. 임베드, 컴포넌트, 쿨다운 관련 함수
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
@supabase_retry_handler()
async def save_embed_to_db(embed_key: str, embed_data: dict):
    await supabase.table('embeds').upsert({'embed_key': embed_key, 'embed_data': embed_data}, on_conflict='embed_key').execute()
@supabase_retry_handler()
async def get_embed_from_db(embed_key: str) -> Optional[dict]:
    response = await supabase.table('embeds').select('embed_data').eq('embed_key', embed_key).limit(1).execute()
    return response.data[0]['embed_data'] if response and response.data else None
@supabase_retry_handler()
async def get_all_embeds() -> List[Dict[str, Any]]:
    response = await supabase.table('embeds').select('embed_key, embed_data').order('embed_key').execute()
    return response.data if response and response.data else []
@supabase_retry_handler()
async def get_onboarding_steps() -> List[dict]:
    response = await supabase.table('onboarding_steps').select('*, embed_data:embeds(embed_data)').order('step_number', desc=False).execute()
    return response.data if response and response.data else []
@supabase_retry_handler()
async def save_panel_component_to_db(component_data: dict):
    await supabase.table('panel_components').upsert(component_data, on_conflict='component_key').execute()
@supabase_retry_handler()
async def get_panel_components_from_db(panel_key: str) -> list:
    response = await supabase.table('panel_components').select('*').eq('panel_key', panel_key).order('row', desc=False).order('order_in_row', desc=False).execute()
    return response.data if response and response.data else []
@supabase_retry_handler()
async def get_cooldown(user_id_str: str, cooldown_key: str) -> float:
    response = await supabase.table('cooldowns').select('last_cooldown_timestamp').eq('user_id', user_id_str).eq('cooldown_key', cooldown_key).limit(1).execute()
    if response and response.data and (timestamp_str := response.data[0].get('last_cooldown_timestamp')) is not None:
        try:
            if timestamp_str.endswith('Z'): timestamp_str = timestamp_str[:-1] + '+00:00'
            return datetime.fromisoformat(timestamp_str).timestamp()
        except (ValueError, TypeError): return 0.0
    return 0.0
@supabase_retry_handler()
async def set_cooldown(user_id_str: str, cooldown_key: str):
    await supabase.table('cooldowns').upsert({ "user_id": user_id_str, "cooldown_key": cooldown_key, "last_cooldown_timestamp": datetime.now(timezone.utc).isoformat() }, on_conflict='user_id, cooldown_key').execute()

# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# 7. 서버 관리 기능 관련 함수
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
@supabase_retry_handler()
async def add_warning(guild_id: int, user_id: int, moderator_id: int, reason: str, amount: int) -> Optional[dict]:
    response = await supabase.table('warnings').insert({"guild_id": guild_id, "user_id": user_id, "moderator_id": moderator_id, "reason": reason, "amount": amount}).select().execute()
    return response.data[0] if response and response.data else None
@supabase_retry_handler()
async def get_total_warning_count(user_id: int, guild_id: int) -> int:
    response = await supabase.table('warnings').select('amount').eq('user_id', user_id).eq('guild_id', guild_id).execute()
    return sum(item['amount'] for item in response.data) if response and response.data else 0
@supabase_retry_handler()
async def add_anonymous_message(guild_id: int, user_id: int, content: str):
    await supabase.table('anonymous_messages').insert({"guild_id": guild_id, "user_id": user_id, "message_content": content}).execute()
@supabase_retry_handler()
async def has_posted_anonymously_today(user_id: int) -> bool:
    today_jst_start = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    today_utc_start = today_jst_start.astimezone(timezone.utc)
    response = await supabase.table('anonymous_messages').select('id', count='exact').eq('user_id', user_id).gte('created_at', today_utc_start.isoformat()).limit(1).execute()
    return response.count > 0 if response else False
@supabase_retry_handler()
async def backup_member_data(user_id: int, guild_id: int, role_ids: List[int], nickname: Optional[str]):
    await supabase.table('left_members').upsert({ 'user_id': user_id, 'guild_id': guild_id, 'roles': role_ids, 'nickname': nickname, 'left_at': datetime.now(timezone.utc).isoformat() }).execute()
@supabase_retry_handler()
async def get_member_backup(user_id: int, guild_id: int) -> Optional[Dict[str, Any]]:
    response = await supabase.table('left_members').select('*').eq('user_id', user_id).eq('guild_id', guild_id).maybe_single().execute()
    return response.data if response else None
@supabase_retry_handler()
async def delete_member_backup(user_id: int, guild_id: int):
    await supabase.table('left_members').delete().eq('user_id', user_id).eq('guild_id', guild_id).execute()

# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# 8. 게임 및 경제 기능 관련 함수
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
@supabase_retry_handler()
async def load_game_data_from_db():
    global _item_database_cache, _fishing_loot_cache
    item_response = await supabase.table('items').select('*').execute()
    if item_response and item_response.data:
        _item_database_cache = {item.pop('name'): item for item in item_response.data}
    loot_response = await supabase.table('fishing_loots').select('*').execute()
    if loot_response and loot_response.data:
        _fishing_loot_cache = loot_response.data

def get_item_database() -> Dict[str, Dict[str, Any]]: return _item_database_cache
def get_fishing_loot() -> List[Dict[str, Any]]: return _fishing_loot_cache

@supabase_retry_handler()
async def get_or_create_user(table_name: str, user_id_str: str, default_data: dict) -> dict:
    response = await supabase.table(table_name).select("*").eq("user_id", user_id_str).limit(1).execute()
    if response and response.data: return response.data[0]
    insert_data = {"user_id": user_id_str, **default_data}
    response = await supabase.table(table_name).insert(insert_data, returning="representation").execute()
    return response.data[0] if response and response.data else default_data

async def get_wallet(user_id: int) -> dict:
    return await get_or_create_user('wallets', str(user_id), {"balance": 0})

@supabase_retry_handler()
async def update_wallet(user: discord.User, amount: int) -> Optional[dict]:
    params = {'p_user_id': str(user.id), 'p_amount': amount}
    response = await supabase.rpc('update_wallet_balance', params).execute()
    return response.data[0] if response and response.data else None
    
@supabase_retry_handler()
async def get_inventory(user: discord.User) -> Dict[str, int]:
    user_id_str = str(user.id)
    response = await supabase.table('inventories').select('item_name, quantity').eq('user_id', user_id_str).gt('quantity', 0).execute()
    return {item['item_name']: item['quantity'] for item in response.data} if response and response.data else {}

@supabase_retry_handler()
async def update_inventory(user_id_str: str, item_name: str, quantity: int):
    params = {'p_user_id': user_id_str, 'p_item_name': item_name, 'p_quantity_delta': quantity}
    await supabase.rpc('update_inventory_quantity', params).execute()

@supabase_retry_handler()
async def get_user_gear(user: discord.User) -> dict:
    default_gear = {"rod": "素手", "bait": "エサなし", "hoe": "素手", "watering_can": "素手"}
    return await get_or_create_user('gear_setups', str(user.id), default_gear)

@supabase_retry_handler()
async def set_user_gear(user_id_str: str, **kwargs):
    if kwargs:
        await supabase.table('gear_setups').update(kwargs).eq('user_id', user_id_str).execute()

@supabase_retry_handler()
async def get_aquarium(user_id_str: str) -> list:
    response = await supabase.table('aquariums').select('id, name, size, emoji').eq('user_id', user_id_str).execute()
    return response.data if response and response.data else []

@supabase_retry_handler()
async def add_to_aquarium(user_id_str: str, fish_data: dict):
    await supabase.table('aquariums').insert({"user_id": user_id_str, **fish_data}).execute()

@supabase_retry_handler()
async def sell_fish_from_db(user_id_str: str, fish_ids: List[int], total_sell_price: int):
    params = {'p_user_id': user_id_str, 'p_fish_ids': fish_ids, 'p_total_value': total_sell_price}
    await supabase.rpc('sell_fishes', params).execute()

@supabase_retry_handler()
async def get_user_abilities(user_id: int) -> List[str]:
    CACHE_TTL = 300
    now = time.time()
    if user_id in _user_abilities_cache:
        cached_data, timestamp = _user_abilities_cache[user_id]
        if now - timestamp < CACHE_TTL: return cached_data
    response = await supabase.rpc('get_user_ability_keys', {'p_user_id': user_id}).execute()
    abilities = response.data if response and hasattr(response, 'data') and response.data else []
    _user_abilities_cache[user_id] = (abilities, now)
    return abilities

@supabase_retry_handler()
async def has_checked_in_today(user_id: int) -> bool:
    today_jst_start = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    response = await supabase.table('attendance_logs').select('id', count='exact').eq('user_id', user_id).gte('checked_in_at', today_jst_start.isoformat()).limit(1).execute()
    return response.count > 0 if response and hasattr(response, 'count') else False

@supabase_retry_handler()
async def record_attendance(user_id: int):
    await supabase.table('attendance_logs').insert({'user_id': user_id}).execute()
    await supabase.rpc('increment_user_progress', {'p_user_id': user_id, 'p_attendance_count': 1}).execute()

@supabase_retry_handler()
async def get_user_progress(user_id: int) -> Dict[str, Any]:
    default = {'daily_voice_minutes': 0, 'daily_fish_count': 0, 'weekly_attendance_count': 0}
    response = await supabase.table('user_progress').select('*').eq('user_id', user_id).maybe_single().execute()
    return response.data if response and hasattr(response, 'data') and response.data else default

@supabase_retry_handler()
async def increment_progress(user_id: int, fish_count: int = 0, voice_minutes: int = 0):
    await supabase.rpc('increment_user_progress', {'p_user_id': user_id, 'p_fish_count': fish_count, 'p_voice_minutes': voice_minutes}).execute()

@supabase_retry_handler()
async def get_farm_data(user_id: int) -> Optional[Dict[str, Any]]:
    response = await supabase.table('farms').select('*, farm_plots(*)').eq('user_id', user_id).maybe_single().execute()
    return response.data if response and hasattr(response, 'data') else None

@supabase_retry_handler()
async def create_farm(user_id: int) -> Optional[Dict[str, Any]]:
    rpc_response = await supabase.rpc('create_farm_for_user', {'p_user_id': user_id}).execute()
    return await get_farm_data(user_id) if rpc_response and rpc_response.data else None

@supabase_retry_handler()
async def get_farmable_item_info(item_name: str) -> Optional[Dict[str, Any]]:
    response = await supabase.table('farm_item_details').select('*').eq('item_name', item_name).maybe_single().execute()
    return response.data if response and hasattr(response, 'data') else None
