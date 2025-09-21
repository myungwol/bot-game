# cogs/system/InteractionHandler.py

import discord
from discord.ext import commands
import time
import asyncio
import logging
# ▼▼▼ [핵심 수정] main 파일의 MyBot 클래스를 직접 import 합니다. ▼▼▼
from main import MyBot

logger = logging.getLogger(__name__)

class InteractionHandler(commands.Cog):
    # ▼▼▼ [핵심 수정] bot의 타입을 MyBot으로 명시합니다. ▼▼▼
    def __init__(self, bot: MyBot):
        self.bot = bot
        self.user_cooldowns: dict[int, float] = {}
        self.user_locks: dict[int, asyncio.Lock] = {}
        self.global_cooldown_seconds: float = 2.0

        self.bot.interaction_handler_cog = self
        logger.info("✅ [진단] InteractionHandler Cog가 초기화되고 'bot.interaction_handler_cog'에 성공적으로 등록되었습니다.")

    async def check_cooldown(self, interaction: discord.Interaction) -> bool:
        user_id = interaction.user.id
        custom_id = interaction.data.get('custom_id', 'N/A') if interaction.data else 'N/A'
        logger.info(f"[쿨다운 검사 시작] User: {interaction.user} ({user_id}), Component ID: '{custom_id}'")

        if interaction.type != discord.InteractionType.component:
            logger.info(f"-> [쿨다운 통과] '{interaction.type}' 타입은 검사 대상이 아닙니다.")
            return True

        now = time.monotonic()
        
        lock = self.user_locks.setdefault(user_id, asyncio.Lock())

        if lock.locked():
            logger.warning(f"-> [쿨다운 차단] User: {user_id}, 사유: 이전 요청 처리 중 (Lock Active)")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message("⏳ 이전 요청을 처리 중입니다...", ephemeral=True, delete_after=2)
                except discord.errors.HTTPException:
                    pass
            return False

        async with lock:
            last_action_time = self.user_cooldowns.get(user_id, 0.0)
            elapsed_time = now - last_action_time
            
            if elapsed_time < self.global_cooldown_seconds:
                remaining = self.global_cooldown_seconds - elapsed_time
                logger.warning(f"-> [쿨다운 차단] User: {user_id}, 사유: 쿨다운 적용 중 ({remaining:.2f}초 남음)")
                if not interaction.response.is_done():
                    try:
                        await interaction.response.send_message(f"⌛ 너무 빨라요! {self.global_cooldown_seconds}초 뒤에 다시 시도해주세요.", ephemeral=True, delete_after=2)
                    except discord.errors.HTTPException:
                        pass
                return False
            
            self.user_cooldowns[user_id] = now
        
        logger.info(f"-> [쿨다운 통과] User: {user_id}, 모든 검사를 통과했습니다.")
        return True

# ▼▼▼ [핵심 수정] bot의 타입을 MyBot으로 명시합니다. ▼▼▼
async def setup(bot: MyBot):
    await bot.add_cog(InteractionHandler(bot))
