# utils/helpers.py (양쪽 봇 공용)
"""
봇 프로젝트 전반에서 사용되는 보조 함수들을 모아놓은 파일입니다.
"""
import discord
import copy
import logging
from typing import Any, Dict, List, Optional
import re
from .database import get_config, get_id

logger = logging.getLogger(__name__)

# [✅✅✅ 핵심 추가 ✅✅✅]
# 초를 "X시간 Y분 Z초" 형식으로 변환하는 함수
def format_seconds_to_hms(seconds: float) -> str:
    """초를 시, 분, 초 형식의 문자열로 변환합니다."""
    if seconds <= 0:
        return "0秒"
    
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    
    parts = []
    if hours > 0:
        parts.append(f"{hours}時間")
    if minutes > 0:
        parts.append(f"{minutes}分")
    if secs > 0 or not parts: # 남은 시간이 0초이거나, 전체가 1분 미만일 때 초를 표시
        parts.append(f"{secs}秒")
        
    return ' '.join(parts)
    
# ▼▼▼ [핵심 추가] 중앙 집중식 권한 확인 함수 ▼▼▼
async def has_required_roles(interaction: discord.Interaction, required_keys: List[str], error_message: str = "❌ このボタンを押す権限がありません。") -> bool:
    """
    사용자가 필요한 역할 중 하나 이상을 가지고 있는지 확인하는 중앙 함수.
    서버 소유자는 항상 통과됩니다.
    """
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ サーバーのメンバーではないため、権限を確認できません。", ephemeral=True)
        return False

    # 서버 소유자는 모든 권한을 가집니다.
    if interaction.user.id == interaction.guild.owner_id:
        return True

    # 필요한 역할 ID들을 DB에서 가져옵니다.
    allowed_role_ids = {get_id(key) for key in required_keys if get_id(key)}
    
    if not allowed_role_ids:
        await interaction.response.send_message("❌ 権限の確認に必要な役職がサーバーに設定されていません。管理者に問い合わせてください。", ephemeral=True)
        return False

    # 사용자가 가진 역할 ID와 비교합니다.
    user_role_ids = {role.id for role in interaction.user.roles}
    if not user_role_ids.intersection(allowed_role_ids):
        await interaction.response.send_message(error_message, ephemeral=True)
        return False
        
    return True
# ▲▲▲ [핵심 추가] ▲▲▲

def format_embed_from_db(embed_data: Dict[str, Any], **kwargs: Any) -> discord.Embed:
    if not isinstance(embed_data, dict):
        logger.error(f"임베드 데이터가 dict 형식이 아닙니다. 실제 타입: {type(embed_data)}")
        return discord.Embed(title="エラー発生", description="埋め込みデータの読み込みに失敗しました。", color=discord.Color.red())
    
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
                    if field.get('name') and isinstance(field['name'], str):
                        field['name'] = field['name'].format_map(safe_kwargs)
                    if field.get('value') and isinstance(field['value'], str):
                        field['value'] = field['value'].format_map(safe_kwargs)
        return discord.Embed.from_dict(formatted_data)
    except (KeyError, ValueError) as e:
        logger.error(f"임베드 데이터 포맷팅 중 오류 발생: {e}", exc_info=True)
        try:
            return discord.Embed.from_dict(embed_data)
        except Exception as final_e:
            logger.critical(f"원본 임베드 데이터로도 임베드 생성 실패: {final_e}", exc_info=True)
            return discord.Embed(title="致命的なエラー", description="埋め込みの作成に失敗しました。データ形式を確認してください。", color=discord.Color.dark_red())

def get_clean_display_name(member: discord.Member) -> str:
    display_name = member.display_name
    prefix_hierarchy = get_config("NICKNAME_PREFIX_HIERARCHY", [])
    for prefix_name in prefix_hierarchy:
        prefix_to_check = f"『 {prefix_name} 』"
        if display_name.startswith(prefix_to_check):
            return re.sub(rf"^{re.escape(prefix_to_check)}\s*", "", display_name).strip()
    return display_name

def calculate_xp_for_level(level: int) -> int:
    if level <= 1:
        return 0
    total_xp = 0
    for l in range(1, level):
        total_xp += 5 * (l ** 2) + (50 * l) + 100
    return total_xp

# --- ▼▼▼ [누락된 함수 복원] ▼▼▼ ---
def create_bar(current: int, required: int, length: int = 10, full_char: str = '▓', empty_char: str = '░') -> str:
    if required <= 0: return full_char * length
    progress = min(current / required, 1.0)
    filled_length = int(length * progress)
    return f"[{full_char * filled_length}{empty_char * (length - filled_length)}]"
# --- ▲▲▲ [복원 완료] ▲▲▲ ---

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
