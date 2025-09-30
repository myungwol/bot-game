# bot-game/cogs/games/dice_game.py

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
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

class BetAmountModal(ui.Modal, title="베팅 금액 입력"):
    amount = ui.TextInput(label="금액 (10코인 단위)", placeholder="예: 100", required=True)

    def __init__(self, cog_instance: 'DiceGame'):
        super().__init__(timeout=180)
        self.cog = cog_instance
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            bet_amount = int(self.amount.value)
            if bet_amount <= 0 or bet_amount % 10 != 0:
                await interaction.response.send_message(
                    "❌ 10코인 단위의 양수만 입력할 수 있습니다.",
                    ephemeral=True
                )
                return

            wallet = await get_wallet(interaction.user.id)
            if wallet.get('balance', 0) < bet_amount:
                await interaction.response.send_message(
                    f"❌ 잔액이 부족합니다. (현재 잔액: {wallet.get('balance', 0):,}{self.currency_icon})",
                    ephemeral=True
                )
                return
            
            view = NumberSelectView(interaction.user, bet_amount, self.cog)
            await interaction.response.send_message(f"베팅 금액 `{bet_amount:,}`{self.currency_icon}을(를) 설정했습니다. 다음으로 주사위 눈을 선택해주세요.", view=view, ephemeral=True)
            view.message = await interaction.original_response() 
            self.cog.active_sessions.add(interaction.user.id)
        
        except ValueError:
            await interaction.response.send_message(
                "❌ 숫자만 입력해주세요.",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"주사위 베팅 처리 중 오류: {e}", exc_info=True)
            message_content = "❌ 처리 중 오류가 발생했습니다."
            if not interaction.response.is_done():
                await interaction.response.send_message(message_content, ephemeral=True)
            else:
                await interaction.followup.send(message_content, ephemeral=True)

class NumberSelectView(ui.View):
    def __init__(self, user: discord.Member, bet_amount: int, cog_instance: 'DiceGame'):
        super().__init__(timeout=60)
        self.user = user
        self.bet_amount = bet_amount
        self.cog = cog_instance
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.message: Optional[discord.InteractionMessage] = None

        for i in range(1, 7):
            button = ui.Button(
                label=str(i),
                style=discord.ButtonStyle.secondary,
                custom_id=f"dice_select_{i}",
                emoji="🎲"
            )
            button.callback = self.button_callback
            self.add_item(button)

    async def button_callback(self, interaction: discord.Interaction):
        chosen_number = int(interaction.data['custom_id'].split('_')[-1])

        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(content=f"당신은 `{chosen_number}`을(를) 선택했습니다. 주사위를 굴립니다...", view=self)
        except discord.NotFound:
            return self.stop()

        # ▼▼▼▼▼ 핵심 수정 부분: 확률 조작 로직 ▼▼▼▼▼
        # 16.5%의 확률로 승리하도록 설정합니다.
        if random.random() < 0.165:
            # 승리: 주사위 결과를 유저가 선택한 숫자로 설정
            dice_result = chosen_number
        else:
            # 패배: 유저가 선택한 숫자를 제외한 나머지 5개 숫자 중 하나로 설정
            possible_outcomes = [1, 2, 3, 4, 5, 6]
            possible_outcomes.remove(chosen_number)
            dice_result = random.choice(possible_outcomes)
        # ▲▲▲▲▲ 수정 완료 ▲▲▲▲▲

        result_embed = None
        if chosen_number == dice_result:
            # 승리 시, 순이익은 5배, 로그에 표시될 총 지급액은 6배로 설정
            reward_amount = self.bet_amount * 6
            profit = self.bet_amount * 5
            await update_wallet(self.user, profit)
            
            if embed_data := await get_embed_from_db("log_dice_game_win"):
                result_embed = format_embed_from_db(
                    embed_data, user_mention=self.user.mention,
                    bet_amount=self.bet_amount, reward_amount=reward_amount,
                    chosen_number=chosen_number, dice_result=dice_result,
                    currency_icon=self.currency_icon
                )
        else:
            # 패배 시, 베팅액만큼 차감 (기존과 동일)
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
                await self.message.edit(content="시간이 초과되었습니다.", view=None)
            except discord.NotFound:
                pass

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
            await interaction.response.send_message(
                "❌ 이미 게임을 플레이 중입니다.", 
                ephemeral=True
            )
            return
        await interaction.response.send_modal(BetAmountModal(self.cog))

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
            except Exception as e: logger.error(f"주사위 게임 로그 메시지 전송 실패: {e}")

        if not (embed_data := await get_embed_from_db(embed_key)):
            logger.warning(f"DB에서 '{embed_key}'의 임베드 데이터를 찾을 수 없어 패널 생성을 건너뜁니다.")
            return

        embed = discord.Embed.from_dict(embed_data)
        view = DiceGamePanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} 패널을 성공적으로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(DiceGame(bot))
