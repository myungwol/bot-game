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

REEL_SYMBOLS = ['🍒', '🍊', '🍇', '🍋', '🔔', '5️⃣', '7️⃣']
FRUIT_SYMBOLS = ['🍒', '🍊', '🍇', '🍋', '🔔']
SPIN_ANIMATION_FRAMES = 5
SPIN_ANIMATION_SPEED = 0.4
MAX_ACTIVE_SLOTS = 5

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
            
            self.cog.active_sessions.add(interaction.user.id)
            await self.cog.update_panel_embed() # [✅] 패널 업데이트 호출
            
            game_view = SlotMachineGameView(interaction.user, bet_amount, self.cog)
            await game_view.start_game(interaction)

        except ValueError:
            await interaction.response.send_message("❌ 数字のみ入力してください。", ephemeral=True, delete_after=10)
        except Exception as e:
            logger.error(f"スロットのベット処理中にエラー: {e}", exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ 処理中にエラーが発生しました。", ephemeral=True, delete_after=10)

class SlotMachineGameView(ui.View):
    # ... (초기화 및 start_game, create_embed 메소드는 이전과 동일) ...
    
    @ui.button(label="スピン！", style=discord.ButtonStyle.success, emoji="🔄")
    async def spin_button(self, interaction: discord.Interaction, button: ui.Button):
        # ... (애니메이션 및 결과 계산 로직은 이전과 동일) ...
        
        self.cog.active_sessions.discard(self.user.id)
        await self.cog.update_panel_embed() # [✅] 패널 업데이트 호출
        await self.cog.regenerate_panel(interaction.channel, last_game_log=result_embed)
        
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass
        self.stop()
    
    # ... (_calculate_payout 메소드는 이전과 동일) ...

    async def on_timeout(self):
        self.cog.active_sessions.discard(self.user.id)
        await self.cog.update_panel_embed() # [✅] 패널 업데이트 호출
        if self.message:
            try:
                await self.message.edit(content="時間切れになりました。", view=None)
            except discord.NotFound:
                pass

class SlotMachinePanelView(ui.View):
    # ... (이전과 동일) ...

class SlotMachine(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_sessions = set()
        self.panel_message: Optional[discord.Message] = None

    # [✅✅✅ 핵심 추가 1 ✅✅✅]
    # Cog가 로드될 때, DB에서 패널 메시지 정보를 불러옵니다.
    async def cog_load(self):
        self.bot.loop.create_task(self._fetch_panel_message())

    async def _fetch_panel_message(self):
        await self.bot.wait_until_ready()
        panel_info = get_panel_id("panel_slot_machine")
        if panel_info and panel_info.get("channel_id") and panel_info.get("message_id"):
            try:
                channel = self.bot.get_channel(panel_info["channel_id"])
                if channel:
                    self.panel_message = await channel.fetch_message(panel_info["message_id"])
                    await self.update_panel_embed() # 봇 시작 시 상태 업데이트
            except (discord.NotFound, discord.Forbidden):
                self.panel_message = None
                logger.warning("スロットマシンのパネルメッセージが見つからないか、アクセスできませんでした。")

    # [✅✅✅ 핵심 추가 2 ✅✅✅]
    # 패널 임베드를 실시간으로 업데이트하는 함수입니다.
    async def update_panel_embed(self):
        if not self.panel_message:
            return

        embed_data = await get_embed_from_db("panel_slot_machine")
        if not embed_data:
            return

        current_players = len(self.active_sessions)
        status_line = f"\n\n**[現在使用中のマシン: {current_players}/{MAX_ACTIVE_SLOTS}]**"
        
        # 원본 설명에 상태 라인을 추가합니다.
        embed_data['description'] += status_line
        
        new_embed = discord.Embed.from_dict(embed_data)
        
        try:
            await self.panel_message.edit(embed=new_embed)
        except discord.NotFound:
            # 메시지가 수동으로 삭제된 경우, 다시 불러옵니다.
            await self._fetch_panel_message()
        except Exception as e:
            logger.error(f"スロットパネルの更新中にエラー: {e}")

    async def register_persistent_views(self):
        view = SlotMachinePanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_slot_machine", last_game_log: Optional[discord.Embed] = None):
        embed_key = "panel_slot_machine"
        
        if self.panel_message:
            try:
                await self.panel_message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
        
        if last_game_log:
            try: await channel.send(embed=last_game_log)
            except Exception as e: logger.error(f"スロットゲームのログメッセージ送信に失敗: {e}")

        if not (embed_data := await get_embed_from_db(embed_key)):
            return

        embed = discord.Embed.from_dict(embed_data)
        view = SlotMachinePanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        
        # [✅] 새 패널 메시지를 저장하고 즉시 상태를 업데이트합니다.
        self.panel_message = new_message
        await self.update_panel_embed()
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。(チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(SlotMachine(bot))
