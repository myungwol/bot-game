# bot-game/utils/database.py

import os
import discord
from supabase import create_client, AsyncClient
import logging
import asyncio
import time
from typing import Dict, Callable, Any, List, Optional, Union
from functools import wraps
from utils.ui_defaults import UI_STRINGS
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

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

BARE_HANDS = "素手"
DEFAULT_ROD = "木の釣竿"

# --- [농장 시스템 함수] ---
@supabase_retry_handler()
async def get_farm_data(user_id: int) -> Optional[Dict[str, Any]]:
    response = await supabase.table('farms').select('*, farm_plots(*)').eq('user_id', user_id).maybe_single().execute()
    return response.data if response and hasattr(response, 'data') else None
@supabase_retry_handler()
async def get_farm_owner_by_thread(thread_id: int) -> Optional[int]:
    response = await supabase.table('farms').select('user_id').eq('thread_id', thread_id).maybe_single().execute()
    return response.data['user_id'] if response and hasattr(response, 'data') and response.data else None
@supabase_retry_handler()
async def create_farm(user_id: int) -> Optional[Dict[str, Any]]:
    rpc_response = await supabase.rpc('create_farm_for_user', {'p_user_id': user_id}).execute()
    if rpc_response and rpc_response.data:
        return await get_farm_data(user_id)
    return None

@supabase_retry_handler()
async def update_plot(plot_id: int, updates: Dict[str, Any]):
    await supabase.table('farm_plots').update(updates).eq('id', plot_id).execute()
@supabase_retry_handler()
async def get_farmable_item_info(item_name: str) -> Optional[Dict[str, Any]]:
    response = await supabase.table('farm_item_details').select('*').eq('item_name', item_name).maybe_single().execute()
    return response.data if response and hasattr(response, 'data') else None
@supabase_retry_handler()
async def clear_plots_db(plot_ids: List[int]):
    await supabase.rpc('clear_plots_to_default', {'p_plot_ids': plot_ids}).execute()
@supabase_retry_handler()
async def check_farm_permission(farm_id: int, user_id: int, action: str) -> bool:
    permission_column = f"can_{action}"
    response = await supabase.table('farm_permissions').select(permission_column, count='exact').eq('farm_id', farm_id).eq('granted_to_user_id', user_id).eq(permission_column, True).execute()
    return response.count > 0
@supabase_retry_handler()
async def grant_farm_permission(farm_id: int, user_id: int):
    await supabase.table('farm_permissions').upsert({
        'farm_id': farm_id, 'granted_to_user_id': user_id, 'can_till': True, 'can_plant': True,
        'can_water': True, 'can_harvest': True
    }, on_conflict='farm_id, granted_to_user_id').execute()

# --- [퀘스트 및 출석체크 함수] ---
@supabase_retry_handler()
async def has_checked_in_today(user_id: int) -> bool:
    JST = timezone(timedelta(hours=9))
    today_jst_start = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    response = await supabase.table('attendance_logs').select('id', count='exact').eq('user_id', user_id).gte('checked_in_at', today_jst_start.isoformat()).limit(1).execute()
    return response.count > 0
@supabase_retry_handler()
async def record_attendance(user_id: int):
    await supabase.table('attendance_logs').insert({'user_id': user_id}).execute()
    await supabase.rpc('increment_user_progress', {'p_user_id': user_id, 'p_attendance_count': 1}).execute()
@supabase_retry_handler()
async def get_user_progress(user_id: int) -> Dict[str, Any]:
    default_progress = {
        'daily_voice_minutes': 0, 'daily_fish_count': 0, 'weekly_attendance_count': 0, 'weekly_voice_minutes': 0,
        'weekly_fish_count': 0, 'last_daily_reset': None, 'last_weekly_reset': None
    }
    response = await supabase.table('user_progress').select('*').eq('user_id', user_id).maybe_single().execute()
    if response and hasattr(response, 'data') and response.data:
        return response.data
    return default_progress
async def increment_progress(user_id: int, fish_count: int = 0, voice_minutes: int = 0):
    await supabase.rpc('increment_user_progress', {'p_user_id': user_id, 'p_fish_count': fish_count, 'p_voice_minutes': voice_minutes}).execute()

# --- [공용 및 기타 게임 함수] ---
async def save_config(key: str, value: Any):
    global _configs_cache
    await supabase.table('bot_configs').upsert({"config_key": key, "config_value": value}).execute()
    _configs_cache[key] = value
    logger.info(f"설정이 업데이트되었습니다: {key} -> {value}")
save_config_to_db = save_config

ONE_WEEK_IN_SECONDS = 7 * 24 * 60 * 60
async def is_legendary_fish_available() -> bool:
    last_caught_ts = get_config("legendary_fish_last_caught_timestamp", 0)
    return (time.time() - float(last_caught_ts)) > ONE_WEEK_IN_SECONDS

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
        for item in response.data: _configs_cache[item['config_key']] = item['config_value']
        logger.info(f"✅ {len(response.data)}개의 봇 설정을 DB에서 로드하고 캐시에 병합했습니다.")
    else: logger.warning("DB 'bot_configs' 테이블에서 설정 정보를 찾을 수 없습니다.")

# [✅ 날씨 버그 수정] DB에서 불러온 값에 따옴표가 있을 경우 제거합니다.
def get_config(key: str, default: Any = None) -> Any:
    value = _configs_cache.get(key, default)
    if isinstance(value, str) and value.startswith('"') and value.endswith('"'):
        return value.strip('"')
    return value

def get_string(key_path: str, default: Any = None, **kwargs) -> Any:
    try:
        keys = key_path.split('.'); value = _configs_cache.get("strings", {})
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
    response = await supabase.table('channel_configs').select('channel_key', 'channel_id').limit(1000).execute()
    if response and response.data:
        _channel_id_cache = {item['channel_key']: int(item['channel_id']) for item in response.data if item.get('channel_id') and item['channel_id'] != '0'}
        logger.info(f"✅ {len(_channel_id_cache)}개의 채널/역할 ID를 DB에서 로드했습니다.")
    else: logger.warning("DB 'channel_configs' 테이블에서 ID 정보를 찾을 수 없습니다.")

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
    db_panel_name = panel_name.replace("panel_", "")
    await save_id_to_db(f"panel_{db_panel_name}_message_id", message_id)
    await save_id_to_db(f"panel_{db_panel_name}_channel_id", channel_id)

def get_panel_id(panel_name: str) -> Optional[dict]:
    db_panel_name = panel_name.replace("panel_", "")
    message_id, channel_id = get_id(f"panel_{db_panel_name}_message_id"), get_id(f"panel_{db_panel_name}_channel_id")
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
    params = {'p_user_id': str(user.id), 'p_amount': amount}
    response = await supabase.rpc('update_wallet_balance', params).execute()
    return response.data[0] if response and response.data else None

@supabase_retry_handler()
async def get_inventory(user: Union[discord.Member, discord.User]) -> Dict[str, int]:
    user_id_str = str(user.id)
    response = await supabase.table('inventories').select('item_name, quantity').eq('user_id', user_id_str).gt('quantity', 0).execute()
    inventory = {item['item_name']: item['quantity'] for item in response.data} if response and response.data else {}
    item_db = get_item_database()
    if isinstance(user, discord.Member):
        user_role_ids = {role.id for role in user.roles}
        for item_name, item_data in item_db.items():
            if (role_key := item_data.get('role_key')) and (role_id := get_id(role_key)) and role_id in user_role_ids:
                inventory[item_name] = inventory.get(item_name, 0) + 1
    return inventory

@supabase_retry_handler()
async def update_inventory(user_id_str: str, item_name: str, quantity: int):
    params = {'p_user_id': user_id_str, 'p_item_name': item_name, 'p_quantity_delta': quantity}
    await supabase.rpc('update_inventory_quantity', params).execute()

async def get_user_gear(user: Union[discord.Member, discord.User]) -> dict:
    default_bait = "エサなし"
    default_gear = {"rod": BARE_HANDS, "bait": default_bait, "hoe": BARE_HANDS, "watering_can": BARE_HANDS}
    
    user_id_str = str(user.id)
    gear = await get_or_create_user('gear_setups', user_id_str, default_gear)
    
    inv = await get_inventory(user)
    
    if inv is None:
        logger.error(f"'{user.name}'님의 인벤토리를 불러오는 데 실패하여, 장비 검사를 건너뜁니다.")
        return gear

    gear_to_check = {"rod": BARE_HANDS, "bait": default_bait, "hoe": BARE_HANDS, "watering_can": BARE_HANDS}
    updated = False
    for gear_type, default_item in gear_to_check.items():
        equipped_item = gear.get(gear_type, default_item)
        if equipped_item != default_item and inv.get(equipped_item, 0) <= 0:
            gear[gear_type] = default_item
            updated = True
    
    if updated:
        gear_updates = {k: v for k, v in gear.items() if k in gear_to_check}
        await set_user_gear(user_id_str, **gear_updates)
        
    return gear

@supabase_retry_handler()
async def set_user_gear(user_id_str: str, rod: str = None, bait: str = None, hoe: str = None, watering_can: str = None):
    data_to_update = {}
    if rod is not None: data_to_update['rod'] = rod
    if bait is not None: data_to_update['bait'] = bait
    if hoe is not None: data_to_update['hoe'] = hoe
    if watering_can is not None: data_to_update['watering_can'] = watering_can
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
        try: return datetime.fromisoformat(ts_str.replace('Z', '+00:00')).timestamp()
        except (ValueError, TypeError): return 0.0
    return 0.0

@supabase_retry_handler()
async def set_cooldown(user_id_str: str, cooldown_key: str, timestamp: float = None):
    ts = timestamp or time.time()
    iso_timestamp = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    await supabase.table('cooldowns').upsert({"user_id": user_id_str, "cooldown_key": cooldown_key, "last_cooldown_timestamp": iso_timestamp}, on_conflict="user_id,cooldown_key").execute()

@supabase_retry_handler()
async def sell_fish_from_db(user_id_str: str, fish_ids: List[int], total_sell_price: int):
    params = {'p_user_id': user_id_str, 'p_fish_ids': fish_ids, 'p_total_value': total_sell_price}
    await supabase.rpc('sell_fishes', params).execute()
