# bot-game/cogs/games/cooking.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
import json
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone, timedelta

from utils.database import (
    get_inventory, get_wallet, get_item_database, get_config, supabase,
    save_panel_id, get_panel_id, get_embed_from_db, update_inventory, update_wallet,
    get_id
)
from utils.helpers import format_embed_from_db, format_timedelta_minutes_seconds

logger = logging.getLogger(__name__)

# 이 리스트는 거래 가능한 아이템 목록과 유사하게, 요리에 사용할 수 있는 아이템 카테고리를 정의합니다.
COOKABLE_CATEGORIES = ["농장_작물", "광물"] # 추후 '물고기' 등 추가 가능

class Cooking(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.currency_icon = "🪙"
        self.check_completed_cooking.start()

    async def cog_load(self):
        game_config = get_config("GAME_CONFIG", {})
        self.currency_icon = game_config.get("CURRENCY_ICON", "🪙")

    def cog_unload(self):
        self.check_completed_cooking.cancel()

    @tasks.loop(minutes=1)
    async def check_completed_cooking(self):
        # ... (요리 완료 체크 로직은 4단계에서 추가)
        pass

    @check_completed_cooking.before_loop
    async def before_check_completed_cooking(self):
        await self.bot.wait_until_ready()

    async def register_persistent_views(self):
        # ... (UI View 등록 로직은 4단계에서 추가)
        pass

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_cooking_creation", **kwargs):
        # ... (패널 재생성 로직은 4단계에서 추가)
        pass
