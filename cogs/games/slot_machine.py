# bot-game/cogs/slot_machine.py

import discord
from discord.ext import commands
from discord import ui
import logging
import random
import asyncio
from typing import Optional

from utils.database import (
    get_wallet, update_wallet, get_config,
    save_panel_id, get_panel_id, get_embed_from_db
)
from utils.helpers import format_embed_from_db, CloseButtonView

logger = logging.getLogger(__name__)

REEL_SYMBOLS = ['🍒', '🍊', '🍇', '🍋', '🔔', '5️⃣', '7️⃣']
FRUIT_SYMBOLS = ['🍒', '🍊', '🍇', '🍋', '🔔']
SPIN_ANIMATION_FRAMES = 5
SPIN_ANIMATION_SPEED = 0.4

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
                await interaction.response.send_message("❌ 100コイン単位の正の整数のみ入力できます。", ephemeral=True, view=CloseButtonView(interaction.user))
                return

            wallet = await get_wallet(interaction.user.id)
            if wallet.get('balance', 0) < bet_amount:
                await interaction.response.send_message(f"❌ 残高が不足しています。(現在の残高: {wallet.get('balance', 0):,}{self.currency_icon})", ephemeral=True, view=CloseButtonView(interaction.user))
                return
            
            self.cog.active_sessions.add(interaction.user.id)
            await self.cog.update_panel_embed()
            
            game_view = SlotMachineGameView(interaction.user, bet_amount, self.cog)
            await game_view.start_game(interaction)

        except ValueError:
            await interaction.response.send_message("❌ 数字のみ入力してください。", ephemeral=True, view=CloseButtonView(interaction.user))
            
        except Exception as e:
            logger.error(f"スロットのベット処理中にエラー: {e}", exc_info=True)
            message_content = "❌ 処理中にエラーが発生しました。"
            view = CloseButtonView(interaction.user)
            if not interaction.response.is_done():
                await interaction.response.send_message(message_content, ephemeral=True, view=view)
            else:
                await interaction.followup.send(message_content, ephemeral=True, view=view)


class SlotMachineGameView(ui.View):
    def __init__(self, user: discord.Member, bet_amount: int, cog_instance: 'SlotMachine'):
        super().__init__(timeout=30)
        self.user = user
        self.bet_amount = bet_amount
        self.cog = cog_instance
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.reels = ['❓', '❓', '❓']
        self.final_reels = ['❓', '❓', '❓']
        self.message: Optional[discord.InteractionMessage] = None

    async def start_game(self, interaction: discord.Interaction):
        embed = self.create_embed("下のボタンでスロットを開始！")
        await interaction.response.send_message(embed=embed, view=self, ephemeral=True)
        self.message = await interaction.original_response()

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
        await interaction.response.edit_message(embed=self.create_embed("リールが回転中..."), view=self)

        if random.random() < 0.50:
            win_types = ['fruit', 'number', 'seven']
            weights = [30, 15, 5]
            chosen_win = random.choices(win_types, weights=weights, k=1)[0]
            symbol = {'fruit': random.choice(FRUIT_SYMBOLS), 'number': '5️⃣', 'seven': '7️⃣'}[chosen_win]
            self.final_reels = [symbol, symbol, symbol]
        else:
            while True:
                reels = [random.choice(REEL_SYMBOLS) for _ in range(3)]
                if not (reels[0] == reels[1] == reels[2]):
                    self.final_reels = reels
                    break

        for i in range(3):
            for _ in range(SPIN_ANIMATION_FRAMES):
                if i < 1: self.reels[0] = random.choice(REEL_SYMBOLS)
                if i < 2: self.reels[1] = random.choice(REEL_SYMBOLS)
                self.reels[2] = random.choice(REEL_SYMBOLS)
                await interaction.edit_original_response(embed=self.create_embed("リールが回転中..."))
                await asyncio.sleep(SPIN_ANIMATION_SPEED)

            self.reels[i] = self.final_reels[i]
            await interaction.edit_original_response(embed=self.create_embed("リールが回転中..."))
            await asyncio.sleep(0.5)

        payout_rate, payout_name = self._calculate_payout()
        result_text = f"| {self.reels[0]} | {self.reels[1]} | {self.reels[2]} |"
        result_embed = None

        if payout_rate > 0:
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
        else:
            await update_wallet(self.user, -self.bet_amount)
            if embed_data := await get_embed_from_db("log_slot_machine_lose"):
                result_embed = format_embed_from_db(
                    embed_data, user_mention=self.user.mention,
                    bet_amount=self.bet_amount, result_text=result_text,
                    currency_icon=self.currency_icon
                )
        
        self.cog.active_sessions.discard(self.user.id)
        await self.cog.update_panel_embed()
        await self.cog.regenerate_panel(interaction.channel, last_game_log=result_embed)
        
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass
        self.stop()
    
    def _calculate_payout(self) -> tuple[float, str]:
        r = self.reels
        if r[0] == r[1] == r[2]:
            if r[0] == '7️⃣': return 2.0, "トリプルセブン"
            if r[0] == '5️⃣': return 1.5, "数字揃い"
            return 1.0, "フルーツ揃い"
        
        if r.count('🍒') == 2:
            return 0.2, "チェリー賞"

        return 0.0, "ハズレ"

    async def on_timeout(self):
        self.cog.active_sessions.discard(self.user.id)
        await self.cog.update_panel_embed()
        if self.message:
            try:
                await self.message.edit(content="時間切れになりました。", view=None)
            except discord.NotFound:
                pass

class SlotMachinePanelView(ui.View):
    def __init__(self, cog_instance: 'SlotMachine'):
        super().__init__(timeout=None)
        self.cog = cog_instance

        slot_button = ui.Button(
            label="スロットをプレイ",
            style=discord.ButtonStyle.success,
            emoji="🎰",
            custom_id="slot_machine_play_button"
        )
        slot_button.callback = self.start_game_callback
        self.add_item(slot_button)

    async def start_game_callback(self, interaction: discord.Interaction):
        if len(self.cog.active_sessions) >= self.cog.max_active_slots:
            await interaction.response.send_message(f"❌ すべてのスロットマシンが使用中です。しばらく待ってからもう一度お試しください。({len(self.cog.active_sessions)}/{self.cog.max_active_slots})", ephemeral=True, view=CloseButtonView(interaction.user))
            return

        if interaction.user.id in self.cog.active_sessions:
            await interaction.response.send_message("❌ すでにゲームをプレイ中です。", ephemeral=True, view=CloseButtonView(interaction.user))
            return
        await interaction.response.send_modal(BetAmountModal(self.cog))

class SlotMachine(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_sessions = set()
        self.panel_message: Optional[discord.Message] = None
        self.max_active_slots = 5

    async def cog_load(self):
        self.max_active_slots = int(get_config("SLOT_MAX_ACTIVE", "5").strip('"'))
        self.bot.loop.create_task(self._fetch_panel_message())

    async def _fetch_panel_message(self):
        await self.bot.wait_until_ready()
        panel_info = get_panel_id("panel_slot_machine")
        if panel_info and panel_info.get("channel_id") and panel_info.get("message_id"):
            try:
                channel = self.bot.get_channel(panel_info["channel_id"])
                if channel:
                    self.panel_message = await channel.fetch_message(panel_info["message_id"])
                    await self.update_panel_embed()
            except (discord.NotFound, discord.Forbidden):
                self.panel_message = None
                logger.warning("スロットマシンのパネルメッセージが見つからないか、アクセスできませんでした。")

    async def update_panel_embed(self):
        if not self.panel_message: return

        embed_data = await get_embed_from_db("panel_slot_machine")
        if not embed_data: return

        # [✅ 핵심 수정] 원본 임베드 데이터를 수정하는 대신, 새로운 설명 텍스트를 생성하여 적용합니다.
        original_description = embed_data.get('description', '')
        current_players = len(self.active_sessions)
        status_line = f"\n\n**[現在使用中のマシン: {current_players}/{self.max_active_slots}]**"
        
        # 원본 데이터를 기반으로 새로운 임베드 객체를 생성합니다.
        new_embed = discord.Embed.from_dict(embed_data)
        # 새로 생성한 설명 텍스트를 임베드에 설정합니다.
        new_embed.description = original_description + status_line
        
        try:
            await self.panel_message.edit(embed=new_embed)
        except discord.NotFound:
            # 메시지가 삭제된 경우 다시 찾아봅니다.
            await self._fetch_panel_message()
        except Exception as e:
            logger.error(f"スロットパネルの更新中にエラー: {e}")

    async def register_persistent_views(self):
        self.bot.add_view(SlotMachinePanelView(self))

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_slot_machine", last_game_log: Optional[discord.Embed] = None):
        if last_game_log:
            try: await channel.send(embed=last_game_log)
            except Exception as e: logger.error(f"スロットゲームのログメッセージ送信に失敗: {e}")

        if panel_info := get_panel_id(panel_key):
            if (old_channel := self.bot.get_channel(panel_info['channel_id'])) and (old_message_id := panel_info.get('message_id')):
                try: await (await old_channel.fetch_message(old_message_id)).delete()
                except (discord.NotFound, discord.Forbidden): pass

        embed_data = await get_embed_from_db(panel_key)
        if not embed_data: return

        embed = discord.Embed.from_dict(embed_data)
        view = SlotMachinePanelView(self)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        
        # 패널을 재생성했으므로, self.panel_message를 새 메시지로 업데이트하고 상태를 즉시 반영합니다.
        self.panel_message = new_message
        await self.update_panel_embed()
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。(チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(SlotMachine(bot))
