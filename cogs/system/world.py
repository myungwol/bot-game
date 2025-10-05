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
    "sunny": {
        "emoji": "☀️", "name": "晴れ", "water_effect": False, "color": 0xFFAC33,
        "description": "空は一点の雲もなく、暖かい日差しが村を照らしています。",
        "tip": "農作物にとっては最高の成長日和かもしれません！"
    },
    "cloudy": {
        "emoji": "☁️", "name": "曇り", "water_effect": False, "color": 0x95A5A6,
        "description": "過ごしやすい曇り空です。時々、太陽が顔を出すかもしれません。",
        "tip": "のんびりと釣りをするには最適な一日です。"
    },
    "rainy": {
        "emoji": "🌧️", "name": "雨", "water_effect": True, "color": 0x3498DB,
        "description": "しとしとと雨が降り続いています。傘をお忘れなく！",
        "tip": "農場に自動で水がまかれます！水やりの手間が省けそうですね。"
    },
    "stormy": {
        "emoji": "⛈️", "name": "嵐", "water_effect": True, "color": 0x2C3E50,
        "description": "激しい雨と雷が鳴り響いています。外出の際はご注意ください。",
        "tip": "海が荒れた日には珍しい魚が釣れるという噂も…？"
    },
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
        # [✅ 핵심 수정] 랜덤 선택 로직을 더 명확하게 변경
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
                    embed_data = {
                        "title": "{emoji} 今日の天気予報",
                        "description": "今日の天気は「**{weather_name}**」です！\n\n> {description}",
                        "fields": [{"name": "💡 今日のヒント", "value": "> {tip}", "inline": False}],
                        "footer": {"text": "天気は毎日深夜0時に変わります。"}
                    }

                # [✅ 핵심 수정] embed_data가 None일 경우를 대비하여 로직 안정화
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
                logger.error(f"날씨 예보 전송에 실패했습니다: {e}", exc_info=True)
        else:
            # [✅ 핵심 수정] 채널이 설정되지 않았을 때, 명확한 에러 로그를 남깁니다.
            logger.error("天気予報を送信するチャンネルが設定されていません。管理者コマンド`/admin setup`で[通知]天気予報チャンネルを設定してください。")


    @update_weather.before_loop
    async def before_update_weather(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(5)
        
        if get_config("current_weather") is None:
            logger.info("현재 날씨가 설정되어 있지 않아, 최초 설정을 실행합니다.")
            if not self.update_weather.is_running():
                self.update_weather.start()

async def setup(bot: commands.Bot):
    await bot.add_cog(WorldSystem(bot))
