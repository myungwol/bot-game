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

class TransferAmountModal(ui.Modal, title="送金金額の入力"):
    amount = ui.TextInput(label="金額", placeholder="送金したいコインの額を入力してください", required=True, style=discord.TextStyle.short)

    def __init__(self, sender: discord.Member, recipient: discord.Member, cog_instance: 'Atm'):
        super().__init__(timeout=180)
        self.sender = sender
        self.recipient = recipient
        self.cog = cog_instance
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount_to_send = int(self.amount.value)
            if amount_to_send <= 0:
                raise ValueError("金額は1以上でなければなりません。")

            sender_wallet = await get_wallet(self.sender.id)
            if sender_wallet.get('balance', 0) < amount_to_send:
                await interaction.response.send_message(f"❌ 残高が不足しています。(現在の残高: {sender_wallet.get('balance', 0):,}{self.currency_icon})", ephemeral=True, delete_after=10)
                return

            params = {'sender_id_param': str(self.sender.id), 'recipient_id_param': str(self.recipient.id), 'amount_param': amount_to_send}
            response = await supabase.rpc('transfer_coins', params).execute()
            
            if not response.data:
                 raise Exception("送金に失敗しました。残高不足またはデータベースエラーの可能性があります。")

            await interaction.response.send_message("✅ 送金が完了しました。パネルを更新します。", ephemeral=True, delete_after=5)

            log_embed = None
            if embed_data := await get_embed_from_db("log_coin_transfer"):
                log_embed = format_embed_from_db(embed_data, sender_mention=self.sender.mention, recipient_mention=self.recipient.mention, amount=f"{amount_to_send:,}", currency_icon=self.currency_icon)
            
            await self.cog.regenerate_panel(interaction.channel, last_transfer_log=log_embed)

        except ValueError:
            await interaction.response.send_message("❌ 金額は数字で入力してください。", ephemeral=True, delete_after=10)
        except Exception as e:
            logger.error(f"송금 처리 중 오류 발생: {e}", exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ 送金中に予期せぬエラーが発生しました。", ephemeral=True, delete_after=10)
            else:
                await interaction.followup.send("❌ 送金中に予期せぬエラーが発生しました。", ephemeral=True, delete_after=10)

class AtmPanelView(ui.View):
    def __init__(self, cog_instance: 'Atm'):
        super().__init__(timeout=None)
        self.cog = cog_instance

        transfer_button = ui.Button(label="コインを送る", style=discord.ButtonStyle.green, emoji="💸", custom_id="atm_start_transfer")
        transfer_button.callback = self.start_transfer
        self.add_item(transfer_button)

    async def start_transfer(self, interaction: discord.Interaction):
        view = ui.View(timeout=180)
        user_select = ui.UserSelect(placeholder="コインを送る相手を選んでください...")
        
        # [✅✅✅ 핵심 수정 ✅✅✅] 누락되었던 함수의 내용을 복원합니다.
        async def select_callback(select_interaction: discord.Interaction):
            try:
                selected_user_id = int(select_interaction.data["values"][0])
                recipient = select_interaction.guild.get_member(selected_user_id)

                if not recipient:
                    await select_interaction.response.send_message("❌ ユーザーが見つかりませんでした。", ephemeral=True, delete_after=10)
                    return

                sender = select_interaction.user

                if recipient.bot or recipient.id == sender.id:
                    await select_interaction.response.send_message("❌ 自分自身やボットには送金できません。", ephemeral=True, delete_after=10)
                    return

                modal = TransferAmountModal(sender, recipient, self.cog)
                await select_interaction.response.send_modal(modal)
                
                await modal.wait()
                await interaction.delete_original_response()
            except discord.NotFound:
                pass
            except Exception as e:
                logger.error(f"ATM 유저 선택 콜백 중 오류: {e}", exc_info=True)

        user_select.callback = select_callback
        view.add_item(user_select)
        await interaction.response.send_message("誰にコインを送りますか？", view=view, ephemeral=True)

class Atm(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def register_persistent_views(self):
        self.bot.add_view(AtmPanelView(self))

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "atm", last_transfer_log: Optional[discord.Embed] = None):
        embed_key = "panel_atm"
        
        if panel_info := get_panel_id(panel_key):
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
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。 (チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Atm(bot))
