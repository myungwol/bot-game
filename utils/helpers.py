# bot-game/utils/helpers.py

import discord
from discord import ui
import copy
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# [✅ 수정] 모든 파일에서 공통으로 사용할 안정적인 CloseButtonView를 여기에 표준으로 정의합니다.
class CloseButtonView(ui.View):
    def __init__(self, user: discord.User, target_message: Optional[discord.Message] = None):
        """
        범용 닫기 버튼 View 입니다.
        :param user: 이 버튼을 누를 수 있는 유저입니다.
        :param target_message: None이 아니면, 이 메시지를 삭제합니다. None이면 버튼이 붙어있는 메시지를 삭제합니다.
        """
        super().__init__(timeout=180)
        self.user = user
        self.target_message = target_message

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # 이 상호작용을 시작한 유저만 버튼을 누를 수 있도록 권한을 확인합니다.
        if interaction.user.id != self.user.id:
            # 다른 사람이 누를 경우, 아무런 응답도 보내지 않고 상호작용을 조용히 무시합니다.
            return False
        return True
        
    @ui.button(label="閉じる", style=discord.ButtonStyle.secondary)
    async def close_button(self, interaction: discord.Interaction, button: ui.Button):
        try:
            await interaction.response.defer()
            message_to_delete = self.target_message or interaction.message
            if message_to_delete:
                await message_to_delete.delete()
        except discord.NotFound:
            pass # 메시지가 이미 삭제된 경우 조용히 넘어갑니다.
        except Exception as e:
            logger.error(f"닫기 버튼 처리 중 예외 발생: {e}", exc_info=True)


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
