# bot-game/cogs/commerce.py

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
    get_config, get_string,
    get_aquarium, get_fishing_loot, sell_fish_from_db,
    save_panel_id, get_panel_id, get_embed_from_db,
    update_inventory, update_wallet, get_farm_data, save_config_to_db,
    load_game_data_from_db
)
from utils.helpers import format_embed_from_db

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
                await i.response.send_message(f"1から{self.max_value}までの数字を入力してください。", ephemeral=True)
                return
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
            await interaction.followup.send(message_content, ephemeral=True)
        else:
            await interaction.response.send_message(message_content, ephemeral=True)

class BuyItemView(ShopViewBase):
    def __init__(self, user: discord.Member, category: str):
        super().__init__(user)
        self.category = category
        self.items_in_category = []
        self.page_index = 0
        self.items_per_page = 20

    async def _filter_items_for_user(self):
        """사용자 상태에 따라 상점 아이템 목록을 필터링합니다."""
        all_items_in_category = sorted(
            [(n, d) for n, d in get_item_database().items() if d.get('buyable') and d.get('category') == self.category],
            key=lambda item: item[1].get('current_price', item[1].get('price', 0))
        )
        
        farm_expansion_item_exists = any(item[1].get('effect_type') == 'expand_farm' for item in all_items_in_category)
        
        if not farm_expansion_item_exists:
            self.items_in_category = all_items_in_category
            return

        farm_res = await supabase.table('farms').select('farm_plots(count)').eq('user_id', self.user.id).maybe_single().execute()
        
        current_plots = 0
        if farm_res and farm_res.data and farm_res.data.get('farm_plots'):
            current_plots = farm_res.data['farm_plots'][0]['count']
        
        is_farm_max_size = current_plots >= 25
        
        filtered_items = []
        for name, data in all_items_in_category:
            if data.get('effect_type') == 'expand_farm':
                if not is_farm_max_size:
                    filtered_items.append((name, data))
            else:
                filtered_items.append((name, data))
        
        self.items_in_category = filtered_items

    async def build_embed(self) -> discord.Embed:
        wallet = await get_wallet(self.user.id)
        balance = wallet.get('balance', 0)
        
        category_display_names = { "アイテム": "雑貨屋", "装備": "武具屋", "エサ": "エサ屋", "農場_種": "種屋" }
        display_name = category_display_names.get(self.category, self.category)

        embed = discord.Embed(
            title=f"🏪 Dico森商店 - {display_name}",
            description=get_string("commerce.item_view_desc", balance=f"{balance:,}", currency_icon=self.currency_icon),
            color=discord.Color.blue()
        )

        if not self.items_in_category:
            embed.add_field(name="準備中", value=get_string("commerce.wip_category", default="このカテゴリーの商品は現在準備中です。"))
        else:
            start_index, end_index = self.page_index * self.items_per_page, (self.page_index + 1) * self.items_per_page
            items_on_page = self.items_in_category[start_index:end_index]

            for name, data in items_on_page:
                price = data.get('current_price', data.get('price', 0))
                field_name = f"{data.get('emoji', '📦')} {name}"
                field_value = f"**価格:** `{price:,}`{self.currency_icon}\n> {data.get('description', '説明がありません。')}"
                embed.add_field(name=field_name, value=field_value, inline=False)
            
            total_pages = math.ceil(len(self.items_in_category) / self.items_per_page)
            if total_pages > 1:
                embed.set_footer(text=f"ページ {self.page_index + 1} / {total_pages}")

        return embed

    async def build_components(self):
        self.clear_items()
        await self._filter_items_for_user()

        start_index, end_index = self.page_index * self.items_per_page, (self.page_index + 1) * self.items_per_page
        items_on_page = self.items_in_category[start_index:end_index]

        if items_on_page:
            options = []
            for name, data in items_on_page:
                price = data.get('current_price', data.get('price', 0))
                options.append(discord.SelectOption(label=name, value=name, description=f"価格: {price:,}{self.currency_icon}", emoji=data.get('emoji')))
            
            select = ui.Select(placeholder="購入したい商品を選択...", options=options)
            select.callback = self.select_callback
            self.add_item(select)
        
        total_pages = math.ceil(len(self.items_in_category) / self.items_per_page)
        if total_pages > 1:
            prev_button = ui.Button(label="◀ 前へ", style=discord.ButtonStyle.grey, custom_id="prev_page", disabled=(self.page_index == 0), row=2)
            prev_button.callback = self.pagination_callback
            next_button = ui.Button(label="次へ ▶", style=discord.ButtonStyle.grey, custom_id="next_page", disabled=(self.page_index >= total_pages - 1), row=2)
            next_button.callback = self.pagination_callback
            self.add_item(prev_button); self.add_item(next_button)

        back_button = ui.Button(label="カテゴリー選択に戻る", style=discord.ButtonStyle.grey, row=3)
        back_button.callback = self.back_callback
        self.add_item(back_button)

    async def pagination_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if interaction.data['custom_id'] == 'next_page': self.page_index += 1
        else: self.page_index -= 1
        await self.update_view(interaction)

    async def select_callback(self, interaction: discord.Interaction):
        item_name = interaction.data['values'][0]
        item_data = get_item_database().get(item_name)
        if not item_data: return
        try:
            if item_data.get('instant_use'):
                await self.handle_instant_use_item(interaction, item_name, item_data)
            elif item_data.get('is_stackable', True):
                await self.handle_quantity_purchase(interaction, item_name, item_data)
            else:
                await self.handle_single_purchase(interaction, item_name, item_data)
        except Exception as e:
            await self.handle_error(interaction, e, str(e))

    async def handle_instant_use_item(self, interaction: discord.Interaction, item_name: str, item_data: Dict):
        await interaction.response.defer(ephemeral=True)
        price = item_data.get('current_price', item_data.get('price', 0))
        wallet = await get_wallet(self.user.id)
        if wallet.get('balance', 0) < price:
            return await interaction.followup.send("❌ 残高が不足しています。", ephemeral=True)
            
        if item_data.get('effect_type') == 'expand_farm':
            farm_res = await supabase.table('farms').select('id, farm_plots(count)').eq('user_id', self.user.id).maybe_single().execute()
            
            if not (farm_res and farm_res.data):
                return await interaction.followup.send("❌ 農場をまず作成してください。", ephemeral=True)

            farm_data = farm_res.data
            current_plots = farm_data['farm_plots'][0]['count'] if farm_data.get('farm_plots') else 0
            
            if current_plots >= 25:
                return await interaction.followup.send("❌ 農場はすでに最大サイズ(25マス)です。", ephemeral=True)

            await update_wallet(self.user, -price)
            
            new_pos_x = current_plots % 5
            new_pos_y = current_plots // 5

            try:
                await supabase.table('farm_plots').insert({
                    'farm_id': farm_data['id'],
                    'pos_x': new_pos_x,
                    'pos_y': new_pos_y
                }).execute()
                
                await save_config_to_db(f"farm_ui_update_request_{self.user.id}", time.time())
                await interaction.followup.send(f"✅ 農場が1マス拡張されました！ (現在の広さ: {current_plots + 1}/25)", ephemeral=True)
            except Exception as e:
                logger.error(f"농장 확장 DB 작업 중 오류 발생: {e}", exc_info=True)
                await update_wallet(self.user, price)
                await interaction.followup.send("❌ 農場の拡張中にエラーが発生しました。", ephemeral=True)
        else:
            await interaction.followup.send("❓ 未知の即時使用アイテムです。", ephemeral=True)

        await self.update_view(interaction)

    async def handle_quantity_purchase(self, interaction: discord.Interaction, item_name: str, item_data: Dict):
        wallet = await get_wallet(self.user.id)
        balance = wallet.get('balance', 0)
        price = item_data.get('current_price', item_data.get('price', 0))
        max_buyable = balance // price if price > 0 else 999
        if max_buyable == 0:
            return await interaction.response.send_message("❌ 残高が不足しています。", ephemeral=True)
            
        modal = QuantityModal(f"{item_name} 購入", max_buyable)
        await interaction.response.send_modal(modal)
        await modal.wait()
        
        if modal.value is None: return

        quantity, total_price = modal.value, price * modal.value
        
        current_wallet = await get_wallet(self.user.id)
        if current_wallet.get('balance', 0) < total_price:
            return await interaction.followup.send("❌ 残高が不足しています。", ephemeral=True)
            
        await update_inventory(str(self.user.id), item_name, quantity)
        await update_wallet(self.user, -total_price)
        await interaction.followup.send(f"✅ **{item_name}** {quantity}個を購入しました。", ephemeral=True)
        await self.update_view(interaction)

    async def handle_single_purchase(self, interaction: discord.Interaction, item_name: str, item_data: Dict):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        price = item_data.get('current_price', item_data.get('price', 0))
        wallet = await get_wallet(user.id)
        if wallet.get('balance', 0) < price:
            return await interaction.followup.send("❌ 残高が不足しています。", ephemeral=True)
        
        inventory = await get_inventory(user)
        if inventory.get(item_name, 0) > 0:
            return await interaction.followup.send(f"❌ 「{item_name}」は既に所持しています。1つしか持てません。", ephemeral=True)
        
        await update_wallet(user, -price)
        await update_inventory(str(user.id), item_name, 1)
        await interaction.followup.send(f"✅ **{item_name}**を購入しました。", ephemeral=True)
        await self.update_view(interaction)

    async def back_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        category_view = BuyCategoryView(self.user)
        category_view.message = self.message
        await category_view.update_view(interaction)

class BuyCategoryView(ShopViewBase):
    async def build_embed(self) -> discord.Embed:
        return discord.Embed(title="🏪 Dico森商店", description="購入したいアイテムのカテゴリーを選択してください。", color=discord.Color.green())
    async def build_components(self):
        self.clear_items()
        item_db = get_item_database()
        available_categories = {d['category'] for d in item_db.values() if d.get('buyable') and d.get('category')}
        category_map = [("アイテム 📜", "アイテム"), ("装備 ⚒️", "装備"), ("エサ 🐛", "エサ"), ("種 🌱", "農場_種"),]
        
        for display_name, db_category in category_map:
            if db_category in available_categories:
                button = ui.Button(label=display_name, custom_id=f"buy_category_{db_category}")
                button.callback = self.category_callback
                self.add_item(button)
    async def category_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        category_db_name = interaction.data['custom_id'].split('buy_category_')[-1]
        item_view = BuyItemView(self.user, category_db_name)
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
                fish_id_str = str(fish['id'])
                loot_info = loot_db.get(fish['name'], {})
                item_data = get_item_database().get(fish['name'], {})
                price = item_data.get('current_price', int(loot_info.get('base_value', 0) + (fish['size'] * loot_info.get('size_multiplier', 0))))
                self.fish_data_map[fish_id_str] = {'price': price, 'name': fish['name']}
                options.append(discord.SelectOption(label=f"{fish['name']} ({fish['size']}cm)", value=fish_id_str, description=f"{price}{self.currency_icon}"))
        
        if options:
            select = ui.Select(placeholder="売却する魚を選択...", options=options, min_values=1, max_values=min(len(options), 25))
            select.callback = self.on_select
            self.add_item(select)
        
        sell_button = ui.Button(label="選択した魚を売却", style=discord.ButtonStyle.success, disabled=True, custom_id="sell_fish_confirm")
        sell_button.callback = self.sell_fish
        self.add_item(sell_button)
        
        back_button = ui.Button(label="カテゴリー選択に戻る", style=discord.ButtonStyle.grey)
        back_button.callback = self.go_back
        self.add_item(back_button)
    async def on_select(self, interaction: discord.Interaction):
        if sell_button := discord.utils.find(lambda c: c.custom_id == "sell_fish_confirm", self.children):
            sell_button.disabled = False
        await interaction.response.edit_message(view=self)
    async def sell_fish(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        select_menu = discord.utils.find(lambda c: isinstance(c, ui.Select), self.children)
        if not (select_menu and select_menu.values):
            return await interaction.followup.send("❌ 売却する魚が選択されていません。", ephemeral=True)
        
        fish_ids_to_sell = [int(val) for val in select_menu.values]
        total_price = sum(self.fish_data_map[val]['price'] for val in select_menu.values)
        try:
            await sell_fish_from_db(str(self.user.id), fish_ids_to_sell, total_price)
            new_wallet = await get_wallet(self.user.id)
            await interaction.followup.send(f"✅ 魚{len(fish_ids_to_sell)}匹を`{total_price:,}`{self.currency_icon}で売却しました。\n(残高: `{new_wallet.get('balance', 0):,}`{self.currency_icon})", ephemeral=True)
            await self.refresh_view(interaction)
        except Exception as e:
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
        if inventory is None:
            self.add_item(ui.Button(label="インベントリの読み込みに失敗しました。", disabled=True))
        else:
            item_db = get_item_database()
            self.crop_data_map.clear()
            crop_items = {name: qty for name, qty in inventory.items() if item_db.get(name, {}).get('category') == '農場_作物'}
            if crop_items:
                options = []
                for name, qty in crop_items.items():
                    item_data = item_db.get(name, {})
                    price = item_data.get('current_price', item_data.get('sell_price', 0))
                    self.crop_data_map[name] = {'price': price, 'max_qty': qty}
                    options.append(discord.SelectOption(label=f"{name} (所持: {qty}個)", value=name, description=f"単価: {price}{self.currency_icon}", emoji=item_data.get('emoji')))
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
        
        if modal.value is None: return

        quantity, total_price = modal.value, crop_info['price'] * modal.value
        try:
            await update_inventory(str(self.user.id), selected_crop, -quantity)
            await update_wallet(self.user, total_price)
            new_wallet = await get_wallet(self.user.id)
            await interaction.followup.send(f"✅ **{selected_crop}** {quantity}個を`{total_price:,}`{self.currency_icon}で売却しました。\n(残高: `{new_wallet.get('balance', 0):,}`{self.currency_icon})", ephemeral=True)
            await self.refresh_view(interaction)
        except Exception as e:
            await self.handle_error(interaction, e)

    async def go_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = SellCategoryView(self.user)
        view.message = self.message
        await view.update_view(interaction)

class SellCategoryView(ShopViewBase):
    async def build_embed(self) -> discord.Embed:
        return discord.Embed(title="📦 買取ボックス", description="売却したいアイテムのカテゴリーを選択してください。", color=discord.Color.green())
    async def build_components(self):
        self.clear_items()
        self.add_item(ui.Button(label="魚 🐟", custom_id="sell_category_fish"))
        self.add_item(ui.Button(label="作物 🌾", custom_id="sell_category_crop"))
        for child in self.children:
            if isinstance(child, ui.Button): child.callback = self.on_button_click
    async def on_button_click(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await load_game_data_from_db()
        category = interaction.data['custom_id'].split('_')[-1]
        view = None
        if category == "fish": view = SellFishView(self.user)
        elif category == "crop": view = SellCropView(self.user)
        if view:
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
        # [✅ 핵심 수정] 상점 UI를 열기 전에 최신 아이템 데이터를 불러옵니다.
        await load_game_data_from_db()
        view = BuyCategoryView(interaction.user)
        embed = await view.build_embed()
        await view.build_components()
        message = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = message

    async def open_market(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        # [✅ 핵심 수정] 판매 UI를 열기 전에 최신 아이템 데이터를 불러옵니다.
        await load_game_data_from_db()
        view = SellCategoryView(interaction.user)
        embed = await view.build_embed()
        await view.build_components()
        message = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = message

class Commerce(commands.Cog):
    def __init__(self, bot: commands.Bot):
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
        
        if not (embed_data := await get_embed_from_db(panel_key)): return
        embed = discord.Embed.from_dict(embed_data)
        view = CommercePanelView(self)
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_name, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。 (チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Commerce(bot))
