# bot-game/utils/database.py (맨손 상태 추가 및 기본 장비 로직 수정)

import os
import discord
from supabase import create_client, AsyncClient
import logging
import asyncio
import time
from typing import Dict, Callable, Any, List, Optional
from functools import wraps
from utils.ui_defaults import UI_STRINGS

logger = logging.getLogger(__name__)

# --- 캐시 영역 및 클라이언트 초기화 ---
_configs_cache: Dict[str, Any] = {"strings": UI_STRINGS}
_channel_id_cache: Dict[str, int] = {}
_item_database_cache: Dict[str, Dict[str, Any]] = {}
_fishing_loot_cache: List[Dict[str, Any]] = []

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

def supabase_retry_handler(retries: int = 3, delay: int = 5):
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            if not supabase: logger.error(f"❌ Supabase 클라이언트가 없어 '{func.__name__}' 함수를 실행할 수 없습니다."); return None
            for attempt in range(retries):
                try: return await func(*args, **kwargs)
                except Exception as e:
                    logger.warning(f"⚠️ '{func.__name__}' 함수 실행 중 오류 발생 (시도 {attempt + 1}/{retries}): {e}")
                    if attempt < retries - 1: await asyncio.sleep(delay)
            logger.error(f"❌ '{func.__name__}' 함수가 모든 재시도({retries}번)에 실패했습니다.", exc_info=True); return None
        return wrapper
    return decorator

# --- [신규 추가] 서버 전체 쿨타임 관련 함수 ---
ONE_WEEK_IN_SECONDS = 7 * 24 * 60 * 60

async def is_legendary_fish_available() -> bool:
    """전설의 물고기를 잡을 수 있는 상태인지 확인합니다 (주 1회)."""
    last_caught_str = get_config("legendary_fish_last_caught_timestamp", '"0"')
    last_caught_timestamp = float(last_caught_str.strip('"'))
    return (time.time() - last_caught_timestamp) > ONE_WEEK_IN_SECONDS

async def save_config(key: str, value: Any):
    """DB와 로컬 캐시에 설정을 저장하는 통합 함수."""
    global _configs_cache
    str_value = f'"{str(value)}"'
    await supabase.table('bot_configs').upsert({"config_key": key, "config_value": str_value}).execute()
    _configs_cache[key] = str_value
    logger.info(f"설정이 업데이트되었습니다: {key} -> {str_value}")

async def set_legendary_fish_cooldown():
    """전설의 물고기 쿨타임을 지금 시간으로 설정합니다."""
    await save_config("legendary_fish_last_caught_timestamp", time.time())

# --- 데이터 로드 및 관리 함수 ---
async def load_all_data_from_db():
    logger.info("------ [ 모든 DB 데이터 로드 시작 ] ------")
    await asyncio.gather(load_bot_configs_from_db(), load_channel_ids_from_db(), load_game_data_from_db())
    logger.info("------ [ 모든 DB 데이터 로드 완료 ] ------")

@supabase_retry_handler()
async def load_bot_configs_from_db():
    global _configs_cache
    response = await supabase.table('bot_configs').select('config_key, config_value').execute()
    if response and response.data:
        for item in response.data:
            _configs_cache[item['config_key']] = item['config_value']
        logger.info(f"✅ {len(response.data)}개의 봇 설정을 DB에서 로드하고 캐시에 병합했습니다.")
    else:
        logger.warning("DB 'bot_configs' 테이블에서 설정 정보를 찾을 수 없습니다.")

def get_config(key: str, default: Any = None) -> Any:
    return _configs_cache.get(key, default)

def get_string(key_path: str, default: Any = None, **kwargs) -> Any:
    try:
        keys = key_path.split('.')
        value = _configs_cache.get("strings", {})
        for key in keys: value = value[key]
        if isinstance(value, str) and kwargs:
            class SafeFormatter(dict):
                def __missing__(self, key: str) -> str: return f'{{{key}}}'
            return value.format_map(SafeFormatter(**kwargs))
        return value
    except (KeyError, TypeError):
        return default if default is not None else f"[{key_path}]"

@supabase_retry_handler()
async def load_channel_ids_from_db():
    global _channel_id_cache
    response = await supabase.table('channel_configs').select('channel_key', 'channel_id').execute()
    if response and response.data:
        _channel_id_cache = {item['channel_key']: int(item['channel_id']) for item in response.data}
        logger.info(f"✅ {len(_channel_id_cache)}개의 채널/역할 ID를 DB에서 로드했습니다.")
    else:
        logger.warning("DB 'channel_configs' 테이블에서 ID 정보를 찾을 수 없습니다.")

def get_id(key: str) -> Optional[int]: return _channel_id_cache.get(key)

@supabase_retry_handler()
async def load_game_data_from_db():
    global _item_database_cache, _fishing_loot_cache
    item_response = await supabase.table('items').select('*').execute()
    if item_response and item_response.data:
        _item_database_cache = {item.pop('name'): item for item in item_response.data}
        logger.info(f"✅ {len(_item_database_cache)}개의 아이템 정보를 DB에서 로드했습니다.")
    loot_response = await supabase.table('fishing_loots').select('*').execute()
    if loot_response and loot_response.data:
        _fishing_loot_cache = loot_response.data
        logger.info(f"✅ {len(_fishing_loot_cache)}개의 낚시 결과물 정보를 DB에서 로드했습니다.")

def get_item_database() -> Dict[str, Dict[str, Any]]: return _item_database_cache
def get_fishing_loot() -> List[Dict[str, Any]]: return _fishing_loot_cache

@supabase_retry_handler()
async def save_id_to_db(key: str, object_id: int):
    global _channel_id_cache
    await supabase.table('channel_configs').upsert({"channel_key": key, "channel_id": str(object_id)}, on_conflict="channel_key").execute()
    _channel_id_cache[key] = object_id

async def save_panel_id(panel_name: str, message_id: int, channel_id: int):
    await save_id_to_db(f"panel_{panel_name}_message_id", message_id)
    await save_id_to_db(f"panel_{panel_name}_channel_id", channel_id)

def get_panel_id(panel_name: str) -> Optional[dict]:
    message_id, channel_id = get_id(f"panel_{panel_name}_message_id"), get_id(f"panel_{panel_name}_channel_id")
    return {"message_id": message_id, "channel_id": channel_id} if message_id and channel_id else None

@supabase_retry_handler()
async def get_embed_from_db(embed_key: str) -> Optional[dict]:
    response = await supabase.table('embeds').select('embed_data').eq('embed_key', embed_key).limit(1).execute()
    return response.data[0]['embed_data'] if response and response.data else None

@supabase_retry_handler()
async def get_panel_components_from_db(panel_key: str) -> list:
    response = await supabase.table('panel_components').select('*').eq('panel_key', panel_key).order('row', desc=False).execute()
    return response.data if response and response.data else []

@supabase_retry_handler()
async def get_or_create_user(table_name: str, user_id_str: str, default_data: dict) -> dict:
    response = await supabase.table(table_name).select("*").eq("user_id", user_id_str).limit(1).execute()
    if response and response.data: return response.data[0]
    insert_data = {"user_id": user_id_str, **default_data}
    response = await supabase.table(table_name).insert(insert_data, returning="representation").execute()
    return response.data[0] if response and response.data else default_data

async def get_wallet(user_id: int) -> dict: return await get_or_create_user('wallets', str(user_id), {"balance": 0})

@supabase_retry_handler()
async def update_wallet(user: discord.User, amount: int) -> Optional[dict]:
    params = {'user_id_param': str(user.id), 'amount_param': amount}
    response = await supabase.rpc('increment_wallet_balance', params).execute()
    return response.data[0] if response and response.data else None

@supabase_retry_handler()
async def get_inventory(user_id_str: str) -> dict:
    response = await supabase.table('inventories').select('item_name, quantity').eq('user_id', user_id_str).gt('quantity', 0).execute()
    return {item['item_name']: item['quantity'] for item in response.data} if response and response.data else {}

@supabase_retry_handler()
async def update_inventory(user_id_str: str, item_name: str, quantity: int):
    params = {'user_id_param': user_id_str, 'item_name_param': item_name, 'amount_param': quantity}
    await supabase.rpc('increment_inventory_quantity', params).execute()

# [추가] 낚싯대가 없는 상태를 나타내는 상수
BARE_HANDS = "素手"

async def get_user_gear(user_id_str: str) -> dict:
    # [🔴 핵심 수정] 기본 낚싯대 설정을 '맨손'으로 변경
    default_bait = "エサなし"
    default_gear = {"rod": BARE_HANDS, "bait": default_bait}
    
    # DB에서 장비 정보를 가져오거나, 없으면 '맨손' 상태로 생성
    gear = await get_or_create_user('gear_setups', user_id_str, default_gear)
    inv = await get_inventory(user_id_str)
    
    rod = gear.get('rod', BARE_HANDS)
    # [🔴 핵심 수정] 장착한 낚싯대가 인벤토리에 없으면(판매했거나 버그) '맨손'으로 되돌림
    if rod != BARE_HANDS and inv.get(rod, 0) <= 0:
        rod = BARE_HANDS
        # DB의 장착 정보도 맨손으로 업데이트
        await set_user_gear(user_id_str, rod=BARE_HANDS)

    bait = gear.get('bait', default_bait)
    if bait != default_bait and inv.get(bait, 0) <= 0:
        bait = default_bait
        await set_user_gear(user_id_str, bait=default_bait)

    return {"rod": rod, "bait": bait}

@supabase_retry_handler()
async def set_user_gear(user_id_str: str, rod: str = None, bait: str = None):
    data_to_update = {}
    if rod is not None: data_to_update['rod'] = rod
    if bait is not None: data_to_update['bait'] = bait
    if data_to_update:
        await supabase.table('gear_setups').update(data_to_update).eq('user_id', user_id_str).execute()

@supabase_retry_handler()
async def get_aquarium(user_id_str: str) -> list:
    response = await supabase.table('aquariums').select('id, name, size, emoji').eq('user_id', user_id_str).execute()
    return response.data if response and response.data else []

@supabase_retry_handler()
async def add_to_aquarium(user_id_str: str, fish_data: dict):
    await supabase.table('aquariums').insert({"user_id": user_id_str, **fish_data}).execute()

@supabase_retry_handler()
async def get_cooldown(user_id_str: str, cooldown_key: str) -> float:
    response = await supabase.table('cooldowns').select('last_cooldown_timestamp').eq('user_id', user_id_str).eq('cooldown_key', cooldown_key).limit(1).execute()
    if response and response.data and response.data[0].get('last_cooldown_timestamp') is not None:
        return float(response.data[0]['last_cooldown_timestamp'])
    return 0.0

@supabase_retry_handler()
async def set_cooldown(user_id_str: str, cooldown_key: str, timestamp: float):
    await supabase.table('cooldowns').upsert({"user_id": user_id_str, "cooldown_key": cooldown_key, "last_cooldown_timestamp": timestamp}).execute()
