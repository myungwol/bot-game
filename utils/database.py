import os
import discord
from supabase import create_client, AsyncClient
import logging
import asyncio
import time
from typing import Dict, Callable, Any, List, Optional
from functools import wraps
from utils.ui_defaults import UI_STRINGS
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# (기존 _configs_cache, supabase 클라이언트 초기화 등은 그대로)
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

# --- [농장 시스템 함수] ---

@supabase_retry_handler()
async def get_farm_data(user_id: int) -> Optional[Dict[str, Any]]:
    """유저의 농장 기본 정보와 모든 칸(plot)들의 정보를 함께 가져옵니다."""
    response = await supabase.table('farms').select('*, farm_plots(*)').eq('user_id', user_id).maybe_single().execute()
    return response.data if response and hasattr(response, 'data') else None

@supabase_retry_handler()
async def create_farm(user_id: int) -> Optional[Dict[str, Any]]:
    """유저를 위한 새로운 농장을 생성하고, 생성된 농장 정보를 반환합니다."""
    # [✅✅✅ 핵심 수정 ✅✅✅]
    # DB 함수는 farm_id(숫자)를 반환합니다. response.data가 아닌 response.data[0]을 직접 사용합니다.
    # supabase-py v1에서는 response.data로 접근해야 할 수 있습니다. v2에서는 다를 수 있습니다.
    # 가장 안전한 방법은 rpc 호출 후 get_farm_data를 다시 호출하는 것입니다.
    rpc_response = await supabase.rpc('create_farm_for_user', {'p_user_id': user_id}).execute()
    if rpc_response and rpc_response.data:
        # DB 함수가 성공적으로 실행되면, 새로 생성된 농장의 전체 데이터를 다시 조회합니다.
        return await get_farm_data(user_id)
    return None

@supabase_retry_handler()
async def expand_farm_db(farm_id: int, new_size_x: int, new_size_y: int):
    """DB에 농장 확장을 요청합니다."""
    await supabase.rpc('expand_farm', {'p_farm_id': farm_id, 'new_size_x': new_size_x, 'new_size_y': new_size_y}).execute()

@supabase_retry_handler()
async def update_plot(plot_id: int, updates: Dict[str, Any]):
    """특정 칸(plot)의 상태를 업데이트합니다."""
    await supabase.table('farm_plots').update(updates).eq('id', plot_id).execute()

@supabase_retry_handler()
async def get_farmable_item_info(item_name: str) -> Optional[Dict[str, Any]]:
    """농사 관련 아이템의 상세 정보를 가져옵니다."""
    response = await supabase.table('farmable_items').select('*').eq('item_name', item_name).maybe_single().execute()
    return response.data if response and hasattr(response, 'data') else None

# --- [퀘스트 및 출석체크 함수] ---
# ... (이하 모든 기존 함수들은 이전과 동일하며, 오류가 없으므로 생략하지 않고 모두 포함합니다) ...
@supabase_retry_handler()
async def has_checked_in_today(user_id: int) -> bool:
    response = await supabase.table('attendance_logs').select('id', count='exact').eq('user_id', user_id).gte('checked_in_at', 'today').limit(1).execute()
    return response.count > 0

@supabase_retry_handler()
async def record_attendance(user_id: int):
    await supabase.table('attendance_logs').insert({'user_id': user_id}).execute()
    await supabase.rpc('increment_user_progress', {'p_user_id': user_id, 'p_attendance_count': 1}).execute()

@supabase_retry_handler()
async def get_user_progress(user_id: int) -> Dict[str, Any]:
    default_progress = {
        'daily_voice_minutes': 0, 'daily_fish_count': 0,
        'weekly_attendance_count': 0, 'weekly_voice_minutes': 0, 'weekly_fish_count': 0,
        'last_daily_reset': None, 'last_weekly_reset': None
    }
    response = await supabase.table('user_progress').select('*').eq('user_id', user_id).maybe_single().execute()
    return response.data if response.data else default_progress

async def increment_progress(user_id: int, fish_count: int = 0, voice_minutes: int = 0):
    await supabase.rpc('increment_user_progress', {
        'p_user_id': user_id,
        'p_fish_count': fish_count,
        'p_voice_minutes': voice_minutes
    }).execute()

ONE_MONTH_IN_SECONDS = 30 * 24 * 60 * 60

async def is_legendary_fish_available() -> bool:
    last_caught_str = get_config("legendary_fish_last_caught_timestamp", '"0"')
    last_caught_timestamp = float(last_caught_str.strip('"'))
    return (time.time() - last_caught_timestamp) > ONE_MONTH_IN_SECONDS

async def save_config(key: str, value: Any):
    global _configs_cache
    str_value = f'"{str(value)}"'
    await supabase.table('bot_configs').upsert({"config_key": key, "config_value": str_value}).execute()
    _configs_cache[key] = str_value
    logger.info(f"설정이 업데이트되었습니다: {key} -> {str_value}")

save_config_to_db = save_config

async def set_legendary_fish_cooldown():
    await save_config("legendary_fish_last_caught_timestamp", time.time())

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
    response = await supabase.table('panel_components').select('*').eq('panel_key', panel_key).order('row', desc=False).order('order_in_row', desc=False).execute()
    return response.data if response and response.data else []

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

BARE_HANDS = "素手"
DEFAULT_ROD = "古い釣竿"

async def get_user_gear(user_id_str: str) -> dict:
    default_bait = "エサなし"
    default_gear = {"rod": BARE_HANDS, "bait": default_bait}
    gear = await get_or_create_user('gear_setups', user_id_str, default_gear)
    inv = await get_inventory(user_id_str)
    rod = gear.get('rod', BARE_HANDS)
    if rod != BARE_HANDS and inv.get(rod, 0) <= 0:
        rod = BARE_HANDS
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
    if response and response.data and (ts_str := response.data[0].get('last_cooldown_timestamp')):
        try:
            return datetime.fromisoformat(ts_str).timestamp()
        except (ValueError, TypeError):
            return 0.0
    return 0.0

@supabase_retry_handler()
async def set_cooldown(user_id_str: str, cooldown_key: str, timestamp: float):
    iso_timestamp = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    await supabase.table('cooldowns').upsert({
        "user_id": user_id_str, 
        "cooldown_key": cooldown_key, 
        "last_cooldown_timestamp": iso_timestamp
    }).execute()

@supabase_retry_handler()
async def sell_fish_from_db(user_id_str: str, fish_ids: List[int], total_sell_price: int):
    params = {
        'p_user_id': user_id_str,
        'p_fish_ids': fish_ids,
        'p_total_value': total_sell_price
    }
    await supabase.rpc('sell_fishes', params).execute()
