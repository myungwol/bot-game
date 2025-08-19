import discord
from discord.ext import commands
from discord import ui
import logging

from utils.database import (
    get_wallet, supabase, get_config, get_panel_components_from_db,
    save_panel_id, get_panel_id, get_embed_from_db
)

logger = logging.getLogger(__name__)

class TransferAmountModal(ui.Modal, title="送金金額の入力"):
    """송금할 금액을 입력받는 Modal 클래스"""
    amount = ui.TextInput(label="金額", placeholder="送金したいコインの額を入力してください", required=True, style=discord.TextStyle.short)

    def __init__(self, sender: discord.Member, recipient: discord.Member):
        super().__init__(timeout=180)
        self.sender = sender
        self.recipient = recipient
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

            economy_cog = interaction.client.get_cog("EconomyCore")
            if economy_cog:
                await economy_cog.log_coin_transfer(self.sender, self.recipient, amount_to_send)

            await interaction.response.send_message(f"✅ {self.recipient.mention}さんへ `{amount_to_send:,}`{self.currency_icon} を正常に送金しました。", ephemeral=True)

        except ValueError:
            await interaction.response.send_message("❌ 金額は数字で入力してください。", ephemeral=True, delete_after=10)
        except Exception as e:
            logger.error(f"송금 처리 중 오류 발생: {e}", exc_info=True)
            await interaction.response.send_message("❌ 送金中に予期せぬエラーが発生しました。", ephemeral=True, delete_after=10)


class AtmPanelView(ui.View):
    """ATM 패널의 버튼과 동작을 관리하는 영구 View"""
    def __init__(self, cog_instance: 'Atm'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    async def setup_buttons(self):
        self.clear_items()
        components = await get_panel_components_from_db("atm")
        for button_info in components:
            button = ui.Button(
                label=button_info.get('label'), 
                style=discord.ButtonStyle.green, 
                emoji=button_info.get('emoji'), 
                custom_id=button_info.get('component_key')
            )
            if button.custom_id == "start_transfer":
                button.callback = self.start_transfer
            self.add_item(button)

    async def start_transfer(self, interaction: discord.Interaction):
        view = ui.View(timeout=180)
        user_select = ui.UserSelect(placeholder="コインを送る相手を選んでください...")
        
        async def select_callback(select_interaction: discord.Interaction):
            recipient = select_interaction.data.users[0]
            sender = select_interaction.user

            if recipient.bot or recipient.id == sender.id:
                await select_interaction.response.send_message("❌ 自分自身やボットには送金できません。", ephemeral=True, delete_after=10)
                return

            modal = TransferAmountModal(sender, recipient)
            await select_interaction.response.send_modal(modal)
            
            await modal.wait()
            try:
                await interaction.delete_original_response()
            except discord.NotFound:
                pass

        user_select.callback = select_callback
        view.add_item(user_select)
        await interaction.response.send_message("誰にコインを送りますか？", view=view, ephemeral=True)


class Atm(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def register_persistent_views(self):
        view = AtmPanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "atm"):
        embed_key = "panel_atm"
        
        if (panel_info := get_panel_id(panel_key)) and (old_id := panel_info.get('message_id')):
            try:
                await (await channel.fetch_message(old_id)).delete()
            except (discord.NotFound, discord.Forbidden):
                pass
        
        if not (embed_data := await get_embed_from_db(embed_key)):
            logger.warning(f"DB에서 '{embed_key}' 임베드 데이터를 찾을 수 없어, 패널 생성을 건너뜁니다.")
            return

        embed = discord.Embed.from_dict(embed_data)
        view = AtmPanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。 (チャンネル: #{channel.name})")


async def setup(bot: commands.Bot):
    await bot.add_cog(Atm(bot))
