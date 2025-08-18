# cogs/economy/commerce.py (오류 수정 최종본)

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
        except ValueError: await i.response.send_message("数字のみ入力してください。", ephemeral=True)
        except Exception: self.stop()

class ShopViewBase(ui.View):
    def __init__(self, user: discord.Member):
        super().__init__(timeout=300)
        self.user = user
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.message: Optional[discord.WebhookMessage] = None
    async def handle_error(self, interaction: discord.Interaction, error: Exception, custom_message: str = ""):
        logger.error(f"상점 처리 중 오류 발생: {error}", exc_info=False)
        message = custom_message or "❌ 購入処理中にエラーが発生しました。"
        if interaction.response.is_done(): await interaction.followup.send(message, ephemeral=True)
        else: await interaction.response.send_message(message, ephemeral=True)

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
        items_in_category = [(n, d) for n, d in item_db.items() if d.get('buyable') and d.get('category') == self.category]
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
        is_modal_needed = item_data and item_data.get('max_ownable', 999) > 1
        if not is_modal_needed: await interaction.response.defer(ephemeral=True)
        try:
            wallet, inventory = await asyncio.gather(get_wallet(self.user.id), get_inventory(str(self.user.id)))
            balance = wallet.get('balance', 0)
            if item_data.get('is_upgrade_item'):
                hierarchy = get_config("ROD_HIERARCHY", [])
                current_rod, current_rank = next(((r, i) for i, r in enumerate(hierarchy) if inventory.get(r, 0) > 0), (None, -1))
                target_rank = hierarchy.index(item_name)
                if target_rank <= current_rank: raise ValueError("error_already_have_better")
                if target_rank > 0 and hierarchy[target_rank - 1] != current_rod: raise ValueError("error_upgrade_needed")
                sell_price = 100 if current_rod and "古い" not in current_rod else 0
                params = {'p_user_id': str(self.user.id), 'p_new_rod_name': item_name, 'p_old_rod_name': current_rod, 'p_price': item_data['price'], 'p_sell_value': sell_price}
                res = await supabase.rpc('upgrade_rod_and_sell_old', params).execute()
                if not res.data or not res.data[0].get('success'): raise ValueError("error_insufficient_funds")
                await interaction.followup.send(get_string("commerce.upgrade_success", new_item=item_name, old_item=current_rod, sell_price=sell_price, currency_icon=self.currency_icon), ephemeral=True)
            elif is_modal_needed:
                max_buyable = balance // item_data['price'] if item_data['price'] > 0 else 999
                if max_buyable == 0: raise ValueError("error_insufficient_funds")
                modal = QuantityModal(f"{item_name} 購入", max_buyable)
                await interaction.response.send_modal(modal)
                await modal.wait()
                if not modal.value: return await interaction.followup.send("購入がキャンセルされました。", ephemeral=True)
                quantity, total_price = modal.value, item_data['price'] * modal.value
                if balance < total_price: raise ValueError("error_insufficient_funds")
                res = await supabase.rpc('buy_item', {'user_id_param': str(self.user.id), 'item_name_param': item_name, 'quantity_param': quantity, 'total_price_param': total_price}).execute()
                if not res.data: raise Exception()
                await interaction.followup.send(get_string("commerce.purchase_success", item_name=item_name, quantity=quantity), ephemeral=True)
            else:
                if inventory.get(item_name, 0) > 0: raise ValueError("error_already_owned")
                total_price, quantity = item_data['price'], 1
                if balance < total_price: raise ValueError("error_insufficient_funds")
                res = await supabase.rpc('buy_item', {'user_id_param': str(self.user.id), 'item_name_param': item_name, 'quantity_param': quantity, 'total_price_param': total_price}).execute()
                if not res.data: raise Exception()
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
        categories = get_string("commerce.categories", {})
        for key, label in categories.items():
            button = ui.Button(label=label, custom_id=f"buy_category_{key}", disabled="準備中" in label)
            button.callback = self.category_callback
            self.add_item(button)
        return self
    async def category_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        category = interaction.data['custom_id'].split('_')[-1]
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
