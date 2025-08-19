import discord
from discord.ext import commands, tasks
import logging

from utils.database import supabase, get_id

logger = logging.getLogger(__name__)

class PanelUpdater(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # [🔴 핵심 변경 1] last_checked_timestamps는 더 이상 필요 없으므로 삭제합니다.
        # self.last_checked_timestamps = {} 
        logger.info("PanelUpdater Cog가 성공적으로 초기화되었습니다.")

    def cog_unload(self):
        self.check_for_panel_updates.cancel()

    @tasks.loop(seconds=10.0)
    async def check_for_panel_updates(self):
        panel_map = {
            "panel_fishing_river": {"cog_name": "Fishing", "channel_key": "river_fishing_panel_channel_id"},
            "panel_fishing_sea":   {"cog_name": "Fishing", "channel_key": "sea_fishing_panel_channel_id"},
            "panel_commerce":      {"cog_name": "Commerce", "channel_key": "commerce_panel_channel_id"},
            "panel_profile":       {"cog_name": "UserProfile", "key": "profile_panel_channel_id"}, # user_profile.py의 오타 수정
        }
        
        try:
            request_keys = [f"panel_regenerate_request_{key}" for key in panel_map.keys()]
            response = await supabase.table('bot_configs').select('config_key').in_('config_key', request_keys).execute()
            
            if not response or not response.data:
                return

            # [🔴 핵심 변경 2] DB에 요청이 존재하는 키 목록만 가져옵니다. (시간 값은 필요 없음)
            db_requests = {item['config_key'] for item in response.data}

        except Exception as e:
            logger.error(f"패널 업데이트 요청 확인 중 DB 오류 발생: {e}")
            return

        for panel_key, info in panel_map.items():
            db_key = f"panel_regenerate_request_{panel_key}"
            
            # [🔴 핵심 변경 3] 시간 비교 대신, 요청 키가 존재하는지만 확인합니다.
            if db_key in db_requests:
                logger.info(f"DB에서 `{panel_key}` 패널에 대한 재설치 요청을 발견했습니다.")
                
                cog = self.bot.get_cog(info["cog_name"])
                
                # 'profile_panel_channel_id' 오타를 고려하여 channel_key를 안전하게 가져옵니다.
                channel_key = info.get("channel_key", info.get("key"))
                if not channel_key:
                    logger.error(f"'{panel_key}'에 대한 channel_key 설정이 누락되었습니다.")
                    continue

                channel_id = get_id(channel_key)
                
                if not cog or not hasattr(cog, 'regenerate_panel'):
                    logger.error(f"'{info['cog_name']}' Cog를 찾을 수 없거나 'regenerate_panel' 함수가 없습니다.")
                    continue
                
                if not channel_id or not (channel := self.bot.get_channel(channel_id)):
                    logger.error(f"'{panel_key}' 패널의 채널(ID: {channel_id})을 찾을 수 없습니다. `/setup`으로 채널을 먼저 설정해주세요.")
                    continue
                
                try:
                    # 패널 키를 명시적으로 전달합니다.
                    await cog.regenerate_panel(channel, panel_key=panel_key)
                    logger.info(f"✅ `{panel_key}` 패널을 성공적으로 재설치했습니다.")

                    # [🔴 핵심 변경 4] 성공적으로 처리된 요청을 DB에서 삭제합니다.
                    await supabase.table('bot_configs').delete().eq('config_key', db_key).execute()
                    logger.info(f"DB에서 처리 완료된 요청 키(`{db_key}`)를 삭제했습니다.")

                except Exception as e:
                    logger.error(f"'{panel_key}' 패널 재설치 중 오류 발생: {e}", exc_info=True)

    @check_for_panel_updates.before_loop
    async def before_check_loop(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(PanelUpdater(bot))
