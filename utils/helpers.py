# bot-game/utils/helpers.py

import discord
from discord import ui
import copy
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# [✅ 수정] 더 이상 사용하지 않는 CloseButtonView 클래스를 완전히 제거합니다.

def format_embed_from_db(embed_data: Dict[str, Any], **kwargs: Any) -> discord.Embed:
    if not isinstance(embed_data, dict):
        logger.error(f"임베드 데이터가 dict 형식이 아닙니다. 실제 타입: {type(embed_data)}")
        return discord.Embed(title="오류 발생", description="임베드 데이터를 불러오는 데 실패했습니다.", color=discord.Color.red())
    formatted_data: Dict[str, Any] = copy.deepcopy(embed_data)
    class SafeFormatter(dict):
        def __missing__(self, key: str) -> str:
            return f'{{{key}}}'
    safe_kwargs = SafeFormatter(**kwargs)
    try:
        if formatted_data.get('title') and isinstance(formatted_data['title'], str):
            formatted_data['title'] = formatted_data['title'].format_map(safe_kwargs)
        if formatted_data.get('description') and isinstance(formatted_data['description'], str):
            formatted_data['description'] = formatted_data['description'].format_map(safe_kwargs)
        if formatted_data.get('footer') and isinstance(formatted_data.get('footer'), dict):
            if formatted_data['footer'].get('text') and isinstance(formatted_data['footer']['text'], str):
                formatted_data['footer']['text'] = formatted_data['footer']['text'].format_map(safe_kwargs)
        if formatted_data.get('fields') and isinstance(formatted_data.get('fields'), list):
            for field in formatted_data['fields']:
                if isinstance(field, dict):
                    if field.get('name') and isinstance(field['name'], str): field['name'] = field['name'].format_map(safe_kwargs)
                    if field.get('value') and isinstance(field.get('value'), str): field['value'] = field['value'].format_map(safe_kwargs)
        return discord.Embed.from_dict(formatted_data)
    except (KeyError, ValueError) as e:
        logger.error(f"임베드 데이터 포맷팅 중 오류 발생: {e}", exc_info=True)
        try: return discord.Embed.from_dict(embed_data)
        except Exception as final_e:
            logger.critical(f"원본 임베드 데이터로도 임베드 생성 실패: {final_e}", exc_info=True)
            return discord.Embed(title="치명적 오류", description="임베드 생성에 실패했습니다. 데이터 형식을 확인해주세요.", color=discord.Color.dark_red())
