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
    get_id
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
        # 1. 상호작용에 응답하여 "생각 중..." 상태로 만듭니다. (사용자에게만 보임)
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        
        # 2. 이미 출석했는지 확인합니다.
        already_checked_in = await has_checked_in_today(user.id)
        if already_checked_in:
            await interaction.followup.send("❌ 本日は既に出席チェックが完了しています。", ephemeral=True)
            return

        # 3. 보상을 설정하고 DB에 기록합니다.
        reward_str = get_config("DAILY_CHECK_REWARD", "100").strip('"')
        attendance_reward = int(reward_str)

        await record_attendance(user.id)  # 'daily_check_in' 활동 기록
        await update_wallet(user, attendance_reward)
        
        # 4. 버튼을 누른 유저에게만 보이는 비공개 확인 메시지를 보냅니다.
        await interaction.followup.send(f"✅ 出席チェックが完了しました！ **`{attendance_reward}`**{self.cog.currency_icon}を獲得しました。", ephemeral=True)

        # --- [✅✅✅ 핵심 수정 ✅✅✅] ---
        # 5. 모두에게 보이는 공개 로그 메시지를 현재 채널에 보내고 패널을 재생성합니다.
        log_embed = None
        if embed_data := await get_embed_from_db("log_daily_check"):
            log_embed = format_embed_from_db(
                embed_data, user_mention=user.mention, 
                reward=attendance_reward, currency_icon=self.cog.currency_icon
            )
        
        if log_embed:
            try:
                # 현재 채널에 공개적으로 로그 메시지를 보냅니다.
                await interaction.channel.send(embed=log_embed)
            except Exception as e:
                logger.error(f"출석체크 공개 로그 메시지 전송 실패 (채널: {interaction.channel.id}): {e}")

            # 별도의 로그 채널이 설정되어 있고, 현재 채널과 다른 경우에만 추가로 보냅니다.
            if self.cog.log_channel_id and self.cog.log_channel_id != interaction.channel.id:
                if log_channel := self.cog.bot.get_channel(self.cog.log_channel_id):
                    try:
                        await log_channel.send(embed=log_embed)
                    except Exception as e:
                        logger.error(f"별도 출석체크 로그 채널로 전송 실패: {e}")
        
        # 6. 패널을 재생성하여 메시지 목록의 맨 아래로 내립니다.
        await self.cog.regenerate_panel(interaction.channel)


class DailyCheck(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.currency_icon = "🪙"
        self.log_channel_id: Optional[int] = None

    async def cog_load(self):
        await self.load_configs()

    async def load_configs(self):
        # [✅ 수정] GAME_CONFIG에서 CURRENCY_ICON을 가져오도록 통일
        game_config = get_config("GAME_CONFIG", {})
        self.currency_icon = game_config.get("CURRENCY_ICON", "🪙")
        self.log_channel_id = get_id("log_daily_check_channel_id")

    async def register_persistent_views(self):
        self.bot.add_view(DailyCheckPanelView(self))

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_daily_check"):
        # 이전 패널 메시지 삭제
        if panel_info := get_panel_id(panel_key):
            if (old_channel := self.bot.get_channel(panel_info['channel_id'])) and (old_message_id := panel_info.get('message_id')):
                # 현재 채널에 있는 패널만 삭제하도록 확인
                if old_channel.id == channel.id:
                    try:
                        await (await old_channel.fetch_message(old_message_id)).delete()
                    except (discord.NotFound, discord.Forbidden):
                        pass
        
        # 새 패널 생성
        embed_data = await get_embed_from_db("panel_daily_check")
        if not embed_data: 
            logger.error("DB에서 'panel_daily_check' 임베드를 찾을 수 없어 패널을 생성할 수 없습니다.")
            return

        embed = discord.Embed.from_dict(embed_data)
        view = DailyCheckPanelView(self)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。(チャンネル: #{channel.name})")


async def setup(bot: commands.Bot):
    await bot.add_cog(DailyCheck(bot))
