import discord
from discord.ext import commands, tasks
import logging

# [✅✅✅ 핵심 수정 ✅✅✅] 실시간으로 DB 정보를 다시 불러올 함수를 import 합니다.
from utils.database import supabase, get_id, load_channel_ids_from_db

logger = logging.getLogger(__name__)

class PanelUpdater(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("PanelUpdater Cog가 성공적으로 초기화되었습니다.")
        self.check_for_panel_updates.start()

    def cog_unload(self):
        self.check_for_panel_updates.cancel()

    @tasks.loop(seconds=10.0)
    async def check_for_panel_updates(self):
        panel_map = {
            "panel_fishing_river": {"cog_name": "Fishing", "channel_key": "river_fishing_panel_channel_id"},
            "panel_fishing_sea":   {"cog_name": "Fishing", "channel_key": "sea_fishing_panel_channel_id"},
            "panel_commerce":      {"cog_name": "Commerce", "channel_key": "commerce_panel_channel_id"},
            "panel_profile":       {"cog_name": "UserProfile", "channel_key": "profile_panel_channel_id"},
            "panel_atm":           {"cog_name": "Atm", "channel_key": "atm_panel_channel_id"},
            "panel_dice_game":     {"cog_name": "DiceGame", "channel_key": "dice_game_panel_channel_id"},
            "panel_slot_machine":  {"cog_name": "SlotMachine", "channel_key": "slot_machine_panel_channel_id"},
            "panel_rps_game":      {"cog_name": "RPSGame", "channel_key": "rps_game_panel_channel_id"},
        }
        
        try:
            request_keys = [f"panel_regenerate_request_{key}" for key in panel_map.keys()]
            response = await supabase.table('bot_configs').select('config_key').in_('config_key', request_keys).execute()
            
            if not response or not response.data:
                return

            db_requests = {item['config_key'] for item in response.data}
            
            # [✅✅✅ 핵심 수정 ✅✅✅] 재설치 요청이 하나라도 있다면, DB에서 최신 채널 ID 목록을 즉시 새로고침합니다.
            if db_requests:
                logger.info("새로운 패널 재설치 요청을 감지하여, DB로부터 모든 채널 ID를 새로고침합니다.")
                await load_channel_ids_from_db()

        except Exception as e:
            logger.error(f"패널 업데이트 요청 확인 중 DB 오류 발생: {e}")
            return

        for panel_key, info in panel_map.items():
            db_key = f"panel_regenerate_request_{panel_key}"
            
            if db_key in db_requests:
                logger.info(f"DB에서 `{panel_key}` 패널에 대한 재설치 요청을 발견했습니다.")
                
                cog = self.bot.get_cog(info["cog_name"])
                # 이제 이 get_id는 방금 새로고침된 최신 정보를 사용합니다.
                channel_id = get_id(info["channel_key"])

                if not cog or not hasattr(cog, 'regenerate_panel'):
                    logger.error(f"'{info['cog_name']}' Cog를 찾을 수 없거나 'regenerate_panel' 함수가 없습니다.")
                    continue
                
                if not channel_id or not (channel := self.bot.get_channel(channel_id)):
                    logger.error(f"'{panel_key}' 패널의 채널(ID: {channel_id or 'None'})을 찾을 수 없습니다. `/setup`으로 채널을 먼저 설정해주세요.")
                    continue
                
                try:
                    await cog.regenerate_panel(channel, panel_key=panel_key)
                    logger.info(f"✅ `{panel_key}` 패널을 성공적으로 재설치했습니다.")

                    await supabase.table('bot_configs').delete().eq('config_key', db_key).execute()
                    logger.info(f"DB에서 처리 완료된 요청 키(`{db_key}`)를 삭제했습니다.")

                except Exception as e:
                    logger.error(f"'{panel_key}' 패널 재설치 중 오류 발생: {e}", exc_info=True)

    @check_for_panel_updates.before_loop
    async def before_check_loop(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(PanelUpdater(bot))
