# cogs/games/boss_raid.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
import random
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

# --- [필수] utils 폴더에서 필요한 함수들을 가져옵니다 ---
from utils.database import (
    supabase, get_user_pet, get_config, get_id,
    update_wallet, update_inventory
)
from utils.helpers import format_embed_from_db, create_bar # create_bar는 helpers에 추가해야 할 수 있습니다.

logger = logging.getLogger(__name__)

# --- [상수] 설정 값들을 정의합니다 ---
WEEKLY_BOSS_CHANNEL_KEY = "weekly_boss_channel_id"
MONTHLY_BOSS_CHANNEL_KEY = "monthly_boss_channel_id"
WEEKLY_BOSS_PANEL_MSG_KEY = "weekly_boss_panel_msg_id"
MONTHLY_BOSS_PANEL_MSG_KEY = "monthly_boss_panel_msg_id"
COMBAT_LOG_CHANNEL_KEY = "boss_log_channel_id" # 주요 이벤트 공지용

class BossPanelView(ui.View):
    """
    각 보스 채널에 위치할 영구 패널의 View입니다.
    '도전하기', '현재 랭킹' 버튼을 포함합니다.
    """
    def __init__(self, cog_instance: 'BossRaid', boss_type: str):
        super().__init__(timeout=None)
        self.cog = cog_instance
        self.boss_type = boss_type # 'weekly' 또는 'monthly'

        # 버튼의 custom_id를 통해 어떤 보스에 대한 요청인지 구분합니다.
        challenge_button = ui.Button(label="⚔️ 도전하기", style=discord.ButtonStyle.success, custom_id=f"boss_challenge:{self.boss_type}")
        challenge_button.callback = self.on_challenge_click
        self.add_item(challenge_button)

        ranking_button = ui.Button(label="🏆 현재 랭킹", style=discord.ButtonStyle.secondary, custom_id=f"boss_ranking:{self.boss_type}")
        ranking_button.callback = self.on_ranking_click
        self.add_item(ranking_button)

    async def on_challenge_click(self, interaction: discord.Interaction):
        # '도전하기' 버튼 클릭 시 BossRaid Cog의 핸들러 함수를 호출합니다.
        await self.cog.handle_challenge(interaction, self.boss_type)

    async def on_ranking_click(self, interaction: discord.Interaction):
        # '현재 랭킹' 버튼 클릭 시 BossRaid Cog의 핸들러 함수를 호출합니다.
        await self.cog.handle_ranking(interaction, self.boss_type)


class BossRaid(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_combats = {} # 동시에 진행되는 전투를 관리 (key: user_id, value: asyncio.Task)
        self.combat_lock = asyncio.Lock() # 단 한 명의 유저만 전투를 시작할 수 있도록 하는 전역 Lock

        # --- [주요 루프] ---
        self.panel_updater_loop.start()
        self.combat_engine_loop.start()
        self.boss_reset_loop.start()

    def cog_unload(self):
        # Cog가 언로드될 때 모든 루프를 안전하게 종료합니다.
        self.panel_updater_loop.cancel()
        self.combat_engine_loop.cancel()
        self.boss_reset_loop.cancel()

    # --- 1. 패널 자동 업데이트 루프 ---
    @tasks.loop(minutes=2)
    async def panel_updater_loop(self):
        """2분마다 모든 활성 보스 패널의 정보를 최신 상태로 업데이트합니다."""
        logger.info("[BossRaid] 패널 자동 업데이트 시작...")
        await self.update_all_boss_panels()
        logger.info("[BossRaid] 패널 자동 업데이트 완료.")

    # --- 2. 자동 전투 엔진 루프 ---
    @tasks.loop(minutes=5)
    async def combat_engine_loop(self):
        """5분마다 모든 활성 레이드의 전투를 처리하고 로그를 기록합니다."""
        # 이 기능은 다음 단계에서 구현합니다.
        pass

    # --- 3. 보스 리셋 루프 ---
    @tasks.loop(hours=1)
    async def boss_reset_loop(self):
        """매시간 실행하여 보스를 리셋할 시간인지 확인합니다."""
        # 이 기능은 다음 단계에서 구현합니다.
        pass

    # --- [핵심 기능] 패널 업데이트 ---
    async def update_all_boss_panels(self):
        """주간/월간 보스 패널을 모두 찾아 업데이트합니다."""
        for boss_type in ['weekly', 'monthly']:
            await self.regenerate_panel(boss_type=boss_type)

    async def regenerate_panel(self, boss_type: str, channel: Optional[discord.TextChannel] = None):
        """
        특정 타입의 보스 패널을 (재)생성하거나 업데이트합니다.
        이 함수는 Cog의 핵심적인 UI 관리 역할을 합니다.
        """
        logger.info(f"[{boss_type.upper()}] 패널 재생성 시작...")
        
        # 1. 필요한 채널 및 메시지 ID를 DB에서 가져옵니다.
        channel_key = WEEKLY_BOSS_CHANNEL_KEY if boss_type == 'weekly' else MONTHLY_BOSS_CHANNEL_KEY
        msg_key = WEEKLY_BOSS_PANEL_MSG_KEY if boss_type == 'weekly' else MONTHLY_BOSS_PANEL_MSG_KEY
        
        # 인자로 채널이 주어지지 않으면 DB에서 찾습니다.
        if not channel:
            channel_id = get_id(channel_key)
            if not channel_id or not (channel := self.bot.get_channel(channel_id)):
                logger.warning(f"[{boss_type.upper()}] 보스 채널이 설정되지 않았거나 찾을 수 없습니다.")
                return

        # 2. 현재 활성화된 보스 레이드 정보를 DB에서 가져옵니다.
        raid_res = await supabase.table('boss_raids').select('*, bosses(*)').eq('status', 'active').eq('bosses.type', boss_type).single().execute()
        
        # 3. 임베드와 View를 생성합니다.
        view = BossPanelView(self, boss_type)
        if raid_res.data:
            # 보스가 활성화된 경우
            embed = self.build_boss_panel_embed(raid_res.data)
        else:
            # 보스가 없는 경우 (리셋 대기 중)
            embed = discord.Embed(
                title=f"👑 다음 {boss_type} 보스를 기다리는 중...",
                description="새로운 보스가 곧 나타납니다!",
                color=0x34495E
            )
            # 보스가 없으면 '도전하기' 버튼 등을 비활성화할 수 있습니다.
            for item in view.children:
                item.disabled = True

        # 4. 기존 메시지를 찾아서 수정하거나, 없으면 새로 생성합니다.
        message_id = get_id(msg_key)
        try:
            if message_id:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=embed, view=view)
                logger.info(f"[{boss_type.upper()}] 패널 메시지(ID: {message_id})를 성공적으로 수정했습니다.")
            else:
                # [중요] 패널이 처음 생성되는 경우
                # 이전 메시지를 모두 삭제하여 패널이 항상 맨 아래에 오도록 합니다.
                await channel.purge(limit=100)
                
                new_message = await channel.send(embed=embed, view=view)
                await supabase.table('channel_configs').upsert({'channel_key': msg_key, 'channel_id': str(new_message.id)}).execute()
                await new_message.pin() # 자동으로 메시지 고정
                logger.info(f"[{boss_type.upper()}] 새로운 패널 메시지(ID: {new_message.id})를 생성하고 고정했습니다.")

        except discord.NotFound:
             # 메시지를 찾을 수 없는 경우 (수동으로 삭제됨)
            logger.warning(f"[{boss_type.upper()}] 패널 메시지(ID: {message_id})를 찾을 수 없어 새로 생성합니다.")
            await channel.purge(limit=100)
            new_message = await channel.send(embed=embed, view=view)
            await supabase.table('channel_configs').upsert({'channel_key': msg_key, 'channel_id': str(new_message.id)}).execute()
            await new_message.pin()
        except (discord.Forbidden, discord.HTTPException) as e:
            logger.error(f"[{boss_type.upper()}] 패널 메시지를 수정/생성/고정하는 데 실패했습니다: {e}")

    def build_boss_panel_embed(self, raid_data: Dict[str, Any]) -> discord.Embed:
        """DB에서 가져온 레이드 정보로 패널 임베드를 생성합니다."""
        boss_info = raid_data['bosses']
        
        # 1. 최근 전투 기록 섹션
        recent_logs = raid_data.get('recent_logs', [])
        log_text = "\n".join(recent_logs) if recent_logs else "아직 전투 기록이 없습니다."

        # 2. 보스 정보 섹션
        hp_bar = create_bar(raid_data['current_hp'], boss_info['max_hp'])
        hp_text = f"`{raid_data['current_hp']:,} / {boss_info['max_hp']:,}`\n{hp_bar}"
        stats_text = f"**속성:** `{boss_info.get('element', '무')}` | **공격력:** `{boss_info['attack']:,}` | **방어력:** `{boss_info['defense']:,}`"
        
        # 3. 이벤트 공지 섹션 (조건부)
        # (다음 단계에서 구현)

        embed = discord.Embed(title=f"👑 {boss_info['name']} 현황", color=0xE74C3C)
        if boss_info.get('image_url'):
            embed.set_thumbnail(url=boss_info['image_url'])

        embed.add_field(name="--- 최근 전투 기록 (최대 10개) ---", value=log_text, inline=False)
        embed.add_field(name="--- 보스 정보 ---", value=f"{stats_text}\n\n**체력:**\n{hp_text}", inline=False)
        
        # 푸터에 다음 리셋 시간 등을 추가할 수 있습니다.
        embed.set_footer(text="패널은 2분마다 자동으로 업데이트됩니다.")
        return embed

    # --- [핸들러] 버튼 상호작용 처리 ---
    async def handle_challenge(self, interaction: discord.Interaction, boss_type: str):
        """'도전하기' 버튼 클릭을 처리하는 로직"""
        await interaction.response.send_message(f"[{boss_type}] 도전하기 기능은 현재 개발 중입니다.", ephemeral=True)
        # 여기에 전투 시작 로직이 들어갑니다. (전역 Lock, 도전 횟수 체크 등)

    async def handle_ranking(self, interaction: discord.Interaction, boss_type: str):
        """'현재 랭킹' 버튼 클릭을 처리하는 로직"""
        await interaction.response.send_message(f"[{boss_type}] 랭킹 보기 기능은 현재 개발 중입니다.", ephemeral=True)
        # 여기에 랭킹을 보여주는 임시 메시지 생성 로직이 들어갑니다.

async def setup(bot: commands.Bot):
    # Cog를 봇에 추가합니다.
    await bot.add_cog(BossRaid(bot))
