# main.py

import discord
from discord.ext import commands
import os
import asyncio
import logging
import logging.handlers

from utils.database import load_all_data_from_db, supabase

# --- 중앙 로깅 설정 ---
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(name)s:%(lineno)d] %(message)s')
log_handler = logging.StreamHandler()
log_handler.setFormatter(log_formatter)
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
if root_logger.hasHandlers():
    root_logger.handlers.clear()
root_logger.addHandler(log_handler)
logging.getLogger('discord').setLevel(logging.WARNING)
logging.getLogger('discord.http').setLevel(logging.WARNING)
logging.getLogger('websockets').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- 환경 변수 및 인텐트 설정 ---
BOT_TOKEN = os.environ.get('BOT_TOKEN')
TEST_GUILD_ID = os.environ.get('TEST_GUILD_ID')
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True

# --- 커스텀 봇 클래스 ---
class MyBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def setup_hook(self):
        await self.load_all_extensions()
        
        cogs_with_persistent_views = [
            "UserProfile", "Fishing", "Commerce", "Atm",
            "DiceGame", "SlotMachine", "RPSGame",
            "DailyCheck", "Quests", "Farm"
        ]
        
        registered_views = 0
        for cog_name in cogs_with_persistent_views:
            cog = self.get_cog(cog_name)
            if cog and hasattr(cog, 'register_persistent_views'):
                try:
                    await cog.register_persistent_views()
                    registered_views += 1
                    logger.info(f"✅ '{cog_name}' Cog의 영구 View가 등록되었습니다.")
                except Exception as e:
                    logger.error(f"❌ '{cog_name}' Cog의 영구 View 등록 중 오류 발생: {e}", exc_info=True)
        logger.info(f"✅ 총 {registered_views}개의 Cog에서 영구 View를 성공적으로 등록했습니다.")

    async def load_all_extensions(self):
        logger.info("------ [ Cog 로드 시작 ] ------")
        # cogs 폴더와 그 하위 폴더들을 모두 탐색
        for root, dirs, files in os.walk('./cogs'):
            # 주석: .pycache 폴더는 건너뛰도록 예외 처리 추가
            if '__pycache__' in root:
                continue
            for filename in files:
                if filename.endswith('.py') and not filename.startswith('__'):
                    # 경로를 Python 모듈 경로 형식으로 변환 (e.g., cogs.games.fishing)
                    path = os.path.join(root, filename)
                    # 주석: Windows와 Linux 호환성을 위해 os.path.sep 사용
                    extension_path = path.replace(os.path.sep, '.')[:-3].lstrip('./')
                    try:
                        await self.load_extension(extension_path)
                        logger.info(f'✅ Cog 로드 성공: {extension_path}')
                    except Exception as e:
                        logger.error(f'❌ Cog 로드 실패: {extension_path} | {e}', exc_info=True)
        logger.info("------ [ Cog 로드 완료 ] ------")


bot = MyBot(command_prefix="/", intents=intents)

@bot.event
async def on_ready():
    logger.info(f'✅ {bot.user.name}(이)가 성공적으로 로그인했습니다.')
    
    try:
        if supabase:
            logger.info("------ [ 오래된 패널 재설치 요청 청소 시작 ] ------")
            await supabase.table('bot_configs').delete().like('config_key', 'panel_regenerate_request_%').execute()
            logger.info("✅ 모든 오래된 패널 재설치 요청을 DB에서 성공적으로 삭제했습니다.")
            logger.info("------ [ 오래된 패널 재설치 요청 청소 완료 ] ------")
    except Exception as e:
        logger.error(f"❌ 오래된 패널 재설치 요청 청소 중 오류 발생: {e}", exc_info=True)

    await load_all_data_from_db()
    
    logger.info("------ [ 모든 Cog 설정 새로고침 시작 ] ------")
    refreshed_cogs = 0
    for cog_name, cog in bot.cogs.items():
        if hasattr(cog, 'load_configs'):
            try:
                await cog.load_configs()
                refreshed_cogs += 1
            except Exception as e:
                logger.error(f"❌ '{cog_name}' Cog 설정 새로고침 중 오류: {e}", exc_info=True)
    logger.info(f"✅ 총 {refreshed_cogs}개의 Cog 설정이 새로고침되었습니다.")
    logger.info("------ [ 모든 Cog 설정 새로고침 완료 ] ------")

    try:
        if TEST_GUILD_ID:
            guild_id = int(TEST_GUILD_ID)
            guild = discord.Object(id=guild_id)
            await bot.tree.sync(guild=guild)
            logger.info(f'✅ 테스트 서버({guild_id})에 명령어를 동기화했습니다.')
        else:
            synced = await bot.tree.sync()
            logger.info(f'✅ {len(synced)}개의 슬래시 명령어를 전체 서버에 동기화했습니다.')
    except Exception as e:
        logger.error(f'❌ 명령어 동기화 중 오류가 발생했습니다: {e}')

async def main():
    async with bot:
        await bot.start(BOT_TOKEN)

if __name__ == "__main__":
    if BOT_TOKEN is None:
        logger.critical("❌ BOT_TOKEN 환경 변수가 설정되지 않았습니다.")
    else:
        try:
            asyncio.run(main())
        except discord.errors.LoginFailure:
            logger.critical("❌ 봇 토큰이 유효하지 않습니다.")
        except Exception as e:
            logger.critical(f"🚨 봇 실행 중 치명적인 오류 발생: {e}", exc_info=True)
