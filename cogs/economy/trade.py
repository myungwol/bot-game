# bot-game/cogs/economy/trade.py

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
import math
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

class CoinInputModal(ui.Modal, title="코인 입력"):
    coin_input = ui.TextInput(label="코인", placeholder="코인을 입력하세요.", required=True)
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
    def __init__(self, current_message: str):
        super().__init__()
        self.message_input.default = current_message
        self.message: Optional[str] = None
    async def on_submit(self, interaction: discord.Interaction):
        self.message = self.message_input.value
        await interaction.response.defer()
        self.stop()

class TradeView(ui.View):
    def __init__(self, cog: 'Trade', initiator: discord.Member, partner: discord.Member):
        super().__init__(timeout=300)
        self.cog = cog
        self.initiator = initiator
        self.partner = partner
        self.trade_id = f"{min(initiator.id, partner.id)}-{max(initiator.id, partner.id)}"
        self.offers = {
            initiator.id: {"items": {}, "coins": 0, "ready": False},
            partner.id: {"items": {}, "coins": 0, "ready": False}
        }
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.message: Optional[discord.Message] = None

    async def start_in_channel(self, channel: discord.TextChannel):
        self.cog.active_trades[self.trade_id] = self
        embed = await self.build_embed()
        self.message = await channel.send(f"{self.partner.mention}, {self.initiator.mention}님이 1:1 거래를 신청했습니다.", embed=embed, view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in [self.initiator.id, self.partner.id]:
            await interaction.response.send_message("거래 당사자만 이용할 수 있습니다.", ephemeral=True, delete_after=5)
            return False
        if self.offers[self.initiator.id]["ready"] and self.offers[self.partner.id]["ready"]:
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

            embed.add_field(
                name=f"참가자 {i+1}",
                value="\n".join(field_value_parts),
                inline=True
            )
        embed.set_footer(text="5분 후 만료됩니다.")
        return embed

    async def update_ui(self):
        if self.is_finished() or not self.message: return
        embed = await self.build_embed()
        try:
            await self.message.edit(embed=embed, view=self)
        except (discord.NotFound, discord.Forbidden) as e:
            logger.warning(f"거래 UI 업데이트 실패 (Message ID: {self.message.id}): {e}")
            self.stop()

    @ui.button(label="아이템 추가", style=discord.ButtonStyle.secondary, emoji="📦")
    async def add_item_button(self, interaction: discord.Interaction, button: ui.Button):
        user_id = interaction.user.id
        if self.offers[user_id]["ready"]:
            return await interaction.response.send_message("준비 완료 상태에서는 제안을 변경할 수 없습니다.", ephemeral=True, delete_after=5)
        inventory = await get_inventory(interaction.user)
        item_db = get_item_database()
        tradeable_items = { name: qty for name, qty in inventory.items() if item_db.get(name, {}).get('category') in TRADEABLE_CATEGORIES }
        if not tradeable_items:
            return await interaction.response.send_message("거래 가능한 아이템이 없습니다.", ephemeral=True, delete_after=5)
        options = [ discord.SelectOption(label=f"{name} ({qty}개)", value=name) for name, qty in tradeable_items.items() ]
        
        select_view = ui.View(timeout=180)
        item_select = ui.Select(placeholder="추가할 아이템을 선택하세요", options=options[:25])
        
        async def select_callback(select_interaction: discord.Interaction):
            item_name = select_interaction.data['values'][0]
            max_qty = tradeable_items.get(item_name, 0)
            modal = ItemSelectModal(f"'{item_name}' 수량 입력", max_qty)
            await select_interaction.response.send_modal(modal)
            await modal.wait()
            if modal.quantity is not None:
                self.offers[user_id]["items"][item_name] = modal.quantity
                await self.update_ui()
            try: await select_interaction.delete_original_response()
            except discord.NotFound: pass
        
        item_select.callback = select_callback
        select_view.add_item(item_select)
        await interaction.response.send_message(view=select_view, ephemeral=True)

    @ui.button(label="코인 추가", style=discord.ButtonStyle.secondary, emoji="🪙")
    async def add_coin_button(self, interaction: discord.Interaction, button: ui.Button):
        user_id = interaction.user.id
        if self.offers[user_id]["ready"]:
            return await interaction.response.send_message("준비 완료 상태에서는 제안을 변경할 수 없습니다.", ephemeral=True, delete_after=5)
        wallet = await get_wallet(user_id)
        max_coins = wallet.get('balance', 0)
        modal = CoinInputModal("거래 코인 입력", max_coins)
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal.coins is not None:
            self.offers[user_id]["coins"] = modal.coins
            await self.update_ui()

    @ui.button(label="준비/확정", style=discord.ButtonStyle.success, emoji="✅")
    async def ready_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        user_id = interaction.user.id
        self.offers[user_id]["ready"] = not self.offers[user_id]["ready"]
        
        if self.offers[self.initiator.id]["ready"] and self.offers[self.partner.id]["ready"]:
            await self.process_trade(interaction)
        else:
            await self.update_ui()

    @ui.button(label="취소", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("거래를 취소했습니다.", ephemeral=True, delete_after=10)
        await self.on_timeout(cancelled_by=interaction.user)

    async def process_trade(self, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await self.message.edit(content="**거래 확정! 처리 중...**", view=self, embed=None)

        user1, user2 = self.initiator, self.partner
        offer1, offer2 = self.offers[user1.id], self.offers[user2.id]

        try:
            user1_wallet, user1_inv = await asyncio.gather(get_wallet(user1.id), get_inventory(user1))
            user2_wallet, user2_inv = await asyncio.gather(get_wallet(user2.id), get_inventory(user2))

            if user1_wallet.get('balance', 0) < offer1['coins']:
                return await self.fail_trade(f"{user1.mention}님의 코인이 부족합니다.")
            if user2_wallet.get('balance', 0) < offer2['coins']:
                return await self.fail_trade(f"{user2.mention}님의 코인이 부족합니다.")

            for item, qty in offer1['items'].items():
                if user1_inv.get(item, 0) < qty:
                    return await self.fail_trade(f"{user1.mention}님의 '{item}' 재고가 부족합니다.")
            for item, qty in offer2['items'].items():
                if user2_inv.get(item, 0) < qty:
                    return await self.fail_trade(f"{user2.mention}님의 '{item}' 재고가 부족합니다.")

            tasks = []
            user1_coin_change = offer2['coins'] - offer1['coins']
            user2_coin_change = offer1['coins'] - offer2['coins']
            if user1_coin_change != 0:
                tasks.append(update_wallet(user1, int(user1_coin_change)))
            if user2_coin_change != 0:
                tasks.append(update_wallet(user2, int(user2_coin_change)))

            for item, qty in offer1['items'].items():
                tasks.append(update_inventory(user1.id, item, -qty))
                tasks.append(update_inventory(user2.id, item, qty))

            for item, qty in offer2['items'].items():
                tasks.append(update_inventory(user2.id, item, -qty))
                tasks.append(update_inventory(user1.id, item, qty))

            if tasks:
                await asyncio.gather(*tasks)

        except Exception as e:
            logger.error(f"거래 처리 중 예외 발생: {e}", exc_info=True)
            return await self.fail_trade("알 수 없는 오류가 발생했습니다. 관리자에게 문의하세요.")

        log_channel = self.message.channel
        if self.message: await self.message.delete()
        
        log_embed_data = await get_embed_from_db("log_trade_success")
        if log_embed_data:
            log_embed = format_embed_from_db(
                log_embed_data, user1_mention=user1.mention, user2_mention=user2.mention,
                commission=0, currency_icon=self.currency_icon
            )
            offer1_str = "\n".join([f"ㄴ {n}: {q}개" for n, q in offer1['items'].items()] + ([f"💰 {offer1['coins']:,}{self.currency_icon}"] if offer1['coins'] > 0 else [])) or "없음"
            offer2_str = "\n".join([f"ㄴ {n}: {q}개" for n, q in offer2['items'].items()] + ([f"💰 {offer2['coins']:,}{self.currency_icon}"] if offer2['coins'] > 0 else [])) or "없음"
            log_embed.add_field(name=f"{user1.display_name} 제공", value=offer1_str, inline=True)
            log_embed.add_field(name=f"{user2.display_name} 제공", value=offer2_str, inline=True)
            await self.cog.regenerate_panel(log_channel, last_log=log_embed)
        self.stop()
    
    async def fail_trade(self, reason: str):
        if self.message:
            if self.initiator:
                trade_fee = 50
                await update_wallet(self.initiator, trade_fee)
                reason += f"\n(거래 수수료 {trade_fee}{self.currency_icon} 환불됨)"
            
            await self.message.channel.send(f"❌ 거래 실패: {reason}", delete_after=10)
            await self.message.delete()
        self.stop()

    async def on_timeout(self, cancelled_by: Optional[discord.User] = None):
        if self.is_finished(): return
        self.stop()
        if self.message:
            try:
                if self.initiator:
                    trade_fee = 50
                    await update_wallet(self.initiator, trade_fee)
                    
                    message_content = ""
                    if cancelled_by:
                        if self.initiator.id == cancelled_by.id:
                             message_content = f"거래를 취소하여 수수료 {trade_fee}{self.currency_icon}를 환불해드렸습니다."
                    else:
                        message_content = f"거래가 시간 초과로 취소되어 수수료 {trade_fee}{self.currency_icon}를 환불해드렸습니다."
                    
                    if message_content:
                        try:
                            await self.initiator.send(message_content)
                        except discord.Forbidden:
                            logger.warning(f"{self.initiator.id}에게 거래 취소 DM을 보낼 수 없습니다.")

                await self.message.edit(content="거래가 종료되었습니다.", view=None, embed=None)
                await asyncio.sleep(10)
                await self.message.delete()
            except (discord.NotFound, discord.Forbidden): pass
    
    def stop(self):
        if self.trade_id in self.cog.active_trades:
            self.cog.active_trades.pop(self.trade_id)
        super().stop()

class MailComposeView(ui.View):
    def __init__(self, cog: 'Trade', user: discord.Member, recipient: discord.Member):
        super().__init__(timeout=300)
        self.cog = cog
        self.user = user
        self.recipient = recipient
        self.message_content = ""
        self.attachments = {"items": {}}
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.shipping_fee = 50
        self.message: Optional[discord.WebhookMessage] = None

    async def start(self, interaction: discord.Interaction):
        await self.update_view(interaction, new_message=True)

    async def update_view(self, interaction: discord.Interaction, new_message=False):
        embed = await self.build_embed()
        if new_message:
            target = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            self.message = await target(embed=embed, view=self, ephemeral=True)
            if not isinstance(self.message, discord.WebhookMessage): self.message = await interaction.original_response()
        else:
            if self.message:
                await self.message.edit(embed=embed, view=self)

    async def refresh_ui(self):
        if self.is_finished() or not self.message: return
        embed = await self.build_embed()
        await self.message.edit(embed=embed, view=self)

    async def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"✉️ 편지 쓰기 (TO: {self.recipient.display_name})", color=0x3498DB)
        att_str = [f"ㄴ {name}: {qty}개" for name, qty in self.attachments["items"].items()]
        embed.add_field(name="첨부 아이템", value="\n".join(att_str) if att_str else "없음", inline=False)
        embed.add_field(name="메시지", value=f"```{self.message_content}```" if self.message_content else "메시지 없음", inline=False)
        embed.set_footer(text=f"배송비: {self.shipping_fee:,}{self.currency_icon}")
        return embed

    @ui.button(label="아이템 첨부", style=discord.ButtonStyle.secondary, emoji="📦")
    async def attach_item_button(self, interaction: discord.Interaction, button: ui.Button):
        inventory = await get_inventory(self.user)
        item_db = get_item_database()
        tradeable_items = { name: qty for name, qty in inventory.items() if item_db.get(name, {}).get('category') in TRADEABLE_CATEGORIES }
        if not tradeable_items:
            return await interaction.response.send_message("첨부할 수 있는 아이템이 없습니다.", ephemeral=True, delete_after=5)
        options = [ discord.SelectOption(label=f"{name} ({qty}개)", value=name) for name, qty in tradeable_items.items() ]
        
        select_view = ui.View(timeout=180)
        item_select = ui.Select(placeholder="첨부할 아이템을 선택하세요", options=options[:25])
        
        async def select_callback(select_interaction: discord.Interaction):
            item_name = select_interaction.data['values'][0]
            max_qty = tradeable_items.get(item_name, 0)
            modal = ItemSelectModal(f"'{item_name}' 수량 입력", max_qty)
            await select_interaction.response.send_modal(modal)
            await modal.wait()
            if modal.quantity is not None:
                self.attachments["items"][item_name] = self.attachments["items"].get(item_name, 0) + modal.quantity
                await self.refresh_ui()
            try: await select_interaction.delete_original_response()
            except discord.NotFound: pass

        item_select.callback = select_callback
        select_view.add_item(item_select)
        await interaction.response.send_message(view=select_view, ephemeral=True)

    @ui.button(label="메시지 작성", style=discord.ButtonStyle.secondary, emoji="✍️")
    async def write_message_button(self, interaction: discord.Interaction, button: ui.Button):
        modal = MessageModal(self.message_content)
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal.message is not None:
            self.message_content = modal.message
            await self.refresh_ui()

    @ui.button(label="보내기", style=discord.ButtonStyle.success, emoji="🚀")
    async def send_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)

        try:
            wallet, inventory = await asyncio.gather(
                get_wallet(self.user.id),
                get_inventory(self.user)
            )

            if wallet.get('balance', 0) < self.shipping_fee:
                return await interaction.followup.send(f"코인이 부족합니다. (배송비: {self.shipping_fee:,}{self.currency_icon})", ephemeral=True)

            for item_name, quantity in self.attachments["items"].items():
                if inventory.get(item_name, 0) < quantity:
                    return await interaction.followup.send(f"아이템 재고가 부족합니다: '{item_name}'", ephemeral=True)
            
            db_tasks = [update_wallet(self.user, -self.shipping_fee)]
            for item_name, quantity in self.attachments["items"].items():
                db_tasks.append(update_inventory(self.user.id, item_name, -quantity))
            
            await asyncio.gather(*db_tasks)
            
            now = datetime.now(timezone.utc)
            expires_at = now + timedelta(days=30)
            
            mail_insert_res = await supabase.table('mails').insert({
                "sender_id": str(self.user.id),
                "recipient_id": str(self.recipient.id),
                "message": self.message_content,
                "sent_at": now.isoformat(),
                "expires_at": expires_at.isoformat()
            }).execute()

            if not (mail_insert_res and mail_insert_res.data):
                logger.error("메일 레코드 생성 실패. 환불을 시도합니다.")
                refund_tasks = [update_wallet(self.user, self.shipping_fee)]
                for item_name, quantity in self.attachments["items"].items():
                    refund_tasks.append(update_inventory(self.user.id, item_name, quantity))
                await asyncio.gather(*refund_tasks)
                return await interaction.followup.send("우편 발송에 실패했습니다 (메일 생성 오류). 비용이 환불되었습니다.", ephemeral=True)

            new_mail_id = mail_insert_res.data[0]['id']

            if self.attachments["items"]:
                attachments_to_insert = [
                    {
                        "mail_id": new_mail_id,
                        "item_name": name,
                        "quantity": qty,
                        "is_coin": False
                    } for name, qty in self.attachments["items"].items()
                ]
                await supabase.table('mail_attachments').insert(attachments_to_insert).execute()
            
            await interaction.edit_original_response(content="✅ 우편을 성공적으로 보냈습니다.", view=None, embed=None)
            
            try:
                log_channel_id = get_id("trade_log_channel_id")
                if log_channel_id and (log_channel := self.cog.bot.get_channel(log_channel_id)):
                    embed_data = await get_embed_from_db("log_new_mail")
                    if embed_data:
                        log_embed = format_embed_from_db(
                            embed_data, 
                            sender_mention=self.user.mention, 
                            recipient_mention=self.recipient.mention
                        )
                        await log_channel.send(content=self.recipient.mention, embed=log_embed, allowed_mentions=discord.AllowedMentions(users=True))
            except Exception as e:
                logger.error(f"우편 발송 로그 알림 전송 실패: {e}", exc_info=True)
            
            self.stop()

        except Exception as e:
            logger.error(f"우편 발송 중 최종 단계에서 예외 발생: {e}", exc_info=True)
            await interaction.followup.send("우편 발송 중 오류가 발생했습니다. 관리자에게 문의하여 재료가 소모되었는지 확인해주세요.", ephemeral=True)
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
        await interaction.response.defer(ephemeral=True)
        embed = await self.build_embed()
        await self.build_components()
        self.message = await interaction.followup.send(embed=embed, view=self, ephemeral=True)

    async def update_view(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer()

        if not self.message:
            logger.error("MailboxView 업데이트가 요청되었지만 self.message가 설정되지 않았습니다.")
            await interaction.followup.send("오류: UI를 찾을 수 없습니다. 우편함을 다시 열어주세요.", ephemeral=True, delete_after=5)
            return

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
            embed.set_footer(text=f"페이지 {self.page + 1} / {math.ceil(res.count / 5)}")
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
        self.selected_mail_ids = interaction.data['values']
        await self.update_view(interaction)

    async def claim_selected_mails(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        claimed_count = 0
        total_items: Dict[str, int] = {}
        failed_count = 0

        for mail_id_str in self.selected_mail_ids:
            mail_id = int(mail_id_str)
            try:
                res = await supabase.rpc('claim_mail', {'p_mail_id': mail_id, 'p_recipient_id': str(self.user.id)}).execute()
                if hasattr(res, 'data') and res.data and res.data.get('success'):
                    claimed_count += 1
                    for item in res.data.get('items', []):
                        total_items[item['name']] = total_items.get(item['name'], 0) + item['qty']
                else:
                    failed_count += 1
                    if hasattr(res, 'data') and res.data and res.data.get('message'):
                        logger.warning(f"Mail claim failed for mail_id {mail_id}: {res.data['message']}")
            except APIError as e:
                if "JSON could not be generated" in str(e.message):
                    logger.warning(f"Handled known JSON decoding APIError for mail_id {mail_id}. Assuming claim failed.")
                    failed_count += 1
                else:
                    logger.error(f"An unexpected APIError occurred while claiming mail_id {mail_id}: {e}", exc_info=True)
                    failed_count += 1
            except Exception as e:
                logger.error(f"A generic exception occurred while claiming mail_id {mail_id}: {e}", exc_info=True)
                failed_count += 1

        if claimed_count > 0:
            item_summary = "\n".join([f"ㄴ {name}: {qty}개" for name, qty in total_items.items()])
            success_message = f"{claimed_count}개의 우편을 수령했습니다!\n\n**총 받은 아이템:**\n{item_summary or '없음'}"
            if failed_count > 0:
                success_message += f"\n\n(주의: {failed_count}개의 우편 수령에 실패했습니다.)"
            await interaction.followup.send(success_message, ephemeral=True, delete_after=10)
        else:
            await interaction.followup.send("우편 수령에 실패했습니다. 다시 시도해주세요.", ephemeral=True, delete_after=5)
        
        self.selected_mail_ids.clear()
        await self.update_view(interaction)

    async def delete_selected_mails(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        mail_ids_to_delete = [int(mid) for mid in self.selected_mail_ids]
        
        await supabase.table('mails').delete().in_('id', mail_ids_to_delete).eq('recipient_id', str(self.user.id)).execute()
        
        self.selected_mail_ids.clear()
        await self.update_view(interaction)
        
    async def send_mail(self, interaction: discord.Interaction):
        select_view = ui.View(timeout=180)
        user_select = ui.UserSelect(placeholder="편지를 보낼 상대를 선택하세요.")
        async def callback(select_interaction: discord.Interaction):
            recipient_id = int(select_interaction.data['values'][0])
            recipient = interaction.guild.get_member(recipient_id)
            if not recipient or recipient.bot or recipient.id == self.user.id:
                return await select_interaction.response.send_message("잘못된 상대입니다.", ephemeral=True, delete_after=5)
            
            try:
                await interaction.delete_original_response()
            except discord.NotFound:
                pass
            
            if self.message: await self.message.delete()
            compose_view = MailComposeView(self.cog, self.user, recipient)
            await compose_view.start(select_interaction)
        user_select.callback = callback
        select_view.add_item(user_select)
        await interaction.response.send_message("누구에게 편지를 보내시겠습니까?", view=select_view, ephemeral=True)
    
    async def prev_page_callback(self, interaction: discord.Interaction):
        self.page -= 1
        await self.update_view(interaction)

    async def next_page_callback(self, interaction: discord.Interaction):
        self.page += 1
        await self.update_view(interaction)

class TradePanelView(ui.View):
    def __init__(self, cog_instance: 'Trade'):
        super().__init__(timeout=None)
        self.cog = cog_instance
        trade_button = ui.Button(label="1:1 거래하기", style=discord.ButtonStyle.success, emoji="🤝", custom_id="trade_panel_direct_trade")
        trade_button.callback = self.direct_trade_button
        self.add_item(trade_button)
        mailbox_button = ui.Button(label="우편함", style=discord.ButtonStyle.primary, emoji="📫", custom_id="trade_panel_mailbox")
        mailbox_button.callback = self.mailbox_button
        self.add_item(mailbox_button)

    async def direct_trade_button(self, interaction: discord.Interaction):
        initiator = interaction.user
        
        trade_fee = 50
        wallet = await get_wallet(initiator.id)
        if wallet.get('balance', 0) < trade_fee:
            return await interaction.response.send_message(f"❌ 거래를 시작하려면 수수료 {trade_fee}{self.cog.currency_icon}가 필요합니다.", ephemeral=True, delete_after=5)

        view = ui.View(timeout=180)
        user_select = ui.UserSelect(placeholder="거래할 상대를 선택하세요.")
        async def select_callback(select_interaction: discord.Interaction):
            partner_id = int(select_interaction.data['values'][0])
            partner = interaction.guild.get_member(partner_id)
            if not partner or partner.bot or partner.id == initiator.id:
                return await select_interaction.response.send_message("잘못된 상대입니다.", ephemeral=True, delete_after=5)
            trade_id = f"{min(initiator.id, partner.id)}-{max(initiator.id, partner.id)}"
            if trade_id in self.cog.active_trades:
                 return await select_interaction.response.send_message("상대방 또는 본인이 이미 다른 거래에 참여 중입니다.", ephemeral=True, delete_after=5)
            
            await select_interaction.response.send_message(f"거래 수수료 {trade_fee}{self.cog.currency_icon}를 지불하고 거래를 시작합니다.", ephemeral=True, delete_after=5)
            await update_wallet(initiator, -trade_fee)
            
            try:
                await interaction.delete_original_response()
            except discord.NotFound:
                pass
            
            trade_view = TradeView(self.cog, initiator, partner)
            await trade_view.start_in_channel(select_interaction.channel)
        user_select.callback = select_callback
        view.add_item(user_select)
        await interaction.response.send_message("누구와 거래하시겠습니까?", view=view, ephemeral=True)

    async def mailbox_button(self, interaction: discord.Interaction):
        mailbox_view = MailboxView(self.cog, interaction.user)
        await mailbox_view.start(interaction)
        
class Trade(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_trades: Dict[str, TradeView] = {}
        self.currency_icon = "🪙" 

    async def cog_load(self):
        self.bot.loop.create_task(self.cleanup_stale_trades())
        game_config = get_config("GAME_CONFIG", {})
        self.currency_icon = game_config.get("CURRENCY_ICON", "🪙")

    async def cleanup_stale_trades(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(60)
            stale_trades = [ tid for tid, view in self.active_trades.items() if view.is_finished() ]
            for tid in stale_trades:
                if tid in self.active_trades:
                    self.active_trades.pop(tid)

    async def register_persistent_views(self):
        self.bot.add_view(TradePanelView(self))
        logger.info("✅ 거래소의 영구 View가 성공적으로 등록되었습니다.")

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_trade", last_log: Optional[discord.Embed] = None):
        if last_log:
            try: await channel.send(embed=last_log)
            except discord.HTTPException as e: logger.error(f"거래 로그 전송 실패: {e}")
        panel_name = panel_key.replace("panel_", "")
        if panel_info := get_panel_id(panel_name):
            try:
                if old_channel := self.bot.get_channel(panel_info['channel_id']):
                    msg = await old_channel.fetch_message(panel_info['message_id'])
                    await msg.delete()
            except (discord.NotFound, discord.Forbidden): pass
        embed_data = await get_embed_from_db(panel_key)
        if not embed_data:
            return logger.error(f"DB에서 '{panel_key}' 임베드를 찾을 수 없습니다.")
        embed = discord.Embed.from_dict(embed_data)
        view = TradePanelView(self)
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_name, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} 패널을 성공적으로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Trade(bot))
