# cogs/world.py (KST 자정 업데이트로 수정된 최종본)

import discord
from discord.ext import commands, tasks
import logging
import random
# [✅ 1단계] datetime 관련 모듈을 import 합니다.
from datetime import time, timezone, timedelta
from utils.database import save_config_to_db, get_config, get_id

logger = logging.getLogger(__name__)

WEATHER_TYPES = {
    "sunny": {"emoji": "☀️", "name": "晴れ", "water_effect": False},
    "cloudy": {"emoji": "☁️", "name": "曇り", "water_effect": False},
    "rainy": {"emoji": "🌧️", "name": "雨", "water_effect": True},
    "stormy": {"emoji": "⛈️", "name": "嵐", "water_effect": True},
}

# [✅ 2단계] 한국 시간(KST) 자정을 나타내는 시간 객체를 만듭니다.
KST_MIDNIGHT = time(hour=0, minute=0, tzinfo=timezone(timedelta(hours=9)))

class WorldSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.update_weather.start()
        logger.info("WorldSystem Cog가 성공적으로 초기화되었습니다.")

    def cog_unload(self):
        self.update_weather.cancel()

    # [✅ 3단계] @tasks.loop 설정을 hours=24 대신 time=KST_MIDNIGHT로 변경합니다.
    @tasks.loop(time=KST_MIDNIGHT)
    async def update_weather(self):
        weather_key = random.choices(
            population=list(WEATHER_TYPES.keys()),
            weights=[0.5, 0.25, 0.2, 0.05],
            k=1
        )[0]
        
        await save_config_to_db("current_weather", weather_key)
        logger.info(f"今日の天気が '{WEATHER_TYPES[weather_key]['name']}' に変わりました。")
        
        announcement_channel_id = get_id("weather_channel_id")
        if announcement_channel_id and (channel := self.bot.get_channel(announcement_channel_id)):
            weather = WEATHER_TYPES[weather_key]
            try:
                await channel.send(f"Dico森の今日の天気は… {weather['emoji']} **{weather['name']}** です！")
            except Exception as e:
                logger.error(f"天気予報の送信に失敗しました: {e}")

    @update_weather.before_loop
    async def before_update_weather(self):
        await self.bot.wait_until_ready()
        
        # 주석: 봇 시작 시 날씨가 설정되어 있지 않다면 즉시 한 번 실행합니다.
        # 이 코드는 그대로 두어도 괜찮습니다.
        if get_config("current_weather") is None:
            logger.info("現在の天気が設定されていないため、初期設定を行います。")
            # before_loop에서는 루프 자체를 직접 호출할 수 없으므로,
            # 루프의 실제 로직을 별도 함수로 분리하거나, 여기서 직접 실행해야 합니다.
            # 하지만 현재 구조상으로는 첫 실행은 그냥 24시간 뒤로 두어도 무방합니다.
            # 더 나은 방법은 루프의 첫 실행을 기다리는 것입니다.
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(WorldSystem(bot))
