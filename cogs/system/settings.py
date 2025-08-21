import discord
from discord.ext import commands
from discord import app_commands
import logging
from utils.database import get_item_database
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
    @app_commands.command(name="debugitem", description="[관리자] 봇의 아이템 캐시 정보를 확인합니다.")
    @app_commands.describe(item_name="캐시에서 확인할 아이템의 정확한 이름")
    @app_commands.checks.has_permissions(administrator=True)
    async def debug_item_command(self, interaction: discord.Interaction, item_name: str):
        # 이 명령어는 DB를 다시 조회하는게 아니라, 봇이 현재 메모리에 저장하고 있는 데이터를 보여줍니다.
        from utils.database import get_item_database

        item_db_cache = get_item_database()
        item_data = item_db_cache.get(item_name)
        
        if not item_data:
            await interaction.response.send_message(f"❌ **'현재 봇의 캐시'**에서 `{item_name}` 아이템을 찾을 수 없습니다.\n이름이 정확한지 확인해주세요.", ephemeral=True)
            return

        # 보기 쉽게 문자열로 변환하여 전송
        response_str = f"## 봇 캐시 데이터: `{item_name}`\n"
        response_str += "```python\n"
        response_str += "{\n"
        for key, value in item_data.items():
            # 문자열 값은 따옴표로 감싸서 명확하게 표시
            value_repr = f"'{value}'" if isinstance(value, str) else value
            response_str += f"    '{key}': {value_repr},\n"
        response_str += "}\n```"
        
        await interaction.response.send_message(response_str, ephemeral=True)
async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))
