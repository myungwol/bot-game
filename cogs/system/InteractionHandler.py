# InteractionHandler.py

import discord
from discord.ext import commands
import time
import asyncio
import logging

# 로거 인스턴스를 가져옵니다.
logger = logging.getLogger(__name__)

class InteractionHandler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.user_cooldowns: dict[int, float] = {}
        self.user_locks: dict[int, asyncio.Lock] = {}
        self.global_cooldown_seconds: float = 2.0

        # main.py의 MyBot 인스턴스에 자기 자신을 등록
        self.bot.interaction_handler_cog = self

    async def check_cooldown(self, interaction: discord.Interaction) -> bool:
        """
        쿨다운을 확인하고, 통과하면 True, 걸리면 False를 반환합니다.
        메시지 전송은 여기서 직접 처리합니다.
        """
        # ▼▼▼ [로깅 추가] 어떤 상호작용이 들어왔는지 기록합니다. ▼▼▼
        user_id = interaction.user.id
        custom_id = interaction.data.get('custom_id', 'N/A') if interaction.data else 'N/A'
        logger.info(f"[쿨다운 검사 시작] User: {interaction.user} ({user_id}), Component ID: '{custom_id}'")
        # ▲▲▲ 로깅 추가 끝 ▲▲▲

        if interaction.type != discord.InteractionType.component:
            # ▼▼▼ [로깅 추가] 컴포넌트가 아니므로 검사를 통과했음을 기록합니다. ▼▼▼
            logger.info(f"-> [쿨다운 통과] '{interaction.type}' 타입은 검사 대상이 아닙니다.")
            # ▲▲▲ 로깅 추가 끝 ▲▲▲
            return True # 컴포넌트(버튼, 선택 메뉴 등)가 아니면 항상 통과

        now = time.monotonic()
        
        lock = self.user_locks.setdefault(user_id, asyncio.Lock())

        if lock.locked():
            # ▼▼▼ [로깅 추가] 이전 요청 처리 중으로 인해 요청이 차단되었음을 기록합니다. ▼▼▼
            logger.warning(f"-> [쿨다운 차단] User: {user_id}, 사유: 이전 요청 처리 중 (Lock Active)")
            # ▲▲▲ 로깅 추가 끝 ▲▲▲
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message("⏳ 이전 요청을 처리 중입니다...", ephemeral=True, delete_after=2)
                except discord.errors.HTTPException:
                    pass
            return False # 처리 중이므로 진행 불가

        async with lock:
            last_action_time = self.user_cooldowns.get(user_id, 0.0)
            elapsed_time = now - last_action_time
            
            if elapsed_time < self.global_cooldown_seconds:
                # ▼▼▼ [로깅 추가] 쿨다운 시간 미경과로 요청이 차단되었음을 기록합니다. ▼▼▼
                remaining = self.global_cooldown_seconds - elapsed_time
                logger.warning(f"-> [쿨다운 차단] User: {user_id}, 사유: 쿨다운 적용 중 ({remaining:.2f}초 남음)")
                # ▲▲▲ 로깅 추가 끝 ▲▲▲
                if not interaction.response.is_done():
                    try:
                        await interaction.response.send_message(f"⌛ 너무 빨라요! {self.global_cooldown_seconds}초 뒤에 다시 시도해주세요.", ephemeral=True, delete_after=2)
                    except discord.errors.HTTPException:
                        pass
                return False # 쿨다운에 걸렸으므로 진행 불가
            
            self.user_cooldowns[user_id] = now
        
        # ▼▼▼ [로깅 추가] 모든 검사를 통과했음을 기록합니다. ▼▼▼
        logger.info(f"-> [쿨다운 통과] User: {user_id}, 모든 검사를 통과했습니다.")
        # ▲▲▲ 로깅 추가 끝 ▲▲▲
        return True # 모든 검사를 통과했으므로 진행 가능

async def setup(bot: commands.Bot):
    await bot.add_cog(InteractionHandler(bot))
