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

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            # 기본 상호작용 처리를 위해 dispatch를 호출합니다.
            return self.bot.dispatch('interaction', interaction)

        user_id = interaction.user.id
        now = time.monotonic()
        
        lock = self.user_locks.setdefault(user_id, asyncio.Lock())

        if lock.locked():
            try:
                await interaction.response.send_message("⏳ 이전 요청을 처리 중입니다. 잠시 후 다시 시도해주세요.", ephemeral=True, delete_after=3)
            except discord.errors.InteractionResponded:
                pass
            return

        async with lock:
            last_action_time = self.user_cooldowns.get(user_id, 0.0)
            if now - last_action_time < self.global_cooldown_seconds:
                try:
                    await interaction.response.send_message(f"⌛ 너무 빨라요! {self.global_cooldown_seconds}초 뒤에 다시 시도해주세요.", ephemeral=True, delete_after=3)
                except discord.errors.InteractionResponded:
                    pass
                return

            self.user_cooldowns[user_id] = now
        
        # 쿨다운 검사를 통과한 상호작용만 원래 처리를 위해 dispatch 합니다.
        self.bot.dispatch('interaction', interaction)

async def setup(bot: commands.Bot):
    # on_interaction 리스너는 하나만 있어야 하므로, 기존 리스너를 제거하고 새 리스너를 추가합니다.
    # 'interaction' 이벤트에 대한 기존의 모든 리스너를 제거합니다.
    bot.extra_events['on_interaction'] = []
    cog = InteractionHandler(bot)
    await bot.add_cog(cog)
