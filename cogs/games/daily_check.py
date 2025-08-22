# bot-game/cogs/daily_check.py

import discord
from discord.ext import commands
from discord import ui
import logging
from typing import Optional

from utils.database import (
    update_wallet, get_config,
    save_panel_id, get_panel_id, get_embed_from_db,
    has_checked_in_today, record_attendance
)
# [✅ 수정] helpers에서 표준 CloseButtonView를 import 합니다.
from utils.helpers import format_embed_from_db, CloseButtonView

logger = logging.getLogger(__name__)

# [✅ 수정] 중복되는 CloseButtonView 클래스 정의를 삭제했습니다.

class DailyCheckPanelView(ui.View):
    def __init__(self, cog_instance: 'DailyCheck'):
        super().__init__(timeout=None)
        self.cog = cog_instance

        check_in_button = ui.Button(
            label="出席チェック",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id="daily_check_button"
        )
        check_in_button.callback = self.check_in_callback
        self.add_item(check_in_button)

    async def check_in_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        
        already_checked_in = await has_checked_in_today(user.id)
        if already_checked_in:
            msg = await interaction.followup.send("❌ 本日は既に出席チェックが完了しています。", ephemeral=True)
            await msg.edit(view=CloseButtonView(user, target_message=msg))
            return

        reward_str = get_config("DAILY_CHECK_REWARD", "100").strip('"')
        attendance_reward = int(reward_str)

        await record_attendance(user.id)
        await update_wallet(user, attendance_reward)
        
        msg = await interaction.followup.send(f"✅ 出席チェックが完了しました！ **`{attendance_reward}`**{self.cog.currency_icon}を獲得しました。", ephemeral=True)
        await msg.edit(view=CloseButtonView(user, target_message=msg))


        log_embed = None
        if embed_data := await get_embed_from_db("log_daily_check"):
            log_embed = format_embed_from_db(
                embed_data, user_mention=user.mention, 
                reward=attendance_reward, currency_icon=self.cog.currency_icon
            )
        
        await self.cog.regenerate_panel(interaction.channel, last_log=log_embed)

class DailyCheck(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.currency_icon = "🪙"

    async def cog_load(self):
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")

    async def register_persistent_views(self):
        self.bot.add_view(DailyCheckPanelView(self))

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_daily_check", last_log: Optional[discord.Embed] = None):
        if last_log:
            try: await channel.send(embed=last_log)
            except Exception as e: logger.error(f"出席チェックのログメッセージ送信に失敗: {e}")

        if panel_info := get_panel_id(panel_key):
            if (old_channel := self.bot.get_channel(panel_info['channel_id'])) and (old_message_id := panel_info.get('message_id')):
                try: await (await old_channel.fetch_message(old_message_id)).delete()
                except (discord.NotFound, discord.Forbidden): pass
        
        embed_data = await get_embed_from_db("panel_daily_check")
        if not embed_data: return

        embed = discord.Embed.from_dict(embed_data)
        view = DailyCheckPanelView(self)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。(チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(DailyCheck(bot))
