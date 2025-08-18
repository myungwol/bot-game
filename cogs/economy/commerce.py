# cogs/economy/commerce.py (업그레이드 로직 제거)

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
from typing import Optional, Dict

logger = logging.getLogger(__name__)

from utils.database import (get_inventory, get_wallet, supabase, get_id, get_item_database, get_config, get_string, get_panel_components_from_db)

class QuantityModal(ui.Modal):
    quantity = ui.TextInput(label="数量", placeholder="例: 10", required=True, max_length=5)
    def __init__(self, title: str, max_value: int):
        super().__init__(title=title)
        self.quantity.placeholder = f"最大 {max_value}個まで"
        self.max_value = max_value
        self.value: Optional[int] = None
    async def on_submit(self, i: discord.Interaction):
        try:
            q_val = int(self.quantity.value)
            if not (1 <= q_val <= self.max_value):
                return await i.response.send_message(f"1から{self.max_value}までの数字を入力してください。", ephemeral=True)
            self.value = q_val
            await i.response.defer(ephemeral=True) 
        except ValueError: 
            await i.response.send_message("数字のみ入力してください。", ephemeral=True)
        except Exception: 
            self.stop()

class ShopViewBase(ui.View):
    def __init__(self, user: discord.Member):
        super().__init__(timeout=300)
        self.user = user
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.message: Optional[discord.WebhookMessage] = None
    
    async def handle_error(self, interaction: discord.Interaction, error: Exception, custom_message: str = ""):
        logger.error(f"상점 처리 중 오류 발생: {error}", exc_info=False)
        message = custom_message or "❌ 購入処理中にエラーが発生しました。"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

class BuyItemView(ShopViewBase):
    def __init__(self, user: discord.Member, category: str):
        super().__init__(user)
        self.category = category
    async def build_embed(self) -> discord.Embed:
        wallet = await get_wallet(self.user.id)
        balance = wallet.get('balance', 0)
        return discord.Embed(title=get_string("commerce.item_view_title", category=self.category), description=get_string("commerce.item_view_desc", balance=f"{balance:,}", currency_icon=self.currency_icon), color=discord.Color.blue())
    async def build_components(self):
        self.clear_items()
        item_db = get_item_database()
        items_in_category = sorted(
            [(n, d) for n, d in item_db.items() if d.get('buyable') and d.get('category') == self.category],
            key=lambda item: item[1].get('price', 0)
        )
        if items_in_category:
            options = [discord.SelectOption(label=n, value=n, description=f"{d['price']}{self.currency_icon} - {d.get('description', '')}"[:100], emoji=d.get('emoji')) for n, d in items_in_category]
            select = ui.Select(placeholder=f"「{self.category}」カテゴリの商品を選択", options=options)
            select.callback = self.select_callback
            self.add_item(select)
        back_button = ui.Button(label=get_string("commerce.back_button"), style=discord.ButtonStyle.grey, row=1)
        back_button.callback = self.back_callback
        self.add_item(back_button)
        return self

    async def select_callback(self, interaction: discord.Interaction):
        item_name = interaction.data['values'][0]
        item_data = get_item_database().get(item_name)
        if not item_data: return

        is_modal_needed = item_data.get('max_ownable', 1) > 1

        try:
            if is_modal_needed:
                wallet = await get_wallet(self.user.id)
                balance = wallet.get('balance', 0)
                max_buyable = balance // item_data['price'] if item_data['price'] > 0 else item_data.get('max_ownable', 999)

                if max_buyable == 0:
                    await interaction.response.send_message(get_string("commerce.error_insufficient_funds"), ephemeral=True)
                    return
                
                modal = QuantityModal(f"{item_name} 購入", max_buyable)
                await interaction.response.send_modal(modal)
                await modal.wait()

                if modal.value is None:
                    await interaction.followup.send("購入がキャンセルされました。", ephemeral=True)
                    return

                quantity, total_price = modal.value, item_data['price'] * modal.value
                wallet_after_modal = await get_wallet(self.user.id)
                if wallet_after_modal.get('balance', 0) < total_price:
                     raise ValueError("error_insufficient_funds")

                res = await supabase.rpc('buy_item', {'user_id_param': str(self.user.id), 'item_name_param': item_name, 'quantity_param': quantity, 'total_price_param': total_price}).execute()
                if not res.data: raise Exception("DB RPC call failed for multi-buy item.")
                
                await interaction.followup.send(get_string("commerce.purchase_success", item_name=item_name, quantity=quantity), ephemeral=True)

            else:
                await interaction.response.defer(ephemeral=True)
                wallet, inventory = await asyncio.gather(get_wallet(self.user.id), get_inventory(str(self.user.id)))
                
                # [🔴 핵심 수정] 업그레이드 로직 제거
                if inventory.get(item_name, 0) > 0 and item_data.get('max_ownable', 1) == 1:
                    raise ValueError("error_already_owned")
                
                total_price, quantity = item_data['price'], 1
                if wallet.get('balance', 0) < total_price:
                    raise ValueError("error_insufficient_funds")
                
                res = await supabase.rpc('buy_item', {'user_id_param': str(self.user.id), 'item_name_param': item_name, 'quantity_param': quantity, 'total_price_param': total_price}).execute()
                if not res.data: raise Exception("DB RPC call failed for single-buy item.")
                
                if id_key := item_data.get('id_key'):
                    if role_id := get_id(id_key):
                        if role := interaction.guild.get_role(role_id): await self.user.add_roles(role)
                
                await interaction.followup.send(get_string("commerce.purchase_success", item_name=item_name, quantity=quantity), ephemeral=True)

            embed, view = await self.build_embed(), await self.build_components()
            await self.message.edit(embed=embed, view=view)

        except ValueError as e:
            await self.handle_error(interaction, e, get_string(f"commerce.{e}", default=str(e)))
        except Exception as e:
            await self.handle_error(interaction, e)

    async def back_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        category_view = BuyCategoryView(self.user)
        category_view.message = self.message
        await category_view.build_components()
        await self.message.edit(embed=category_view.build_embed(), view=category_view)

class BuyCategoryView(ShopViewBase):
    def build_embed(self) -> discord.Embed:
        return discord.Embed(title=get_string("commerce.category_view_title"), description=get_string("commerce.category_view_desc"), color=discord.Color.green())
    
    async def build_components(self):
        self.clear_items()
        item_db = get_item_database()
        categories = sorted(list(set(
            d['category'] for d in item_db.values() if d.get('buyable') and d.get('category')
        )))
        
        if not categories:
            self.add_item(ui.Button(label="販売中の商品がありません。", disabled=True))
            return self

        for category_name in categories:
            button = ui.Button(label=category_name, custom_id=f"buy_category_{category_name}")
            button.callback = self.category_callback
            self.add_item(button)
        return self
    
    async def category_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        category = interaction.data['custom_id'].split('buy_category_')[-1]
        item_view = BuyItemView(self.user, category)
        item_view.message = self.message
        embed, view = await item_view.build_embed(), await item_view.build_components()
        await self.message.edit(embed=embed, view=view)

class CommercePanelView(ui.View):
    def __init__(self, cog_instance: 'Commerce'):
        super().__init__(timeout=None)
        self.commerce_cog = cog_instance
    async def setup_buttons(self):
        self.clear_items()
        components_data = await get_panel_components_from_db('commerce')
        for comp in components_data:
            key = comp.get('component_key')
            if comp.get('component_type') == 'button' and key:
                style_str = comp.get('style', 'secondary')
                style = discord.ButtonStyle[style_str] if hasattr(discord.ButtonStyle, style_str) else discord.ButtonStyle.secondary
                button = ui.Button(label=comp.get('label'), style=style, emoji=comp.get('emoji'), custom_id=key)
                if key == 'open_shop': button.callback = self.open_shop
                elif key == 'open_market': button.callback = self.open_market
                self.add_item(button)
    async def open_shop(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        view = BuyCategoryView(interaction.user)
        embed, view = view.build_embed(), await view.build_components()
        message = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = message
    async def open_market(self, interaction: discord.Interaction):
        await interaction.response.send_message("販売機能は現在準備中です。", ephemeral=True)

class Commerce(commands.Cog):
    def __init__(self, bot: commands.Cog):
        self.bot = bot
    async def register_persistent_views(self):
        view = CommercePanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)
    async def regenerate_panel(self, channel: discord.TextChannel): pass

async def setup(bot: commands.Cog):
    await bot.add_cog(Commerce(bot))
