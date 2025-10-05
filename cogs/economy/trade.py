# cogs/economy/trade.py

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
import math
import time
from typing import Optional, Dict, List, Any
# ▼▼▼ [핵심 수정] 아래 datetime 관련 import를 추가합니다. ▼▼▼
from datetime import datetime, timezone, timedelta
from postgrest.exceptions import APIError
import json

from utils.database import (
    get_inventory, get_wallet, get_item_database, get_config, supabase,
    save_panel_id, get_panel_id, get_embed_from_db, update_inventory, update_wallet,
    get_id
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

TRADEABLE_CATEGORIES = ["농장_작물", "농장_씨앗", "광물", "미끼", "아이템"]

async def delete_after(message: discord.WebhookMessage, delay: int):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden):
        pass

# --- MODALS ---
class ItemSelectModal(ui.Modal, title="数量入力"):
    quantity_input = ui.TextInput(label="数量", placeholder="数量を入力してください。", required=True)
    def __init__(self, title: str, max_quantity: int):
        super().__init__(title=title)
        self.max_quantity = max_quantity
        self.quantity: Optional[int] = None
    async def on_submit(self, interaction: discord.Interaction):
        try:
            qty = int(self.quantity_input.value)
            if not 1 <= qty <= self.max_quantity: raise ValueError
            self.quantity = qty
            await interaction.response.defer()
        except ValueError:
            await interaction.response.send_message(f"1から{self.max_quantity}までの数字のみ入力してください。", ephemeral=True, delete_after=5)
        self.stop()

class MailItemSelectModal(ui.Modal):
    quantity_input = ui.TextInput(label="数量", placeholder="数量を入力してください。", required=True)
    def __init__(self, title: str, max_quantity: int, item_name: str, parent_view: 'MailComposeView'):
        super().__init__(title=title)
        self.max_quantity = max_quantity
        self.item_name = item_name
        self.parent_view = parent_view
        self.quantity_input.placeholder = f"最大{max_quantity}個"
    async def on_submit(self, interaction: discord.Interaction):
        try:
            qty = int(self.quantity_input.value)
            if not 1 <= qty <= self.max_quantity:
                await interaction.response.send_message(f"1から{self.max_quantity}までの数字のみ入力してください。", ephemeral=True, delete_after=5)
                return
            await self.parent_view.add_attachment(interaction, self.item_name, qty)
        except ValueError:
            await interaction.response.send_message("数字のみ入力してください。", ephemeral=True, delete_after=5)

class CoinInputModal(ui.Modal, title="コイン設定"):
    coin_input = ui.TextInput(label="コイン", placeholder="設定するコインの額を入力してください (削除は0を入力)", required=True)
    def __init__(self, title:str, max_coins: int):
        super().__init__(title=title)
        self.max_coins = max_coins
        self.coins: Optional[int] = None
    async def on_submit(self, interaction: discord.Interaction):
        try:
            coins = int(self.coin_input.value)
            if not 0 <= coins <= self.max_coins: raise ValueError
            self.coins = coins
            await interaction.response.defer()
        except ValueError:
            await interaction.response.send_message(f"0から{self.max_coins:,}までの数字のみ入力してください。", ephemeral=True, delete_after=5)
        self.stop()

class MessageModal(ui.Modal, title="メッセージ作成"):
    message_input = ui.TextInput(label="メッセージ (最大100文字)", style=discord.TextStyle.paragraph, max_length=100, required=False)
    def __init__(self, current_message: str, parent_view: 'MailComposeView'):
        super().__init__()
        self.message_input.default = current_message
        self.parent_view = parent_view
    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.message_content = self.message_input.value
        await self.parent_view.refresh(interaction)

# --- HELPER VIEWS for Mail System ---
class RemoveItemSelectView(ui.View):
    def __init__(self, parent_view: 'MailComposeView'):
        super().__init__(timeout=180)
        self.parent_view = parent_view
    async def start(self, interaction: discord.Interaction):
        await self.build_components()
        await interaction.followup.send("削除するアイテムを選択してください。", view=self, ephemeral=True)
    async def build_components(self):
        self.clear_items()
        attached_items = self.parent_view.attachments.get("items", {})
        if not attached_items:
            self.add_item(ui.Button(label="削除するアイテムがありません。", disabled=True))
            return
        options = [discord.SelectOption(label=f"{name} ({qty}個)", value=name) for name, qty in attached_items.items()]
        item_select = ui.Select(placeholder="削除するアイテムを選択...", options=options)
        item_select.callback = self.on_item_select
        self.add_item(item_select)
    async def on_item_select(self, interaction: discord.Interaction):
        item_name = interaction.data['values'][0]
        if item_name in self.parent_view.attachments["items"]:
            del self.parent_view.attachments["items"][item_name]
        await self.parent_view.refresh(interaction)
        try:
            await interaction.delete_original_response()
        except (discord.NotFound, discord.HTTPException):
            pass

class IngredientSelectView(ui.View):
    def __init__(self, parent_view: 'MailComposeView'):
        super().__init__(timeout=180)
        self.parent_view = parent_view
        self.user = parent_view.user
    async def start(self, interaction: discord.Interaction):
        await self.build_components()
        await interaction.followup.send("添付するアイテムを選択してください。", view=self, ephemeral=True)
    async def build_components(self):
        self.clear_items()
        inventory = await get_inventory(self.user)
        item_db = get_item_database()
        attached_items = self.parent_view.attachments.get("items", {}).keys()
        tradeable_items = {
            name: qty for name, qty in inventory.items()
            if item_db.get(name, {}).get('category') in TRADEABLE_CATEGORIES and name not in attached_items
        }
        if not tradeable_items:
            self.add_item(ui.Button(label="添付可能なアイテムがありません。", disabled=True))
            return
        options = [discord.SelectOption(label=f"{name} ({qty}個)", value=name) for name, qty in tradeable_items.items()]
        item_select = ui.Select(placeholder="アイテム選択...", options=options[:25])
        item_select.callback = self.on_item_select
        self.add_item(item_select)
    async def on_item_select(self, interaction: discord.Interaction):
        item_name = interaction.data['values'][0]
        inventory = await get_inventory(self.user)
        max_qty = inventory.get(item_name, 0)
        modal = MailItemSelectModal(f"'{item_name}' 数量入力", max_qty, item_name, self.parent_view)
        await interaction.response.send_modal(modal)
        try:
            await interaction.delete_original_response()
        except (discord.NotFound, discord.HTTPException):
            pass

# ▼▼▼ [핵심 수정] MailboxView에서 분리된 받는 사람 선택 전용 View ▼▼▼
class RecipientSelectView(ui.View):
    def __init__(self, cog: 'Trade', user: discord.Member, original_interaction: discord.Interaction):
        super().__init__(timeout=180)
        self.cog = cog
        self.user = user
        self.original_interaction = original_interaction # MailboxView의 상호작용
    
    async def start(self):
        user_select = ui.UserSelect(placeholder="手紙を送る相手を選択してください。")
        user_select.callback = self.on_recipient_select
        self.add_item(user_select)

        back_button = ui.Button(label="メールボックスに戻る", style=discord.ButtonStyle.grey)
        back_button.callback = self.on_back
        self.add_item(back_button)

        logger.info(f"[{self.original_interaction.id}] Editing MailboxView to RecipientSelectView.")
        await self.original_interaction.edit_original_response(content="誰に手紙を送りますか？", view=self, embed=None)

    async def on_recipient_select(self, interaction: discord.Interaction):
        logger.info(f"[{interaction.id}] Recipient selected. Is original response done? {interaction.response.is_done()}")
        if not interaction.response.is_done():
            await interaction.response.defer() # 중요!

        recipient_id = int(interaction.data['values'][0])
        recipient = interaction.guild.get_member(recipient_id)
        if not recipient or recipient.bot or recipient.id == self.user.id:
            msg = await interaction.followup.send("無効な相手です。", ephemeral=True)
            await delete_after(msg, 5)
            return
        
        # MailComposeView로 교체
        compose_view = MailComposeView(self.cog, self.user, recipient, interaction)
        await compose_view.start_from_selection()

    async def on_back(self, interaction: discord.Interaction):
        # 다시 MailboxView로 돌아감
        mailbox_view = MailboxView(self.cog, self.user)
        mailbox_view.message = await self.original_interaction.original_response()
        await mailbox_view.update_view(interaction)

# --- MAIN VIEWS ---
class TradeView(ui.View):
    def __init__(self, cog: 'Trade', initiator: discord.Member, partner: discord.Member, trade_id: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.initiator = initiator
        self.partner = partner
        self.trade_id = trade_id
        self.offers = { initiator.id: {"items": {}, "coins": 0, "ready": False}, partner.id: {"items": {}, "coins": 0, "ready": False} }
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.message: Optional[discord.Message] = None
        self.build_components()
    async def start_in_thread(self, thread: discord.Thread):
        self.cog.active_trades[self.trade_id] = self
        embed = await self.build_embed()
        self.message = await thread.send(f"{self.partner.mention}, {self.initiator.mention}さんの1:1取引チャンネルです。", embed=embed, view=self)
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in [self.initiator.id, self.partner.id]:
            await interaction.response.send_message("取引の当事者のみ利用できます。", ephemeral=True)
            return False
        if interaction.data.get('custom_id') == "confirm_trade_button" and interaction.user.id != self.initiator.id:
            await interaction.response.send_message("取引申請者のみ確定できます。", ephemeral=True, delete_after=5)
            return False
        return True
    async def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🤝 1:1取引", color=0x3498DB)
        for i, user in enumerate([self.initiator, self.partner]):
            offer = self.offers[user.id]
            status = "✅ 準備完了" if offer["ready"] else "⏳ 準備中"
            field_value_parts = [f"**{user.mention}** ({status})"]
            if offer["items"]:
                field_value_parts.extend([f"ㄴ {name}: {qty}個" for name, qty in offer["items"].items()])
            if offer["coins"] > 0:
                field_value_parts.append(f"💰 {offer['coins']:,}{self.currency_icon}")
            if len(field_value_parts) == 1:
                field_value_parts.append("提案なし")
            embed.add_field(name=f"参加者 {i+1}", value="\n".join(field_value_parts), inline=True)
        embed.set_footer(text="5分後に期限切れになります。")
        return embed
    def build_components(self):
        self.clear_items()
        initiator_ready = self.offers[self.initiator.id]["ready"]
        partner_ready = self.offers[self.partner.id]["ready"]
        both_ready = initiator_ready and partner_ready
        self.add_item(ui.Button(label="アイテム追加", style=discord.ButtonStyle.secondary, emoji="📦", custom_id="add_item", row=0))
        self.add_item(ui.Button(label="アイテム削除", style=discord.ButtonStyle.secondary, emoji="🗑️", custom_id="remove_item", row=0))
        self.add_item(ui.Button(label="コイン設定", style=discord.ButtonStyle.secondary, emoji="🪙", custom_id="add_coin", row=0))
        self.add_item(ui.Button(label="準備", style=discord.ButtonStyle.primary, emoji="✅", custom_id="ready", row=1))
        self.add_item(ui.Button(label="準備解除", style=discord.ButtonStyle.grey, emoji="↩️", custom_id="unready", row=1))
        confirm_button = ui.Button(label="取引確定", style=discord.ButtonStyle.success, emoji="🤝", custom_id="confirm_trade_button", row=2, disabled=not both_ready)
        self.add_item(confirm_button)
        cancel_button = ui.Button(label="取引キャンセル", style=discord.ButtonStyle.danger, emoji="✖️", custom_id="cancel_button", row=2)
        self.add_item(cancel_button)
        for item in self.children:
            item.callback = self.dispatch_callback
    async def update_ui(self, interaction: discord.Interaction):
        if self.is_finished() or not self.message: return
        self.build_components()
        embed = await self.build_embed()
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await self.message.edit(embed=embed, view=self)
        except (discord.NotFound, discord.Forbidden):
            self.stop()
    async def dispatch_callback(self, interaction: discord.Interaction):
        action = interaction.data['custom_id']
        if action in ["add_item", "remove_item", "add_coin"]:
            pass
        elif not interaction.response.is_done():
            await interaction.response.defer()
        if action == "add_item": await self.handle_add_item(interaction)
        elif action == "remove_item": await self.handle_remove_item(interaction)
        elif action == "add_coin": await self.handle_add_coin(interaction)
        elif action == "ready": await self.handle_ready(interaction)
        elif action == "unready": await self.handle_unready(interaction)
        elif action == "confirm_trade_button": await self.process_trade(interaction)
        elif action == "cancel_button": await self.handle_cancel(interaction)
    async def handle_add_item(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        if self.offers[user_id]["ready"]: return await interaction.response.send_message("準備完了状態では提案を変更できません。", ephemeral=True, delete_after=5)
        inventory, item_db = await get_inventory(interaction.user), get_item_database()
        tradeable_items = { n: q for n, q in inventory.items() if item_db.get(n, {}).get('category') in TRADEABLE_CATEGORIES }
        if not tradeable_items: return await interaction.response.send_message("取引可能なアイテムがありません。", ephemeral=True, delete_after=5)
        options = [ discord.SelectOption(label=f"{name} ({qty}個)", value=name) for name, qty in tradeable_items.items() ]
        select_view = ui.View(timeout=180); item_select = ui.Select(placeholder="追加するアイテムを選択してください", options=options[:25])
        async def select_callback(si: discord.Interaction):
            item_name, max_qty = si.data['values'][0], tradeable_items.get(si.data['values'][0], 0)
            modal = ItemSelectModal(f"'{item_name}' 数量入力", max_qty)
            await si.response.send_modal(modal); await modal.wait()
            if modal.quantity is not None:
                self.offers[user_id]["items"][item_name] = modal.quantity
                await self.update_ui(si)
            try: await si.delete_original_response()
            except discord.NotFound: pass
        item_select.callback = select_callback; select_view.add_item(item_select)
        await interaction.response.send_message(view=select_view, ephemeral=True)
    async def handle_remove_item(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        if self.offers[user_id]["ready"]:
            return await interaction.response.send_message("準備完了状態では提案を変更できません。", ephemeral=True, delete_after=5)
        offered_items = self.offers[user_id]["items"]
        if not offered_items:
            return await interaction.response.send_message("削除するアイテムがありません。", ephemeral=True, delete_after=5)
        options = [discord.SelectOption(label=f"{name} ({qty}個)", value=name) for name, qty in offered_items.items()]
        select_view = ui.View(timeout=180)
        item_select = ui.Select(placeholder="削除するアイテムを選択してください", options=options)
        async def select_callback(si: discord.Interaction):
            item_name_to_remove = si.data['values'][0]
            if item_name_to_remove in self.offers[user_id]["items"]:
                del self.offers[user_id]["items"][item_name_to_remove]
                await self.update_ui(interaction)
            try:
                await si.response.defer()
                await si.delete_original_response()
            except discord.NotFound:
                pass
        item_select.callback = select_callback
        select_view.add_item(item_select)
        await interaction.response.send_message("削除するアイテムを選択してください。", view=select_view, ephemeral=True)
    async def handle_add_coin(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        if self.offers[user_id]["ready"]: return await interaction.response.send_message("準備完了状態では提案を変更できません。", ephemeral=True, delete_after=5)
        wallet = await get_wallet(user_id); max_coins = wallet.get('balance', 0)
        modal = CoinInputModal("取引コイン設定", max_coins)
        await interaction.response.send_modal(modal); await modal.wait()
        if modal.coins is not None:
            self.offers[user_id]["coins"] = modal.coins
            await self.update_ui(interaction)
    async def handle_ready(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        if self.offers[user_id]["ready"]:
            msg = await interaction.followup.send("すでに準備完了状態です。", ephemeral=True)
            self.cog.bot.loop.create_task(delete_after(msg, 5))
            return
        self.offers[user_id]["ready"] = True
        await self.update_ui(interaction)
    async def handle_unready(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        if not self.offers[user_id]["ready"]:
            msg = await interaction.followup.send("まだ準備完了状態ではありません。", ephemeral=True)
            self.cog.bot.loop.create_task(delete_after(msg, 5))
            return
        self.offers[user_id]["ready"] = False
        await self.update_ui(interaction)
    async def handle_cancel(self, interaction: discord.Interaction):
        await interaction.followup.send("取引のキャンセルをリクエストしました。", ephemeral=True)
        await self._end_trade(cancelled_by=interaction.user)
    async def process_trade(self, interaction: discord.Interaction):
        self.build_components()
        for item in self.children: item.disabled = True
        await self.message.edit(content="**取引確定！処理中...**", view=self, embed=await self.build_embed())
        user1, user2, offer1, offer2 = self.initiator, self.partner, self.offers[self.initiator.id], self.offers[self.partner.id]
        
        # ▼▼▼▼▼ 핵심 수정 시작 ▼▼▼▼▼
        try:
            # 1. 수수료 계산
            commission_rate = 0.05
            commission = math.ceil((offer1['coins'] + offer2['coins']) * commission_rate)

            # 2. DB 함수에 전달할 파라미터 준비
            params = {
                'p_user1_id': user1.id,
                'p_user2_id': user2.id,
                'p_user1_offer_items': json.dumps(offer1['items']),
                'p_user2_offer_items': json.dumps(offer2['items']),
                'p_user1_offer_coins': offer1['coins'],
                'p_user2_offer_coins': offer2['coins'],
                'p_commission_fee': commission
            }
            
            # 3. 단일 RPC 함수 호출
            response = await supabase.rpc('execute_trade', params).execute()
            
            # 4. 결과 확인
            result_message = response.data
            if result_message != '거래 성공':
                # DB 함수가 실패 메시지를 반환하면, 해당 메시지를 표시하고 거래 실패 처리
                return await self.fail_trade(result_message)

        except Exception as e:
            logger.error(f"거래 처리 RPC 호출 중 예외 발생: {e}", exc_info=True)
            return await self.fail_trade("不明なエラーが発生しました。")
        # ▲▲▲▲▲ 핵심 수정 종료 ▲▲▲▲▲

        if self.message:
            log_channel_id = get_id("trade_panel_channel_id")
            if log_channel_id and (log_channel := self.cog.bot.get_channel(log_channel_id)):
                if log_embed_data := await get_embed_from_db("log_trade_success"):
                    log_embed = format_embed_from_db(log_embed_data, user1_mention=user1.mention, user2_mention=user2.mention, commission=commission, currency_icon=self.currency_icon)
                    offer1_str = "\n".join([f"ㄴ {n}: {q}個" for n, q in offer1['items'].items()] + ([f"💰 {offer1['coins']:,}{self.currency_icon}"] if offer1['coins'] > 0 else [])) or "なし"
                    offer2_str = "\n".join([f"ㄴ {n}: {q}個" for n, q in offer2['items'].items()] + ([f"💰 {offer2['coins']:,}{self.currency_icon}"] if offer2['coins'] > 0 else [])) or "なし"
                    log_embed.add_field(name=f"{user1.display_name}の提供", value=offer1_str, inline=True)
                    log_embed.add_field(name=f"{user2.display_name}の提供", value=offer2_str, inline=True)
                    log_embed.set_footer(text=f"取引税: {commission}{self.currency_icon} (申請手数料250コインは返金されません)")
                    await self.cog.regenerate_panel(log_channel, last_log=log_embed)
            await self.message.channel.send("✅ 取引が正常に完了しました。このチャンネルは10秒後に削除されます。")
            await asyncio.sleep(10); await self.message.channel.delete()
        self.stop()
    async def fail_trade(self, reason: str):
        if self.message:
            if self.initiator:
                refund_result = await update_wallet(self.initiator, 250)
                if refund_result:
                    reason += f"\n(取引申請手数料250{self.currency_icon}返金済み)"
                    logger.info(f"거래 실패로 {self.initiator.id}에게 수수료 250코인 환불 완료.")
                else:
                    logger.error(f"거래 실패 후 {self.initiator.id}에게 수수료 환불 실패!")
            await self.message.channel.send(f"❌ 取引失敗: {reason}\nこのチャンネルは10秒後に削除されます。")
            await asyncio.sleep(10); await self.message.channel.delete()
        self.stop()
    async def _end_trade(self, cancelled_by: Optional[discord.User] = None):
        if self.is_finished(): return
        self.stop()
        if not self.message: return
        channel = self.message.channel
        try:
            final_messages = []
            if cancelled_by:
                final_messages.append(f"{cancelled_by.mention}さんが取引をキャンセルしました。")
            else:
                final_messages.append("時間切れのため、取引は自動的に終了しました。")
            if self.initiator:
                refund_result = await update_wallet(self.initiator, 250)
                if refund_result:
                    logger.info(f"거래 취소/타임아웃으로 {self.initiator.id}에게 수수료 250코인 환불 완료.")
                    final_messages.append(f"{self.initiator.mention}さんに取引申請手数料250{self.currency_icon}を返金しました。")
                else:
                    logger.error(f"거래 취소/타임아웃 후 {self.initiator.id}에게 수수료 환불 실패!")
            final_messages.append("\nこのチャンネルは10秒後に削除されます。")
            await channel.send("\n".join(final_messages))
            await asyncio.sleep(10)
            await channel.delete()
        except (discord.NotFound, discord.Forbidden) as e:
            logger.warning(f"거래 종료/삭제 중 오류: {e}")
        except Exception as e:
            logger.error(f"거래 종료 중 예외 발생: {e}", exc_info=True)
    async def on_timeout(self):
        await self._end_trade()
    def stop(self):
        if self.trade_id in self.cog.active_trades: self.cog.active_trades.pop(self.trade_id)
        super().stop()

class MailComposeView(ui.View):
    def __init__(self, cog: 'Trade', user: discord.Member, recipient: discord.Member, original_interaction: discord.Interaction):
        super().__init__(timeout=300)
        self.cog = cog
        self.user = user
        self.recipient = recipient
        self.original_interaction = original_interaction
        self.message_content = ""
        self.attachments = {"items": {}}
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.shipping_fee = 100
        self.message: Optional[discord.WebhookMessage] = None
    
    async def start_from_selection(self):
        embed = await self.build_embed()
        await self.build_components()
        logger.info(f"[{self.original_interaction.id}] Editing RecipientSelect message to MailComposeView.")
        self.message = await self.original_interaction.edit_original_response(content=None, embed=embed, view=self)

    async def refresh(self, interaction: Optional[discord.Interaction] = None):
        if interaction and not interaction.response.is_done():
            await interaction.response.defer()
        embed = await self.build_embed()
        await self.build_components()
        if self.message:
            await self.message.edit(embed=embed, view=self)

    async def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"✉️ 手紙を書く (TO: {self.recipient.display_name})", color=0x3498DB)
        att_items = self.attachments.get("items", {})
        att_str = [f"ㄴ {name}: {qty}個" for name, qty in att_items.items()]
        embed.add_field(name="添付アイテム", value="\n".join(att_str) if att_str else "なし", inline=False)
        embed.add_field(name="メッセージ", value=f"```{self.message_content}```" if self.message_content else "メッセージなし", inline=False)
        embed.set_footer(text=f"配送料: {self.shipping_fee:,}{self.currency_icon}")
        return embed

    async def build_components(self):
        self.clear_items()
        self.add_item(ui.Button(label="アイテム添付", style=discord.ButtonStyle.secondary, emoji="📦", custom_id="attach_item", row=0))
        remove_disabled = not self.attachments.get("items")
        self.add_item(ui.Button(label="アイテム削除", style=discord.ButtonStyle.secondary, emoji="🗑️", custom_id="remove_item", row=0, disabled=remove_disabled))
        self.add_item(ui.Button(label="メッセージ作成/修正", style=discord.ButtonStyle.secondary, emoji="✍️", custom_id="write_message", row=0))
        send_disabled = not (self.attachments.get("items") or self.message_content)
        self.add_item(ui.Button(label="送信", style=discord.ButtonStyle.success, emoji="🚀", custom_id="send_mail", row=1, disabled=send_disabled))
        for item in self.children:
            item.callback = self.dispatch_callback

    async def dispatch_callback(self, interaction: discord.Interaction):
        custom_id = interaction.data['custom_id']
        if custom_id == "write_message":
            return await self.handle_write_message(interaction)
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if custom_id == "attach_item":
            await self.handle_attach_item(interaction)
        elif custom_id == "remove_item":
            await self.handle_remove_item(interaction)
        elif custom_id == "send_mail":
            await self.handle_send(interaction)

    async def handle_attach_item(self, interaction: discord.Interaction):
        view = IngredientSelectView(self)
        await view.start(interaction)

    async def add_attachment(self, interaction: discord.Interaction, item_name: str, quantity: int):
        self.attachments['items'][item_name] = self.attachments['items'].get(item_name, 0) + quantity
        await self.refresh(interaction)
    
    async def handle_remove_item(self, interaction: discord.Interaction):
        view = RemoveItemSelectView(self)
        await view.start(interaction)

    async def handle_write_message(self, interaction: discord.Interaction):
        modal = MessageModal(self.message_content, self)
        await interaction.response.send_modal(modal)

    # ▼▼▼ [수정] handle_send 메서드를 아래 코드로 전체 교체합니다. ▼▼▼
    async def handle_send(self, interaction: discord.Interaction):
        # 1. 즉시 모든 버튼을 비활성화하고 로딩 메시지를 표시합니다.
        for item in self.children:
            item.disabled = True
        
        # 'original_interaction'은 RecipientSelectView에서 온 것이므로,
        # MailComposeView를 표시하고 있는 메시지를 직접 수정해야 합니다.
        if self.message:
            await self.message.edit(content="メールを送信中です...", view=self, embed=None)

        try:
            if not self.attachments.get("items") and not self.message_content:
                msg = await interaction.followup.send("❌ アイテムまたはメッセージのいずれかを含める必要があります。", ephemeral=True)
                # 실패 시 View를 다시 활성화하기 위해 되돌립니다.
                await self.refresh()
                return await delete_after(msg, 5)

            wallet, inventory = await asyncio.gather(get_wallet(self.user.id), get_inventory(self.user))
            if wallet.get('balance', 0) < self.shipping_fee:
                msg = await interaction.followup.send(f"コインが不足しています。(配送料: {self.shipping_fee:,}{self.currency_icon})", ephemeral=True)
                await self.refresh()
                return await delete_after(msg, 5)
            
            for item, qty in self.attachments["items"].items():
                if inventory.get(item, 0) < qty:
                    msg = await interaction.followup.send(f"アイテムの在庫が不足しています: '{item}'", ephemeral=True)
                    await self.refresh()
                    return await delete_after(msg, 5)
            
            # --- DB 작업 시작 ---
            db_tasks = [update_wallet(self.user, -self.shipping_fee)]
            for item, qty in self.attachments["items"].items():
                db_tasks.append(update_inventory(self.user.id, item, -qty))
            await asyncio.gather(*db_tasks)
            
            now, expires_at = datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(days=30)
            
            mail_res = await supabase.table('mails').insert({
                "sender_id": str(self.user.id), 
                "recipient_id": str(self.recipient.id), 
                "message": self.message_content, 
                "sent_at": now.isoformat(), 
                "expires_at": expires_at.isoformat()
            }).execute()

            if not mail_res.data:
                logger.error("메일 레코드 생성 실패. 비용 및 아이템 환불 시도."); 
                refund_tasks = [update_wallet(self.user, self.shipping_fee)]
                for item, qty in self.attachments["items"].items():
                    refund_tasks.append(update_inventory(self.user.id, item, qty))
                await asyncio.gather(*refund_tasks)
                await self.message.edit(content="❌ メール送信に失敗しました。費用とアイテムは全て返金されました。", view=None, embed=None)
                return
            
            new_mail_id = mail_res.data[0]['id']
            if self.attachments["items"]:
                att_to_insert = [{"mail_id": new_mail_id, "item_name": n, "quantity": q, "is_coin": False} for n, q in self.attachments["items"].items()]
                await supabase.table('mail_attachments').insert(att_to_insert).execute()
            
            await self.message.edit(content="✅ メールを正常に送信しました。", view=None, embed=None)
            
            if (panel_ch_id := get_id("trade_panel_channel_id")) and (panel_ch := self.cog.bot.get_channel(panel_ch_id)):
                if embed_data := await get_embed_from_db("log_new_mail"):
                    log_embed = format_embed_from_db(embed_data, sender_mention=self.user.mention, recipient_mention=self.recipient.mention)
                    await panel_ch.send(content=self.recipient.mention, embed=log_embed, allowed_mentions=discord.AllowedMentions(users=True), delete_after=60.0)
                await self.cog.regenerate_panel(panel_ch)
            
        except Exception as e:
            logger.error(f"우편 발송 중 최종 단계에서 예외 발생: {e}", exc_info=True)
            try:
                # original_interaction은 이미 만료되었을 수 있으므로, 현재 interaction에 followup으로 응답
                await interaction.followup.send("メール送信中にエラーが発生しました。素材の消費状況を確認してください。", ephemeral=True)
            except discord.NotFound:
                pass
        finally:
            self.stop()
    # ▲▲▲ [수정] 완료 ▲▲▲
            
class MailboxView(ui.View):
    # ▼▼▼ [핵심 수정] __init__ 메서드를 추가합니다. ▼▼▼
    def __init__(self, cog: 'Trade', user: discord.Member):
        super().__init__(timeout=180)
        self.cog = cog
        self.user = user
        self.page = 0
        self.mails_on_page: List[Dict] = []
        self.message: Optional[discord.WebhookMessage] = None
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.selected_mail_ids: List[str] = []

    # ... (start, update_view, build_embed, build_components, on_mail_select, claim_selected_mails, delete_selected_mails 메서드는 이전과 동일)
    async def start(self, interaction: discord.Interaction):
        embed = await self.build_embed()
        await self.build_components()
        self.message = await interaction.followup.send(embed=embed, view=self, ephemeral=True)
    async def update_view(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer()
        if not self.message:
            self.message = await interaction.original_response()
        embed = await self.build_embed()
        await self.build_components()
        try:
            await self.message.edit(embed=embed, view=self)
        except (discord.NotFound, discord.Forbidden) as e:
            logger.warning(f"MailboxView 메시지(ID: {self.message.id})를 수정할 수 없습니다: {e}")
            self.stop()
    async def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"📫 {self.user.display_name}のメールボックス", color=0x964B00)
        res = await supabase.table('mails').select('*, mail_attachments(*)', count='exact').eq('recipient_id', str(self.user.id)).is_('claimed_at', None).order('sent_at', desc=True).range(self.page * 5, self.page * 5 + 4).execute()
        self.mails_on_page = res.data if res.data else []
        if not self.mails_on_page:
            embed.description = "受信した手紙がありません。"
        else:
            embed.set_footer(text=f"ページ {self.page + 1} / {math.ceil((res.count or 0) / 5)}")
            for i, mail in enumerate(self.mails_on_page):
                sender_id_int = int(mail['sender_id'])
                sender = self.cog.bot.get_user(sender_id_int)
                sender_name = sender.display_name if sender else f"不明なユーザー ({sender_id_int})"
                sender_mention = sender.mention if sender else sender_name
                attachments = mail['mail_attachments']
                att_str = [f"📦 {att['item_name']}: {att['quantity']}個" for att in attachments if not att['is_coin']]
                field_value = (f"**送信者:** {sender_mention}\n" + (f"> **メッセージ:** {mail['message']}\n" if mail['message'] else "") + "**添付アイテム:**\n" + ("\n".join(att_str) if att_str else "なし"))
                embed.add_field(name=f"FROM: {sender_name} ({discord.utils.format_dt(datetime.fromisoformat(mail['sent_at']), 'R')})", value=field_value, inline=False)
                if i < len(self.mails_on_page) - 1:
                    embed.add_field(name="\u200b", value="━━━━━━━━━━━━━━━━━━", inline=False)
        return embed
    async def build_components(self):
        self.clear_items()
        mail_options = [discord.SelectOption(label=f"送信者: {getattr(self.cog.bot.get_user(int(m['sender_id'])), 'display_name', m['sender_id'])}", value=str(m['id']), default=(str(m['id']) in self.selected_mail_ids)) for m in self.mails_on_page]
        if mail_options:
            select = ui.Select(placeholder="処理するメールを選択してください (複数選択可)", options=mail_options, max_values=len(mail_options))
            select.callback = self.on_mail_select
            self.add_item(select)
        claim_all_button = ui.Button(label="選択したメールを全て受け取る", style=discord.ButtonStyle.success, emoji="📥", disabled=not self.selected_mail_ids, row=1)
        claim_all_button.callback = self.claim_selected_mails
        self.add_item(claim_all_button)
        delete_all_button = ui.Button(label="選択したメールを全て削除", style=discord.ButtonStyle.danger, emoji="🗑️", disabled=not self.selected_mail_ids, row=1)
        delete_all_button.callback = self.delete_selected_mails
        self.add_item(delete_all_button)
        send_button = ui.Button(label="手紙を送る", style=discord.ButtonStyle.success, emoji="✉️", row=2)
        send_button.callback = self.send_mail
        self.add_item(send_button)
        res = await supabase.table('mails').select('id', count='exact').eq('recipient_id', str(self.user.id)).is_('claimed_at', None).execute()
        total_mails = res.count or 0
        prev_button = ui.Button(label="◀", style=discord.ButtonStyle.secondary, disabled=self.page == 0, row=2)
        prev_button.callback = self.prev_page_callback
        self.add_item(prev_button)
        next_button = ui.Button(label="▶", style=discord.ButtonStyle.secondary, disabled=(self.page + 1) * 5 >= total_mails, row=2)
        next_button.callback = self.next_page_callback
        self.add_item(next_button)
    async def on_mail_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.selected_mail_ids = interaction.data['values']
        await self.update_view(interaction)
    async def claim_selected_mails(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        claimed_count = 0
        total_items: Dict[str, int] = {}
        db_tasks = []
        mail_ids_to_process = [int(mid) for mid in self.selected_mail_ids]
        attachments_res = await supabase.table('mail_attachments').select('*').in_('mail_id', mail_ids_to_process).execute()
        if attachments_res.data:
            for att in attachments_res.data:
                total_items[att['item_name']] = total_items.get(att['item_name'], 0) + att['quantity']
            for item_name, qty in total_items.items():
                db_tasks.append(update_inventory(self.user.id, item_name, qty))
        try:
            if db_tasks:
                await asyncio.gather(*db_tasks)
            now_iso = datetime.now(timezone.utc).isoformat()
            await supabase.table('mails').update({'claimed_at': now_iso}).in_('id', mail_ids_to_process).execute()
            claimed_count = len(mail_ids_to_process)
        except Exception as e:
            logger.error(f"우편 일괄 수령 중 DB 작업 오류: {e}", exc_info=True)
            await interaction.followup.send("メールの受け取り中にエラーが発生しました。", ephemeral=True)
            return
        if claimed_count > 0:
            item_summary = "\n".join([f"ㄴ {name}: {qty}個" for name, qty in total_items.items()])
            success_message = f"{claimed_count}件のメールを受け取りました！\n\n**合計受け取りアイテム:**\n{item_summary or 'なし'}"
            msg = await interaction.followup.send(success_message, ephemeral=True)
            self.cog.bot.loop.create_task(delete_after(msg, 10))
        else:
            await interaction.followup.send("受け取るメールがないか、エラーが発生しました。", ephemeral=True)
        self.selected_mail_ids.clear()
        await self.update_view(interaction)
    async def delete_selected_mails(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        mail_ids_to_delete = [int(mid) for mid in self.selected_mail_ids]
        await supabase.table('mails').delete().in_('id', mail_ids_to_delete).eq('recipient_id', str(self.user.id)).execute()
        self.selected_mail_ids.clear()
        await self.update_view(interaction)
    
    async def send_mail(self, interaction: discord.Interaction):
        # ▼▼▼ [핵심 수정] 이 메서드를 아래 코드로 교체합니다. ▼▼▼
        logger.info(f"[{interaction.id}] User {interaction.user.id} clicked 'send_mail' button. Is response done? {interaction.response.is_done()}")
        if not interaction.response.is_done():
            # defer()는 전역 핸들러나 버튼 콜백에서 처리되므로, 여기서는 is_done()만 확인
            # 만약 응답이 안됐다면, 무언가 잘못된 것임.
            try:
                await interaction.response.defer()
            except discord.errors.InteractionResponded:
                pass # 이미 응답됨

        recipient_view = RecipientSelectView(self.cog, self.user, interaction)
        await recipient_view.start()

    async def prev_page_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.page -= 1
        await self.update_view(interaction)
    async def next_page_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.page += 1
        await self.update_view(interaction)

class TradePanelView(ui.View):
    def __init__(self, cog_instance: 'Trade'):
        super().__init__(timeout=None)
        self.cog = cog_instance
        trade_button = ui.Button(label="1:1取引", style=discord.ButtonStyle.success, emoji="🤝", custom_id="trade_panel_direct_trade")
        trade_button.callback = self.dispatch_callback
        self.add_item(trade_button)
        mailbox_button = ui.Button(label="メールボックス", style=discord.ButtonStyle.primary, emoji="📫", custom_id="trade_panel_mailbox")
        mailbox_button.callback = self.dispatch_callback
        self.add_item(mailbox_button)
    async def dispatch_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        custom_id = interaction.data['custom_id']
        if custom_id == "trade_panel_direct_trade":
            await self.handle_direct_trade(interaction)
        elif custom_id == "trade_panel_mailbox":
            await self.handle_mailbox(interaction)
    async def handle_direct_trade(self, interaction: discord.Interaction):
        initiator = interaction.user
        trade_fee = 250
        wallet = await get_wallet(initiator.id)
        if wallet.get('balance', 0) < trade_fee:
            return await interaction.followup.send(f"❌ 取引を開始するには手数料{trade_fee}{self.cog.currency_icon}が必要です。", ephemeral=True)

        view = ui.View(timeout=180)
        user_select = ui.UserSelect(placeholder="取引する相手を選択してください。")
        
        async def select_callback(si: discord.Interaction):
            # UserSelect 상호작용(si)에 대해 응답합니다.
            await si.response.defer(ephemeral=True, thinking=False)

            partner_id = int(si.data['values'][0])
            partner = si.guild.get_member(partner_id)
            if not partner or partner.bot or partner.id == initiator.id:
                return await si.followup.send("❌ 無効な相手です。", ephemeral=True, delete_after=5)
            
            trade_id = f"{min(initiator.id, partner.id)}-{max(initiator.id, partner.id)}"
            if trade_id in self.cog.active_trades:
                 return await si.followup.send("相手または自分がすでに他の取引に参加しています。", ephemeral=True)
            
            result = await update_wallet(initiator, -trade_fee)
            if not result:
                logger.error(f"{initiator.id}의 거래 수수료 차감 실패. 잔액 부족 가능성.")
                return await si.followup.send(f"❌ 手数料({trade_fee}{self.cog.currency_icon})の支払いに失敗しました。残高を確認してください。", ephemeral=True)
            
            logger.info(f"{initiator.id}에게서 거래 수수료 250코인 차감 완료.")
            await si.followup.send(f"✅ 取引申請手数料{trade_fee}{self.cog.currency_icon}を支払いました。", ephemeral=True)

            try:
                thread_name = f"🤝｜{initiator.display_name}↔️{partner.display_name}"
                thread = await si.channel.create_thread(name=thread_name, type=discord.ChannelType.private_thread)
                await thread.add_user(initiator)
                await thread.add_user(partner)
                trade_view = TradeView(self.cog, initiator, partner, trade_id)
                await trade_view.start_in_thread(thread)
                
                # 원본 메시지가 아닌, UserSelect가 있던 메시지를 수정합니다.
                await si.edit_original_response(content=f"✅ 取引チャンネルを作成しました！{thread.mention}チャンネルを確認してください。", view=None)

            except Exception as e:
                logger.error(f"거래 스레드 생성 중 오류: {e}", exc_info=True)
                await update_wallet(initiator, trade_fee)
                logger.info(f"거래 스레드 생성 오류로 {initiator.id}에게 수수료 250코인 환불 완료.")
                await si.followup.send("❌ 取引チャンネルの作成中にエラーが発生しました。", ephemeral=True)
        
        user_select.callback = select_callback
        view.add_item(user_select)
        await interaction.followup.send("誰と取引しますか？", view=view, ephemeral=True)
    async def handle_mailbox(self, interaction: discord.Interaction):
        mailbox_view = MailboxView(self.cog, interaction.user)
        await mailbox_view.start(interaction)

class Trade(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_trades: Dict[str, TradeView] = {}
        self.currency_icon = "🪙"

    async def cog_load(self):
        self.bot.loop.create_task(self.cleanup_stale_trades())
    
    async def cleanup_stale_trades(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(60)
            stale_trades = [tid for tid, view in self.active_trades.items() if view.is_finished()]
            for tid in stale_trades:
                if tid in self.active_trades: self.active_trades.pop(tid)

    async def register_persistent_views(self):
        self.bot.add_view(TradePanelView(self))
        logger.info("✅ 거래소의 영구 View가 성공적으로 등록되었습니다.")

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_trade", last_log: Optional[discord.Embed] = None):
        if last_log:
            try: await channel.send(embed=last_log)
            except discord.HTTPException: pass
        
        panel_info = get_panel_id(panel_key)
        if panel_info and panel_info.get("message_id"):
            try:
                msg = await channel.fetch_message(panel_info["message_id"])
                await msg.delete()
                logger.info(f"이전 거래소 패널(ID: {panel_info['message_id']})을 삭제했습니다.")
            except (discord.NotFound, discord.Forbidden):
                logger.warning(f"이전 거래소 패널을 찾거나 삭제할 수 없습니다.")

        embed_data = await get_embed_from_db(panel_key)
        if not embed_data:
            logger.error(f"DB에서 '{panel_key}' 임베드를 찾을 수 없어 패널을 생성할 수 없습니다.")
            return

        embed = discord.Embed.from_dict(embed_data)
        view = TradePanelView(self)
        try:
            new_message = await channel.send(embed=embed, view=view)
            await save_panel_id(panel_key, new_message.id, channel.id)
            logger.info(f"✅ '{panel_key}' パネルを正常に再生成しました。(チャンネル: #{channel.name})")
        except discord.Forbidden:
            logger.error(f"'{channel.name}' 채널에 패널을 생성할 권한이 없습니다.")

async def setup(bot: commands.Cog):
    await bot.add_cog(Trade(bot))
