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

        if lock.locked():
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message("⏳ 이전 요청을 처리 중입니다...", ephemeral=True, delete_after=2)
                except discord.errors.HTTPException:
                    pass
            return False

        async with lock:
            last_action_time = self.user_cooldowns.get(user_id, 0.0)
            if now - last_action_time < self.global_cooldown_seconds:
                # ▼▼▼ [핵심 수정] 쿨다운 시 응답하는 로직을 다시 활성화하고 안정화합니다. ▼▼▼
                if not interaction.response.is_done():
                    try:
                        await interaction.response.send_message(f"⌛ 너무 빨라요! {self.global_cooldown_seconds}초 뒤에 다시 시도해주세요.", ephemeral=True, delete_after=2)
                    except discord.errors.HTTPException:
                        # 이미 다른 곳에서 응답이 갔을 경우를 대비한 안전장치
                        pass
                return False
                # ▲▲▲ 핵심 수정 끝 ▲▲▲
            
            self.user_cooldowns[user_id] = now
        
        return True

async def setup(bot: commands.Bot):
    await bot.add_cog(InteractionHandler(bot))
