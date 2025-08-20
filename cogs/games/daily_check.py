import discord
from discord.ext import commands, tasks
from discord import ui
import logging
from typing import Optional

from utils.database import (
    update_wallet, get_config, get_panel_components_from_db,
    save_panel_id, get_panel_id, get_embed_from_db,
    has_checked_in_today, record_attendance
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)
ATTENDANCE_REWARD = 100

class DailyCheckPanelView(ui.View):
    def __init__(self, cog_instance: 'DailyCheck'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    async def setup_buttons(self):
        self.clear_items()
        components = await get_panel_components_from_db("panel_daily_check")
        for button_info in components:
            button = ui.Button(
                label=button_info.get('label'), style=discord.ButtonStyle.success,
                emoji=button_info.get('emoji'), custom_id=button_info.get('component_key')
            )
            button.callback = self.check_in_callback
            self.add_item(button)

    async def check_in_callback(self, interaction: discord.Interaction):
        # [✅✅✅ 핵심 수정 ✅✅✅]
        # 모든 DB 작업 전에 defer()를 호출하여 '상호작용 실패'를 방지합니다.
        await interaction.response.defer(ephemeral=True)

        user = interaction.user
        
        already_checked_in = await has_checked_in_today(user.id)
        if already_checked_in:
            await interaction.followup.send("❌ 本日は既に出席チェックが完了しています。", ephemeral=True)
            return

        # DB 작업들을 수행합니다.
        await record_attendance(user.id)
        await update_wallet(user, ATTENDANCE_REWARD)
        
        # 모든 작업이 끝난 후 followup.send로 최종 결과를 보냅니다.
        await interaction.followup.send(f"✅ 出席チェックが完了しました！ **`{ATTENDANCE_REWARD}`**{self.cog.currency_icon}を獲得しました。", ephemeral=True)

        log_embed = None
        if embed_data := await get_embed_from_db("log_daily_check"):
            log_embed = format_embed_from_db(
                embed_data, user_mention=user.mention, 
                reward=ATTENDANCE_REWARD, currency_icon=self.cog.currency_icon
            )
        
        # 패널 재설치는 별개의 작업이므로 그대로 둡니다.
        await self.cog.regenerate_panel(interaction.channel, last_log=log_embed)

class DailyCheck(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.currency_icon = "🪙"

    async def cog_load(self):
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")

    async def register_persistent_views(self):
        view = DailyCheckPanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_daily_check", last_log: Optional[discord.Embed] = None):
        # 로그 메시지를 먼저 보냅니다.
        if last_log:
            try: await channel.send(embed=last_log)
            except Exception as e: logger.error(f"出席チェックのログメッセージ送信に失敗: {e}")

        # 이전 패널을 삭제합니다.
        if panel_info := get_panel_id(panel_key):
            if (old_channel := self.bot.get_channel(panel_info['channel_id'])) and (old_message_id := panel_info.get('message_id')):
                try: await (await old_channel.fetch_message(old_message_id)).delete()
                except (discord.NotFound, discord.Forbidden): pass
        
        # 새 패널을 생성합니다.
        embed_data = await get_embed_from_db("panel_daily_check")
        if not embed_data: return

        embed = discord.Embed.from_dict(embed_data)
        view = DailyCheckPanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。(チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(DailyCheck(bot))
