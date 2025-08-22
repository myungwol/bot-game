# cogs/world.py

import discord
from discord.ext import commands, tasks
import logging
import random
from datetime import time as dt_time, timezone, timedelta

# [✅ 수정] DB 함수 및 헬퍼 함수 import
from utils.database import save_config_to_db, get_config, get_id, get_embed_from_db
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

# [✅ 수정] 날씨별 세부 정보 추가
WEATHER_TYPES = {
    "sunny": {
        "emoji": "☀️", "name": "晴れ", "water_effect": False, "color": 0xFFAC33,
        "description": "空は一点の曇りもなく、暖かな日差しが村を照らしています。",
        "tip": "農作物にとっては最高の成長日和かもしれません！"
    },
    "cloudy": {
        "emoji": "☁️", "name": "曇り", "water_effect": False, "color": 0x95A5A6,
        "description": "過ごしやすい曇り空です。時々太陽が顔を出すかもしれません。",
        "tip": "のんびり釣りをするには最適な一日です。"
    },
    "rainy": {
        "emoji": "🌧️", "name": "雨", "water_effect": True, "color": 0x3498DB,
        "description": "しとしとと雨が降り続いています。傘を忘れずに！",
        "tip": "農場に自動で水がまかれます！水やりの手間が省けますね。"
    },
    "stormy": {
        "emoji": "⛈️", "name": "嵐", "water_effect": True, "color": 0x2C3E50,
        "description": "激しい雨と雷が鳴り響いています。外出の際はご注意ください。",
        "tip": "海が荒れている日は、珍しい魚が釣れるという噂も…？"
    },
}

JST = timezone(timedelta(hours=9))
JST_MIDNIGHT = dt_time(hour=0, minute=0, tzinfo=JST)

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
        weather_info = WEATHER_TYPES[weather_key]
        logger.info(f"今日の天気が '{weather_info['name']}' に変わりました。")
        
        announcement_channel_id = get_id("weather_channel_id")
        if not (announcement_channel_id and (channel := self.bot.get_channel(announcement_channel_id))):
            return

        try:
            # [✅ 수정] 임베드 기반 공지 전송
            embed_data = await get_embed_from_db("embed_weather_forecast")
            
            # DB에 템플릿이 없으면 기본값 사용
            if not embed_data:
                embed_data = {
                    "title": "{emoji} Dico森の今日の天気予報",
                    "description": "今日の天気は「**{weather_name}**」です！\n\n> {description}",
                    "fields": [{"name": "💡 今日のヒント", "value": "> {tip}", "inline": False}],
                    "footer": {"text": "天気は毎日午前0時に変わります。"}
                }

            # 색상 값을 int로 변환
            embed_data_copy = embed_data.copy()
            embed_data_copy['color'] = weather_info['color']

            embed = format_embed_from_db(
                embed_data_copy,
                emoji=weather_info['emoji'],
                weather_name=weather_info['name'],
                description=weather_info['description'],
                tip=weather_info['tip']
            )
            
            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"天気予報の送信に失敗しました: {e}", exc_info=True)

    @update_weather.before_loop
    async def before_update_weather(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(5) # 다른 봇에서 설정을 불러올 시간을 줌
        
        # 봇 시작 시 날씨가 설정되지 않았다면 즉시 한번 실행
        if get_config("current_weather") is None:
            logger.info("現在の天気が設定されていないため、初回設定を実行します。")
            await self.update_weather()

async def setup(bot: commands.Bot):
    await bot.add_cog(WorldSystem(bot))
