# cogs/world.py

import discord
from discord.ext import commands, tasks
import logging
import random
# [✅ 현지화] timedelta, timezone을 import 합니다.
from datetime import time, timezone, timedelta
from utils.database import save_config_to_db, get_config, get_id

logger = logging.getLogger(__name__)

WEATHER_TYPES = {
    "sunny": {"emoji": "☀️", "name": "晴れ", "water_effect": False},
    "cloudy": {"emoji": "☁️", "name": "曇り", "water_effect": False},
    "rainy": {"emoji": "🌧️", "name": "雨", "water_effect": True},
    "stormy": {"emoji": "⛈️", "name": "嵐", "water_effect": True},
}

# [✅ 현지화] KST를 JST로 변경하여 코드의 명확성을 높입니다.
JST_MIDNIGHT = time(hour=0, minute=0, tzinfo=timezone(timedelta(hours=9)))

class WorldSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.update_weather.start()
        logger.info("WorldSystem Cog가 성공적으로 초기화되었습니다.")

    def cog_unload(self):
        self.update_weather.cancel()

    @tasks.loop(time=JST_MIDNIGHT)
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
        
        if get_config("current_weather") is None:
            logger.info("現在の天気が設定されていないため、初期設定を行います。")
            # 루프가 곧 시작될 것이므로 여기서 직접 호출할 필요는 없습니다.
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(WorldSystem(bot))
