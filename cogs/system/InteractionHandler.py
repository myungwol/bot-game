# cogs/system/InteractionHandler.py

import discord
from discord.ext import commands
import time
import asyncio
import logging

logger = logging.getLogger(__name__)

class InteractionHandler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.user_cooldowns: dict[int, float] = {}
        self.user_locks: dict[int, asyncio.Lock] = {}
        self.global_cooldown_seconds: float = 2.0

        self.bot.interaction_handler_cog = self

    async def check_cooldown(self, interaction: discord.Interaction) -> bool:
        if interaction.type != discord.InteractionType.component:
            return True

        user_id = interaction.user.id
        now = time.monotonic()
        
        lock = self.user_locks.setdefault(user_id, asyncio.Lock())

        # ▼▼▼ [핵심 수정] 모든 응답(send_message) 로직을 삭제합니다. ▼▼▼
        if lock.locked():
            # 응답 없이 그냥 False만 반환
            return False

        async with lock:
            last_action_time = self.user_cooldowns.get(user_id, 0.0)
            if now - last_action_time < self.global_cooldown_seconds:
                # 응답 없이 그냥 False만 반환
                return False
            
            self.user_cooldowns[user_id] = now
        # ▲▲▲ 핵심 수정 끝 ▲▲▲
        
        return True

async def setup(bot: commands.Bot):
    await bot.add_cog(InteractionHandler(bot))
