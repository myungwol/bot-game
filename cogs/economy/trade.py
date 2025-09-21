# cogs/economy/trade.py

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
import math
import time
from typing import Optional, Dict, List, Any
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
            await self.parent_view.add_attachment(interaction, self.item_name, qty)
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
        await self.parent_view.refresh(interaction)

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
        await interaction.followup.send("첨부할 아이템을 선택하세요.", view=self, ephemeral=True)

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
            self.add_item(ui.Button(label="첨부 가능한 아이템이 없습니다.", disabled=True))
            return
        options = [discord.SelectOption(label=f"{name} ({qty}개)", value=name) for name, qty in tradeable_items.items()]
        item_select = ui.Select(placeholder="아이템 선택...", options=options[:25])
        item_select.callback = self.on_item_select
        self.add_item(item_select)

    async def on_item_select(self, interaction: discord.Interaction):
        item_name = interaction.data['values'][0]
        inventory = await get_inventory(self.user)
        max_qty = inventory.get(item_name, 0)
        modal = MailItemSelectModal(f"'{item_name}' 수량 입력", max_qty, item_name, self.parent_view)
        await interaction.response.send_modal(modal)
        try:
            await interaction.delete_original_response()
        except (discord.NotFound, discord.HTTPException):
            pass

class MailComposeView(ui.View):
    def __init__(self, cog: 'Trade', user: discord.Member, recipient: discord.Member, original_interaction: discord.Interaction):
        super().__init__(timeout=300)
        self.cog = cog; self.user = user; self.recipient = recipient
        self.original_interaction = original_interaction
        self.message_content = ""; self.attachments = {"items": {}}
        self.currency_icon = get_config("CURRENCY_ICON", "🪙"); self.shipping_fee = 100
        self.message: Optional[discord.WebhookMessage] = None
        
    async def start(self):
        embed = await self.build_embed()
        await self.build_components()
        self.message = await self.original_interaction.followup.send(embed=embed, view=self, ephemeral=True)

    async def refresh(self, interaction: Optional[discord.Interaction] = None):
        if interaction and not interaction.response.is_done():
            await interaction.response.defer()

        embed = await self.build_embed()
        await self.build_components()
        
        if self.message:
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
        
        self.add_item(ui.Button(label="아이템 첨부", style=discord.ButtonStyle.secondary, emoji="📦", custom_id="attach_item", row=0))
        remove_disabled = not self.attachments.get("items")
        self.add_item(ui.Button(label="아이템 제거", style=discord.ButtonStyle.secondary, emoji="🗑️", custom_id="remove_item", row=0, disabled=remove_disabled))
        self.add_item(ui.Button(label="메시지 작성/수정", style=discord.ButtonStyle.secondary, emoji="✍️", custom_id="write_message", row=0))
        
        send_disabled = not (self.attachments.get("items") or self.message_content)
        self.add_item(ui.Button(label="보내기", style=discord.ButtonStyle.success, emoji="🚀", custom_id="send_mail", row=1, disabled=send_disabled))

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

    async def handle_send(self, interaction: discord.Interaction):
        try:
            if not self.attachments.get("items") and not self.message_content:
                msg = await interaction.followup.send("❌ 아이템이나 메시지 중 하나는 반드시 포함되어야 합니다.", ephemeral=True)
                return await delete_after(msg, 5)

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
    # ... (기존 MailboxView 코드는 대부분 유지) ...
    async def send_mail(self, interaction: discord.Interaction):
        # ▼▼▼ [핵심 수정] 이 메서드를 아래 코드로 교체합니다. ▼▼▼
        view = ui.View(timeout=180)
        user_select = ui.UserSelect(placeholder="편지를 보낼 상대를 선택하세요.")
        
        async def callback(select_interaction: discord.Interaction):
            await select_interaction.response.defer(ephemeral=True)
            
            recipient_id = int(select_interaction.data['values'][0])
            recipient = interaction.guild.get_member(recipient_id)
            if not recipient or recipient.bot or recipient.id == self.user.id:
                await select_interaction.followup.send("잘못된 상대입니다.", ephemeral=True, delete_after=5)
                return
            
            compose_view = MailComposeView(self.cog, self.user, recipient, select_interaction)
            await compose_view.start()

            try:
                await interaction.delete_original_response()
            except discord.NotFound:
                pass

        user_select.callback = callback
        view.add_item(user_select)
        
        # 기존 메시지를 수정하는 대신 새로운 메시지를 보냅니다.
        await interaction.followup.send("누구에게 편지를 보내시겠습니까?", view=view, ephemeral=True)
    
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
        trade_button.callback = self.dispatch_callback
        self.add_item(trade_button)
        mailbox_button = ui.Button(label="우편함", style=discord.ButtonStyle.primary, emoji="📫", custom_id="trade_panel_mailbox")
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
                await interaction.edit_original_response(content="거래 상대 선택이 완료되었습니다.", view=None)

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
