import discord
from discord.ext import commands
from discord import ui
import logging
import random
from typing import Optional

from utils.database import (
    get_wallet, update_wallet, get_config, get_panel_components_from_db,
    save_panel_id, get_panel_id, get_embed_from_db
)
from utils.helpers import format_embed_from_db, CloseButtonView

logger = logging.getLogger(__name__)

# 베팅 금액을 입력받는 모달
class BetAmountModal(ui.Modal, title="ベット額の入力"):
    amount = ui.TextInput(label="金額 (10コイン単位)", placeholder="例: 100", required=True)

    def __init__(self, cog_instance: 'DiceGame'):
        super().__init__(timeout=180)
        self.cog = cog_instance
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            bet_amount = int(self.amount.value)
            if bet_amount <= 0 or bet_amount % 10 != 0:
                msg = await interaction.response.send_message("❌ 10コイン単位の正の整数のみ入力できます。", ephemeral=True)
                await msg.edit(view=CloseButtonView(interaction.user, target_message=msg))
                return

            wallet = await get_wallet(interaction.user.id)
            if wallet.get('balance', 0) < bet_amount:
                msg = await interaction.response.send_message(f"❌ 残高が不足しています。(現在の残高: {wallet.get('balance', 0):,}{self.currency_icon})", ephemeral=True)
                await msg.edit(view=CloseButtonView(interaction.user, target_message=msg))
                return
            
            view = NumberSelectView(interaction.user, bet_amount, self.cog)
            await interaction.response.send_message(f"ベット額 `{bet_amount:,}`{self.currency_icon}を設定しました。次にサイコロの出る目を選択してください。", view=view, ephemeral=True)
            view.message = await interaction.original_response() # 메시지 객체 저장
            self.cog.active_sessions.add(interaction.user.id)

        # [✅ 오류 수정] IndentationError를 해결하기 위해 들여쓰기를 수정합니다.
        except ValueError:
            msg = await interaction.response.send_message("❌ 数字のみ入力してください。", ephemeral=True)
            await msg.edit(view=CloseButtonView(interaction.user, target_message=msg))
        except Exception as e:
            logger.error(f"サイコロのベット処理中にエラー: {e}", exc_info=True)
            if not interaction.response.is_done():
                msg = await interaction.response.send_message("❌ 処理中にエラーが発生しました。", ephemeral=True)
            else:
                msg = await interaction.followup.send("❌ 処理中にエラーが発生しました。", ephemeral=True)
            await msg.edit(view=CloseButtonView(interaction.user, target_message=msg))

# 1~6 숫자 버튼이 있는 View
class NumberSelectView(ui.View):
    def __init__(self, user: discord.Member, bet_amount: int, cog_instance: 'DiceGame'):
        super().__init__(timeout=60)
        self.user = user
        self.bet_amount = bet_amount
        self.cog = cog_instance
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.message: Optional[discord.InteractionMessage] = None

        for i in range(1, 7):
            button = ui.Button(label=str(i), style=discord.ButtonStyle.secondary, emoji="🎲", custom_id=f"dice_choice_{i}")
            button.callback = self.button_callback
            self.add_item(button)

    async def button_callback(self, interaction: discord.Interaction):
        chosen_number = int(interaction.data['custom_id'].split('_')[-1])

        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(content=f"あなたは `{chosen_number}` を選択しました。サイコロを振っています...", view=self)
        except discord.NotFound:
            return self.stop()

        if random.random() < 0.30:
            dice_result = chosen_number
        else:
            possible_outcomes = [1, 2, 3, 4, 5, 6]
            possible_outcomes.remove(chosen_number)
            dice_result = random.choice(possible_outcomes)

        result_embed = None
        if chosen_number == dice_result:
            reward_amount = self.bet_amount * 2
            await update_wallet(self.user, self.bet_amount)
            if embed_data := await get_embed_from_db("log_dice_game_win"):
                result_embed = format_embed_from_db(
                    embed_data, user_mention=self.user.mention,
                    bet_amount=self.bet_amount, reward_amount=reward_amount,
                    chosen_number=chosen_number, dice_result=dice_result,
                    currency_icon=self.currency_icon
                )
        else:
            await update_wallet(self.user, -self.bet_amount)
            if embed_data := await get_embed_from_db("log_dice_game_lose"):
                result_embed = format_embed_from_db(
                    embed_data, user_mention=self.user.mention,
                    bet_amount=self.bet_amount,
                    chosen_number=chosen_number, dice_result=dice_result,
                    currency_icon=self.currency_icon
                )
        
        self.cog.active_sessions.discard(self.user.id)
        await self.cog.regenerate_panel(interaction.channel, last_game_log=result_embed)
        
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass
        self.stop()
    
    async def on_timeout(self):
        self.cog.active_sessions.discard(self.user.id)
        if self.message:
            try:
                await self.message.edit(content="時間切れになりました。", view=None)
            except discord.NotFound:
                pass

# 메인 패널 View
class DiceGamePanelView(ui.View):
    def __init__(self, cog_instance: 'DiceGame'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    async def setup_buttons(self):
        self.clear_items()
        components = await get_panel_components_from_db("panel_dice_game")
        for button_info in components:
            button = ui.Button(
                label=button_info.get('label'), style=discord.ButtonStyle.primary, 
                emoji=button_info.get('emoji'), custom_id=button_info.get('component_key')
            )
            button.callback = self.start_game_callback
            self.add_item(button)

    async def start_game_callback(self, interaction: discord.Interaction):
        if interaction.user.id in self.cog.active_sessions:
            msg = await interaction.response.send_message("❌ すでにゲームをプレイ中です。", ephemeral=True)
            await msg.edit(view=CloseButtonView(interaction.user, target_message=msg))
            return
        await interaction.response.send_modal(BetAmountModal(self.cog))

# 메인 Cog
class DiceGame(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_sessions = set()

    async def register_persistent_views(self):
        view = DiceGamePanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_dice_game", last_game_log: Optional[discord.Embed] = None):
        embed_key = "panel_dice_game"
        
        if panel_info := get_panel_id(panel_key):
            if (old_channel := self.bot.get_channel(panel_info['channel_id'])) and (old_message_id := panel_info.get('message_id')):
                try:
                    await (await old_channel.fetch_message(old_message_id)).delete()
                except (discord.NotFound, discord.Forbidden): pass
        
        if last_game_log:
            try: await channel.send(embed=last_game_log)
            except Exception as e: logger.error(f"サイコロゲームのログメッセージ送信に失敗: {e}")

        if not (embed_data := await get_embed_from_db(embed_key)):
            logger.warning(f"DBから'{embed_key}'の埋め込みデータが見つからず、パネル生成をスキップします。")
            return

        embed = discord.Embed.from_dict(embed_data)
        view = DiceGamePanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。(チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(DiceGame(bot))
