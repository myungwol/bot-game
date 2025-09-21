# cogs/economy/trade.py

import discord
from discord.ext import commandsㄹ
from discord import ui
import logging
import asyncio
import math
import time
from typing import Optional, Dict, List, Any
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

async def delete_after(message: discord.WebhookMessage, delay: int):
    """메시지를 보낸 후 지정된 시간 뒤에 삭제하는 헬퍼 함수"""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden):
        pass
# ▲▲▲ 추가 끝 ▲▲▲

TRADEABLE_CATEGORIES = ["농장_작물", "농장_씨앗", "광물", "미끼", "아이템"]

class ItemSelectModal(ui.Modal, title="수량 입력"):
    quantity_input = ui.TextInput(label="수량", placeholder="수량을 입력하세요.", required=True)
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
            await interaction.response.send_message(f"1에서 {self.max_quantity} 사이의 숫자만 입력해주세요.", ephemeral=True, delete_after=5)
        self.stop()

class MailItemSelectModal(ui.Modal):
    quantity_input = ui.TextInput(label="수량", placeholder="수량을 입력하세요.", required=True)

    def __init__(self, title: str, max_quantity: int, item_name: str, parent_view: 'MailComposeView'):
        super().__init__(title=title)
        self.max_quantity = max_quantity
        self.item_name = item_name
        self.parent_view = parent_view
        self.quantity_input.placeholder = f"최대 {max_quantity}개"

    async def on_submit(self, interaction: discord.Interaction):
        try:
            qty = int(self.quantity_input.value)
            if not 1 <= qty <= self.max_quantity:
                await interaction.response.send_message(f"1에서 {self.max_quantity} 사이의 숫자만 입력해주세요.", ephemeral=True, delete_after=5)
                return

            self.parent_view.attachments["items"][self.item_name] = self.parent_view.attachments["items"].get(self.item_name, 0) + qty
            self.parent_view.current_state = "composing"
            await self.parent_view.update_message(interaction)

        except ValueError:
            await interaction.response.send_message("숫자만 입력해주세요.", ephemeral=True, delete_after=5)


class CoinInputModal(ui.Modal, title="코인 설정"):
    coin_input = ui.TextInput(label="코인", placeholder="설정할 코인 액수를 입력하세요 (제거는 0 입력)", required=True)
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
            await interaction.response.send_message(f"0에서 {self.max_coins:,} 사이의 숫자만 입력해주세요.", ephemeral=True, delete_after=5)
        self.stop()

class MessageModal(ui.Modal, title="메시지 작성"):
    message_input = ui.TextInput(label="메시지 (최대 100자)", style=discord.TextStyle.paragraph, max_length=100, required=False)
    def __init__(self, current_message: str, parent_view: 'MailComposeView'):
        super().__init__()
        self.message_input.default = current_message
        self.parent_view = parent_view
    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.message_content = self.message_input.value
        await self.parent_view.update_message(interaction)

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
        self.message = await thread.send(f"{self.partner.mention}, {self.initiator.mention}님의 1:1 거래 채널입니다.", embed=embed, view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in [self.initiator.id, self.partner.id]:
            await interaction.response.send_message("거래 당사자만 이용할 수 있습니다.", ephemeral=True)
            return False
        
        if interaction.data.get('custom_id') == "confirm_trade_button" and interaction.user.id != self.initiator.id:
            await interaction.response.send_message("거래 신청자만 확정할 수 있습니다.", ephemeral=True, delete_after=5)
            return False
        return True
        
    async def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🤝 1:1 거래", color=0x3498DB)
        for i, user in enumerate([self.initiator, self.partner]):
            offer = self.offers[user.id]
            status = "✅ 준비 완료" if offer["ready"] else "⏳ 준비 중"
            field_value_parts = [f"**{user.mention}** ({status})"]
            if offer["items"]:
                field_value_parts.extend([f"ㄴ {name}: {qty}개" for name, qty in offer["items"].items()])
            if offer["coins"] > 0:
                field_value_parts.append(f"💰 {offer['coins']:,}{self.currency_icon}")
            if len(field_value_parts) == 1:
                field_value_parts.append("제안 없음")
            embed.add_field(name=f"참가자 {i+1}", value="\n".join(field_value_parts), inline=True)
        embed.set_footer(text="5분 후 만료됩니다.")
        return embed

    def build_components(self):
        self.clear_items()
        
        initiator_ready = self.offers[self.initiator.id]["ready"]
        partner_ready = self.offers[self.partner.id]["ready"]
        both_ready = initiator_ready and partner_ready

        # --- 액션 버튼 ---
        self.add_item(ui.Button(label="아이템 추가", style=discord.ButtonStyle.secondary, emoji="📦", custom_id="add_item", row=0))
        self.add_item(ui.Button(label="아이템 제거", style=discord.ButtonStyle.secondary, emoji="🗑️", custom_id="remove_item", row=0))
        self.add_item(ui.Button(label="코인 설정", style=discord.ButtonStyle.secondary, emoji="🪙", custom_id="add_coin", row=0))

        # --- 준비/해제 버튼 ---
        self.add_item(ui.Button(label="준비", style=discord.ButtonStyle.primary, emoji="✅", custom_id="ready", row=1))
        self.add_item(ui.Button(label="준비 해제", style=discord.ButtonStyle.grey, emoji="↩️", custom_id="unready", row=1))
        
        # --- 최종 결정 버튼 ---
        confirm_button = ui.Button(label="거래 확정", style=discord.ButtonStyle.success, emoji="🤝", custom_id="confirm_trade_button", row=2, disabled=not both_ready)
        self.add_item(confirm_button)
        
        cancel_button = ui.Button(label="거래 취소", style=discord.ButtonStyle.danger, emoji="✖️", custom_id="cancel_button", row=2)
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
        if self.offers[user_id]["ready"]: return await interaction.response.send_message("준비 완료 상태에서는 제안을 변경할 수 없습니다.", ephemeral=True, delete_after=5)
        inventory, item_db = await get_inventory(interaction.user), get_item_database()
        tradeable_items = { n: q for n, q in inventory.items() if item_db.get(n, {}).get('category') in TRADEABLE_CATEGORIES }
        if not tradeable_items: return await interaction.response.send_message("거래 가능한 아이템이 없습니다.", ephemeral=True, delete_after=5)
        options = [ discord.SelectOption(label=f"{name} ({qty}개)", value=name) for name, qty in tradeable_items.items() ]
        select_view = ui.View(timeout=180); item_select = ui.Select(placeholder="추가할 아이템을 선택하세요", options=options[:25])
        async def select_callback(si: discord.Interaction):
            item_name, max_qty = si.data['values'][0], tradeable_items.get(si.data['values'][0], 0)
            modal = ItemSelectModal(f"'{item_name}' 수량 입력", max_qty)
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
            return await interaction.response.send_message("준비 완료 상태에서는 제안을 변경할 수 없습니다.", ephemeral=True, delete_after=5)
        
        offered_items = self.offers[user_id]["items"]
        if not offered_items:
            return await interaction.response.send_message("제거할 아이템이 없습니다.", ephemeral=True, delete_after=5)

        options = [discord.SelectOption(label=f"{name} ({qty}개)", value=name) for name, qty in offered_items.items()]
        
        select_view = ui.View(timeout=180)
        item_select = ui.Select(placeholder="제거할 아이템을 선택하세요", options=options)

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
        await interaction.response.send_message("제거할 아이템을 선택하세요.", view=select_view, ephemeral=True)

    async def handle_add_coin(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        if self.offers[user_id]["ready"]: return await interaction.response.send_message("준비 완료 상태에서는 제안을 변경할 수 없습니다.", ephemeral=True, delete_after=5)
        wallet = await get_wallet(user_id); max_coins = wallet.get('balance', 0)
        modal = CoinInputModal("거래 코인 설정", max_coins)
        await interaction.response.send_modal(modal); await modal.wait()
        if modal.coins is not None:
            self.offers[user_id]["coins"] = modal.coins
            await self.update_ui(interaction)

    async def handle_ready(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        if self.offers[user_id]["ready"]:
            msg = await interaction.followup.send("이미 준비 완료 상태입니다.", ephemeral=True)
            self.cog.bot.loop.create_task(delete_after(msg, 5))
            return
        self.offers[user_id]["ready"] = True
        await self.update_ui(interaction)

    # ▼▼▼ [핵심 수정] handle_unready 메서드를 아래 코드로 수정합니다. (오류 수정) ▼▼▼
    async def handle_unready(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        if not self.offers[user_id]["ready"]:
            msg = await interaction.followup.send("아직 준비 완료 상태가 아닙니다.", ephemeral=True)
            self.cog.bot.loop.create_task(delete_after(msg, 5))
            return
        self.offers[user_id]["ready"] = False
        await self.update_ui(interaction)

    async def handle_cancel(self, interaction: discord.Interaction):
        await interaction.followup.send("거래 취소를 요청했습니다.", ephemeral=True)
        await self._end_trade(cancelled_by=interaction.user)

    async def process_trade(self, interaction: discord.Interaction):
        self.build_components()
        for item in self.children: item.disabled = True
        await self.message.edit(content="**거래 확정! 처리 중...**", view=self, embed=await self.build_embed())
        user1, user2, offer1, offer2 = self.initiator, self.partner, self.offers[self.initiator.id], self.offers[self.partner.id]
        try:
            user1_wallet, user1_inv = await asyncio.gather(get_wallet(user1.id), get_inventory(user1))
            user2_wallet, user2_inv = await asyncio.gather(get_wallet(user2.id), get_inventory(user2))
            if user1_wallet.get('balance', 0) < offer1['coins']: return await self.fail_trade(f"{user1.mention}님의 코인이 부족합니다.")
            if user2_wallet.get('balance', 0) < offer2['coins']: return await self.fail_trade(f"{user2.mention}님의 코인이 부족합니다.")
            for item, qty in offer1['items'].items():
                if user1_inv.get(item, 0) < qty: return await self.fail_trade(f"{user1.mention}님의 '{item}' 재고가 부족합니다.")
            for item, qty in offer2['items'].items():
                if user2_inv.get(item, 0) < qty: return await self.fail_trade(f"{user2.mention}님의 '{item}' 재고가 부족합니다.")
            
            commission_rate = 0.05
            commission = math.ceil((offer1['coins'] + offer2['coins']) * commission_rate)

            tasks = []
            user1_coin_change = offer2['coins'] - offer1['coins']
            user2_coin_change = offer1['coins'] - offer2['coins']
            if user1_coin_change != 0: tasks.append(update_wallet(user1, int(user1_coin_change)))
            if user2_coin_change != 0: tasks.append(update_wallet(user2, int(user2_coin_change)))
            
            if commission > 0:
                half_commission = math.ceil(commission / 2)
                tasks.append(update_wallet(user1, -half_commission))
                tasks.append(update_wallet(user2, -half_commission))

            for item, qty in offer1['items'].items(): tasks.extend([update_inventory(user1.id, item, -qty), update_inventory(user2.id, item, qty)])
            for item, qty in offer2['items'].items(): tasks.extend([update_inventory(user2.id, item, -qty), update_inventory(user1.id, item, qty)])
            if tasks: await asyncio.gather(*tasks)
        except Exception as e:
            logger.error(f"거래 처리 중 예외 발생: {e}", exc_info=True)
            return await self.fail_trade("알 수 없는 오류가 발생했습니다.")
        
        if self.message:
            log_channel_id = get_id("trade_panel_channel_id")
            if log_channel_id and (log_channel := self.cog.bot.get_channel(log_channel_id)):
                if log_embed_data := await get_embed_from_db("log_trade_success"):
                    log_embed = format_embed_from_db(log_embed_data, user1_mention=user1.mention, user2_mention=user2.mention, commission=commission, currency_icon=self.currency_icon)
                    offer1_str = "\n".join([f"ㄴ {n}: {q}개" for n, q in offer1['items'].items()] + ([f"💰 {offer1['coins']:,}{self.currency_icon}"] if offer1['coins'] > 0 else [])) or "없음"
                    offer2_str = "\n".join([f"ㄴ {n}: {q}개" for n, q in offer2['items'].items()] + ([f"💰 {offer2['coins']:,}{self.currency_icon}"] if offer2['coins'] > 0 else [])) or "없음"
                    log_embed.add_field(name=f"{user1.display_name} 제공", value=offer1_str, inline=True)
                    log_embed.add_field(name=f"{user2.display_name} 제공", value=offer2_str, inline=True)
                    log_embed.set_footer(text=f"거래세: {commission}{self.currency_icon} (신청 수수료 250코인은 환불되지 않음)")
                    await self.cog.regenerate_panel(log_channel, last_log=log_embed)
            
            await self.message.channel.send("✅ 거래가 성공적으로 완료되었습니다. 이 채널은 10초 후에 삭제됩니다.")
            await asyncio.sleep(10); await self.message.channel.delete()
        self.stop()

    async def fail_trade(self, reason: str):
        if self.message:
            if self.initiator:
                refund_result = await update_wallet(self.initiator, 250)
                if refund_result:
                    reason += f"\n(거래 신청 수수료 250{self.currency_icon} 환불됨)"
                    logger.info(f"거래 실패로 {self.initiator.id}에게 수수료 250코인 환불 완료.")
                else:
                    logger.error(f"거래 실패 후 {self.initiator.id}에게 수수료 환불 실패!")
            
            await self.message.channel.send(f"❌ 거래 실패: {reason}\n이 채널은 10초 후에 삭제됩니다.")
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
                final_messages.append(f"{cancelled_by.mention}님이 거래를 취소했습니다.")
            else:
                final_messages.append("시간이 초과되어 거래가 자동으로 종료되었습니다.")

            if self.initiator:
                refund_result = await update_wallet(self.initiator, 250)
                if refund_result:
                    logger.info(f"거래 취소/타임아웃으로 {self.initiator.id}에게 수수료 250코인 환불 완료.")
                    final_messages.append(f"{self.initiator.mention}님에게 거래 신청 수수료 250{self.currency_icon}을(를) 환불해드렸습니다.")
                else:
                    logger.error(f"거래 취소/타임아웃 후 {self.initiator.id}에게 수수료 환불 실패!")
            
            final_messages.append("\n이 채널은 10초 후에 삭제됩니다.")
            
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
        
class RemoveItemSelectView(ui.View):
    def __init__(self, parent_view: 'MailComposeView'):
        super().__init__(timeout=180)
        self.parent_view = parent_view

    async def start(self, interaction: discord.Interaction):
        await self.build_components()
        await interaction.followup.send("제거할 아이템을 선택하세요.", view=self, ephemeral=True)

    async def build_components(self):
        self.clear_items()
        attached_items = self.parent_view.attachments.get("items", {})
        if not attached_items:
            self.add_item(ui.Button(label="제거할 아이템이 없습니다.", disabled=True))
            return
        
        options = [discord.SelectOption(label=f"{name} ({qty}개)", value=name) for name, qty in attached_items.items()]
        item_select = ui.Select(placeholder="제거할 아이템 선택...", options=options)
        item_select.callback = self.on_item_select
        self.add_item(item_select)

    async def on_item_select(self, interaction: discord.Interaction):
        item_name = interaction.data['values'][0]
        if item_name in self.parent_view.attachments["items"]:
            del self.parent_view.attachments["items"][item_name]
        
        # 부모 View를 업데이트하고 현재 상호작용(선택 메뉴) 메시지는 삭제
        await self.parent_view.refresh(interaction)
        try:
            await interaction.delete_original_response()
        except (discord.NotFound, discord.HTTPException):
            pass
            
class MailComposeView(ui.View):
    def __init__(self, cog: 'Trade', user: discord.Member, recipient: discord.Member, original_interaction: discord.Interaction):
        super().__init__(timeout=300)
        self.cog = cog
        self.user = user
        self.recipient = recipient
        # 이 상호작용은 이제 UserSelect의 상호작용이 됩니다.
        self.original_interaction = original_interaction 
        self.message_content = ""
        self.attachments = {"items": {}}
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.shipping_fee = 100
        self.message: Optional[discord.WebhookMessage] = None

    async def start(self):
        # 시작할 때 첫 메시지를 보냅니다.
        embed = await self.build_embed()
        await self.build_components()
        
        # ▼▼▼ [핵심 수정] original_interaction이 defer되었으므로 followup.send를 사용합니다. ▼▼▼
        self.message = await self.original_interaction.followup.send(embed=embed, view=self, ephemeral=True)
        # ▲▲▲ 수정 끝 ▲▲▲

    async def refresh(self, interaction: Optional[discord.Interaction] = None):
        # View를 새로고침하는 중앙 함수
        if interaction and not interaction.response.is_done():
            await interaction.response.defer()

        embed = await self.build_embed()
        await self.build_components()
        
        target = interaction or self # 수정할 메시지를 찾기 위함
        if target and self.message:
            await self.message.edit(embed=embed, view=self)

    async def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"✉️ 편지 쓰기 (TO: {self.recipient.display_name})", color=0x3498DB)
        att_items = self.attachments.get("items", {})
        att_str = [f"ㄴ {name}: {qty}개" for name, qty in att_items.items()]
        embed.add_field(name="첨부 아이템", value="\n".join(att_str) if att_str else "없음", inline=False)
        embed.add_field(name="메시지", value=f"```{self.message_content}```" if self.message_content else "메시지 없음", inline=False)
        embed.set_footer(text=f"배송비: {self.shipping_fee:,}{self.currency_icon}")
        return embed

    async def build_components(self):
        self.clear_items()
        
        # 액션 버튼들
        self.add_item(ui.Button(label="아이템 첨부", style=discord.ButtonStyle.secondary, emoji="📦", custom_id="attach_item", row=0))
        # 아이템이 있을 때만 제거 버튼 활성화
        remove_disabled = not self.attachments.get("items")
        self.add_item(ui.Button(label="아이템 제거", style=discord.ButtonStyle.secondary, emoji="🗑️", custom_id="remove_item", row=0, disabled=remove_disabled))
        self.add_item(ui.Button(label="메시지 작성/수정", style=discord.ButtonStyle.secondary, emoji="✍️", custom_id="write_message", row=0))
        
        # 보내기 버튼
        self.add_item(ui.Button(label="보내기", style=discord.ButtonStyle.success, emoji="🚀", custom_id="send_mail", row=1))

        for item in self.children:
            item.callback = self.dispatch_callback

    async def dispatch_callback(self, interaction: discord.Interaction):
        custom_id = interaction.data['custom_id']
        
        # 모달을 여는 작업은 defer를 하지 않고 바로 실행
        if custom_id == "write_message":
            return await self.handle_write_message(interaction)
        
        # 나머지 작업은 defer 후 실행
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
    
    async def handle_remove_item(self, interaction: discord.Interaction):
        view = RemoveItemSelectView(self)
        await view.start(interaction)

    async def handle_write_message(self, interaction: discord.Interaction):
        modal = MessageModal(self.message_content, self)
        await interaction.response.send_modal(modal)
        # 모달이 닫힌 후 refresh는 MessageModal의 on_submit에서 처리

    async def handle_send(self, interaction: discord.Interaction):
        try:
            wallet, inventory = await asyncio.gather(get_wallet(self.user.id), get_inventory(self.user))
            if wallet.get('balance', 0) < self.shipping_fee:
                msg = await interaction.followup.send(f"코인이 부족합니다. (배송비: {self.shipping_fee:,}{self.currency_icon})", ephemeral=True)
                return await delete_after(msg, 5)
            
            for item, qty in self.attachments["items"].items():
                if inventory.get(item, 0) < qty:
                    msg = await interaction.followup.send(f"아이템 재고가 부족합니다: '{item}'", ephemeral=True)
                    return await delete_after(msg, 5)
            
            db_tasks = [update_wallet(self.user, -self.shipping_fee)]
            for item, qty in self.attachments["items"].items(): db_tasks.append(update_inventory(self.user.id, item, -qty))
            await asyncio.gather(*db_tasks)
            now, expires_at = datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(days=30)
            
            mail_res = await supabase.table('mails').insert({"sender_id": str(self.user.id), "recipient_id": str(self.recipient.id), "message": self.message_content, "sent_at": now.isoformat(), "expires_at": expires_at.isoformat()}).execute()

            if not mail_res.data:
                logger.error("메일 레코드 생성 실패. 환불 시도."); refund_tasks = [update_wallet(self.user, self.shipping_fee)]
                for item, qty in self.attachments["items"].items(): refund_tasks.append(update_inventory(self.user.id, item, qty))
                await asyncio.gather(*refund_tasks)
                return await interaction.edit_original_response(content="우편 발송 실패. 비용이 환불되었습니다.", view=None, embed=None)
            
            new_mail_id = mail_res.data[0]['id']
            if self.attachments["items"]:
                att_to_insert = [{"mail_id": new_mail_id, "item_name": n, "quantity": q, "is_coin": False} for n, q in self.attachments["items"].items()]
                await supabase.table('mail_attachments').insert(att_to_insert).execute()
            
            await interaction.edit_original_response(content="✅ 우편을 성공적으로 보냈습니다.", view=None, embed=None)
            
            if (panel_ch_id := get_id("trade_panel_channel_id")) and (panel_ch := self.cog.bot.get_channel(panel_ch_id)):
                if embed_data := await get_embed_from_db("log_new_mail"):
                    log_embed = format_embed_from_db(embed_data, sender_mention=self.user.mention, recipient_mention=self.recipient.mention)
                    await panel_ch.send(content=self.recipient.mention, embed=log_embed, allowed_mentions=discord.AllowedMentions(users=True), delete_after=60.0)
                await self.cog.regenerate_panel(panel_ch)
            self.stop()
        except Exception as e:
            logger.error(f"우편 발송 중 최종 단계에서 예외 발생: {e}", exc_info=True)
            await interaction.followup.send("우편 발송 중 오류가 발생했습니다. 재료 소모 여부를 확인해주세요.", ephemeral=True)
            self.stop()

class MailboxView(ui.View):
    def __init__(self, cog: 'Trade', user: discord.Member):
        super().__init__(timeout=180)
        self.cog = cog
        self.user = user
        self.page = 0
        self.mails_on_page: List[Dict] = []
        self.message: Optional[discord.WebhookMessage] = None
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.selected_mail_ids: List[str] = []

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
            await interaction.followup.send("오류: UI가 만료되었거나 찾을 수 없습니다. 우편함을 다시 열어주세요.", ephemeral=True, delete_after=5)
            self.stop()


    async def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"📫 {self.user.display_name}의 우편함", color=0x964B00)
        res = await supabase.table('mails').select('*, mail_attachments(*)', count='exact').eq('recipient_id', str(self.user.id)).is_('claimed_at', None).order('sent_at', desc=True).range(self.page * 5, self.page * 5 + 4).execute()
        
        self.mails_on_page = res.data if res.data else []
        
        if not self.mails_on_page:
            embed.description = "받은 편지가 없습니다."
        else:
            embed.set_footer(text=f"페이지 {self.page + 1} / {math.ceil((res.count or 0) / 5)}")
            for i, mail in enumerate(self.mails_on_page):
                sender_id_int = int(mail['sender_id'])
                sender = self.cog.bot.get_user(sender_id_int)
                sender_name = sender.display_name if sender else f"알 수 없는 유저 ({sender_id_int})"
                sender_mention = sender.mention if sender else sender_name

                attachments = mail['mail_attachments']
                att_str = [f"📦 {att['item_name']}: {att['quantity']}개" for att in attachments if not att['is_coin']]
                field_value = (f"**보낸 사람:** {sender_mention}\n" +
                               (f"> **메시지:** {mail['message']}\n" if mail['message'] else "") +
                               "**첨부 아이템:**\n" + ("\n".join(att_str) if att_str else "없음"))
                embed.add_field(name=f"FROM: {sender_name} ({discord.utils.format_dt(datetime.fromisoformat(mail['sent_at']), 'R')})", value=field_value, inline=False)
                
                if i < len(self.mails_on_page) - 1:
                    embed.add_field(name="\u200b", value="━━━━━━━━━━━━━━━━━━", inline=False)
        return embed

    async def build_components(self):
        self.clear_items()
        
        mail_options = [
            discord.SelectOption(
                label=f"보낸사람: {getattr(self.cog.bot.get_user(int(m['sender_id'])), 'display_name', m['sender_id'])}",
                value=str(m['id']),
                default=(str(m['id']) in self.selected_mail_ids)
            ) for m in self.mails_on_page
        ]

        if mail_options:
            select = ui.Select(
                placeholder="처리할 우편을 선택하세요 (여러 개 선택 가능)",
                options=mail_options,
                max_values=len(mail_options)
            )
            select.callback = self.on_mail_select
            self.add_item(select)

        claim_all_button = ui.Button(label="선택한 우편 모두 받기", style=discord.ButtonStyle.success, emoji="📥", disabled=not self.selected_mail_ids, row=1)
        claim_all_button.callback = self.claim_selected_mails
        self.add_item(claim_all_button)

        delete_all_button = ui.Button(label="선택한 우편 모두 삭제", style=discord.ButtonStyle.danger, emoji="🗑️", disabled=not self.selected_mail_ids, row=1)
        delete_all_button.callback = self.delete_selected_mails
        self.add_item(delete_all_button)

        send_button = ui.Button(label="편지 보내기", style=discord.ButtonStyle.success, emoji="✉️", row=2)
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
            await interaction.followup.send("우편을 수령하는 중 오류가 발생했습니다.", ephemeral=True)
            return
        
        if claimed_count > 0:
            item_summary = "\n".join([f"ㄴ {name}: {qty}개" for name, qty in total_items.items()])
            success_message = f"{claimed_count}개의 우편을 수령했습니다!\n\n**총 받은 아이템:**\n{item_summary or '없음'}"
            
            msg = await interaction.followup.send(success_message, ephemeral=True)

            async def delete_msg_after(delay, message):
                await asyncio.sleep(delay)
                try: await message.delete()
                except discord.NotFound: pass
            self.cog.bot.loop.create_task(delete_msg_after(10, msg))

        else:
            await interaction.followup.send("수령할 우편이 없거나 오류가 발생했습니다.", ephemeral=True)
        
        self.selected_mail_ids.clear()
        await self.update_view(interaction)

    async def delete_selected_mails(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        mail_ids_to_delete = [int(mid) for mid in self.selected_mail_ids]
        
        await supabase.table('mails').delete().in_('id', mail_ids_to_delete).eq('recipient_id', str(self.user.id)).execute()
        
        self.selected_mail_ids.clear()
        await self.update_view(interaction)
        
    async def send_mail(self, interaction: discord.Interaction):
        view = ui.View(timeout=180)
        user_select = ui.UserSelect(placeholder="편지를 보낼 상대를 선택하세요.")
        
        # ▼▼▼ [핵심 수정] 아래 select_callback 함수를 교체합니다. ▼▼▼
        async def callback(select_interaction: discord.Interaction):
            # 1. 먼저 UserSelect 상호작용에 응답하여 "상호작용 실패"를 방지합니다.
            #    여기서는 아무것도 하지 않는 defer()를 사용합니다.
            #    MailComposeView가 이 상호작용을 수정할 것이기 때문입니다.
            await select_interaction.response.defer(ephemeral=True)
            
            recipient_id = int(select_interaction.data['values'][0])
            recipient = interaction.guild.get_member(recipient_id)
            if not recipient or recipient.bot or recipient.id == self.user.id:
                await select_interaction.followup.send("잘못된 상대입니다.", ephemeral=True, delete_after=5)
                return
            
            # 2. MailComposeView를 생성할 때, UserSelect의 상호작용(select_interaction)을 넘겨줍니다.
            compose_view = MailComposeView(self.cog, self.user, recipient, select_interaction)
            # 3. MailComposeView의 start 메서드가 이제 새 메시지를 보내거나 기존 메시지를 수정합니다.
            await compose_view.start()

            # 4. "누구에게 편지를 보내시겠습니까?" 메시지를 수정하여 UI를 정리합니다.
            try:
                await interaction.edit_original_response(content="편지 작성 UI가 열렸습니다.", view=None)
            except discord.NotFound:
                pass
        # ▲▲▲ 수정 끝 ▲▲▲

        user_select.callback = callback
        view.add_item(user_select)
        await interaction.edit_original_response(content="누구에게 편지를 보내시겠습니까?", view=view, embed=None)
    
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
        trade_button = ui.Button(label="1:1 거래하기", style=discord.ButtonStyle.success, emoji="🤝", custom_id="trade_panel_direct_trade")
        trade_button.callback = self.dispatch_callback # <--- 콜백을 dispatch_callback으로 변경
        self.add_item(trade_button)
        mailbox_button = ui.Button(label="우편함", style=discord.ButtonStyle.primary, emoji="📫", custom_id="trade_panel_mailbox")
        mailbox_button.callback = self.dispatch_callback # <--- 콜백을 dispatch_callback으로 변경
        self.add_item(mailbox_button)

    # ▼▼▼ [핵심 수정] dispatch_callback 메서드를 추가합니다. ▼▼▼
    async def dispatch_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
            
            # --- 기존 로직 시작 ---
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
            return await interaction.followup.send(f"❌ 거래를 시작하려면 수수료 {trade_fee}{self.cog.currency_icon}가 필요합니다.", ephemeral=True)

        view = ui.View(timeout=180)
        user_select = ui.UserSelect(placeholder="거래할 상대를 선택하세요.")
        async def select_callback(si: discord.Interaction):
            await si.response.defer(ephemeral=True)
            partner_id = int(si.data['values'][0])
            partner = si.guild.get_member(partner_id)
            if not partner or partner.bot or partner.id == initiator.id:
                return await si.followup.send("잘못된 상대입니다.", ephemeral=True)
            trade_id = f"{min(initiator.id, partner.id)}-{max(initiator.id, partner.id)}"
            if trade_id in self.cog.active_trades:
                 return await si.followup.send("상대방 또는 본인이 이미 다른 거래에 참여 중입니다.", ephemeral=True)
            
            result = await update_wallet(initiator, -trade_fee)
            if not result:
                logger.error(f"{initiator.id}의 거래 수수료 차감 실패. 잔액 부족 가능성.")
                return await si.followup.send(f"❌ 수수료({trade_fee}{self.cog.currency_icon})를 지불하는데 실패했습니다. 잔액을 확인해주세요.", ephemeral=True)
            
            logger.info(f"{initiator.id}에게서 거래 수수료 250코인 차감 완료.")
            await si.followup.send(f"✅ 거래 신청 수수료 {trade_fee}{self.cog.currency_icon}를 지불했습니다.", ephemeral=True)

            try:
                thread_name = f"🤝｜{initiator.display_name}↔️{partner.display_name}"
                thread = await si.channel.create_thread(name=thread_name, type=discord.ChannelType.private_thread)
                await thread.add_user(initiator)
                await thread.add_user(partner)
                trade_view = TradeView(self.cog, initiator, partner, trade_id)
                await trade_view.start_in_thread(thread)
                
                await si.followup.send(f"✅ 거래 채널을 만들었습니다! {thread.mention} 채널을 확인해주세요.", ephemeral=True)
                # ▼▼▼ [핵심 수정] 아래 1줄을 삭제합니다. ▼▼▼
                # await interaction.edit_original_response(content=f"거래 상대({partner.mention}) 선택 완료.", view=None)

            except Exception as e:
                logger.error(f"거래 스레드 생성 중 오류: {e}", exc_info=True)
                await update_wallet(initiator, trade_fee)
                logger.info(f"거래 스레드 생성 오류로 {initiator.id}에게 수수료 250코인 환불 완료.")
                await si.followup.send("❌ 거래 채널을 만드는 중 오류가 발생했습니다.", ephemeral=True)
        
        user_select.callback = select_callback
        view.add_item(user_select)
        await interaction.followup.send("누구와 거래하시겠습니까?", view=view, ephemeral=True)

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
            logger.info(f"✅ '{panel_key}' 패널을 성공적으로 재생성했습니다. (채널: #{channel.name})")
        except discord.Forbidden:
            logger.error(f"'{channel.name}' 채널에 패널을 생성할 권한이 없습니다.")

async def setup(bot: commands.Cog):
    await bot.add_cog(Trade(bot))
