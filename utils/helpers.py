# game-bot/utils/helpers.py
import discord
import copy
import logging
from typing import Any, Dict
from datetime import datetime, timezone, timedelta # ◀ timedelta 추가

logger = logging.getLogger(__name__)

def format_embed_from_db(embed_data: Dict[str, Any], **kwargs: Any) -> discord.Embed:
    if not isinstance(embed_data, dict):
        logger.error(f"임베드 데이터가 딕셔너리(dict) 형식이 아닙니다. 타입: {type(embed_data)}")
        return discord.Embed(title="오류", description="임베드 데이터를 불러오는 데 실패했습니다.", color=discord.Color.red())
    
    formatted_data = copy.deepcopy(embed_data)
    class SafeFormatter(dict):
        def __missing__(self, key: str) -> str: return f'{{{key}}}'
    safe_kwargs = SafeFormatter(**kwargs)
    
    try:
        if 'title' in formatted_data and isinstance(formatted_data['title'], str):
            formatted_data['title'] = formatted_data['title'].format_map(safe_kwargs)
        if 'description' in formatted_data and isinstance(formatted_data['description'], str):
            formatted_data['description'] = formatted_data['description'].format_map(safe_kwargs)
        if 'footer' in formatted_data and isinstance(formatted_data.get('footer'), dict):
            if 'text' in formatted_data['footer'] and isinstance(formatted_data['footer']['text'], str):
                formatted_data['footer']['text'] = formatted_data['footer']['text'].format_map(safe_kwargs)
        if 'fields' in formatted_data and isinstance(formatted_data.get('fields'), list):
            for field in formatted_data['fields']:
                if isinstance(field, dict):
                    if 'name' in field and isinstance(field['name'], str):
                        field['name'] = field['name'].format_map(safe_kwargs)
                    if 'value' in field and isinstance(field['value'], str):
                        field['value'] = field['value'].format_map(safe_kwargs)
        return discord.Embed.from_dict(formatted_data)
    except Exception as e:
        logger.error(f"임베드 포맷팅 중 오류가 발생했습니다: {e}", exc_info=True)
        return discord.Embed(title="오류", description="임베드 형식을 만드는 데 실패했습니다.", color=discord.Color.red())

def calculate_xp_for_level(level: int) -> int:
    """
    특정 레벨에 도달하기 위해 필요한 *총* 경험치를 계산합니다.
    (예: level=3을 입력하면, Lv.1에서 Lv.3까지 도달하는 데 필요한 총 XP가 반환됩니다.)
    """
    if level <= 1: 
        return 0
        
    total_xp = 0
    for l in range(1, level):
        xp_for_this_level = 3 * (l ** 2) + (10 * l) + 37
        total_xp += xp_for_this_level
        
    return total_xp

# ▼▼▼ [핵심 수정] 아래 함수를 파일 맨 끝에 추가해주세요. ▼▼▼
def format_timedelta_minutes_seconds(delta: timedelta) -> str:
    """timedelta를 'N분 M초' 형식의 문자열로 변환합니다."""
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "종료됨"
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}분 {seconds}초"
# ▲▲▲ [핵심 수정] ▲▲▲

# ▼ [helpers.py 맨 아래에 추가] ▼
def coerce_item_emoji(value):
    """
    DB에서 읽은 emoji 값이 유니코드('🐟')면 그대로,
    커스텀 이모지 마크업('<:name:id>' 또는 '<a:name:id>')이면 PartialEmoji로 변환.
    SelectOption/Button 등 discord.py 컴포넌트의 'emoji' 파라미터에서 안전하게 사용 가능.
    """
    if not value:
        return None
    try:
        # discord.PartialEmoji는 '<:name:id>' 형태를 제대로 파싱함
        if isinstance(value, str) and value.startswith("<") and value.endswith(">"):
            return discord.PartialEmoji.from_str(value)
    except Exception:
        # 문제가 있으면 그냥 원본(유니코드 같은)을 돌려준다
        return value
    return value
# ▲ [helpers.py 추가 끝] ▲
