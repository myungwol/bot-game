# cogs/system/AdminBridge.py

import discord
from discord.ext import commands, tasks
import logging
import asyncio

# [수정] update_wallet 함수를 import 합니다.
from utils.database import supabase, get_config, update_wallet

logger = logging.getLogger(__name__)

class AdminBridge(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # [수정] 봇이 준비된 후에 루프를 시작하도록 변경
        # self.check_for_admin_requests.start() 
        logger.info("AdminBridge Cog (관리-게임 봇 연동)가 성공적으로 초기화되었습니다.")

    # [추가] 봇이 완전히 준비된 후에 루프를 시작하기 위한 리스너
    @commands.Cog.listener()
    async def on_ready(self):
        if not self.check_for_admin_requests.is_running():
            self.check_for_admin_requests.start()
            logger.info("AdminBridge: 봇이 준비되어 관리자 요청 확인 루프를 시작합니다.")

    def cog_unload(self):
        self.check_for_admin_requests.cancel()

    @tasks.loop(seconds=20.0)
    async def check_for_admin_requests(self):
        # [수정] 루프가 실행되고 있음을 명확히 알리기 위해 로그 추가
        logger.info("[AdminBridge] 관리자 요청을 확인합니다...")
        
        try:
            server_id_str = get_config("SERVER_ID")
            if not server_id_str:
                # [수정] 루프를 멈추는 대신, 경고를 남기고 다음 시도를 기다립니다.
                logger.warning("DB에서 'SERVER_ID'를 찾을 수 없습니다. 관리자 봇에서 `/admin setup action:[중요] 서버 ID 설정` 명령어를 실행해주세요. 20초 후에 다시 시도합니다.")
                return

            try:
                server_id = int(server_id_str)
            except (ValueError, TypeError):
                logger.error(f"DB에 저장된 'SERVER_ID'({server_id_str})가 올바른 숫자 형식이 아닙니다.")
                return

            guild = self.bot.get_guild(server_id)
            if not guild:
                logger.error(f"설정된 SERVER_ID({server_id})에 해당하는 서버를 찾을 수 없습니다. 봇이 해당 서버에 참여해 있는지 확인해주세요.")
                return

            response = await supabase.table('bot_configs').select('config_key, config_value').like('config_key', '%_admin_update_request_%').execute()
            
            if not (response and response.data):
                logger.info("[AdminBridge] 처리할 새로운 관리자 요청이 없습니다.")
                return

            requests_to_process = response.data
            logger.info(f"[AdminBridge] {len(requests_to_process)}개의 관리자 요청을 발견하여 처리를 시작합니다.")
            
            keys_to_delete = []
            
            level_cog = self.bot.get_cog("LevelSystem")
            
            for req in requests_to_process:
                success = False
                try:
                    key_parts = req['config_key'].split('_')
                    req_type = key_parts[0]
                    user_id = int(key_parts[-1])
                    
                    user = guild.get_member(user_id)
                    if not user:
                        logger.warning(f"관리자 요청 처리 중 유저(ID: {user_id})를 서버에서 찾을 수 없어 해당 요청을 삭제합니다.")
                        keys_to_delete.append(req['config_key'])
                        continue
                    
                    payload = req.get('config_value', {})

                    if req_type == 'xp' and level_cog:
                        xp_to_add = payload.get('xp_to_add')
                        exact_level = payload.get('exact_level')
                        if xp_to_add is not None:
                            success = await level_cog.update_user_xp_and_level_from_admin(user, xp_to_add=xp_to_add)
                        elif exact_level is not None:
                            success = await level_cog.update_user_xp_and_level_from_admin(user, exact_level=exact_level)
                    
                    elif req_type == 'coin':
                        amount = payload.get('amount')
                        if amount is not None:
                            result = await update_wallet(user, amount)
                            if result:
                                success = True
                    
                    if success:
                        logger.info(f"요청 처리 성공: {req['config_key']}")
                        keys_to_delete.append(req['config_key'])
                    else:
                        logger.error(f"관리자 요청 처리에 실패했습니다: {req['config_key']}. 다음 루프에서 재시도합니다.")

                except (ValueError, IndexError) as e:
                    logger.error(f"잘못된 형식의 관리자 요청 키를 발견하여 삭제합니다: {req['config_key']} - {e}")
                    keys_to_delete.append(req['config_key'])
            
            if keys_to_delete:
                await supabase.table('bot_configs').delete().in_('config_key', keys_to_delete).execute()
                logger.info(f"DB에서 처리 완료/실패/오류 요청 키 {len(keys_to_delete)}개를 삭제했습니다.")

        except Exception as e:
            logger.error(f"관리자 요청 확인 중 심각한 DB 오류가 발생했습니다: {e}", exc_info=True)

    @check_for_admin_requests.before_loop
    async def before_check_loop(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(5)

async def setup(bot: commands.Bot):
    await bot.add_cog(AdminBridge(bot))
