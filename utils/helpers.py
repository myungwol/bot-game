# game-bot/utils/helpers.py
import discord
import copy
import logging
from typing import Any, Dict
from datetime import datetime, timezone, timedelta
import re

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

# ▼▼▼ [수정] 플레이어 경험치 공식을 더 완만한 곡선으로 변경합니다. ▼▼▼
def calculate_xp_for_level(level: int) -> int:
    """
    특정 레벨에 도달하기 위해 필요한 *총* 경험치를 계산합니다.
    """
    if level <= 1: 
        return 0
        
    total_xp = 0
    for l in range(1, level):
        # 새로운 공식: 100 * (l^1.4) + 150
        xp_for_this_level = int(100 * (l ** 1.4) + 150)
        total_xp += xp_for_this_level
        
    return total_xp

def format_timedelta_minutes_seconds(delta: timedelta) -> str:
    """timedelta를 'N분 M초' 형식의 문자열로 변환합니다."""
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "종료됨"
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}분 {seconds}초"

def coerce_item_emoji(value):
    """
    [강화된 버전]
    DB에서 읽은 emoji 값에서 유효한 Discord 커스텀 이모지 패턴(<:name:id>)을
    정규식으로 추출하거나, 유니코드 이모지인 경우 그대로 반환합니다.
    데이터에 포함된 보이지 않는 문자나 불필요한 공백을 완벽하게 무시합니다.
    """
    if not value or not isinstance(value, str):
        return None
    
    cleaned_value = value.strip()

    match = re.search(r'<a?:\w+:\d+>', cleaned_value)
    
    if match:
        emoji_str = match.group(0)
        try:
            return discord.PartialEmoji.from_str(emoji_str)
        except Exception:
            return emoji_str
            
    return cleaned_value

def create_bar(current: int, required: int, length: int = 10, full_char: str = '▓', empty_char: str = '░') -> str:
    if required <= 0: return full_char * length
    progress = min(current / required, 1.0)
    filled_length = int(length * progress)
    return f"[{full_char * filled_length}{empty_char * (length - filled_length)}]"
