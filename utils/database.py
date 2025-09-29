# game-bot/utils/database.py
import os
import asyncio
import logging
import time
import json
import discord
from functools import wraps
from datetime import datetime, timezone, timedelta
from typing import Dict, Callable, Any, List, Optional
from collections import defaultdict
from supabase import create_client, AsyncClient
from postgrest.exceptions import APIError

logger = logging.getLogger(__name__)

supabase: AsyncClient = None
try:
    url: str = os.environ.get("SUPABASE_URL")
    key: str = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("SUPABASE_URL 또는 SUPABASE_KEY 환경 변수가 설정되지 않았습니다.")
    supabase = AsyncClient(supabase_url=url, supabase_key=key)
    logger.info("✅ Supabase 비동기 클라이언트가 성공적으로 생성되었습니다.")
except Exception as e:
    logger.critical(f"❌ Supabase 클라이언트 생성에 실패했습니다: {e}", exc_info=True)

_bot_configs_cache: Dict[str, Any] = {}
_channel_id_cache: Dict[str, int] = {}
_item_database_cache: Dict[str, Dict[str, Any]] = {}
_fishing_loot_cache: List[Dict[str, Any]] = []
_user_abilities_cache: Dict[int, tuple[List[str], float]] = {}
_exploration_locations_cache: List[Dict[str, Any]] = []
_exploration_loot_cache: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
_initial_load_complete = False

KST = timezone(timedelta(hours=9))
BARE_HANDS = "맨손"
DEFAULT_ROD = "평범한 낚싯대"

def supabase_retry_handler(retries: int = 3, delay: int = 2):
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not supabase: return None
            for attempt in range(retries):
                try: return await func(*args, **kwargs)
                except Exception as e:
                    logger.error(f"'{func.__name__}' 함수 실행 중 오류 발생 (시도 {attempt + 1}/{retries}): {e}", exc_info=True)
                    if attempt < retries - 1: await asyncio.sleep(delay)
            return None
        return wrapper
    return decorator

async def load_all_data_from_db():
    global _initial_load_complete
    if _initial_load_complete:
        return
    logger.info("------ [ 모든 DB 데이터 캐시 로드 시작 ] ------")
    await asyncio.gather(
        load_bot_configs_from_db(), 
        load_channel_ids_from_db(), 
        load_game_data_from_db(),
        load_exploration_data_from_db()
    )
    logger.info("------ [ 모든 DB 데이터 캐시 로드 완료 ] ------")
    _initial_load_complete = True

@supabase_retry_handler()
async def load_bot_configs_from_db():
    global _bot_configs_cache
    response = await supabase.table('bot_configs').select('config_key, config_value').execute()
    if response and response.data:
        _bot_configs_cache = {item['config_key']: item['config_value'] for item in response.data}
        logger.info(f"✅ {len(_bot_configs_cache)}개의 봇 설정을 DB에서 로드했습니다.")

@supabase_retry_handler()
async def load_channel_ids_from_db():
    global _channel_id_cache
    response = await supabase.table('channel_configs').select('channel_key, channel_id').execute()
    if response and response.data:
        _channel_id_cache = {item['channel_key']: int(item['channel_id']) for item in response.data if item.get('channel_id') and item['channel_id'] != '0'}
        logger.info(f"✅ {len(_channel_id_cache)}개의 채널/역할 ID를 DB에서 로드했습니다.")

@supabase_retry_handler()
async def load_game_data_from_db():
    global _item_database_cache, _fishing_loot_cache
    item_response = await supabase.table('items').select('*').execute()
    if item_response and item_response.data:
        _item_database_cache = {item.pop('name'): item for item in item_response.data}
    loot_response = await supabase.table('fishing_loots').select('*').execute()
    if loot_response and loot_response.data:
        _fishing_loot_cache = loot_response.data
    logger.info(f"✅ 게임 데이터를 DB에서 로드했습니다. (아이템: {len(_item_database_cache)}개, 낚시: {len(_fishing_loot_cache)}개)")

@supabase_retry_handler()
async def load_exploration_data_from_db():
    """펫 탐사 관련 데이터를 DB에서 로드하여 캐시에 저장합니다."""
    global _exploration_locations_cache, _exploration_loot_cache
    locations_res, loot_res = await asyncio.gather(
        supabase.table('exploration_locations').select('*').order('required_pet_level').execute(),
        supabase.table('exploration_loot').select('*').execute()
    )
    if locations_res and locations_res.data:
        _exploration_locations_cache = locations_res.data
    
    _exploration_loot_cache.clear()
    if loot_res and loot_res.data:
        for item in loot_res.data:
            _exploration_loot_cache[item['location_key']].append(item)
    logger.info(f"✅ 펫 탐사 데이터를 DB에서 로드했습니다. (지역: {len(_exploration_locations_cache)}개, 보상 설정: {len(loot_res.data)}개)")

@supabase_retry_handler()
async def reload_game_data_from_db():
    global _item_database_cache, _fishing_loot_cache
    try:
        await load_game_data_from_db()
        return True
    except Exception as e:
        logger.error(f"게임 데이터 리로드 중 오류가 발생했습니다: {e}", exc_info=True)
        return False

def get_config(key: str, default: Any = None) -> Any: return _bot_configs_cache.get(key, default)
def get_id(key: str) -> Optional[int]: return _channel_id_cache.get(key)
def get_item_database() -> Dict[str, Dict[str, Any]]: return _item_database_cache
def get_fishing_loot() -> List[Dict[str, Any]]: return _fishing_loot_cache

def get_exploration_locations() -> List[Dict[str, Any]]:
    return _exploration_locations_cache

def get_exploration_loot(location_key: str, pet_level: int) -> List[Dict[str, Any]]:
    """특정 지역에서 해당 펫 레벨에 맞는 모든 보상 목록을 반환합니다."""
    all_loot_for_location = _exploration_loot_cache.get(location_key, [])
    return [loot for loot in all_loot_for_location]


def get_string(key_path: str, default: Any = None, **kwargs) -> Any:
    try:
        keys = key_path.split('.')
        value = get_config("strings", {})
        for key in keys:
            value = value[key]
        if isinstance(value, str) and kwargs:
            class SafeFormatter(dict):
                def __missing__(self, key: str) -> str: return f'{{{key}}}'
            return value.format_map(SafeFormatter(**kwargs))
        return value
    except (KeyError, TypeError):
        return default if default is not None else f"[{key_path}]"

@supabase_retry_handler()
async def save_config_to_db(key: str, value: Any):
    global _bot_configs_cache
    await supabase.table('bot_configs').upsert({"config_key": key, "config_value": value}).execute()
    _bot_configs_cache[key] = value

@supabase_retry_handler()
async def delete_config_from_db(key: str):
    """특정 설정 키를 DB와 로컬 캐시에서 삭제합니다."""
    global _bot_configs_cache
    await supabase.table('bot_configs').delete().eq('config_key', key).execute()
    _bot_configs_cache.pop(key, None)

@supabase_retry_handler()
async def save_id_to_db(key: str, object_id: int):
    global _channel_id_cache
    await supabase.table('channel_configs').upsert({"channel_key": key, "channel_id": str(object_id)}, on_conflict="channel_key").execute()
    _channel_id_cache[key] = object_id

async def save_panel_id(panel_name: str, message_id: int, channel_id: int):
    await save_id_to_db(f"panel_{panel_name}_message_id", message_id)
    await save_id_to_db(f"panel_{panel_name}_channel_id", channel_id)

def get_panel_id(panel_name: str) -> Optional[Dict[str, int]]:
    message_id = get_id(f"panel_{panel_name}_message_id")
    channel_id = get_id(f"panel_{panel_name}_channel_id")
    return {"message_id": message_id, "channel_id": channel_id} if message_id and channel_id else None

def is_whale_available() -> bool:
    return get_config("whale_announcement_message_id") is not None

async def set_whale_caught():
    await save_config_to_db("whale_announcement_message_id", None)

@supabase_retry_handler()
async def get_embed_from_db(embed_key: str) -> Optional[dict]:
    response = await supabase.table('embeds').select('embed_data').eq('embed_key', embed_key).limit(1).execute()
    return response.data[0]['embed_data'] if response and response.data else None

@supabase_retry_handler()
async def get_panel_components_from_db(panel_key: str) -> list:
    response = await supabase.table('panel_components').select('*').eq('panel_key', panel_key).order('row').order('order_in_row').execute()
    return response.data if response and response.data else []

@supabase_retry_handler()
async def get_or_create_user(table_name: str, user_id: int, default_data: dict) -> dict:
    user_id_str = str(user_id)
    response = await supabase.table(table_name).select("*").eq("user_id", user_id_str).maybe_single().execute()
    if response and response.data:
        return response.data

    logger.warning(f"테이블 '{table_name}'에서 유저(ID: {user_id})의 정보를 찾을 수 없어 새로 생성합니다.")
    insert_data = {"user_id": user_id_str, **default_data}
    response = await supabase.table(table_name).upsert(insert_data, on_conflict="user_id").select().maybe_single().execute()
    
    return response.data if response and response.data else default_data

async def get_wallet(user_id: int) -> dict:
    return await get_or_create_user('wallets', user_id, {"balance": 0})

@supabase_retry_handler()
async def update_wallet(user: discord.User, amount: int) -> Optional[dict]:
    params = {'p_user_id': str(user.id), 'p_amount': amount}
    response = await supabase.rpc('update_wallet_balance', params).select().maybe_single().execute()
    return response.data if response and response.data else None

@supabase_retry_handler()
async def get_inventory(user: discord.User) -> Dict[str, int]:
    response = await supabase.table('inventories').select('item_name, quantity').eq('user_id', str(user.id)).gt('quantity', 0).execute()
    return {item['item_name']: item['quantity'] for item in response.data} if response and response.data else {}

@supabase_retry_handler()
async def update_inventory(user_id: int, item_name: str, quantity: int):
    params = {'p_user_id': str(user_id), 'p_item_name': item_name, 'p_quantity_delta': quantity}
    await supabase.rpc('update_inventory_quantity', params).execute()

@supabase_retry_handler()
async def ensure_user_gear_exists(user_id: int):
    await supabase.rpc('create_user_gear_if_not_exists', {'p_user_id': str(user_id)}).execute()

@supabase_retry_handler()
async def get_user_gear(user: discord.User) -> dict:
    user_id_str = str(user.id)
    await ensure_user_gear_exists(user.id)
    
    response = await supabase.table('gear_setups').select('*').eq('user_id', user_id_str).maybe_single().execute()

    if response and response.data:
        return response.data
    
    logger.warning(f"DB에서 유저(ID: {user.id})의 장비 정보를 가져오지 못했습니다. 기본값을 반환합니다.")
    return {"rod": BARE_HANDS, "bait": "미끼 없음", "hoe": BARE_HANDS, "watering_can": BARE_HANDS, "pickaxe": BARE_HANDS}

@supabase_retry_handler()
async def set_user_gear(user_id: int, **kwargs):
    if kwargs:
        await ensure_user_gear_exists(user_id)
        await supabase.table('gear_setups').update(kwargs).eq('user_id', str(user_id)).execute()
        
@supabase_retry_handler()
async def get_aquarium(user_id: int) -> list:
    response = await supabase.table('aquariums').select('id, name, size, emoji').eq('user_id', str(user_id)).execute()
    return response.data if response and response.data else []

@supabase_retry_handler()
async def add_to_aquarium(user_id: int, fish_data: dict):
    await supabase.table('aquariums').insert({"user_id": str(user_id), **fish_data}).execute()

@supabase_retry_handler()
async def sell_fish_from_db(user_id: int, fish_ids: List[int], total_sell_price: int):
    params = {'p_user_id': str(user_id), 'p_fish_ids': fish_ids, 'p_total_value': total_sell_price}
    await supabase.rpc('sell_fishes', params).execute()

@supabase_retry_handler()
async def get_user_abilities(user_id: int) -> List[str]:
    CACHE_TTL = 300; now = time.time()
    if user_id in _user_abilities_cache:
        cached_data, timestamp = _user_abilities_cache[user_id]
        if now - timestamp < CACHE_TTL: return cached_data
    response = await supabase.rpc('get_user_ability_keys', {'p_user_id': str(user_id)}).execute()
    abilities = response.data if response and hasattr(response, 'data') and response.data else []
    _user_abilities_cache[user_id] = (abilities, now)
    return abilities
    
@supabase_retry_handler()
async def get_cooldown(subject_id_int: int, cooldown_key: str) -> float:
    subject_id_str = str(subject_id_int)
    response = await supabase.table('cooldowns').select('last_cooldown_timestamp').eq('subject_id', subject_id_str).eq('cooldown_key', cooldown_key).maybe_single().execute()
    if response and response.data and (ts_str := response.data.get('last_cooldown_timestamp')):
        try: return datetime.fromisoformat(ts_str.replace('Z', '+00:00')).timestamp()
        except (ValueError, TypeError): return 0.0
    return 0.0

@supabase_retry_handler()
async def set_cooldown(subject_id_int: int, cooldown_key: str):
    subject_id_str = str(subject_id_int)
    iso_timestamp = datetime.now(timezone.utc).isoformat()
    await supabase.table('cooldowns').upsert({"subject_id": subject_id_str, "cooldown_key": cooldown_key, "last_cooldown_timestamp": iso_timestamp}).execute()

@supabase_retry_handler()
async def get_user_pet(user_id: int) -> Optional[Dict]:
    res = await supabase.table('pets').select('*, pet_species(*)').eq('user_id', str(user_id)).gt('current_stage', 1).maybe_single().execute()
    return res.data if res and res.data else None

@supabase_retry_handler()
async def create_initial_user_data(user_id: int):
    """
    신규 유저가 서버에 참여했을 때 지갑, 레벨, 장비 등 필수 데이터를 생성합니다.
    이미 데이터가 있는 경우 ON CONFLICT 덕분에 아무 작업도 하지 않습니다.
    """
    try:
        await supabase.rpc('create_new_user_records', {'p_user_id': user_id}).execute()
        logger.info(f"신규 유저(ID: {user_id})의 초기 데이터 생성을 요청했습니다.")
    except Exception as e:
        logger.error(f"신규 유저(ID: {user_id}) 데이터 생성 중 오류 발생: {e}", exc_info=True)

@supabase_retry_handler()
async def log_activity(
    user_id: int, activity_type: str, amount: int = 1,
    xp_earned: int = 0, coin_earned: int = 0
):
    try:
        await supabase.table('user_activities').insert({
            'user_id': str(user_id),
            'activity_type': activity_type,
            'amount': amount,
            'xp_earned': xp_earned,
            'coin_earned': coin_earned
        }).execute()
    except Exception as e:
        logger.error(f"활동 기록(log_activity) 중 오류가 발생했습니다: {e}", exc_info=True)

@supabase_retry_handler()
async def get_all_user_stats(user_id: int) -> Dict[str, Any]:
    try:
        user_id_str = str(user_id)
        daily_task = supabase.table('daily_stats').select('*').eq('user_id', user_id_str).maybe_single().execute()
        weekly_task = supabase.table('weekly_stats').select('*').eq('user_id', user_id_str).maybe_single().execute()
        monthly_task = supabase.table('monthly_stats').select('*').eq('user_id', user_id_str).maybe_single().execute()
        total_task = supabase.table('total_stats').select('*').eq('user_id', user_id_str).maybe_single().execute()
        daily_res, weekly_res, monthly_res, total_res = await asyncio.gather(daily_task, weekly_task, monthly_task, total_task)
        stats = {
            "daily": daily_res.data if daily_res and hasattr(daily_res, 'data') and daily_res.data else {},
            "weekly": weekly_res.data if weekly_res and hasattr(weekly_res, 'data') and weekly_res.data else {},
            "monthly": monthly_res.data if monthly_res and hasattr(monthly_res, 'data') and monthly_res.data else {},
            "total": total_res.data if total_res and hasattr(total_res, 'data') and total_res.data else {}
        }
        return stats
    except Exception as e:
        logger.error(f"전체 유저 통계 VIEW 조회 중 오류가 발생했습니다: {e}")
        return {}

# --- ▼▼▼▼▼ 핵심 수정 시작 ▼▼▼▼▼ ---
@supabase_retry_handler()
async def log_chest_reward(user_id: int, chest_type: str, contents: Dict[str, Any]):
    """
    유저가 획득한 보물 상자의 내용물을 DB에 기록합니다.
    """
    await supabase.table('user_chests').insert({
        "user_id": user_id,
        "chest_type": chest_type,
        "contents": contents
    }).execute()


# --- ▼▼▼▼▼ 핵심 수정 시작 ▼▼▼▼▼ ---
@supabase_retry_handler()
async def open_boss_chest(user_id: int, chest_type: str) -> Optional[Dict[str, Any]]:
    """
    [수정됨] DB 함수 대신 Python에서 직접 상자를 열고 내용물을 처리합니다.
    1. 유저가 해당 타입의 상자를 가지고 있는지 확인합니다.
    2. 상자가 있다면, 내용물을 가져오고 DB에서 해당 상자 기록을 삭제합니다. (트랜잭션 효과)
    3. 내용물을 반환합니다.
    """
    try:
        # 1. 유저가 열 수 있는 해당 타입의 상자가 있는지 확인하고 가져옵니다.
        chest_res = await supabase.table('user_chests').select('*').eq('user_id', user_id).eq('chest_type', chest_type).limit(1).maybe_single().execute()

        if not (chest_res and chest_res.data):
            logger.warning(f"상자 열기 시도: 유저(ID:{user_id})가 '{chest_type}'을(를) 가지고 있지 않습니다.")
            return None

        chest_to_open = chest_res.data
        chest_id = chest_to_open['id']
        contents = chest_to_open.get('contents')
        
        # 2. 내용물을 가져온 후, DB에서 해당 상자 기록을 삭제합니다.
        await supabase.table('user_chests').delete().eq('id', chest_id).execute()
        
        logger.info(f"상자 열기 성공: 유저(ID:{user_id})가 chest_id:{chest_id} ('{chest_type}')를 열었습니다. 내용물: {contents}")

        # 3. 내용물 반환
        return contents

    except Exception as e:
        # 오류 발생 시 어떤 상자를 열려고 했는지 명확히 로깅합니다.
        logger.error(f"open_boss_chest 함수 실행 중 오류 발생! user_id: {user_id}, chest_type: {chest_type}", exc_info=True)
        # 실패 시에는 상자가 삭제되지 않으므로, 유저는 아이템을 잃지 않습니다.
        return None
# --- ▲▲▲▲▲ 핵심 수정 종료 ▲▲▲▲▲ ---


@supabase_retry_handler()
async def get_farm_data(user_id: int) -> Optional[Dict[str, Any]]:
    response = await supabase.table('farms').select('*, farm_plots(*)').eq('user_id', str(user_id)).maybe_single().execute()
    return response.data if response and hasattr(response, 'data') else None

@supabase_retry_handler()
async def create_farm(user_id: int) -> Optional[Dict[str, Any]]:
    rpc_response = await supabase.rpc('create_farm_for_user', {'p_user_id': str(user_id)}).execute()
    return await get_farm_data(user_id) if rpc_response and rpc_response.data else None

@supabase_retry_handler()
async def expand_farm_db(farm_id: int, current_plot_count: int) -> bool:
    if current_plot_count >= 25: return False
    try:
        new_pos_x = current_plot_count % 5; new_pos_y = current_plot_count // 5
        await supabase.table('farm_plots').insert({'farm_id': farm_id, 'pos_x': new_pos_x, 'pos_y': new_pos_y, 'state': 'default'}).execute()
        return True
    except Exception as e:
        logger.error(f"농장 확장 DB 작업(farm_id: {farm_id}) 중 오류: {e}", exc_info=True)
        return False

@supabase_retry_handler()
async def update_plot(plot_id: int, updates: Dict[str, Any]):
    await supabase.table('farm_plots').update(updates).eq('id', plot_id).execute()

@supabase_retry_handler()
async def clear_plots_db(plot_ids: List[int]):
    await supabase.rpc('clear_plots_to_default', {'p_plot_ids': plot_ids}).execute()

@supabase_retry_handler()
async def check_farm_permission(farm_id: int, user_id: int, action: str) -> bool:
    permission_column = f"can_{action}"
    response = await supabase.table('farm_permissions').select(permission_column, count='exact').eq('farm_id', farm_id).eq('granted_to_user_id', str(user_id)).eq(permission_column, True).execute()
    return response.count > 0

@supabase_retry_handler()
async def grant_farm_permission(farm_id: int, user_id: int):
    await supabase.table('farm_permissions').upsert({'farm_id': farm_id, 'granted_to_user_id': str(user_id), 'can_till': True, 'can_plant': True, 'can_water': True, 'can_harvest': True}, on_conflict='farm_id, granted_to_user_id').execute()

@supabase_retry_handler()
async def get_farm_owner_by_thread(thread_id: int) -> Optional[int]:
    response = await supabase.table('farms').select('user_id').eq('thread_id', thread_id).maybe_single().execute()
    return response.data['user_id'] if response and hasattr(response, 'data') and response.data else None

@supabase_retry_handler()
async def get_farmable_item_info(item_name: str) -> Optional[Dict[str, Any]]:
    response = await supabase.table('farm_item_details').select('*').eq('item_name', item_name).maybe_single().execute()
    return response.data if response and hasattr(response, 'data') else None

# --- ▼▼▼▼▼ 핵심 추가 시작 ▼▼▼▼▼ ---
@supabase_retry_handler()
async def add_xp_to_pet_db(user_id: int, xp_to_add: int) -> Optional[List[Dict]]:
    """
    펫에게 경험치를 추가하는 DB 함수를 안전하게 호출합니다.
    user_id를 문자열로 변환하여 함수 오버로딩 모호성을 해결합니다.
    """
    if xp_to_add <= 0:
        return None
    try:
        res = await supabase.rpc('add_xp_to_pet', {
            'p_user_id': str(user_id), 
            'p_xp_to_add': xp_to_add
        }).execute()
        return res.data if res and hasattr(res, 'data') else None
    except APIError as e:
        logger.error(f"add_xp_to_pet_db RPC 실행 중 오류 (User: {user_id}): {e}", exc_info=True)
        return None

# --- 펫 탐사 시스템을 위한 새로운 함수들 ---

@supabase_retry_handler()
async def start_pet_exploration(pet_id: int, user_id: int, location_key: str, start_time: datetime, end_time: datetime) -> Optional[Dict]:
    """새로운 펫 탐사를 시작하고, pets 테이블 상태를 업데이트합니다."""
    # 1. pet_explorations 테이블에 새로운 탐사 기록 생성
    exploration_res = await supabase.table('pet_explorations').insert({
        'pet_id': pet_id,
        'user_id': str(user_id),
        'location_key': location_key,
        'start_time': start_time.isoformat(),
        'end_time': end_time.isoformat()
    }).execute()

    if not (exploration_res and exploration_res.data):
        logger.error(f"펫 탐사 기록 생성 실패 (Pet ID: {pet_id})")
        return None
    
    new_exploration = exploration_res.data[0]
    
    # 2. pets 테이블의 상태를 'exploring'으로 업데이트
    await supabase.table('pets').update({
        'status': 'exploring',
        'exploration_end_time': end_time.isoformat(),
        'current_exploration_id': new_exploration['id']
    }).eq('id', pet_id).execute()

    return new_exploration

@supabase_retry_handler()
async def get_completed_explorations() -> List[Dict]:
    """완료되었지만 아직 알림이 가지 않은 탐사 목록을 가져옵니다."""
    now = datetime.now(timezone.utc).isoformat()
    response = await supabase.table('pet_explorations').select('*').is_('completion_message_id', None).lte('end_time', now).execute()
    return response.data if response and response.data else []

@supabase_retry_handler()
async def update_exploration_message_id(exploration_id: int, message_id: int):
    """탐사 완료 메시지 ID를 DB에 기록합니다."""
    await supabase.table('pet_explorations').update({'completion_message_id': str(message_id)}).eq('id', exploration_id).execute()

@supabase_retry_handler()
async def get_exploration_by_id(exploration_id: int) -> Optional[Dict]:
    """특정 ID의 탐사 정보를 가져옵니다. (수정된 버전)"""
    # 1. 기본 탐사 정보 조회
    exp_res = await supabase.table('pet_explorations').select('*').eq('id', exploration_id).maybe_single().execute()
    if not (exp_res and exp_res.data):
        return None
    exploration_data = exp_res.data

    # 2. 관련 펫 및 지역 정보 별도 조회
    pet_task = supabase.table('pets').select('level').eq('id', exploration_data['pet_id']).maybe_single().execute()
    loc_task = supabase.table('exploration_locations').select('*').eq('location_key', exploration_data['location_key']).maybe_single().execute()
    
    pet_res, loc_res = await asyncio.gather(pet_task, loc_task)
    
    # 3. 결과 조합
    exploration_data['pets'] = pet_res.data if pet_res and pet_res.data else {}
    exploration_data['exploration_locations'] = loc_res.data if loc_res and loc_res.data else {}
    
    return exploration_data

@supabase_retry_handler()
async def claim_and_end_exploration(exploration_id: int, pet_id: int) -> bool:
    """탐사 보상을 수령하고, 펫의 상태를 되돌린 후 탐사 기록을 삭제합니다."""
    try:
        # 1. 펫 상태를 'idle'로 되돌림
        await supabase.table('pets').update({
            'status': 'idle',
            'exploration_end_time': None,
            'current_exploration_id': None
        }).eq('id', pet_id).execute()

        # 2. 탐사 기록 삭제
        await supabase.table('pet_explorations').delete().eq('id', exploration_id).execute()
        return True
    except Exception as e:
        logger.error(f"탐사 보상 수령 처리(ID: {exploration_id}) 중 DB 오류: {e}", exc_info=True)
        return False

