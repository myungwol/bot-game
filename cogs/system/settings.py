# cogs/system/settings.py (강/바다 낚시터 설정 분리 최종본)

import discord
from discord.ext import commands
from discord import app_commands
import logging

from utils.database import save_id_to_db

logger = logging.getLogger(__name__)

class Settings(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("Settings Cog가 성공적으로 초기화되었습니다.")

    setup_group = app_commands.Group(name="setup", description="봇의 여러 설정을 관리합니다.")

    @setup_group.command(name="channel", description="[관리자] 특정 기능에 대한 채널을 설정합니다.")
    @app_commands.describe(
        channel_type="설정할 채널의 종류를 선택하세요.",
        channel="지정할 텍스트 채널을 선택하세요."
    )
    @app_commands.choices(channel_type=[
        # [🔴 핵심] 이 부분이 디스코드에 표시될 선택지입니다.
        app_commands.Choice(name="[낚시] 강 낚시터 패널", value="river_fishing_panel_channel_id"),
        app_commands.Choice(name="[낚시] 바다 낚시터 패널", value="sea_fishing_panel_channel_id"),
        app_commands.Choice(name="코인 활동 로그", value="coin_log_channel_id"),
        app_commands.Choice(name="낚시 결과 로그", value="fishing_log_channel_id"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def set_channel(self, interaction: discord.Interaction, channel_type: app_commands.Choice[str], channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        key = channel_type.value
        channel_id = channel.id
        try:
            await save_id_to_db(key, channel_id)
            logger.info(f"관리자({interaction.user})가 채널 설정을 업데이트했습니다: {key} -> #{channel.name}({channel_id})")
            await interaction.followup.send(
                f"✅ **{channel_type.name}** 채널이 {channel.mention}(으)로 성공적으로 설정되었습니다."
            )
        except Exception as e:
            logger.error(f"채널 설정 저장 중 오류 발생: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ 채널 설정 중 오류가 발생했습니다. 로그를 확인해주세요."
            )

async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))
