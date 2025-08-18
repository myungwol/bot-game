
# bot-game/cogs/system/panel_updater.py

import discord
from discord.ext import commands, tasks
import logging
from datetime import datetime, timezone

from utils.database import get_config, get_id

logger = logging.getLogger(__name__)

class PanelUpdater(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 각 패널의 마지막 확인 타임스탬프를 저장 (메모리 캐시)
        self.last_checked_timestamps = {}
        logger.info("PanelUpdater Cog가 성공적으로 초기화되었습니다.")

    def cog_unload(self):
        self.check_for_panel_updates.cancel()

    @tasks.loop(seconds=10.0)
    async def check_for_panel_updates(self):
        # 패널을 관리하는 Cog와 DB 키, 채널 설정 키를 매핑
        panel_map = {
            "commerce": {"cog_name": "Commerce", "channel_key": "commerce_panel_channel_id"},
            "fishing": {"cog_name": "Fishing", "channel_key": "fishing_panel_channel_id"},
            "profile": {"cog_name": "UserProfile", "channel_key": "profile_panel_channel_id"},
        }

        for panel_key, info in panel_map.items():
            db_key = f"panel_regenerate_request_{panel_key}"
            
            # DB에서 최신 요청 타임스탬프를 가져옴
            request_timestamp = get_config(db_key)
            if not request_timestamp:
                continue

            # 로컬에 저장된 마지막 확인 타임스탬프와 비교
            last_checked = self.last_checked_timestamps.get(db_key, 0)
            
            if float(request_timestamp) > last_checked:
                logger.info(f"DB에서 `{panel_key}` 패널에 대한 새로운 재설치 요청을 발견했습니다. (요청 시간: {request_timestamp})")
                
                # 업데이트가 필요하므로, 로컬 타임스탬프를 갱신
                self.last_checked_timestamps[db_key] = float(request_timestamp)

                # 패널 재생성 로직 실행
                cog = self.bot.get_cog(info["cog_name"])
                channel_id = get_id(info["channel_key"])

                if not cog or not hasattr(cog, 'regenerate_panel'):
                    logger.error(f"'{info['cog_name']}' Cog를 찾을 수 없거나 'regenerate_panel' 함수가 없습니다.")
                    continue
                
                if not channel_id or not (channel := self.bot.get_channel(channel_id)):
                    logger.error(f"'{panel_key}' 패널의 채널(ID: {channel_id})을 찾을 수 없습니다. `/setup`으로 채널을 먼저 설정해주세요.")
                    continue
                
                try:
                    await cog.regenerate_panel(channel)
                    logger.info(f"✅ `{panel_key}` 패널을 성공적으로 재설치했습니다.")
                except Exception as e:
                    logger.error(f"'{panel_key}' 패널 재설치 중 오류 발생: {e}", exc_info=True)

    @check_for_panel_updates.before_loop
    async def before_check_loop(self):
        # 봇이 완전히 준비될 때까지 대기
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(PanelUpdater(bot))
