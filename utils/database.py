# game-bot/utils/database.py
import os
import asyncio
import logging
import time
from functools import wraps
from datetime import datetime, timezone, timedelta
from typing import Dict, Callable, Any, List, Optional

import discord
from supabase import create_client, AsyncClient

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
    logger.critical(f"❌ Supabase 클라이언트 생성 실패: {e}", exc_info=True)

_bot_configs_cache: Dict[str, Any] = {}
_channel_id_cache: Dict[str, int] = {}
_item_database_cache: Dict[str, Dict[str, Any]] = {}
_fishing_loot_cache: List[Dict[str, Any]] = {}
_user_abilities_cache: Dict[int, tuple[List[str], float]] = {}
JST = timezone(timedelta(hours=9))
BARE_HANDS = "素手"
DEFAULT_ROD = "普通の釣竿"

def supabase_retry_handler(retries: int = 3, delay: int = 2):
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not supabase: return None
            for attempt in range(retries):
                try: return await func(*args, **kwargs)
                except Exception as e:
                    if attempt < retries - 1: await asyncio.sleep(delay)
            return None
        return wrapper
    return decorator

async def load_all_data_from_db():
    logger.info("------ [ 모든 DB 데이터 캐시 로드 시작 ] ------")
    await asyncio.gather(load_bot_configs_from_db(), load_channel_ids_from_db(), load_game_data_from_db())
    logger.info("------ [ 모든 DB 데이터 캐시 로드 완료 ] ------")

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

def get_config(key: str, default: Any = None) -> Any: return _bot_configs_cache.get(key, default)
def get_id(key: str) -> Optional[int]: return _channel_id_cache.get(key)
def get_item_database() -> Dict[str, Dict[str, Any]]: return _item_database_cache
def get_fishing_loot() -> List[Dict[str, Any]]: return _fishing_loot_cache

@supabase_retry_handler()
async def save_config_to_db(key: str, value: Any):
    global _bot_configs_cache
    await supabase.table('bot_configs').upsert({"config_key": key, "config_value": value}).execute()
    _bot_configs_cache[key] = value

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
    response = await supabase.table(table_name).select("*").eq("user_id", user_id).maybe_single().execute()
    if response and response.data: return response.data
    insert_data = {"user_id": user_id, **default_data}
    response = await supabase.table(table_name).insert(insert_data).select().maybe_single().execute()
    return response.data if response and response.data else default_data

async def get_wallet(user_id: int) -> dict:
    return await get_or_create_user('wallets', user_id, {"balance": 0})

@supabase_retry_handler()
async def update_wallet(user: discord.User, amount: int) -> Optional[dict]:
    params = {'p_user_id': user.id, 'p_amount': amount}
    response = await supabase.rpc('update_wallet_balance', params).select().maybe_single().execute()
    return response.data if response and response.data else None

@supabase_retry_handler()
async def get_inventory(user: discord.User) -> Dict[str, int]:
    response = await supabase.table('inventories').select('item_name, quantity').eq('user_id', user.id).gt('quantity', 0).execute()
    return {item['item_name']: item['quantity'] for item in response.data} if response and response.data else {}

@supabase_retry_handler()
async def update_inventory(user_id: int, item_name: str, quantity: int):
    params = {'p_user_id': user_id, 'p_item_name': item_name, 'p_quantity_delta': quantity}
    await supabase.rpc('update_inventory_quantity', params).execute()

@supabase_retry_handler()
async def get_user_gear(user: discord.User) -> dict:
    return await get_or_create_user('gear_setups', user.id, {"rod": "素手", "bait": "エサなし", "hoe": "素手", "watering_can": "素手"})

@supabase_retry_handler()
async def set_user_gear(user_id: int, **kwargs):
    if kwargs:
        await supabase.table('gear_setups').update(kwargs).eq('user_id', user_id).execute()
        
@supabase_retry_handler()
async def get_aquarium(user_id: int) -> list:
    response = await supabase.table('aquariums').select('id, name, size, emoji').eq('user_id', user_id).execute()
    return response.data if response and response.data else []

@supabase_retry_handler()
async def add_to_aquarium(user_id: int, fish_data: dict):
    await supabase.table('aquariums').insert({"user_id": user_id, **fish_data}).execute()

@supabase_retry_handler()
async def sell_fish_from_db(user_id: int, fish_ids: List[int], total_sell_price: int):
    params = {'p_user_id': user_id, 'p_fish_ids': fish_ids, 'p_total_value': total_sell_price}
    await supabase.rpc('sell_fishes', params).execute()

@supabase_retry_handler()
async def get_user_abilities(user_id: int) -> List[str]:
    CACHE_TTL = 300; now = time.time()
    if user_id in _user_abilities_cache:
        cached_data, timestamp = _user_abilities_cache[user_id]
        if now - timestamp < CACHE_TTL: return cached_data
    response = await supabase.rpc('get_user_ability_keys', {'p_user_id': user_id}).execute()
    abilities = response.data if response and hasattr(response, 'data') and response.data else []
    _user_abilities_cache[user_id] = (abilities, now)
    return abilities
    
@supabase_retry_handler()
async def get_cooldown(user_id: int, cooldown_key: str) -> float:
    response = await supabase.table('cooldowns').select('last_cooldown_timestamp').eq('user_id', user_id).eq('cooldown_key', cooldown_key).maybe_single().execute()
    if response and response.data and (ts_str := response.data.get('last_cooldown_timestamp')):
        try: return datetime.fromisoformat(ts_str.replace('Z', '+00:00')).timestamp()
        except (ValueError, TypeError): return 0.0
    return 0.0

@supabase_retry_handler()
async def set_cooldown(user_id: int, cooldown_key: str):
    iso_timestamp = datetime.now(timezone.utc).isoformat()
    await supabase.table('cooldowns').upsert({"user_id": user_id, "cooldown_key": cooldown_key, "last_cooldown_timestamp": iso_timestamp}).execute()

@supabase_retry_handler()
async def log_activity(
    user_id: int, activity_type: str, amount: int = 1,
    xp_earned: int = 0, coin_earned: int = 0
):
    """모든 활동을 DB의 log_activity 함수를 통해 기록합니다."""
    try:
        await supabase.rpc('log_activity', {
            'p_user_id': user_id,
            'p_activity_type': activity_type,
            'p_amount': amount,
            'p_xp_earned': xp_earned,
            'p_coin_earned': coin_earned
        }).execute()
    except Exception as e:
        logger.error(f"활동 기록 RPC(log_activity) 호출 중 오류: {e}", exc_info=True)

@supabase_retry_handler()
async def get_all_user_stats(user_id: int) -> Dict[str, Any]:
    """모든 기간의 유저 통계를 한 번에 가져옵니다."""
    try:
        response = await supabase.rpc('get_all_user_stats', {'p_user_id': user_id}).single().execute()
        return response.data if response and response.data else {}
    except Exception as e:
        logger.error(f"전체 유저 통계(get_all_user_stats) 조회 중 오류: {e}")
        return {}

@supabase_retry_handler()
async def get_farm_data(user_id: int) -> Optional[Dict[str, Any]]:
    response = await supabase.table('farms').select('*, farm_plots(*)').eq('user_id', user_id).maybe_single().execute()
    return response.data if response and hasattr(response, 'data') else None

@supabase_retry_handler()
async def create_farm(user_id: int) -> Optional[Dict[str, Any]]:
    rpc_response = await supabase.rpc('create_farm_for_user', {'p_user_id': user_id}).execute()
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
    response = await supabase.table('farm_permissions').select(permission_column, count='exact').eq('farm_id', farm_id).eq('granted_to_user_id', user_id).eq(permission_column, True).execute()
    return response.count > 0

@supabase_retry_handler()
async def grant_farm_permission(farm_id: int, user_id: int):
    await supabase.table('farm_permissions').upsert({'farm_id': farm_id, 'granted_to_user_id': user_id, 'can_till': True, 'can_plant': True, 'can_water': True, 'can_harvest': True}, on_conflict='farm_id, granted_to_user_id').execute()

@supabase_retry_handler()
async def get_farm_owner_by_thread(thread_id: int) -> Optional[int]:
    response = await supabase.table('farms').select('user_id').eq('thread_id', thread_id).maybe_single().execute()
    return response.data['user_id'] if response and hasattr(response, 'data') and response.data else None

@supabase_retry_handler()
async def get_farmable_item_info(item_name: str) -> Optional[Dict[str, Any]]:
    response = await supabase.table('farm_item_details').select('*').eq('item_name', item_name).maybe_single().execute()
    return response.data if response and hasattr(response, 'data') else None
