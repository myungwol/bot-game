# cogs/system/AdminBridge.py

import discord
from discord.ext import commands, tasks
import logging
import asyncio

from utils.database import supabase, get_config, update_wallet

logger = logging.getLogger(__name__)

class AdminBridge(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_for_admin_requests.start()
        logger.info("AdminBridge Cog (관리-게임 봇 연동)가 성공적으로 초기화되었습니다.")

    def cog_unload(self):
        self.check_for_admin_requests.cancel()

    @tasks.loop(seconds=20.0)
    async def check_for_admin_requests(self):
        try:
            server_id_str = get_config("SERVER_ID")
            if not server_id_str:
                # 서버 ID가 설정될 때까지 루프를 잠시 멈춤
                if self.check_for_admin_requests.is_running():
                     logger.error("DB에 'SERVER_ID'가 설정되지 않아 관리자 요청 확인을 일시 중단합니다. 설정 후 봇을 재시작해주세요.")
                     self.check_for_admin_requests.stop()
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

            response = await supabase.table('bot_configs').select('config_key, config_value').like('config_key', '%_admin_update_request_%').execute()
            
            if not response or not response.data:
                return

            requests_to_process = response.data
            keys_to_delete = [] # 성공한 요청 키만 담을 리스트
            
            level_cog = self.bot.get_cog("LevelSystem")
            
            for req in requests_to_process:
                success = False # 각 요청의 성공 여부를 추적
                try:
                    key_parts = req['config_key'].split('_')
                    req_type = key_parts[0] # 'xp' or 'coin'
                    user_id = int(key_parts[-1])
                    
                    user = guild.get_member(user_id)
                    if not user:
                        logger.warning(f"관리자 요청 처리 중 유저(ID: {user_id})를 서버에서 찾을 수 없어 해당 요청을 삭제합니다.")
                        keys_to_delete.append(req['config_key']) # 찾을 수 없는 유저는 그냥 삭제 처리
                        continue
                    
                    payload = req.get('config_value', {})

                    if req_type == 'xp' and level_cog:
                        xp_to_add = payload.get('xp_to_add')
                        exact_level = payload.get('exact_level')
                        if xp_to_add:
                            success = await level_cog.update_user_xp_and_level_from_admin(user, xp_to_add=xp_to_add)
                        elif exact_level:
                            success = await level_cog.update_user_xp_and_level_from_admin(user, exact_level=exact_level)
                    
                    elif req_type == 'coin':
                        amount = payload.get('amount')
                        if amount:
                            # update_wallet은 성공 시 데이터를, 실패 시 None을 반환할 수 있음
                            result = await update_wallet(user, amount)
                            if result:
                                success = True
                    
                    if success:
                        keys_to_delete.append(req['config_key'])
                    else:
                        logger.error(f"관리자 요청 처리에 실패했습니다: {req['config_key']}. DB에서 삭제하지 않고 다음 루프에서 재시도합니다.")

                except (ValueError, IndexError) as e:
                    logger.error(f"잘못된 형식의 관리자 요청 키를 발견하여 삭제합니다: {req['config_key']} - {e}")
                    keys_to_delete.append(req['config_key']) # 잘못된 형식의 키는 삭제
            
            if keys_to_delete:
                await supabase.table('bot_configs').delete().in_('config_key', keys_to_delete).execute()
                logger.info(f"DB에서 처리 완료/실패/오류 요청 키 {len(keys_to_delete)}개를 삭제했습니다.")

        except Exception as e:
            logger.error(f"관리자 요청 확인 중 DB 오류가 발생했습니다: {e}", exc_info=True)
    @check_for_admin_requests.before_loop
    async def before_check_loop(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(5)

async def setup(bot: commands.Bot):
    await bot.add_cog(AdminBridge(bot))
