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
        
        # 기존 on_interaction 리스너가 있다면 저장해둡니다.
        self._original_on_interaction = bot.on_interaction
        # 봇의 on_interaction을 우리의 커스텀 핸들러로 교체합니다.
        bot.on_interaction = self.global_interaction_handler

    def cog_unload(self):
        # Cog가 언로드될 때, 원래의 on_interaction 핸들러로 복구합니다.
        self.bot.on_interaction = self._original_on_interaction

    async def global_interaction_handler(self, interaction: discord.Interaction):
        # 봇의 원래 상호작용 처리기나 다른 리스너가 먼저 실행되도록 합니다.
        if self._original_on_interaction:
            await self._original_on_interaction(interaction)

        # 컴포넌트(버튼 등)가 아니면 쿨다운을 적용하지 않습니다.
        if interaction.type != discord.InteractionType.component:
            return

        user_id = interaction.user.id
        now = time.monotonic()
        
        lock = self.user_locks.setdefault(user_id, asyncio.Lock())

        if lock.locked():
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message("⏳ 이전 요청을 처리 중입니다...", ephemeral=True, delete_after=3)
                except discord.errors.HTTPException:
                    pass
            # 이미 처리 중인 요청이 있으면, 현재 요청은 여기서 중단합니다.
            # 하지만 InteractionFailed 오류를 막기 위해 아무것도 하지 않습니다.
            # 상위 레벨에서 이미 응답했을 가능성이 높기 때문입니다.
            return

        async with lock:
            last_action_time = self.user_cooldowns.get(user_id, 0.0)
            if now - last_action_time < self.global_cooldown_seconds:
                if not interaction.response.is_done():
                    try:
                        await interaction.response.send_message(f"⌛ 너무 빨라요! {self.global_cooldown_seconds}초 뒤에 다시 시도해주세요.", ephemeral=True, delete_after=3)
                    except discord.errors.HTTPException:
                        pass
                # 쿨다운에 걸리면 여기서 중단합니다.
                # 마찬가지로 InteractionFailed를 막기 위해 아무것도 하지 않습니다.
                return
            
            # 쿨다운 통과 시, 시간 기록
            self.user_cooldowns[user_id] = now
        
        # 모든 검사를 통과한 상호작용은 이미 봇에 의해 처리되었으므로,
        # 여기서는 추가로 dispatch 할 필요가 없습니다.

async def setup(bot: commands.Bot):
    await bot.add_cog(InteractionHandler(bot))
