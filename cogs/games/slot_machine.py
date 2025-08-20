import discord
from discord.ext import commands
from discord import ui
import logging
import random
import asyncio
from typing import Optional

from utils.database import (
    get_wallet, update_wallet, get_config, get_panel_components_from_db,
    save_panel_id, get_panel_id, get_embed_from_db
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

# --- 슬롯머신 설정 ---
REEL_SYMBOLS = ['🍒', '🍊', '🍇', '🍋', '🔔', '5️⃣', '7️⃣']
FRUIT_SYMBOLS = ['🍒', '🍊', '🍇', '🍋', '🔔'] # 과일/벨 심볼만 따로 정의
SPIN_ANIMATION_FRAMES = 5
SPIN_ANIMATION_SPEED = 0.4

# 베팅 금액을 입력받는 모달
class BetAmountModal(ui.Modal, title="ベット額の入力 (スロット)"):
    amount = ui.TextInput(label="金額 (100コイン単位)", placeholder="例: 1000", required=True)

    def __init__(self, cog_instance: 'SlotMachine'):
        super().__init__(timeout=180)
        self.cog = cog_instance
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            bet_amount = int(self.amount.value)
            if bet_amount <= 0 or bet_amount % 100 != 0:
                await interaction.response.send_message("❌ 100コイン単位の正の整数のみ入力できます。", ephemeral=True, delete_after=10)
                return

            wallet = await get_wallet(interaction.user.id)
            if wallet.get('balance', 0) < bet_amount:
                await interaction.response.send_message(f"❌ 残高が不足しています。(現在の残高: {wallet.get('balance', 0):,}{self.currency_icon})", ephemeral=True, delete_after=10)
                return
            
            game_view = SlotMachineGameView(interaction.user, bet_amount, self.cog)
            await game_view.start_game(interaction)
            self.cog.active_sessions.add(interaction.user.id)

        except ValueError:
            await interaction.response.send_message("❌ 数字のみ入力してください。", ephemeral=True, delete_after=10)
        except Exception as e:
            logger.error(f"スロットのベット処理中にエラー: {e}", exc_info=True)
            await interaction.response.send_message("❌ 処理中にエラーが発生しました。", ephemeral=True, delete_after=10)

# 실제 슬롯머신 게임이 진행되는 View
class SlotMachineGameView(ui.View):
    def __init__(self, user: discord.Member, bet_amount: int, cog_instance: 'SlotMachine'):
        super().__init__(timeout=60)
        self.user = user
        self.bet_amount = bet_amount
        self.cog = cog_instance
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.reels = ['❓', '❓', '❓']
        self.final_reels = ['❓', '❓', '❓'] # 스핀 시 결정되므로 초기화

    async def start_game(self, interaction: discord.Interaction):
        embed = self.create_embed("下のボタンでスロットを開始！")
        await interaction.response.send_message(embed=embed, view=self, ephemeral=True)
    
    def create_embed(self, description: str) -> discord.Embed:
        embed = discord.Embed(title="🎰 スロットマシン", description=description, color=0xFF9800)
        embed.add_field(name="結果", value=f"**| {self.reels[0]} | {self.reels[1]} | {self.reels[2]} |**", inline=False)
        embed.add_field(name="ベット額", value=f"`{self.bet_amount:,}`{self.currency_icon}")
        embed.set_footer(text=f"{self.user.display_name}さんのプレイ")
        return embed

    @ui.button(label="スピン！", style=discord.ButtonStyle.success, emoji="🔄")
    async def spin_button(self, interaction: discord.Interaction, button: ui.Button):
        button.disabled = True
        button.label = "回転中..."
        await interaction.response.edit_message(view=self)

        # [✅ 확률 조정] 최종 릴 결과를 새로운 확률 모델에 따라 먼저 결정합니다.
        # 1. 50% 확률로 당첨 여부 결정
        if random.random() < 0.50: # 50% 확률로 당첨
            # 2. 당첨된 경우, 가중치에 따라 당첨 종류 결정
            win_types = ['fruit', 'number', 'seven']
            weights = [30, 15, 5] # 과일: 30, 숫자: 15, 777: 5
            chosen_win = random.choices(win_types, weights=weights, k=1)[0]

            if chosen_win == 'fruit':
                symbol = random.choice(FRUIT_SYMBOLS)
                self.final_reels = [symbol, symbol, symbol]
            elif chosen_win == 'number':
                self.final_reels = ['5️⃣', '5️⃣', '5️⃣']
            else: # 'seven'
                self.final_reels = ['7️⃣', '7️⃣', '7️⃣']
        else: # 50% 확률로 꽝
            # 꽝이 보장되는 릴을 생성합니다. (3개가 모두 같지 않도록)
            while True:
                reels = [random.choice(REEL_SYMBOLS) for _ in range(3)]
                if not (reels[0] == reels[1] == reels[2]):
                    self.final_reels = reels
                    break

        # 애니메이션: 왼쪽 릴부터 하나씩 멈춤
        for i in range(3):
            for _ in range(SPIN_ANIMATION_FRAMES):
                if i < 1: self.reels[0] = random.choice(REEL_SYMBOLS)
                if i < 2: self.reels[1] = random.choice(REEL_SYMBOLS)
                self.reels[2] = random.choice(REEL_SYMBOLS)
                
                embed = self.create_embed("リールが回転中...")
                await interaction.edit_original_response(embed=embed)
                await asyncio.sleep(SPIN_ANIMATION_SPEED)

            self.reels[i] = self.final_reels[i]
            embed = self.create_embed("リールが回転中...")
            await interaction.edit_original_response(embed=embed)
            await asyncio.sleep(0.5)


        # 최종 결과 처리
        payout_rate, payout_name = self._calculate_payout()
        result_text = f"| {self.reels[0]} | {self.reels[1]} | {self.reels[2]} |"
        result_embed = None

        if payout_rate > 0: # 승리
            payout_amount = int(self.bet_amount * payout_rate)
            net_gain = payout_amount - self.bet_amount
            await update_wallet(self.user, net_gain)
            if embed_data := await get_embed_from_db("log_slot_machine_win"):
                result_embed = format_embed_from_db(
                    embed_data, user_mention=self.user.mention,
                    payout_amount=payout_amount, bet_amount=self.bet_amount,
                    result_text=result_text, payout_name=payout_name, payout_rate=payout_rate,
                    currency_icon=self.currency_icon
                )
        else: # 패배
            await update_wallet(self.user, -self.bet_amount)
            if embed_data := await get_embed_from_db("log_slot_machine_lose"):
                result_embed = format_embed_from_db(
                    embed_data, user_mention=self.user.mention,
                    bet_amount=self.bet_amount, result_text=result_text,
                    currency_icon=self.currency_icon
                )
        
        self.cog.active_sessions.discard(self.user.id)
        await self.cog.regenerate_panel(interaction.channel, last_game_log=result_embed)
        await interaction.delete_original_response()
        self.stop()
    
    def _calculate_payout(self) -> tuple[float, str]:
        r = self.reels
        if r[0] == r[1] == r[2]:
            if r[0] == '7️⃣': return 2.0, "トリプルセブン"
            if r[0] == '5️⃣': return 1.5, "数字揃い"
            return 1.0, "フルーツ揃い" # 과일, 벨
        return 0.0, "ハズレ"

    async def on_timeout(self):
        self.cog.active_sessions.discard(self.user.id)

# 메인 패널 View
class SlotMachinePanelView(ui.View):
    def __init__(self, cog_instance: 'SlotMachine'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    async def setup_buttons(self):
        self.clear_items()
        components = await get_panel_components_from_db("panel_slot_machine")
        for button_info in components:
            button = ui.Button(
                label=button_info.get('label'), style=discord.ButtonStyle.success,
                emoji=button_info.get('emoji'), custom_id=button_info.get('component_key')
            )
            button.callback = self.start_game_callback
            self.add_item(button)

    async def start_game_callback(self, interaction: discord.Interaction):
        if interaction.user.id in self.cog.active_sessions:
            await interaction.response.send_message("❌ すでにゲームをプレイ中です。", ephemeral=True, delete_after=5)
            return
        await interaction.response.send_modal(BetAmountModal(self.cog))

# 메인 Cog
class SlotMachine(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_sessions = set()

    async def register_persistent_views(self):
        view = SlotMachinePanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_slot_machine", last_game_log: Optional[discord.Embed] = None):
        embed_key = "panel_slot_machine"
        
        if panel_info := get_panel_id(panel_key):
            if (old_channel := self.bot.get_channel(panel_info['channel_id'])) and (old_message_id := panel_info.get('message_id')):
                try: await (await old_channel.fetch_message(old_message_id)).delete()
                except (discord.NotFound, discord.Forbidden): pass
        
        if last_game_log:
            try: await channel.send(embed=last_game_log)
            except Exception as e: logger.error(f"スロットゲームのログメッセージ送信に失敗: {e}")

        if not (embed_data := await get_embed_from_db(embed_key)):
            logger.warning(f"DBから'{embed_key}'の埋め込みデータが見つからず、パネル生成をスキップします。")
            return

        embed = discord.Embed.from_dict(embed_data)
        view = SlotMachinePanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。(チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(SlotMachine(bot))
