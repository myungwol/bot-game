import discord
from discord.ext import commands
import time
import asyncio
import logging

logger = logging.getLogger(__name__)

class InteractionHandler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 사용자별 마지막 상호작용 시간을 저장합니다.
        # key: user_id, value: time.monotonic()
        self.user_cooldowns: dict[int, float] = {}
        # 동시 처리 방지를 위한 락
        self.user_locks: dict[int, asyncio.Lock] = {}
        # 쿨다운 시간(초)
        self.global_cooldown_seconds: float = 1.5

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        # 컴포넌트(버튼, 선택 메뉴 등) 상호작용에만 쿨다운을 적용합니다.
        if interaction.type != discord.InteractionType.component:
            return

        user_id = interaction.user.id
        now = time.monotonic()
        
        # 락을 가져오거나 생성합니다.
        lock = self.user_locks.setdefault(user_id, asyncio.Lock())

        # 만약 해당 유저의 락이 이미 잠겨있다면, 다른 상호작용이 처리 중이라는 의미이므로 무시합니다.
        if lock.locked():
            try:
                # 사용자에게 피드백을 주어 버튼이 무시되었음을 알립니다.
                await interaction.response.send_message("⏳ 이전 요청을 처리 중입니다. 잠시 후 다시 시도해주세요.", ephemeral=True, delete_after=3)
            except discord.errors.InteractionResponded:
                pass # 이미 다른 곳에서 응답한 경우 그냥 넘어갑니다.
            return

        async with lock:
            # 쿨다운 시간을 확인합니다.
            last_action_time = self.user_cooldowns.get(user_id, 0.0)
            if now - last_action_time < self.global_cooldown_seconds:
                # 쿨다운 중임을 알리는 메시지를 보냅니다.
                try:
                    await interaction.response.send_message(f"⌛ 너무 빨라요! {self.global_cooldown_seconds}초 뒤에 다시 시도해주세요.", ephemeral=True, delete_after=3)
                except discord.errors.InteractionResponded:
                    pass
                return # 쿨다운 중이면 여기서 처리를 중단합니다.

            # 쿨다운을 통과하면, 마지막 상호작용 시간을 업데이트합니다.
            self.user_cooldowns[user_id] = now
        
        # 쿨다운 검사를 통과한 상호작용은 원래대로 처리되도록 봇에 전달합니다.
        # 이 줄이 없으면 어떤 버튼도 작동하지 않습니다!
        self.bot.dispatch('interaction', interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(InteractionHandler(bot))
