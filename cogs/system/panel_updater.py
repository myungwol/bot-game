# game-bot/cogs/system/panel_updater.py

import discord
from discord.ext import commands
import logging
from typing import List, Dict, Any
import asyncio

from utils.database import get_config, get_id

logger = logging.getLogger(__name__)

class PanelUpdater(commands.Cog):
    """
    관리자 봇으로부터 오는 각종 패널 재생성 요청을 감지하고 처리하는 전용 Cog입니다.
    EconomyCore의 unified_request_dispatcher로부터 작업을 위임받습니다.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("PanelUpdater Cog (패널 재생성 요청 처리기)가 성공적으로 초기화되었습니다.")

    async def process_panel_regenerate_requests(self, requests: List[Dict[str, Any]]):
        """
        패널 재생성 요청 목록을 받아 순차적으로 처리합니다.
        
        Args:
            requests (List[Dict[str, Any]]): DB에서 가져온 요청 데이터 목록
        """
        if not requests:
            return

        logger.info(f"[PanelUpdater] {len(requests)}개의 패널 재생성 요청을 처리합니다.")
        
        # 전체 설정 맵을 한 번만 불러옵니다.
        setup_command_map = get_config("SETUP_COMMAND_MAP", {})
        if not setup_command_map:
            logger.error("[PanelUpdater] SETUP_COMMAND_MAP 설정을 DB에서 찾을 수 없어 패널 재생성을 진행할 수 없습니다.")
            return

        for req in requests:
            try:
                db_key = req.get('config_key')
                if not db_key or not db_key.startswith('panel_regenerate_request_'):
                    continue

                # 'panel_regenerate_request_' 접두사를 제거하여 실제 패널 키를 추출합니다.
                # 예: panel_regenerate_request_panel_friend_invite -> panel_friend_invite
                panel_key = db_key.replace('panel_regenerate_request_', '', 1)
                
                panel_config = setup_command_map.get(panel_key)
                if not panel_config:
                    logger.warning(f"[PanelUpdater] '{panel_key}'에 대한 설정을 찾을 수 없어 건너뜁니다.")
                    continue

                cog_name = panel_config.get("cog_name")
                channel_db_key = panel_config.get("key")
                friendly_name = panel_config.get("friendly_name", panel_key)

                if not all([cog_name, channel_db_key]):
                    logger.error(f"[PanelUpdater] '{friendly_name}' 패널의 설정 정보(Cog 또는 채널 키)가 불완전합니다.")
                    continue

                target_cog = self.bot.get_cog(cog_name)
                if not target_cog:
                    logger.warning(f"[PanelUpdater] '{friendly_name}' 패널을 담당하는 '{cog_name}' Cog를 찾을 수 없습니다. 로드되었는지 확인해주세요.")
                    continue
                
                if not hasattr(target_cog, 'regenerate_panel'):
                    logger.error(f"[PanelUpdater] '{cog_name}' Cog에 'regenerate_panel' 메소드가 없어 '{friendly_name}' 패널을 재생성할 수 없습니다.")
                    continue

                channel_id = get_id(channel_db_key)
                if not channel_id or not (target_channel := self.bot.get_channel(channel_id)):
                    logger.warning(f"[PanelUpdater] '{friendly_name}' 패널이 설치될 채널(Key: {channel_db_key})을 찾을 수 없습니다. 채널이 설정되었는지 확인해주세요.")
                    continue

                logger.info(f"[PanelUpdater] '{friendly_name}' 패널 재설치를 시작합니다... (대상 채널: #{target_channel.name})")
                await target_cog.regenerate_panel(target_channel, panel_key=panel_key)
                
                # 요청 사이에 약간의 딜레이를 주어 API 제한을 피합니다.
                await asyncio.sleep(2)

            except Exception as e:
                logger.error(f"[PanelUpdater] 패널 재설치 요청 처리 중 오류 발생 (Request: {req}): {e}", exc_info=True)


async def setup(bot: commands.Bot):
    """Cog를 봇에 추가합니다."""
    await bot.add_cog(PanelUpdater(bot))
