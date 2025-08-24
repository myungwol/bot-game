# game-bot/utils/helpers.py
import discord
import copy
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

def format_embed_from_db(embed_data: Dict[str, Any], **kwargs: Any) -> discord.Embed:
    if not isinstance(embed_data, dict):
        logger.error(f"임베드 데이터가 dict 형식이 아닙니다. 타입: {type(embed_data)}")
        return discord.Embed(title="오류", description="임베드 로딩 실패.", color=discord.Color.red())
    
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
        logger.error(f"임베드 포맷팅 중 오류: {e}", exc_info=True)
        return discord.Embed(title="오류", description="임베드 포맷팅 실패.", color=discord.Color.red())

def calculate_xp_for_level(level: int) -> int:
    """
    특정 레벨에 도달하기 위해 필요한 *총* 경험치를 계산합니다.
    (예: level=3을 입력하면, Lv.1 -> Lv.2 -> Lv.3에 필요한 총 XP가 반환됩니다.)
    """
    if level <= 1: 
        return 0
        
    total_xp = 0
    for l in range(1, level):
        xp_for_this_level = 3 * (l ** 2) + (10 * l) + 37
        total_xp += xp_for_this_level
        
    return total_xp
