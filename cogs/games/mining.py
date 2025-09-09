# cogs/economy/atm.py

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
from typing import Optional

from utils.database import (
    get_wallet, supabase, get_config,
    save_panel_id, get_panel_id, get_embed_from_db, update_wallet
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

class TransferAmountModal(ui.Modal, title="송금 금액 입력"):
    amount = ui.TextInput(label="금액", placeholder="보낼 코인의 액수를 입력해주세요", required=True, style=discord.TextStyle.short)

    def __init__(self, sender: discord.Member, recipient: discord.Member, cog_instance: 'Atm'):
        super().__init__(timeout=180)
        self.sender = sender
        self.recipient = recipient
        self.cog = cog_instance
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            amount_to_send = int(self.amount.value)
            if amount_to_send <= 0:
                raise ValueError("금액은 1 이상이어야 합니다.")

            sender_wallet = await get_wallet(self.sender.id)
            if sender_wallet.get('balance', 0) < amount_to_send:
                await interaction.followup.send(
                    f"❌ 잔액이 부족합니다. (현재 잔액: {sender_wallet.get('balance', 0):,}{self.currency_icon})", 
                    ephemeral=True
                )
                return

            params = {'sender_id_param': str(self.sender.id), 'recipient_id_param': str(self.recipient.id), 'amount_param': amount_to_send}
            response = await supabase.rpc('transfer_coins', params).execute()
            
            if not (response and hasattr(response, 'data') and response.data is True):
                 raise Exception("송금에 실패했습니다. 잔액 부족 또는 데이터베이스 오류일 수 있습니다.")

            await interaction.followup.send("✅ 송금이 완료되었습니다. 패널을 새로고침합니다.", ephemeral=True)

            log_embed = None
            if embed_data := await get_embed_from_db("log_coin_transfer"):
                log_embed = format_embed_from_db(embed_data, sender_mention=self.sender.mention, recipient_mention=self.recipient.mention, amount=f"{amount_to_send:,}", currency_icon=self.currency_icon)
            
            log_channel = self.cog.bot.get_channel(interaction.channel_id)
            if log_channel:
                 await self.cog.regenerate_panel(log_channel, last_transfer_log=log_embed)

        except ValueError:
            await interaction.followup.send("❌ 금액은 숫자로 입력해주세요.", ephemeral=True)
        except Exception as e:
            logger.error(f"송금 처리 중 오류 발생: {e}", exc_info=True)
            await interaction.followup.send("❌ 송금 중 예기치 않은 오류가 발생했습니다.", ephemeral=True)

class AtmPanelView(ui.View):
    def __init__(self, cog_instance: 'Atm'):
        super().__init__(timeout=None)
        self.cog = cog_instance

        transfer_button = ui.Button(label="코인 보내기", style=discord.ButtonStyle.green, emoji="💸", custom_id="atm_start_transfer")
        transfer_button.callback = self.start_transfer
        self.add_item(transfer_button)

    async def start_transfer(self, interaction: discord.Interaction):
        view = ui.View(timeout=180)
        user_select = ui.UserSelect(placeholder="코인을 보낼 상대를 선택해주세요...")
        
        async def select_callback(select_interaction: discord.Interaction):
            try:
                selected_user_id = int(select_interaction.data["values"][0])
                recipient = select_interaction.guild.get_member(selected_user_id)

                if not recipient:
                    await select_interaction.response.send_message("❌ 유저를 찾을 수 없습니다.", ephemeral=True)
                    return

                sender = select_interaction.user

                if recipient.bot or recipient.id == sender.id:
                    await select_interaction.response.send_message("❌ 자기 자신이나 봇에게는 보낼 수 없습니다.", ephemeral=True)
                    return
                
                await select_interaction.response.send_modal(TransferAmountModal(sender, recipient, self.cog))
                
                try:
                    await interaction.delete_original_response()
                except discord.NotFound:
                    pass

            except Exception as e:
                logger.error(f"ATM 유저 선택 콜백 중 오류: {e}", exc_info=True)

        user_select.callback = select_callback
        view.add_item(user_select)
        await interaction.response.send_message("누구에게 코인을 보내시겠습니까?", view=view, ephemeral=True)

class Atm(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def register_persistent_views(self):
        self.bot.add_view(AtmPanelView(self))

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_atm", last_transfer_log: Optional[discord.Embed] = None):
        embed_key = "panel_atm"
        
        panel_name = panel_key.replace("panel_", "")
        if panel_info := get_panel_id(panel_name):
            old_message_id = panel_info.get('message_id')
            old_channel_id = panel_info.get('channel_id')
            
            if old_message_id and old_channel_id and (old_channel := self.bot.get_channel(old_channel_id)):
                try:
                    message_to_delete = await old_channel.fetch_message(old_message_id)
                    await message_to_delete.delete()
                except (discord.NotFound, discord.Forbidden):
                    logger.warning(f"이전 ATM 패널(ID: {old_message_id})을 원래 위치인 채널 #{old_channel.name}에서도 찾을 수 없었습니다.")
        
        if last_transfer_log:
            try: await channel.send(embed=last_transfer_log)
            except Exception as e: logger.error(f"ATM 송금 로그 메시지 전송 실패: {e}")

        if not (embed_data := await get_embed_from_db(embed_key)):
            return

        embed = discord.Embed.from_dict(embed_data)
        view = AtmPanelView(self)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_name, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} 패널을 성공적으로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Atm(bot))
