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
        self.global_cooldown_seconds: float = 1.5

        # main.py의 MyBot 인스턴스에 자기 자신을 등록
        self.bot.interaction_handler_cog = self

    async def check_cooldown(self, interaction: discord.Interaction) -> bool:
        """
        쿨다운을 확인하고, 통과하면 True, 걸리면 False를 반환합니다.
        메시지 전송은 여기서 직접 처리합니다.
        """
        if interaction.type != discord.InteractionType.component:
            return True # 컴포넌트가 아니면 항상 통과

        user_id = interaction.user.id
        now = time.monotonic()
        
        lock = self.user_locks.setdefault(user_id, asyncio.Lock())

        if lock.locked():
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message("⏳ 이전 요청을 처리 중입니다...", ephemeral=True, delete_after=3)
                except discord.errors.HTTPException:
                    pass
            return False # 처리 중이므로 진행 불가

        async with lock:
            last_action_time = self.user_cooldowns.get(user_id, 0.0)
            if now - last_action_time < self.global_cooldown_seconds:
                if not interaction.response.is_done():
                    try:
                        await interaction.response.send_message(f"⌛ 너무 빨라요! {self.global_cooldown_seconds}초 뒤에 다시 시도해주세요.", ephemeral=True, delete_after=3)
                    except discord.errors.HTTPException:
                        pass
                return False # 쿨다운에 걸렸으므로 진행 불가
            
            self.user_cooldowns[user_id] = now
        
        return True # 모든 검사를 통과했으므로 진행 가능

async def setup(bot: commands.Bot):
    await bot.add_cog(InteractionHandler(bot))
