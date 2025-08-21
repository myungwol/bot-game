# cogs/world.py

import discord
from discord.ext import commands, tasks
import logging
import random
from utils.database import save_config_to_db, get_config, get_id

logger = logging.getLogger(__name__)

# 주석: 날씨 유형과 효과를 정의합니다. 'water_effect'가 True이면 비가 오는 것으로 간주합니다.
WEATHER_TYPES = {
    "sunny": {"emoji": "☀️", "name": "晴れ", "water_effect": False},
    "cloudy": {"emoji": "☁️", "name": "曇り", "water_effect": False},
    "rainy": {"emoji": "🌧️", "name": "雨", "water_effect": True},
    "stormy": {"emoji": "⛈️", "name": "嵐", "water_effect": True},
}

class WorldSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.update_weather.start()
        logger.info("WorldSystem Cog가 성공적으로 초기화되었습니다.")

    def cog_unload(self):
        self.update_weather.cancel()

    @tasks.loop(hours=24) # 주석: 24시간마다 날씨를 변경합니다. 테스트 시에는 hours=1 등으로 줄여서 사용하세요.
    async def update_weather(self):
        # 주석: 가중치를 두어 날씨를 랜덤하게 선택합니다.
        weather_key = random.choices(
            population=list(WEATHER_TYPES.keys()),
            weights=[0.5, 0.25, 0.2, 0.05], # 맑음(50%), 흐림(25%), 비(20%), 폭풍(5%)
            k=1
        )[0]
        
        # 주석: 결정된 날씨를 DB의 bot_configs 테이블에 저장합니다.
        await save_config_to_db("current_weather", weather_key)
        logger.info(f"今日の天気が '{WEATHER_TYPES[weather_key]['name']}' に変わりました。")
        
         주석: (선택 사항) 날씨가 바뀌었음을 특정 채널에 공지하는 기능입니다.
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
        
        # 주석: 봇이 처음 켜졌을 때 날씨 정보가 없으면, 즉시 한 번 실행하여 날씨를 설정합니다.
        if get_config("current_weather") is None:
            logger.info("現在の天気が設定されていないため、初期設定を行います。")
            await self.update_weather()

async def setup(bot: commands.Bot):
    await bot.add_cog(WorldSystem(bot))
