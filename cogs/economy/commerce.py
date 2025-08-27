# cogs/economy/commerce.py

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
import math
import time
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)

from utils.database import (
    get_inventory, get_wallet, supabase, get_id, get_item_database,
    get_config,
    get_aquarium, get_fishing_loot, sell_fish_from_db,
    save_panel_id, get_panel_id, get_embed_from_db,
    update_inventory, update_wallet, get_farm_data, expand_farm_db
)
from utils.helpers import format_embed_from_db

async def delete_after(message: discord.WebhookMessage, delay: int):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden):
        pass

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
                await i.response.send_message(f"1から{self.max_value}までの数字を入力してください。", ephemeral=True, delete_after=5)
                return
            self.value = q_val
            await i.response.defer(ephemeral=True)
        except ValueError:
            await i.response.send_message("数字のみ入力してください。", ephemeral=True, delete_after=5)
        except Exception:
            self.stop()

class ShopViewBase(ui.View):
    def __init__(self, user: discord.Member):
        super().__init__(timeout=300)
        self.user = user
        self.currency_icon = get_config("GAME_CONFIG", {}).get("CURRENCY_ICON", "🪙")
        self.message: Optional[discord.WebhookMessage] = None

    async def update_view(self, interaction: discord.Interaction):
        embed = await self.build_embed()
        await self.build_components()
        await interaction.edit_original_response(embed=embed, view=self)

    async def build_embed(self) -> discord.Embed:
        raise NotImplementedError("build_embed must be implemented in subclasses")

    async def build_components(self):
        raise NotImplementedError("build_components must be implemented in subclasses")

    async def handle_error(self, interaction: discord.Interaction, error: Exception, custom_message: str = ""):
        logger.error(f"상점 처리 중 오류 발생: {error}", exc_info=True)
        message_content = custom_message or "❌ 処理中にエラーが発生しました。"
        if interaction.response.is_done():
            msg = await interaction.followup.send(message_content, ephemeral=True)
            asyncio.create_task(delete_after(msg, 5))
        else:
            await interaction.response.send_message(message_content, ephemeral=True, delete_after=5)

class BuyItemView(ShopViewBase):
    def __init__(self, user: discord.Member, category: str):
        super().__init__(user)
        self.category = category
        self.items_in_category = []
        self.page_index = 0
        self.items_per_page = 20

    async def _filter_items_for_user(self):
        all_items_in_category = sorted(
            [(n, d) for n, d in get_item_database().items() if d.get('buyable') and d.get('category', '').strip() == self.category],
            key=lambda item: item[1].get('price', 0)
        )
        self.items_in_category = all_items_in_category

    async def build_embed(self) -> discord.Embed:
        wallet = await get_wallet(self.user.id)
        balance = wallet.get('balance', 0)
        
        all_ui_strings = get_config("strings", {})
        commerce_strings = all_ui_strings.get("commerce", {})
        
        category_display_names = { "アイテム": "雑貨屋", "装備": "武具屋", "エサ": "エサ屋", "農場_種": "種屋" }
        display_name = category_display_names.get(self.category, self.category)
        
        description_template = commerce_strings.get("item_view_desc", "現在の所持金: `{balance}`{currency_icon}\n購入したい商品を選択してください。")

        embed = discord.Embed(
            title=f"🏪 Dico森商店 - {display_name}",
            description=description_template.format(balance=f"{balance:,}", currency_icon=self.currency_icon),
            color=discord.Color.blue()
        )
        
        await self._filter_items_for_user()

        if not self.items_in_category:
            wip_message = commerce_strings.get("wip_category", "このカテゴリーの商品は現在準備中です。")
            embed.add_field(name="準備中", value=wip_message)
        else:
            start_index = self.page_index * self.items_per_page
            end_index = start_index + self.items_per_page
            items_on_page = self.items_in_category[start_index:end_index]

            for name, data in items_on_page:
                field_name = f"{data.get('emoji', '📦')} {name}"
                field_value = (
                    f"**価格:** `{data.get('current_price', data.get('price', 0)):,}`{self.currency_icon}\n"
                    f"> {data.get('description', '説明がありません。')}"
                )
                embed.add_field(name=field_name, value=field_value, inline=False)
            
            total_pages = math.ceil(len(self.items_in_category) / self.items_per_page)
            if total_pages > 1:
                embed.set_footer(text=f"ページ {self.page_index + 1} / {total_pages}")

        return embed

    async def build_components(self):
        self.clear_items()
        
        start_index = self.page_index * self.items_per_page
        end_index = start_index + self.items_per_page
        items_on_page = self.items_in_category[start_index:end_index]

        if items_on_page:
            options = [
                discord.SelectOption(
                    label=name, value=name,
                    description=f"価格: {data.get('current_price', data.get('price', 0)):,}{self.currency_icon}",
                    emoji=data.get('emoji')
                ) for name, data in items_on_page
            ]
            select = ui.Select(placeholder=f"購入したい「{self.category}」の商品を選択...", options=options)
            select.callback = self.select_callback
            self.add_item(select)
        
        total_pages = math.ceil(len(self.items_in_category) / self.items_per_page)
        if total_pages > 1:
            prev_button = ui.Button(label="◀ 前へ", custom_id="prev_page", disabled=(self.page_index == 0), row=2)
            prev_button.callback = self.pagination_callback
            self.add_item(prev_button)
            
            next_button = ui.Button(label="次へ ▶", custom_id="next_page", disabled=(self.page_index >= total_pages - 1), row=2)
            next_button.callback = self.pagination_callback
            self.add_item(next_button)

        back_button = ui.Button(label="カテゴリー選択に戻る", style=discord.ButtonStyle.grey, row=3)
        back_button.callback = self.back_callback
        self.add_item(back_button)

    async def pagination_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if interaction.data['custom_id'] == 'next_page':
            self.page_index += 1
        else:
            self.page_index -= 1
        await self.update_view(interaction)

    async def select_callback(self, interaction: discord.Interaction):
        item_name = interaction.data['values'][0]
        item_data = get_item_database().get(item_name)
        if not item_data: return

        try:
            if item_data.get('instant_use'):
                await self.handle_instant_use_item(interaction, item_name, item_data)
            elif item_data.get('max_ownable', 1) > 1:
                await self.handle_quantity_purchase(interaction, item_name, item_data)
            else:
                await self.handle_single_purchase(interaction, item_name, item_data)
            
            await self.update_view(interaction)

        except Exception as e:
            await self.handle_error(interaction, e, str(e))

    async def handle_instant_use_item(self, interaction: discord.Interaction, item_name: str, item_data: Dict):
        await interaction.response.defer(ephemeral=True)
        price = item_data.get('current_price', item_data.get('price', 0))
        wallet = await get_wallet(self.user.id)
        if wallet.get('balance', 0) < price:
            msg = await interaction.followup.send("❌ 残高が不足しています。", ephemeral=True)
            asyncio.create_task(delete_after(msg, 5))
            return

        if item_data.get('effect_type') == 'expand_farm':
            farm_data = await get_farm_data(self.user.id)
            
            if not farm_data:
                msg = await interaction.followup.send("❌ 農場をまず作成してください。", ephemeral=True)
                asyncio.create_task(delete_after(msg, 5))
                return

            farm_id = farm_data['id']
            current_plots = len(farm_data.get('farm_plots', []))

            if current_plots >= 25:
                msg = await interaction.followup.send("❌ 農場はすでに最大サイズ(25マス)です。", ephemeral=True)
                asyncio.create_task(delete_after(msg, 5))
                return

            await update_wallet(self.user, -price)
            success = await expand_farm_db(farm_id, current_plots)

            if success:
                # [✅✅✅ 핵심 수정] self.user.bot 대신 interaction.client 사용
                farm_cog = interaction.client.get_cog("Farm")
                if farm_cog:
                    thread_id = farm_data.get('thread_id')
                    if thread_id and (thread := interaction.client.get_channel(thread_id)):
                        updated_farm_data = await get_farm_data(self.user.id)
                        if updated_farm_data:
                            await farm_cog.update_farm_ui(thread, self.user, updated_farm_data)
                
                msg = await interaction.followup.send(f"✅ 農場が1マス拡張されました！ (現在の広さ: {current_plots + 1}/25)", ephemeral=True)
                asyncio.create_task(delete_after(msg, 10))
            else:
                await update_wallet(self.user, price)
                msg = await interaction.followup.send("❌ 農場の拡張中にエラーが発生しました。", ephemeral=True)
                asyncio.create_task(delete_after(msg, 5))
        else:
            msg = await interaction.followup.send("❓ 未知の即時使用アイテムです。", ephemeral=True)
            asyncio.create_task(delete_after(msg, 5))

    async def handle_quantity_purchase(self, interaction: discord.Interaction, item_name: str, item_data: Dict):
        wallet = await get_wallet(self.user.id)
        balance = wallet.get('balance', 0)
        price = item_data.get('current_price', item_data.get('price', 0))
        max_buyable = balance // price if price > 0 else item_data.get('max_ownable', 999)

        if max_buyable == 0:
            await interaction.response.send_message("❌ 残高が不足しています。", ephemeral=True, delete_after=5)
            return

        modal = QuantityModal(f"{item_name} 購入", max_buyable)
        await interaction.response.send_modal(modal)
        await modal.wait()

        if modal.value is None:
            if interaction.response.is_done():
                msg = await interaction.followup.send("購入がキャンセルされました。", ephemeral=True)
                asyncio.create_task(delete_after(msg, 5))
            return

        quantity, total_price = modal.value, price * modal.value
        wallet_after_modal = await get_wallet(self.user.id)
        if wallet_after_modal.get('balance', 0) < total_price:
            msg = await interaction.followup.send("❌ 残高が不足しています。", ephemeral=True)
            asyncio.create_task(delete_after(msg, 5))
            return

        await update_inventory(str(self.user.id), item_name, quantity)
        await update_wallet(self.user, -total_price)

        new_wallet = await get_wallet(self.user.id)
        new_balance = new_wallet.get('balance', 0)
        success_message = f"✅ **{item_name}** {quantity}個を`{total_price:,}`{self.currency_icon}で購入しました。\n(残高: `{new_balance:,}`{self.currency_icon})"
        
        msg = await interaction.followup.send(success_message, ephemeral=True)
        asyncio.create_task(delete_after(msg, 10))

    async def handle_single_purchase(self, interaction: discord.Interaction, item_name: str, item_data: Dict):
        await interaction.response.defer(ephemeral=True)
        wallet, inventory = await asyncio.gather(get_wallet(self.user.id), get_inventory(self.user))

        if inventory.get(item_name, 0) > 0 and item_data.get('max_ownable', 1) == 1:
            error_message = f"❌ 「{item_name}」は既に所持しています。1つしか持てません。"
            msg = await interaction.followup.send(error_message, ephemeral=True)
            asyncio.create_task(delete_after(msg, 5))
            return

        total_price = item_data.get('current_price', item_data.get('price', 0))
        if wallet.get('balance', 0) < total_price:
            msg = await interaction.followup.send("❌ 残高が不足しています。", ephemeral=True)
            asyncio.create_task(delete_after(msg, 5))
            return

        await update_inventory(str(self.user.id), item_name, 1)
        await update_wallet(self.user, -total_price)

        if id_key := item_data.get('id_key'):
            if role_id := get_id(id_key):
                if role := interaction.guild.get_role(role_id): await self.user.add_roles(role)

        new_wallet = await get_wallet(self.user.id)
        new_balance = new_wallet.get('balance', 0)
        success_message = f"✅ **{item_name}**を`{total_price:,}`{self.currency_icon}で購入しました。\n(残高: `{new_balance:,}`{self.currency_icon})"
        
        msg = await interaction.followup.send(success_message, ephemeral=True)
        asyncio.create_task(delete_after(msg, 10))

    async def back_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        category_view = BuyCategoryView(self.user)
        category_view.message = self.message
        await category_view.update_view(interaction)

class BuyCategoryView(ShopViewBase):
    async def build_embed(self) -> discord.Embed:
        all_ui_strings = get_config("strings", {})
        commerce_strings = all_ui_strings.get("commerce", {})
        
        title = commerce_strings.get("category_view_title", "🏪 Dico森商店")
        description = commerce_strings.get("category_view_desc", "購入したいアイテムのカテゴリーを選択してください。")

        return discord.Embed(title=title, description=description, color=discord.Color.green())
    
    async def build_components(self):
        self.clear_items()
        item_db = get_item_database()
        
        available_categories = set(
            d.get('category', '').strip() for d in item_db.values() if d.get('buyable') and d.get('category')
        )
        preferred_order = ["アイテム", "装備", "エサ", "農場_種", "農場_道具"]
        
        sorted_categories = []
        for category in preferred_order:
            if category in available_categories:
                sorted_categories.append(category)
                available_categories.remove(category)
        sorted_categories.extend(sorted(list(available_categories)))

        if not sorted_categories:
            self.add_item(ui.Button(label="販売中の商品がありません。", disabled=True))
            return

        for category_name in sorted_categories:
            button = ui.Button(label=category_name, custom_id=f"buy_category_{category_name}")
            button.callback = self.category_callback
            self.add_item(button)
    
    async def category_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        category = interaction.data['custom_id'].split('buy_category_')[-1]
        item_view = BuyItemView(self.user, category)
        item_view.message = self.message
        await item_view.update_view(interaction)
        
class SellFishView(ShopViewBase):
    def __init__(self, user: discord.Member):
        super().__init__(user)
        self.fish_data_map: Dict[str, Dict[str, Any]] = {}

    async def refresh_view(self, interaction: discord.Interaction):
        embed = await self.build_embed()
        await self.build_components()
        await interaction.edit_original_response(embed=embed, view=self)
    
    async def build_embed(self) -> discord.Embed:
        wallet = await get_wallet(self.user.id)
        balance = wallet.get('balance', 0)
        embed = discord.Embed(title="🎣 買取ボックス - 魚", description=f"現在の所持金: `{balance:,}`{self.currency_icon}\n売却したい魚を下のメニューから複数選択してください。", color=discord.Color.blue())
        return embed

    async def build_components(self):
        self.clear_items()
        aquarium = await get_aquarium(str(self.user.id))
        loot_db = {loot['name']: loot for loot in get_fishing_loot()}
        self.fish_data_map.clear()
        
        options = []
        if aquarium:
            for fish in aquarium:
                fish_id = str(fish['id'])
                loot_info = loot_db.get(fish['name'], {})
                
                base_value = loot_info.get('current_base_value', loot_info.get('base_value', 0))
                size_multiplier = loot_info.get('size_multiplier', 0)
                price = int(base_value + (fish['size'] * size_multiplier))
                self.fish_data_map[fish_id] = {'price': price, 'name': fish['name']}
                
                options.append(discord.SelectOption(
                    label=f"{fish['name']} ({fish['size']}cm)", 
                    value=fish_id, 
                    description=f"{price}{self.currency_icon}"
                ))

        if options:
            max_select = min(len(options), 25)
            select = ui.Select(placeholder="売却する魚を選択...", options=options, min_values=1, max_values=max_select)
            select.callback = self.on_select
            self.add_item(select)
        
        sell_button = ui.Button(label="選択した魚を売却", style=discord.ButtonStyle.success, disabled=True, custom_id="sell_fish_confirm")
        sell_button.callback = self.sell_fish
        self.add_item(sell_button)
        back_button = ui.Button(label="カテゴリー選択に戻る", style=discord.ButtonStyle.grey)
        back_button.callback = self.go_back
        self.add_item(back_button)

    async def on_select(self, interaction: discord.Interaction):
        sell_button = next((c for c in self.children if isinstance(c, ui.Button) and c.custom_id == "sell_fish_confirm"), None)
        if sell_button:
            sell_button.disabled = False
        await interaction.response.edit_message(view=self)

    async def sell_fish(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        select_menu = next((c for c in self.children if isinstance(c, ui.Select)), None)
        if not select_menu or not select_menu.values:
            msg = await interaction.followup.send("❌ 売却する魚が選択されていません。", ephemeral=True)
            asyncio.create_task(delete_after(msg, 5))
            return
        
        fish_ids_to_sell = [int(val) for val in select_menu.values]
        total_price = sum(self.fish_data_map[val]['price'] for val in select_menu.values)
        
        try:
            await sell_fish_from_db(str(self.user.id), fish_ids_to_sell, total_price)
            
            new_wallet = await get_wallet(self.user.id)
            new_balance = new_wallet.get('balance', 0)
            sold_fish_count = len(fish_ids_to_sell)
            
            success_message = f"✅ 魚{sold_fish_count}匹を`{total_price:,}`{self.currency_icon}で売却しました。\n(残高: `{new_balance:,}`{self.currency_icon})"
            msg = await interaction.followup.send(success_message, ephemeral=True)
            asyncio.create_task(delete_after(msg, 10))
            
            await self.refresh_view(interaction)
        except Exception as e:
            logger.error(f"물고기 판매 중 오류: {e}", exc_info=True)
            await self.handle_error(interaction, e)
    
    async def go_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = SellCategoryView(self.user)
        view.message = self.message
        await view.update_view(interaction)

class SellCropView(ShopViewBase):
    def __init__(self, user: discord.Member):
        super().__init__(user)
        self.crop_data_map: Dict[str, Dict[str, Any]] = {}

    async def refresh_view(self, interaction: discord.Interaction):
        embed = await self.build_embed()
        await self.build_components()
        await interaction.edit_original_response(embed=embed, view=self)
    
    async def build_embed(self) -> discord.Embed:
        wallet = await get_wallet(self.user.id)
        balance = wallet.get('balance', 0)
        embed = discord.Embed(title="🌾 買取ボックス - 作物", description=f"現在の所持金: `{balance:,}`{self.currency_icon}\n売却したい作物を下のメニューから選択してください。", color=discord.Color.green())
        return embed

    async def build_components(self):
        self.clear_items()
        inventory = await get_inventory(self.user)
        item_db = get_item_database()
        self.crop_data_map.clear()
        
        options = []
        crop_items = {name: qty for name, qty in inventory.items() if item_db.get(name, {}).get('category', '').strip() == '農場_作物'}

        if crop_items:
            for name, qty in crop_items.items():
                item_data = item_db.get(name, {})
                price = item_data.get('current_price', int(item_data.get('sell_price', item_data.get('price', 10) * 0.8))) 
                self.crop_data_map[name] = {'price': price, 'name': name, 'max_qty': qty}
                
                options.append(discord.SelectOption(
                    label=f"{name} (所持: {qty}個)", 
                    value=name, 
                    description=f"単価: {price}{self.currency_icon}",
                    emoji=item_data.get('emoji')
                ))

        if options:
            select = ui.Select(placeholder="売却する作物を選択...", options=options)
            select.callback = self.on_select
            self.add_item(select)
        
        back_button = ui.Button(label="カテゴリー選択に戻る", style=discord.ButtonStyle.grey, row=1)
        back_button.callback = self.go_back
        self.add_item(back_button)

    async def on_select(self, interaction: discord.Interaction):
        selected_crop = interaction.data['values'][0]
        crop_info = self.crop_data_map.get(selected_crop)
        if not crop_info: return

        modal = QuantityModal(f"「{selected_crop}」売却", crop_info['max_qty'])
        await interaction.response.send_modal(modal)
        await modal.wait()

        if modal.value is None:
            msg = await interaction.followup.send("売却がキャンセルされました。", ephemeral=True)
            asyncio.create_task(delete_after(msg, 5))
            return

        quantity_to_sell = modal.value
        total_price = crop_info['price'] * quantity_to_sell
        
        try:
            await update_inventory(str(self.user.id), selected_crop, -quantity_to_sell)
            await update_wallet(self.user, total_price)

            new_wallet = await get_wallet(self.user.id)
            new_balance = new_wallet.get('balance', 0)
            success_message = f"✅ **{selected_crop}** {quantity_to_sell}個を`{total_price:,}`{self.currency_icon}で売却しました。\n(残高: `{new_balance:,}`{self.currency_icon})"
            msg = await interaction.followup.send(success_message, ephemeral=True)
            asyncio.create_task(delete_after(msg, 10))
            
            await self.refresh_view(interaction)
        except Exception as e:
            logger.error(f"작물 판매 중 오류: {e}", exc_info=True)
            await self.handle_error(interaction, e)

    async def go_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = SellCategoryView(self.user)
        view.message = self.message
        await view.update_view(interaction)

class SellCategoryView(ShopViewBase):
    async def build_embed(self) -> discord.Embed:
        return discord.Embed(title="📦 買取ボックス - カテゴリー選択", description="売却したいアイテムのカテゴリーを選択してください。", color=discord.Color.green())

    async def build_components(self):
        self.clear_items()
        self.add_item(ui.Button(label="装備", custom_id="sell_category_gear", disabled=True))
        self.add_item(ui.Button(label="魚", custom_id="sell_category_fish"))
        self.add_item(ui.Button(label="作物", custom_id="sell_category_crop"))
        for child in self.children:
            if isinstance(child, ui.Button):
                child.callback = self.on_button_click

    async def on_button_click(self, interaction: discord.Interaction):
        await interaction.response.defer()
        category = interaction.data['custom_id'].split('_')[-1]
        
        if category == "fish": view = SellFishView(self.user)
        elif category == "crop": view = SellCropView(self.user)
        else: return

        view.message = self.message
        await view.refresh_view(interaction)

class CommercePanelView(ui.View):
    def __init__(self, cog_instance: 'Commerce'):
        super().__init__(timeout=None)
        self.commerce_cog = cog_instance
        
        shop_button = ui.Button(label="商店 (アイテム購入)", style=discord.ButtonStyle.success, emoji="🏪", custom_id="commerce_open_shop")
        shop_button.callback = self.open_shop
        self.add_item(shop_button)

        market_button = ui.Button(label="買取ボックス (アイテム売却)", style=discord.ButtonStyle.danger, emoji="📦", custom_id="commerce_open_market")
        market_button.callback = self.open_market
        self.add_item(market_button)

    async def open_shop(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        view = BuyCategoryView(interaction.user)
        embed = await view.build_embed()
        await view.build_components()
        message = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = message

    async def open_market(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        view = SellCategoryView(interaction.user)
        embed = await view.build_embed()
        await view.build_components()
        message = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = message

class Commerce(commands.Cog):
    def __init__(self, bot: commands.Cog):
        self.bot = bot
    async def register_persistent_views(self):
        self.bot.add_view(CommercePanelView(self))
        
    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_commerce"):
        panel_name = panel_key.replace("panel_", "")
        
        if (panel_info := get_panel_id(panel_name)):
            if (old_channel_id := panel_info.get("channel_id")) and (old_channel := self.bot.get_channel(old_channel_id)):
                try:
                    old_message = await old_channel.fetch_message(panel_info["message_id"])
                    await old_message.delete()
                except (discord.NotFound, discord.Forbidden): pass
        
        if not (embed_data := await get_embed_from_db(panel_key)):
            logger.warning(f"DB에서 '{panel_key}' 임베드 데이터를 찾을 수 없어 패널 생성을 건너뜁니다.")
            return

        market_updates_list = get_config("market_fluctuations", [])
        if market_updates_list:
            market_updates_text = "\n".join(market_updates_list)
        else:
            market_updates_text = "今日は大きな価格変動がありませんでした。"
        
        embed = format_embed_from_db(embed_data, market_updates=market_updates_text)
        view = CommercePanelView(self)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_name, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。 (チャンネル: #{channel.name})")

async def setup(bot: commands.Cog):
    await bot.add_cog(Commerce(bot))
