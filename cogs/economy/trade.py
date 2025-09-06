# cogs/economy/trade.py

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone, timedelta

from utils.database import (
    get_inventory, get_wallet, get_item_database, get_config, supabase,
    save_panel_id, get_panel_id, get_embed_from_db, update_wallet, update_inventory
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

TRADEABLE_CATEGORIES = ["농장_작물", "농장_씨앗", "광물", "미끼", "아이템"]

class ItemSelectModal(ui.Modal, title="아이템 수량 입력"):
    quantity_input = ui.TextInput(label="수량", placeholder="거래에 올릴 수량을 입력하세요.", required=True)

    def __init__(self, max_quantity: int):
        super().__init__()
        self.max_quantity = max_quantity
        self.quantity: Optional[int] = None

    async def on_submit(self, interaction: discord.Interaction):
        try:
            qty = int(self.quantity_input.value)
            if not 1 <= qty <= self.max_quantity:
                raise ValueError
            self.quantity = qty
            await interaction.response.defer()
            self.stop()
        except ValueError:
            await interaction.response.send_message(f"1에서 {self.max_quantity} 사이의 숫자만 입력해주세요.", ephemeral=True, delete_after=5)
            self.stop()

class CoinInputModal(ui.Modal, title="코인 입력"):
    coin_input = ui.TextInput(label="코인", placeholder="거래에 올릴 코인을 입력하세요.", required=True)

    def __init__(self, max_coins: int):
        super().__init__()
        self.max_coins = max_coins
        self.coins: Optional[int] = None

    async def on_submit(self, interaction: discord.Interaction):
        try:
            coins = int(self.coin_input.value)
            if not 0 <= coins <= self.max_coins:
                raise ValueError
            self.coins = coins
            await interaction.response.defer()
            self.stop()
        except ValueError:
            await interaction.response.send_message(f"0에서 {self.max_coins:,} 사이의 숫자만 입력해주세요.", ephemeral=True, delete_after=5)
            self.stop()

class TradeView(ui.View):
    def __init__(self, cog: 'Trade', initiator: discord.Member, partner: discord.Member):
        super().__init__(timeout=300)
        self.cog = cog
        self.initiator = initiator
        self.partner = partner
        self.trade_id = f"{initiator.id}-{partner.id}"
        self.offers = {
            initiator.id: {"items": {}, "coins": 0, "ready": False},
            partner.id: {"items": {}, "coins": 0, "ready": False}
        }
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.commission_percent = get_config("TRADE_COMMISSION_PERCENT", {}).get("value", 5)

    async def start(self, interaction: discord.Interaction):
        self.cog.active_trades[self.trade_id] = self
        embed = await self.build_embed()
        await interaction.response.send_message(f"{self.partner.mention}, {self.initiator.mention}님이 1:1 거래를 신청했습니다.", embed=embed, view=self)
        self.message = await interaction.original_response()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in [self.initiator.id, self.partner.id]:
            await interaction.response.send_message("거래 당사자만 이용할 수 있습니다.", ephemeral=True, delete_after=5)
            return False
        if self.offers[self.initiator.id]["ready"] and self.offers[self.partner.id]["ready"]:
            # 둘 다 준비 완료 상태면 거래 완료 전까지 상호작용 비활성화
            return False
        return True
        
    async def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🤝 1:1 거래", color=0x3498DB)
        
        for user in [self.initiator, self.partner]:
            offer = self.offers[user.id]
            field_value = []
            if offer["items"]:
                field_value.extend([f"ㄴ {name}: {qty}개" for name, qty in offer["items"].items()])
            if offer["coins"] > 0:
                field_value.append(f"💰 {offer['coins']:,}{self.currency_icon}")
            
            status = "✅ 준비 완료" if offer["ready"] else "⏳ 준비 중"
            embed.add_field(
                name=f"{user.display_name}의 제안 ({status})",
                value="\n".join(field_value) if field_value else "제안 없음",
                inline=True
            )
        
        commission = int((self.offers[self.initiator.id]['coins'] + self.offers[self.partner.id]['coins']) * (self.commission_percent / 100))
        embed.set_footer(text=f"거래세 ({self.commission_percent}%): {commission:,}{self.currency_icon} | 5분 후 만료")
        return embed

    async def update_ui(self, interaction: discord.Interaction):
        if self.is_finished(): return
        embed = await self.build_embed()
        await interaction.edit_original_response(embed=embed, view=self)

    @ui.button(label="아이템 추가", style=discord.ButtonStyle.secondary, emoji="📦")
    async def add_item_button(self, interaction: discord.Interaction, button: ui.Button):
        user_id = interaction.user.id
        if self.offers[user_id]["ready"]:
            return await interaction.response.send_message("준비 완료 상태에서는 제안을 변경할 수 없습니다.", ephemeral=True, delete_after=5)

        inventory = await get_inventory(interaction.user)
        item_db = get_item_database()
        
        tradeable_items = {
            name: qty for name, qty in inventory.items()
            if item_db.get(name, {}).get('category') in TRADEABLE_CATEGORIES
        }

        if not tradeable_items:
            return await interaction.response.send_message("거래 가능한 아이템이 없습니다.", ephemeral=True, delete_after=5)

        options = [
            discord.SelectOption(label=f"{name} ({qty}개)", value=name)
            for name, qty in tradeable_items.items()
        ]
        
        select_view = ui.View(timeout=180)
        item_select = ui.Select(placeholder="추가할 아이템을 선택하세요", options=options[:25])
        
        async def select_callback(select_interaction: discord.Interaction):
            item_name = select_interaction.data['values'][0]
            max_qty = tradeable_items.get(item_name, 0)

            modal = ItemSelectModal(max_qty)
            await select_interaction.response.send_modal(modal)
            await modal.wait()

            if modal.quantity is not None:
                self.offers[user_id]["items"][item_name] = self.offers[user_id]["items"].get(item_name, 0) + modal.quantity
                await self.update_ui(interaction)
                await select_interaction.delete_original_response()

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
        
        modal = CoinInputModal(max_coins)
        await interaction.response.send_modal(modal)
        await modal.wait()

        if modal.coins is not None:
            self.offers[user_id]["coins"] = modal.coins
            await self.update_ui(interaction)

    @ui.button(label="준비/확정", style=discord.ButtonStyle.success, emoji="✅")
    async def ready_button(self, interaction: discord.Interaction, button: ui.Button):
        user_id = interaction.user.id
        self.offers[user_id]["ready"] = not self.offers[user_id]["ready"]
        
        if self.offers[self.initiator.id]["ready"] and self.offers[self.partner.id]["ready"]:
            await self.process_trade(interaction)
        else:
            await self.update_ui(interaction)

    @ui.button(label="취소", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.on_timeout()
        if self.message:
            await self.message.channel.send(f"{interaction.user.mention}님이 거래를 취소했습니다.", delete_after=10)

    async def process_trade(self, interaction: discord.Interaction):
        for item in self.children: item.disabled = True
        await interaction.response.edit_message(content="**거래 확정! 처리 중...**", view=self)
        
        user1, user2 = self.initiator, self.partner
        offer1, offer2 = self.offers[user1.id], self.offers[user2.id]
        
        commission = int((offer1['coins'] + offer2['coins']) * (self.commission_percent / 100))
        
        # 유효성 재검사
        wallet1, inv1 = await asyncio.gather(get_wallet(user1.id), get_inventory(user1))
        wallet2, inv2 = await asyncio.gather(get_wallet(user2.id), get_inventory(user2))

        if wallet1.get('balance', 0) < offer1['coins'] + commission:
            return await self.fail_trade(interaction, f"{user1.mention}님의 코인이 부족합니다.")
        for name, qty in offer1['items'].items():
            if inv1.get(name, 0) < qty:
                return await self.fail_trade(interaction, f"{user1.mention}님의 '{name}' 아이템이 부족합니다.")
        
        if wallet2.get('balance', 0) < offer2['coins']:
            return await self.fail_trade(interaction, f"{user2.mention}님의 코인이 부족합니다.")
        for name, qty in offer2['items'].items():
            if inv2.get(name, 0) < qty:
                return await self.fail_trade(interaction, f"{user2.mention}님의 '{name}' 아이템이 부족합니다.")

        # DB 함수 호출
        p_offer1 = {"items": [{"name": k, "qty": v} for k, v in offer1['items'].items()], "coins": offer1['coins']}
        p_offer2 = {"items": [{"name": k, "qty": v} for k, v in offer2['items'].items()], "coins": offer2['coins']}
        
        res = await supabase.rpc('process_trade', {
            'p_user1_id': str(user1.id), 'p_user2_id': str(user2.id),
            'p_user1_offer': p_offer1, 'p_user2_offer': p_offer2,
            'p_commission_fee': commission
        }).execute()
        
        if not (res.data and res.data is True):
             return await self.fail_trade(interaction, "데이터베이스 처리 중 오류가 발생했습니다.")
        
        await self.message.delete()
        
        # 거래 로그 생성
        log_embed_data = await get_embed_from_db("log_trade_success")
        if log_embed_data:
            log_embed = format_embed_from_db(
                log_embed_data,
                user1_mention=user1.mention,
                user2_mention=user2.mention,
                commission=commission,
                currency_icon=self.currency_icon
            )
            # Add fields for each user's offer
            offer1_str = "\n".join([f"ㄴ {n}: {q}개" for n, q in offer1['items'].items()] + [f"💰 {offer1['coins']:,}{self.currency_icon}"]) or "없음"
            offer2_str = "\n".join([f"ㄴ {n}: {q}개" for n, q in offer2['items'].items()] + [f"💰 {offer2['coins']:,}{self.currency_icon}"]) or "없음"
            log_embed.add_field(name=f"{user1.display_name} 제공", value=offer1_str, inline=True)
            log_embed.add_field(name=f"{user2.display_name} 제공", value=offer2_str, inline=True)

            await self.cog.regenerate_panel(interaction.channel, last_log=log_embed)
        
        self.stop()
    
    async def fail_trade(self, interaction: discord.Interaction, reason: str):
        await self.message.delete()
        await interaction.channel.send(f"交易失敗: {reason}", delete_after=10)
        self.stop()

    async def on_timeout(self):
        self.stop()
        if self.message:
            try:
                await self.message.edit(content="시간이 초과되어 거래가 취소되었습니다.", view=None, embed=None)
                await asyncio.sleep(10)
                await self.message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
    
    def stop(self):
        self.cog.active_trades.pop(self.trade_id, None)
        super().stop()


class MailboxView(ui.View):
    def __init__(self, cog: 'Trade', user: discord.Member):
        super().__init__(timeout=180)
        self.cog = cog
        self.user = user
        self.page = 0

    async def start(self, interaction: discord.Interaction):
        await self.update_view(interaction, new_message=True)

    async def update_view(self, interaction: discord.Interaction, new_message=False):
        embed = await self.build_embed()
        if new_message:
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)
            self.message = await interaction.original_response()
        else:
            await interaction.response.edit_message(embed=embed, view=self)
    
    async def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"{self.user.display_name}의 우편함", color=0x964B00)
        res = await supabase.table('mails').select('*, mail_attachments(*)', count='exact').eq('recipient_id', str(self.user.id)).order('sent_at', desc=True).range(self.page * 5, self.page * 5 + 4).execute()
        
        if not res.data:
            embed.description = "받은 편지가 없습니다."
            return embed
            
        embed.set_footer(text=f"페이지 {self.page + 1} / {((res.count - 1) // 5) + 1}")
        
        for mail in res.data:
            sender = await self.cog.bot.fetch_user(int(mail['sender_id']))
            
            attachments = mail['mail_attachments']
            att_str = []
            for att in attachments:
                if att['is_coin']:
                    att_str.append(f"💰 {att['quantity']:,}{get_config('CURRENCY_ICON', '🪙')}")
                else:
                    att_str.append(f"📦 {att['item_name']}: {att['quantity']}개")

            embed.add_field(
                name=f"FROM: {sender.display_name} ({discord.utils.format_dt(datetime.fromisoformat(mail['sent_at']), 'R')})",
                value=f"**첨부파일:**\n" + "\n".join(att_str) if att_str else "첨부파일 없음",
                inline=False
            )
        return embed
    
    @ui.button(label="편지 보내기", style=discord.ButtonStyle.success, emoji="✉️")
    async def send_mail_button(self, interaction: discord.Interaction, button: ui.Button):
        # Implement send mail logic here
        pass # To be implemented

    @ui.button(label="받기/삭제", style=discord.ButtonStyle.primary, emoji="📬")
    async def claim_delete_button(self, interaction: discord.Interaction, button: ui.Button):
        # Implement claim/delete logic here
        pass # To be implemented

    @ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: ui.Button):
        if self.page > 0:
            self.page -= 1
            await self.update_view(interaction)

    @ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        res = await supabase.table('mails').select('id', count='exact').eq('recipient_id', str(self.user.id)).execute()
        if (self.page + 1) * 5 < res.count:
            self.page += 1
            await self.update_view(interaction)

class TradePanelView(ui.View):
    def __init__(self, cog_instance: 'Trade'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    @ui.button(label="1:1 거래하기", style=discord.ButtonStyle.success, emoji="🤝")
    async def direct_trade_button(self, interaction: discord.Interaction, button: ui.Button):
        view = ui.View(timeout=180)
        user_select = ui.UserSelect(placeholder="거래할 상대를 선택하세요.")

        async def select_callback(select_interaction: discord.Interaction):
            partner_id = int(select_interaction.data['values'][0])
            partner = interaction.guild.get_member(partner_id)
            initiator = interaction.user

            if not partner or partner.bot or partner.id == initiator.id:
                return await select_interaction.response.send_message("잘못된 상대입니다.", ephemeral=True, delete_after=5)
            
            trade_id = f"{min(initiator.id, partner.id)}-{max(initiator.id, partner.id)}"
            if trade_id in self.cog.active_trades:
                 return await select_interaction.response.send_message("상대방 또는 본인이 이미 다른 거래에 참여 중입니다.", ephemeral=True, delete_after=5)

            await select_interaction.message.delete()
            trade_view = TradeView(self.cog, initiator, partner)
            await trade_view.start(select_interaction)

        user_select.callback = select_callback
        view.add_item(user_select)
        await interaction.response.send_message("누구와 거래하시겠습니까?", view=view, ephemeral=True)

    @ui.button(label="우편함", style=discord.ButtonStyle.primary, emoji="📫")
    async def mailbox_button(self, interaction: discord.Interaction, button: ui.Button):
        mailbox_view = MailboxView(self.cog, interaction.user)
        await mailbox_view.start(interaction)
        
class Trade(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_trades: Dict[str, TradeView] = {}
        self.user_locks: Dict[int, asyncio.Lock] = {}

    async def cog_load(self):
        self.bot.loop.create_task(self.cleanup_stale_trades())

    async def cleanup_stale_trades(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(60)
            stale_trades = [
                tid for tid, view in self.active_trades.items()
                if view.is_finished()
            ]
            for tid in stale_trades:
                self.active_trades.pop(tid, None)

    async def register_persistent_views(self):
        self.bot.add_view(TradePanelView(self))
        logger.info("✅ 거래소의 영구 View가 성공적으로 등록되었습니다.")

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_trade", last_log: Optional[discord.Embed] = None):
        if last_log:
            try:
                await channel.send(embed=last_log)
            except discord.HTTPException as e:
                logger.error(f"거래 로그 전송 실패: {e}")

        panel_name = panel_key.replace("panel_", "")
        if panel_info := get_panel_id(panel_name):
            try:
                if old_channel := self.bot.get_channel(panel_info['channel_id']):
                    msg = await old_channel.fetch_message(panel_info['message_id'])
                    await msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
        
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
