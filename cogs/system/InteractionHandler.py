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

    @commands.Cog.listener('on_interaction')
    async def global_interaction_handler(self, interaction: discord.Interaction):
        # 봇의 기본 상호작용 처리가 먼저 실행되도록 이벤트를 다시 디스패치합니다.
        # 이렇게 하면 View 콜백 등이 먼저 실행될 기회를 가집니다.
        self.bot.dispatch('interaction_dispatch', interaction)

        if interaction.type != discord.InteractionType.component:
            return

        user_id = interaction.user.id
        now = time.monotonic()
        
        lock = self.user_locks.setdefault(user_id, asyncio.Lock())

        if lock.locked():
            # ▼▼▼ [핵심 수정] is_done()으로 응답 여부 확인 ▼▼▼
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message("⏳ 이전 요청을 처리 중입니다.", ephemeral=True, delete_after=3)
                except discord.errors.HTTPException:
                    pass
            return

        async with lock:
            last_action_time = self.user_cooldowns.get(user_id, 0.0)
            if now - last_action_time < self.global_cooldown_seconds:
                # ▼▼▼ [핵심 수정] is_done()으로 응답 여부 확인 ▼▼▼
                if not interaction.response.is_done():
                    try:
                        await interaction.response.send_message(f"⌛ 너무 빨라요! {self.global_cooldown_seconds}초 뒤에 다시 시도해주세요.", ephemeral=True, delete_after=3)
                    except discord.errors.HTTPException:
                        pass
                return

            self.user_cooldowns[user_id] = now
        
async def setup(bot: commands.Bot):
    # 'interaction'을 'interaction_dispatch'로 복제하여 리스너 순서를 제어합니다.
    # 이제 봇의 기본 View 처리기가 먼저 실행된 후, 우리의 핸들러가 실행됩니다.
    bot.add_listener(bot.on_interaction, name='on_interaction_dispatch')
    await bot.add_cog(InteractionHandler(bot))
