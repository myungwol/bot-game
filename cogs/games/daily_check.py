# bot-game/cogs/daily_check.py

import discord
from discord.ext import commands
from discord import ui
import logging
from typing import Optional

from utils.database import (
    update_wallet, get_config,
    save_panel_id, get_panel_id, get_embed_from_db,
    has_checked_in_today, record_attendance,
    get_id  # [✅ 수정] get_id 함수를 import합니다.
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

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
            await interaction.followup.send("❌ 本日は既に出席チェックが完了しています。", ephemeral=True)
            return

        reward_str = get_config("DAILY_CHECK_REWARD", "100").strip('"')
        attendance_reward = int(reward_str)

        await record_attendance(user.id)
        await update_wallet(user, attendance_reward)
        
        # 유저에게 보내는 확인 메시지 (ephemeral)
        await interaction.followup.send(f"✅ 出席チェックが完了しました！ **`{attendance_reward}`**{self.cog.currency_icon}を獲得しました。", ephemeral=True)

        # [✅ 핵심 수정] 로그 메시지를 생성하고, 설정된 로그 채널에 직접 보냅니다.
        if embed_data := await get_embed_from_db("log_daily_check"):
            log_embed = format_embed_from_db(
                embed_data, user_mention=user.mention, 
                reward=attendance_reward, currency_icon=self.cog.currency_icon
            )
            
            # Cog에 저장된 로그 채널 ID를 사용합니다.
            if self.cog.log_channel_id and (log_channel := self.cog.bot.get_channel(self.cog.log_channel_id)):
                try:
                    await log_channel.send(embed=log_embed)
                except Exception as e:
                    logger.error(f"출석체크 로그 메시지 전송에 실패했습니다: {e}")
            else:
                logger.warning("출석체크 로그 채널이 설정되지 않았거나, 채널을 찾을 수 없습니다.")

        # [✅ 수정] 이제 regenerate_panel은 순수하게 패널 재설치만 담당합니다.
        await self.cog.regenerate_panel(interaction.channel)


class DailyCheck(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.currency_icon = "🪙"
        # [✅ 수정] 로그 채널 ID를 저장할 변수를 추가합니다.
        self.log_channel_id: Optional[int] = None

    # [✅ 수정] Cog가 로드될 때 DB에서 설정을 불러오는 함수를 추가합니다.
    async def cog_load(self):
        await self.load_configs()

    async def load_configs(self):
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        # '/setup'으로 설정한 로그 채널 ID를 불러옵니다.
        self.log_channel_id = get_id("log_daily_check_channel_id")

    async def register_persistent_views(self):
        self.bot.add_view(DailyCheckPanelView(self))

    # [✅ 수정] regenerate_panel 함수에서 last_log 관련 로직을 모두 제거합니다.
    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_daily_check"):
        if panel_info := get_panel_id(panel_key):
            if (old_channel := self.bot.get_channel(panel_info['channel_id'])) and (old_message_id := panel_info.get('message_id')):
                try: await (await old_channel.fetch_message(old_message_id)).delete()
                except (discord.NotFound, discord.Forbidden): pass
        
        embed_data = await get_embed_from_db("panel_daily_check")
        if not embed_data: 
            logger.error("DB에서 'panel_daily_check' 임베드를 찾을 수 없어 패널을 생성할 수 없습니다.")
            return

        embed = discord.Embed.from_dict(embed_data)
        view = DailyCheckPanelView(self)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        # logger.info(f"✅ {panel_key} パネルを正常に生成しました。(チャンネル: #{channel.name})") # 너무 자주 로깅되므로 주석 처리

async def setup(bot: commands.Bot):
    await bot.add_cog(DailyCheck(bot))
