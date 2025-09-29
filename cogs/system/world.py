# cogs/world.py

import discord
from discord.ext import commands, tasks
import logging
import random
from datetime import time as dt_time, timezone, timedelta
import asyncio

from utils.database import save_config_to_db, get_config, get_id, get_embed_from_db
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

WEATHER_TYPES = {
    "sunny": { "emoji": "☀️", "name": "맑음", "water_effect": False, "color": 0xFFAC33, "description": "하늘은 한 점의 구름도 없이, 따스한 햇살이 마을을 비추고 있습니다.", "tip": "농작물에게는 최고의 성장일지도 모릅니다!" },
    "cloudy": { "emoji": "☁️", "name": "흐림", "water_effect": False, "color": 0x95A5A6, "description": "지내기 좋은 흐린 하늘입니다. 때때로 해가 얼굴을 내밀지도 모릅니다.", "tip": "느긋하게 낚시를 하기에 최적의 하루입니다." },
    "rainy": { "emoji": "🌧️", "name": "비", "water_effect": True, "color": 0x3498DB, "description": "부슬부슬 비가 계속 내리고 있습니다. 우산을 잊지 마세요!", "tip": "농장에 자동으로 물이 뿌려집니다! 물을 주는 수고를 덜 수 있겠네요." },
    "stormy": { "emoji": "⛈️", "name": "폭풍", "water_effect": True, "color": 0x2C3E50, "description": "거센 비와 천둥이 울려 퍼지고 있습니다. 외출 시 주의하세요.", "tip": "바다가 거친 날에는 희귀한 물고기가 잡힌다는 소문도...?" },
}

KST = timezone(timedelta(hours=9))
KST_MIDNIGHT = dt_time(hour=0, minute=0, tzinfo=KST)

class WorldSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.update_weather.start()
        logger.info("WorldSystem Cog가 성공적으로 초기화되었습니다.")

    def cog_unload(self):
        self.update_weather.cancel()

    @tasks.loop(time=KST_MIDNIGHT)
    async def update_weather(self):
        weather_keys = list(WEATHER_TYPES.keys())
        weights = [0.5, 0.25, 0.2, 0.05]
        chosen_key = random.choices(population=weather_keys, weights=weights, k=1)[0]
        
        await save_config_to_db("current_weather", chosen_key)
        weather_info = WEATHER_TYPES[chosen_key]
        logger.info(f"오늘의 날씨가 '{weather_info['name']}'(으)로 바뀌었습니다.")
        
        announcement_channel_id = get_id("weather_channel_id")
        if announcement_channel_id and (channel := self.bot.get_channel(announcement_channel_id)):
            try:
                embed_data = await get_embed_from_db("embed_weather_forecast")
                if not embed_data:
                    logger.warning("DB에서 'embed_weather_forecast' 템플릿을 찾을 수 없어 기본 템플릿으로 전송합니다.")
                    embed_data = { "title": "{emoji} 오늘의 날씨 예보", "description": "오늘의 날씨는 「**{weather_name}**」입니다!\n\n> {description}", "fields": [{"name": "💡 오늘의 팁", "value": "> {tip}", "inline": False}], "footer": {"text": "날씨는 매일 자정에 바뀝니다."} }
                
                embed_data_copy = embed_data.copy()
                embed_data_copy['color'] = weather_info['color']

                embed = format_embed_from_db(embed_data_copy, emoji=weather_info['emoji'], weather_name=weather_info['name'], description=weather_info['description'], tip=weather_info['tip'])
                
                await channel.send(embed=embed)
            except Exception as e:
                logger.error(f"날씨 예보 전송에 실패했습니다: {e}", exc_info=True)
        else:
            logger.error("날씨 예보를 전송할 채널이 설정되지 않았습니다. 관리자 명령어 `/admin setup`을 통해 [알림] 날씨 예보 채널을 설정해주세요.")

    # ▼▼▼ [수정] before_loop 로직 변경 ▼▼▼
    @update_weather.before_loop
    async def before_update_weather(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(5) # 다른 Cog들이 설정을 로드할 시간을 줍니다.
        
        # 봇 시작 시 날씨 정보가 없으면, 즉시 1회 실행하여 초기 날씨를 설정합니다.
        if get_config("current_weather") is None:
            logger.info("현재 날씨가 설정되어 있지 않아, 봇 시작 시 최초 날씨를 설정합니다.")
            try:
                # 루프의 본문 코루틴을 직접 호출하여 즉시 실행
                await self.update_weather.coro(self)
            except Exception as e:
                logger.error(f"최초 날씨 설정 중 오류 발생: {e}", exc_info=True)
    # ▲▲▲ [수정] 완료 ▲▲▲

async def setup(bot: commands.Bot):
    await bot.add_cog(WorldSystem(bot))
