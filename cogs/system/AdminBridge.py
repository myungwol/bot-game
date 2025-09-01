# bot-game/cogs/systems/AdminBridge.py

import discord
from discord.ext import commands, tasks
import logging
import asyncio

from utils.database import supabase, get_config

logger = logging.getLogger(__name__)

class AdminBridge(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_for_admin_requests.start()
        logger.info("AdminBridge Cog (관리-게임 봇 연동)가 성공적으로 초기화되었습니다.")

    def cog_unload(self):
        self.check_for_admin_requests.cancel()

    @tasks.loop(seconds=10.0)
    async def check_for_admin_requests(self):
        try:
            # SERVER_ID가 DB에 설정되어 있는지 확인하고, 없으면 에러 로그를 남기고 대기합니다.
            server_id_str = get_config("SERVER_ID")
            if not server_id_str:
                logger.error("DB에 'SERVER_ID'가 설정되지 않았습니다. 관리자 봇에서 `/admin set_server_id` 명령어를 실행해주세요.")
                await asyncio.sleep(60) # 60초 대기 후 다시 시도
                return

            try:
                server_id = int(server_id_str)
            except (ValueError, TypeError):
                logger.error(f"DB에 저장된 'SERVER_ID'({server_id_str})가 올바른 숫자 형식이 아닙니다.")
                await asyncio.sleep(60)
                return

            guild = self.bot.get_guild(server_id)
            if not guild:
                logger.error(f"설정된 SERVER_ID({server_id})에 해당하는 서버를 찾을 수 없습니다. 봇이 해당 서버에 참여해 있는지 확인해주세요.")
                await asyncio.sleep(60)
                return

            # XP 및 레벨 업데이트 요청을 확인합니다.
            response = await supabase.table('bot_configs').select('config_key, config_value').like('config_key', 'xp_admin_update_request_%').execute()
            
            if not response or not response.data:
                return

            requests_to_process = response.data
            keys_to_delete = [req['config_key'] for req in requests_to_process]
            
            level_cog = self.bot.get_cog("LevelSystem")
            if not level_cog:
                logger.error("LevelSystem Cog를 찾을 수 없어 관리자 요청을 처리할 수 없습니다.")
                return

            tasks = []
            for req in requests_to_process:
                try:
                    user_id = int(req['config_key'].split('_')[-1])
                    user = guild.get_member(user_id)
                    if not user:
                        logger.warning(f"관리자 요청 처리 중 유저(ID: {user_id})를 서버에서 찾을 수 없습니다.")
                        continue
                    
                    payload = req.get('config_value', {})
                    xp_to_add = payload.get('xp_to_add')
                    exact_level = payload.get('exact_level')

                    if xp_to_add:
                        tasks.append(level_cog.update_user_xp_and_level_from_admin(user, xp_to_add=xp_to_add))
                    elif exact_level:
                        tasks.append(level_cog.update_user_xp_and_level_from_admin(user, exact_level=exact_level))

                except (ValueError, IndexError) as e:
                    logger.error(f"잘못된 형식의 관리자 요청 키를 발견했습니다: {req['config_key']} - {e}")
            
            if tasks:
                await asyncio.gather(*tasks)

            if keys_to_delete:
                await supabase.table('bot_configs').delete().in_('config_key', keys_to_delete).execute()
                logger.info(f"DB에서 처리 완료된 관리자 요청 키 {len(keys_to_delete)}개를 삭제했습니다.")

        except Exception as e:
            logger.error(f"관리자 요청 확인 중 DB 오류가 발생했습니다: {e}", exc_info=True)

    @check_for_admin_requests.before_loop
    async def before_check_loop(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(5)

async def setup(bot: commands.Bot):
    await bot.add_cog(AdminBridge(bot))
