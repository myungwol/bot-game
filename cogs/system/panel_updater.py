# cogs/panel_updater.py

import discord
from discord.ext import commands, tasks
import logging
import asyncio

# [✅✅✅ 핵심 수정 ✅✅✅] 실시간으로 DB 정보를 다시 불러올 함수를 import 합니다.
from utils.database import supabase, get_id, load_channel_ids_from_db, get_config

logger = logging.getLogger(__name__)

class PanelUpdater(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_for_panel_updates.start()
        logger.info("PanelUpdater Cog가 성공적으로 초기화되었습니다.")

    def cog_unload(self):
        self.check_for_panel_updates.cancel()

    @tasks.loop(seconds=10.0)
    async def check_for_panel_updates(self):
        # [✅ 구조 개선] 하드코딩된 목록 대신 DB의 SETUP_COMMAND_MAP을 사용합니다.
        setup_map = get_config("SETUP_COMMAND_MAP", {})
        if not setup_map:
            return

        game_panels = {
            key: info for key, info in setup_map.items()
            if info.get("type") == "panel" and "[게임]" in info.get("friendly_name", "")
        }

        try:
            request_keys = [f"panel_regenerate_request_{key}" for key in game_panels.keys()]
            if not request_keys:
                return

            response = await supabase.table('bot_configs').select('config_key').in_('config_key', request_keys).execute()
            
            if not response or not response.data:
                return

            db_requests = {item['config_key'] for item in response.data}
            
            # [✅✅✅ 핵심 수정: 레이스 컨디션 해결 ✅✅✅]
            # 재설치 요청이 하나라도 있다면, DB에서 최신 채널 ID 목록을 즉시 새로고침합니다.
            if db_requests:
                logger.info("새로운 패널 재설치 요청을 감지하여, DB로부터 모든 채널 ID를 새로고침합니다.")
                await load_channel_ids_from_db()

        except Exception as e:
            logger.error(f"패널 업데이트 요청 확인 중 DB 오류 발생: {e}", exc_info=True)
            return

        tasks_to_run = []
        keys_to_delete = []

        for panel_key, info in game_panels.items():
            db_key = f"panel_regenerate_request_{panel_key}"
            
            if db_key in db_requests:
                logger.info(f"DB에서 `{panel_key}` 패널에 대한 재설치 요청을 발견했습니다.")
                
                cog = self.bot.get_cog(info["cog_name"])
                # 이제 이 get_id는 방금 새로고침된 최신 정보를 사용합니다.
                channel_id = get_id(info["key"])

                if not cog or not hasattr(cog, 'regenerate_panel'):
                    logger.error(f"'{info['cog_name']}' Cog를 찾을 수 없거나 'regenerate_panel' 함수가 없습니다.")
                    continue
                
                if not channel_id or not (channel := self.bot.get_channel(channel_id)):
                    logger.error(f"'{panel_key}' 패널의 채널(ID: {channel_id or 'None'})을 찾을 수 없습니다. `/setup`으로 채널을 먼저 설정해주세요.")
                    continue
                
                # 비동기 작업을 리스트에 추가
                tasks_to_run.append(cog.regenerate_panel(channel, panel_key=panel_key))
                keys_to_delete.append(db_key)

        if tasks_to_run:
            results = await asyncio.gather(*tasks_to_run, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    panel_key_for_error = keys_to_delete[i].replace("panel_regenerate_request_", "")
                    logger.error(f"'{panel_key_for_error}' 패널 재설치 중 오류 발생: {result}", exc_info=result)
        
        if keys_to_delete:
            try:
                await supabase.table('bot_configs').delete().in_('config_key', keys_to_delete).execute()
                logger.info(f"DB에서 처리 완료된 요청 키 {len(keys_to_delete)}개를 삭제했습니다.")
            except Exception as e:
                logger.error(f"처리 완료된 패널 요청 키 삭제 중 오류: {e}", exc_info=True)


    @check_for_panel_updates.before_loop
    async def before_check_loop(self):
        await self.bot.wait_until_ready()
        # 봇 시작 시, DB에서 SETUP_COMMAND_MAP을 로드할 시간을 줍니다.
        await asyncio.sleep(5) 

async def setup(bot: commands.Bot):
    await bot.add_cog(PanelUpdater(bot))
