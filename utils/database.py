# utils/database.py (최종 통합본 - 양쪽 봇 공용)
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

# ui_defaults는 서버 관리 봇에서만 DB 동기화에 사용되지만,
# 게임 봇에서도 오류 없이 임포트될 수 있도록 try-except 구문을 사용합니다.
try:
    from .ui_defaults import (
        UI_EMBEDS, UI_PANEL_COMPONENTS, UI_ROLE_KEY_MAP, 
        SETUP_COMMAND_MAP, JOB_SYSTEM_CONFIG, AGE_ROLE_MAPPING, GAME_CONFIG,
        ONBOARDING_CHOICES
    )
except ImportError:
    # 게임 봇에서는 이 파일이 없으므로, 비어있는 값으로 설정하여 오류를 방지합니다.
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
# 3. 데이터 로드 및 동기화
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
async def sync_defaults_to_db():
    logger.info("------ [ 기본값 DB 동기화 시작 (서버 관리 봇) ] ------")
    try:
        if not UI_ROLE_KEY_MAP: # 게임 봇에서 호출될 경우 실행하지 않음
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

def get_id(key: str) -> Optional[int]:
    return _channel_id_cache.get(key)

# ... (이후의 모든 함수들을 여기에 포함합니다. 양이 많아 일부만 표시합니다.)
# ... (이전 답변의 `utils/database.py`에서 제공했던 모든 함수가 여기에 포함되어야 합니다.)
# ... (예: get_embed_from_db, update_wallet, get_farm_data, get_wallet 등 모든 함수)

# [✅✅✅ 여기에 모든 함수를 다 넣는 것이 핵심입니다]
# get_embed_from_db, get_cooldown, set_cooldown, add_warning, get_wallet,
# get_farm_data 등 이전 답변에 있던 모든 함수를 여기에 포함시킵니다.
# 아래는 주요 게임 관련 함수들입니다.

@supabase_retry_handler()
async def get_embed_from_db(embed_key: str) -> Optional[dict]:
    response = await supabase.table('embeds').select('embed_data').eq('embed_key', embed_key).limit(1).execute()
    return response.data[0]['embed_data'] if response and response.data else None

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
async def get_farm_data(user_id: int) -> Optional[Dict[str, Any]]:
    response = await supabase.table('farms').select('*, farm_plots(*)').eq('user_id', user_id).maybe_single().execute()
    return response.data if response and hasattr(response, 'data') else None

# ... (나머지 모든 함수들도 여기에 포함되어야 합니다) ...
