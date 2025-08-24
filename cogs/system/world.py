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
```

---

### `panel_updater.py`

```python
import discord
from discord.ext import commands, tasks
import logging
import asyncio

# [✅✅✅ 핵심 수정 ✅✅✅] 실시간으로 DB 정보를 다시 불러올 함수를 import 합니다.
from utils.database import supabase, get_id, load_channel_ids_from_db, get_config

logger = logging.getLogger(__name__)

class PanelUpdater(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_for_panel_updates.start()
        logger.info("PanelUpdater Cog가 성공적으로 초기화되었습니다.")

    def cog_unload(self):
        self.check_for_panel_updates.cancel()

    @tasks.loop(seconds=10.0)
    async def check_for_panel_updates(self):
        # [✅ 구조 개선] 하드코딩된 목록 대신 DB의 SETUP_COMMAND_MAP을 사용합니다.
        setup_map = get_config("SETUP_COMMAND_MAP", {})
        if not setup_map:
            return

        game_panels = {
            key: info for key, info in setup_map.items()
            if info.get("type") == "panel" and "[게임]" in info.get("friendly_name", "")
        }

        try:
            request_keys = [f"panel_regenerate_request_{key}" for key in game_panels.keys()]
            if not request_keys:
                return

            response = await supabase.table('bot_configs').select('config_key').in_('config_key', request_keys).execute()
            
            if not response or not response.data:
                return

            db_requests = {item['config_key'] for item in response.data}
            
            # [✅✅✅ 핵심 수정: 레이스 컨디션 해결 ✅✅✅]
            # 재설치 요청이 하나라도 있다면, DB에서 최신 채널 ID 목록을 즉시 새로고침합니다.
            if db_requests:
                logger.info("새로운 패널 재설치 요청을 감지하여, DB로부터 모든 채널 ID를 새로고침합니다.")
                await load_channel_ids_from_db()

        except Exception as e:
            logger.error(f"패널 업데이트 요청 확인 중 DB 오류 발생: {e}", exc_info=True)
            return

        tasks_to_run = []
        keys_to_delete = []

        for panel_key, info in game_panels.items():
            db_key = f"panel_regenerate_request_{panel_key}"
            
            if db_key in db_requests:
                logger.info(f"DB에서 `{panel_key}` 패널에 대한 재설치 요청을 발견했습니다。")
                
                cog = self.bot.get_cog(info["cog_name"])
                # 이제 이 get_id는 방금 새로고침된 최신 정보를 사용합니다.
                channel_id = get_id(info["key"])

                if not cog or not hasattr(cog, 'regenerate_panel'):
                    logger.error(f"'{info['cog_name']}' Cog를 찾을 수 없거나 'regenerate_panel' 함수가 없습니다.")
                    continue
                
                if not channel_id or not (channel := self.bot.get_channel(channel_id)):
                    logger.error(f"'{panel_key}' 패널의 채널(ID: {channel_id or 'None'})을 찾을 수 없습니다. `/setup`으로 채널을 먼저 설정해주세요.")
                    continue
                
                # 비동기 작업을 리스트에 추가
                tasks_to_run.append(cog.regenerate_panel(channel, panel_key=panel_key))
                keys_to_delete.append(db_key)

        if tasks_to_run:
            results = await asyncio.gather(*tasks_to_run, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    panel_key_for_error = keys_to_delete[i].replace("panel_regenerate_request_", "")
                    logger.error(f"'{panel_key_for_error}' 패널 재설치 중 오류 발생: {result}", exc_info=result)
        
        if keys_to_delete:
            try:
                await supabase.table('bot_configs').delete().in_('config_key', keys_to_delete).execute()
                logger.info(f"DB에서 처리 완료된 요청 키 {len(keys_to_delete)}개를 삭제했습니다.")
            except Exception as e:
                logger.error(f"처리 완료된 패널 요청 키 삭제 중 오류: {e}", exc_info=True)


    @check_for_panel_updates.before_loop
    async def before_check_loop(self):
        await self.bot.wait_until_ready()
        # 봇 시작 시, DB에서 SETUP_COMMAND_MAP을 로드할 시간을 줍니다.
        await asyncio.sleep(5) 

async def setup(bot: commands.Bot):
    await bot.add_cog(PanelUpdater(bot))
