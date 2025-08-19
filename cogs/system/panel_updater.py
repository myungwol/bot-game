
import discord
from discord.ext import commands, tasks
import logging

# [🔴 핵심 수정] supabase와 get_id 만 가져오도록 수정합니다.
from utils.database import supabase, get_id

logger = logging.getLogger(__name__)

class PanelUpdater(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.last_checked_timestamps = {}
        logger.info("PanelUpdater Cog가 성공적으로 초기화되었습니다.")

    def cog_unload(self):
        self.check_for_panel_updates.cancel()

    @tasks.loop(seconds=10.0)
    async def check_for_panel_updates(self):
        panel_map = {
            "panel_fishing_river": {"cog_name": "Fishing", "channel_key": "river_fishing_panel_channel_id"},
            "panel_fishing_sea":   {"cog_name": "Fishing", "channel_key": "sea_fishing_panel_channel_id"},
            "panel_commerce":      {"cog_name": "Commerce", "channel_key": "commerce_panel_channel_id"},
            "panel_profile":       {"cog_name": "UserProfile", "channel_key": "profile_panel_channel_id"},
        }
        
        try:
            request_keys = [f"panel_regenerate_request_{key}" for key in panel_map.keys()]
            response = await supabase.table('bot_configs').select('config_key, config_value').in_('config_key', request_keys).execute()
            
            if not response or not response.data:
                return

            db_requests = {item['config_key']: item['config_value'] for item in response.data}

        except Exception as e:
            logger.error(f"패널 업데이트 요청 확인 중 DB 오류 발생: {e}")
            return

        for panel_key, info in panel_map.items():
            db_key = f"panel_regenerate_request_{panel_key}"
            
            request_timestamp = db_requests.get(db_key)
            if not request_timestamp:
                continue

            last_checked = self.last_checked_timestamps.get(db_key, 0)
            
            # request_timestamp는 DB에서 가져올 때 따옴표가 포함될 수 있으므로 제거 후 float 변환
            if float(str(request_timestamp).strip('"')) > last_checked:
                logger.info(f"DB에서 `{panel_key}` 패널에 대한 새로운 재설치 요청을 발견했습니다.")
                
                self.last_checked_timestamps[db_key] = float(str(request_timestamp).strip('"'))

                cog = self.bot.get_cog(info["cog_name"])
                channel_id = get_id(info["channel_key"])

                if not cog or not hasattr(cog, 'regenerate_panel'):
                    logger.error(f"'{info['cog_name']}' Cog를 찾을 수 없거나 'regenerate_panel' 함수가 없습니다.")
                    continue
                
                if not channel_id or not (channel := self.bot.get_channel(channel_id)):
                    logger.error(f"'{panel_key}' 패널의 채널(ID: {channel_id})을 찾을 수 없습니다. `/setup`으로 채널을 먼저 설정해주세요.")
                    continue
                
                try:
                    await cog.regenerate_panel(channel, panel_key)
                    logger.info(f"✅ `{panel_key}` 패널을 성공적으로 재설치했습니다.")
                except Exception as e:
                    logger.error(f"'{panel_key}' 패널 재설치 중 오류 발생: {e}", exc_info=True)

    @check_for_panel_updates.before_loop
    async def before_check_loop(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(PanelUpdater(bot))
