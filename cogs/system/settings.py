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
        channel_type="設定するチャンネルの種類を選択してください。",
        channel="指定するテキストチャンネルを選択してください。"
    )
    @app_commands.choices(channel_type=[
        app_commands.Choice(name="[釣り] 川の釣り場パネル", value="river_fishing_panel_channel_id"),
        app_commands.Choice(name="[釣り] 海の釣り場パネル", value="sea_fishing_panel_channel_id"),
        app_commands.Choice(name="コイン活動ログ", value="coin_log_channel_id"),
        app_commands.Choice(name="釣り結果ログ", value="fishing_log_channel_id"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def set_channel(self, interaction: discord.Interaction, channel_type: app_commands.Choice[str], channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        key = channel_type.value
        channel_id = channel.id
        try:
            await save_id_to_db(key, channel_id)
            logger.info(f"管理者({interaction.user})がチャンネル設定を更新しました: {key} -> #{channel.name}({channel_id})")
            await interaction.followup.send(
                f"✅ **{channel_type.name}** チャンネルが {channel.mention} に設定されました。"
            )
        except Exception as e:
            logger.error(f"チャンネル設定の保存中にエラーが発生しました: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ チャンネル設定中にエラーが発生しました。ログを確認してください。"
            )

async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))
