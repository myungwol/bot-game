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
    update_inventory, update_wallet, get_farm_data, expand_farm_db,
    save_config_to_db
)
from utils.helpers import format_embed_from_db

async def delete_after(message: discord.WebhookMessage, delay: int):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden):
        pass

class QuantityModal(ui.Modal):
    quantity = ui.TextInput(label="수량", placeholder="예: 10", required=True, max_length=5)
    def __init__(self, title: str, max_value: int):
        super().__init__(title=title)
        self.quantity.placeholder = f"최대 {max_value}개까지"
        self.max_value = max_value
        self.value: Optional[int] = None
    async def on_submit(self, i: discord.Interaction):
        try:
            q_val = int(self.quantity.value)
            if not (1 <= q_val <= self.max_value):
                await i.response.send_message(f"1부터 {self.max_value} 사이의 숫자를 입력해주세요.", ephemeral=True, delete_after=5)
                return
            self.value = q_val
            await i.response.defer(ephemeral=True)
        except ValueError:
            await i.response.send_message("숫자만 입력해주세요.", ephemeral=True, delete_after=5)
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
        raise NotImplementedError("build_embed는 하위 클래스에서 구현해야 합니다.")

    async def build_components(self):
        raise NotImplementedError("build_components는 하위 클래스에서 구현해야 합니다.")

    async def handle_error(self, interaction: discord.Interaction, error: Exception, custom_message: str = ""):
        logger.error(f"상점 처리 중 오류 발생: {error}", exc_info=True)
        message_content = custom_message or "❌ 처리 중 오류가 발생했습니다."
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
        
        category_display_names = { "아이템": "잡화점", "장비": "장비점", "미끼": "미끼가게", "농장_씨앗": "씨앗가게" }
        display_name = category_display_names.get(self.category, self.category)
        
        description_template = commerce_strings.get("item_view_desc", "현재 소지금: `{balance}`{currency_icon}\n구매하고 싶은 상품을 선택해주세요.")

        embed = discord.Embed(
            title=f"🏪 구매함 - {display_name}",
            description=description_template.format(balance=f"{balance:,}", currency_icon=self.currency_icon),
            color=discord.Color.blue()
        )
        
        await self._filter_items_for_user()

        if not self.items_in_category:
            wip_message = commerce_strings.get("wip_category", "이 카테고리의 상품은 현재 준비 중입니다.")
            embed.add_field(name="준비 중", value=wip_message)
        else:
            start_index = self.page_index * self.items_per_page
            end_index = start_index + self.items_per_page
            items_on_page = self.items_in_category[start_index:end_index]

            for name, data in items_on_page:
                field_name = f"{data.get('emoji', '📦')} {name}"
                field_value = (
                    f"**가격:** `{data.get('current_price', data.get('price', 0)):,}`{self.currency_icon}\n"
                    f"> {data.get('description', '설명이 없습니다.')}"
                )
                embed.add_field(name=field_name, value=field_value, inline=False)
            
            total_pages = math.ceil(len(self.items_in_category) / self.items_per_page)
            footer_text = "매일 00:05(KST)에 시세 변동"
            if total_pages > 1:
                page_text = f"페이지 {self.page_index + 1} / {total_pages}"
                embed.set_footer(text=f"{page_text} | {footer_text}")
            else:
                embed.set_footer(text=footer_text)

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
                    description=f"가격: {data.get('current_price', data.get('price', 0)):,}{self.currency_icon}",
                    emoji=data.get('emoji')
                ) for name, data in items_on_page
            ]
            select = ui.Select(placeholder=f"구매할 '{self.category}' 상품을 선택하세요...", options=options)
            select.callback = self.select_callback
            self.add_item(select)
        
        total_pages = math.ceil(len(self.items_in_category) / self.items_per_page)
        if total_pages > 1:
            prev_button = ui.Button(label="◀ 이전", custom_id="prev_page", disabled=(self.page_index == 0), row=2)
            prev_button.callback = self.pagination_callback
            self.add_item(prev_button)
            
            next_button = ui.Button(label="다음 ▶", custom_id="next_page", disabled=(self.page_index >= total_pages - 1), row=2)
            next_button.callback = self.pagination_callback
            self.add_item(next_button)

        back_button = ui.Button(label="카테고리 선택으로 돌아가기", style=discord.ButtonStyle.grey, row=3)
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
            inventory = await get_inventory(self.user)
            max_ownable = item_data.get('max_ownable', 999)

            if inventory.get(item_name, 0) >= max_ownable:
                await interaction.response.send_message(f"❌ '{item_name}'은(는) 최대 {max_ownable}개까지만 보유할 수 있습니다.", ephemeral=True, delete_after=5)
                return

            price = item_data.get('current_price', item_data.get('price', 0))
            wallet = await get_wallet(self.user.id)
            if wallet.get('balance', 0) < price:
                await interaction.response.send_message("❌ 잔액이 부족합니다.", ephemeral=True, delete_after=5)
                return

            if item_data.get('instant_use', False):
                await self.handle_instant_use_item(interaction, item_name, item_data, price, wallet)
            elif max_ownable > 1:
                await self.handle_quantity_purchase(interaction, item_name, item_data, inventory, wallet)
            else:
                await self.handle_single_purchase(interaction, item_name, item_data, price)

        except Exception as e:
            await self.handle_error(interaction, e, str(e))

    async def handle_instant_use_item(self, interaction: discord.Interaction, item_name: str, item_data: Dict, price: int, wallet: Dict):
        await interaction.response.defer(ephemeral=True)

        if item_data.get('effect_type') == 'expand_farm':
            farm_data = await get_farm_data(self.user.id)
            if not farm_data:
                msg = await interaction.followup.send("❌ 농장을 먼저 만들어주세요.", ephemeral=True); asyncio.create_task(delete_after(msg, 5)); return

            current_plots = len(farm_data.get('farm_plots', []))
            if current_plots >= 25:
                msg = await interaction.followup.send("❌ 농장이 이미 최대 크기(25칸)입니다.", ephemeral=True); asyncio.create_task(delete_after(msg, 5)); return

            await update_wallet(self.user, -price)
            success = await expand_farm_db(farm_data['id'], current_plots)

            if success:
                msg = await interaction.followup.send(f"✅ 농장이 1칸 확장되었습니다! (현재 크기: {current_plots + 1}/25)", ephemeral=True)
                asyncio.create_task(delete_after(msg, 10))
            else:
                await update_wallet(self.user, price)
                msg = await interaction.followup.send("❌ 농장을 확장하는 중 오류가 발생했습니다.", ephemeral=True); asyncio.create_task(delete_after(msg, 5))
        else:
            msg = await interaction.followup.send("❓ 알 수 없는 즉시 사용 아이템입니다.", ephemeral=True); asyncio.create_task(delete_after(msg, 5))
        
        await self.update_view(interaction)

    async def handle_quantity_purchase(self, interaction: discord.Interaction, item_name: str, item_data: Dict, inventory: Dict, wallet: Dict):
        price = item_data.get('current_price', item_data.get('price', 0))
        max_ownable = item_data.get('max_ownable', 999)
        can_own_more = max_ownable - inventory.get(item_name, 0)
        max_from_balance = wallet.get('balance', 0) // price if price > 0 else can_own_more
        max_buyable = min(can_own_more, max_from_balance)

        if max_buyable <= 0:
            await interaction.response.send_message("❌ 잔액이 부족하거나 더 이상 구매할 수 없습니다.", ephemeral=True, delete_after=5)
            return

        modal = QuantityModal(f"{item_name} 구매", max_buyable)
        await interaction.response.send_modal(modal)
        await modal.wait()

        if modal.value is None: return

        quantity, total_price = modal.value, price * modal.value
        await update_inventory(str(self.user.id), item_name, quantity)
        await update_wallet(self.user, -total_price)

        if item_name == "가마솥":
            await save_config_to_db(f"kitchen_ui_update_request_{self.user.id}", time.time())

        new_wallet = await get_wallet(self.user.id)
        success_message = f"✅ **{item_name}** {quantity}개를 `{total_price:,}`{self.currency_icon}에 구매했습니다.\n(잔액: `{new_wallet.get('balance', 0):,}`{self.currency_icon})"
        
        msg = await interaction.followup.send(success_message, ephemeral=True)
        asyncio.create_task(delete_after(msg, 10))
        await self.update_view(interaction)

    async def handle_single_purchase(self, interaction: discord.Interaction, item_name: str, item_data: Dict, price: int):
        await interaction.response.defer(ephemeral=True)
        
        await update_inventory(str(self.user.id), item_name, 1)
        await update_wallet(self.user, -price)
        
        if id_key := item_data.get('id_key'):
            if role_id := get_id(id_key):
                if role := interaction.guild.get_role(role_id):
                    try:
                        await self.user.add_roles(role, reason=f"'{item_name}' 아이템 구매")
                    except discord.Forbidden:
                        logger.error(f"역할 부여 실패: {role.name} 역할을 부여할 권한이 없습니다.")

        new_wallet = await get_wallet(self.user.id)
        success_message = f"✅ **{item_name}**을(를) `{price:,}`{self.currency_icon}에 구매했습니다.\n(잔액: `{new_wallet.get('balance', 0):,}`{self.currency_icon})"
        
        msg = await interaction.followup.send(success_message, ephemeral=True)
        asyncio.create_task(delete_after(msg, 10))
        await self.update_view(interaction)

    async def back_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        category_view = BuyCategoryView(self.user)
        category_view.message = self.message
        await category_view.update_view(interaction)

class BuyCategoryView(ShopViewBase):
    async def build_embed(self) -> discord.Embed:
        all_ui_strings = get_config("strings", {})
        commerce_strings = all_ui_strings.get("commerce", {})
        
        title = commerce_strings.get("category_view_title", "🏪 구매함")
        description = commerce_strings.get("category_view_desc", "구매하고 싶은 아이템의 카테고리를 선택해주세요.")

        embed = discord.Embed(title=title, description=description, color=discord.Color.green())
        embed.set_footer(text="매일 00:05(KST)에 시세 변동")
        return embed
    
    async def build_components(self):
        self.clear_items()
        item_db = get_item_database()
        
        available_categories = set(
            d.get('category', '').strip() for d in item_db.values() if d.get('buyable') and d.get('category')
        )
        preferred_order = ["아이템", "장비", "미끼", "농장_씨앗", "농장_도구"]
        
        sorted_categories = []
        for category in preferred_order:
            if category in available_categories:
                sorted_categories.append(category)
                available_categories.remove(category)
        sorted_categories.extend(sorted(list(available_categories)))

        if not sorted_categories:
            self.add_item(ui.Button(label="판매 중인 상품이 없습니다.", disabled=True))
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
        embed = discord.Embed(title="🎣 판매함 - 물고기", description=f"현재 소지금: `{balance:,}`{self.currency_icon}\n판매할 물고기를 아래 메뉴에서 여러 개 선택해주세요.", color=discord.Color.blue())
        embed.set_footer(text="매일 00:05(KST)에 시세 변동")
        return embed

    async def build_components(self):
        self.clear_items()
        aquarium = await get_aquarium(str(self.user.id))
        
        # [핵심 수정] 메모리 캐시 대신 DB에서 직접 최신 낚시 정보를 불러옵니다.
        loot_res = await supabase.table('fishing_loots').select('*').execute()
        if not (loot_res and loot_res.data):
            logger.error("데이터베이스에서 낚시 아이템 정보를 불러오는 데 실패했습니다.")
            self.add_item(ui.Button(label="오류: 가격 정보를 불러올 수 없습니다.", disabled=True))
            return
        loot_db = {loot['name']: loot for loot in loot_res.data}
        # [핵심 수정] 여기까지
        
        self.fish_data_map.clear()
        
        options = []
        if aquarium:
            for fish in aquarium:
                fish_id = str(fish['id'])
                loot_info = loot_db.get(fish['name'], {})
                
                # 이제 항상 최신 current_base_value를 사용합니다.
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
            select = ui.Select(placeholder="판매할 물고기를 선택하세요...", options=options, min_values=1, max_values=max_select)
            select.callback = self.on_select
            self.add_item(select)
        
        sell_button = ui.Button(label="선택한 물고기 판매", style=discord.ButtonStyle.success, disabled=True, custom_id="sell_fish_confirm")
        sell_button.callback = self.sell_fish
        self.add_item(sell_button)
        back_button = ui.Button(label="카테고리 선택으로 돌아가기", style=discord.ButtonStyle.grey)
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
            msg = await interaction.followup.send("❌ 판매할 물고기가 선택되지 않았습니다.", ephemeral=True)
            asyncio.create_task(delete_after(msg, 5))
            return
        
        fish_ids_to_sell = [int(val) for val in select_menu.values]
        total_price = sum(self.fish_data_map[val]['price'] for val in select_menu.values)
        
        try:
            await sell_fish_from_db(str(self.user.id), fish_ids_to_sell, total_price)
            
            new_wallet = await get_wallet(self.user.id)
            new_balance = new_wallet.get('balance', 0)
            sold_fish_count = len(fish_ids_to_sell)
            
            success_message = f"✅ 물고기 {sold_fish_count}마리를 `{total_price:,}`{self.currency_icon}에 판매했습니다.\n(잔액: `{new_balance:,}`{self.currency_icon})"
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
        embed = discord.Embed(title="🌾 판매함 - 작물", description=f"현재 소지금: `{balance:,}`{self.currency_icon}\n판매할 작물을 아래 메뉴에서 선택해주세요.", color=discord.Color.green())
        embed.set_footer(text="매일 00:05(KST)에 시세 변동")
        return embed

    async def build_components(self):
        self.clear_items()
        inventory = await get_inventory(self.user)
        item_db = get_item_database()
        self.crop_data_map.clear()
        
        options = []
        crop_items = {name: qty for name, qty in inventory.items() if item_db.get(name, {}).get('category', '').strip() == '농장_작물'}

        if crop_items:
            for name, qty in crop_items.items():
                item_data = item_db.get(name, {})
                price = item_data.get('current_price', int(item_data.get('sell_price', item_data.get('price', 10) * 0.8))) 
                self.crop_data_map[name] = {'price': price, 'name': name, 'max_qty': qty}
                
                options.append(discord.SelectOption(
                    label=f"{name} (보유: {qty}개)", 
                    value=name, 
                    description=f"개당: {price}{self.currency_icon}",
                    emoji=item_data.get('emoji')
                ))

        if options:
            select = ui.Select(placeholder="판매할 작물을 선택하세요...", options=options)
            select.callback = self.on_select
            self.add_item(select)
        
        back_button = ui.Button(label="카테고리 선택으로 돌아가기", style=discord.ButtonStyle.grey, row=1)
        back_button.callback = self.go_back
        self.add_item(back_button)

    async def on_select(self, interaction: discord.Interaction):
        selected_crop = interaction.data['values'][0]
        crop_info = self.crop_data_map.get(selected_crop)
        if not crop_info: return

        modal = QuantityModal(f"'{selected_crop}' 판매", crop_info['max_qty'])
        await interaction.response.send_modal(modal)
        await modal.wait()

        if modal.value is None:
            msg = await interaction.followup.send("판매가 취소되었습니다.", ephemeral=True)
            asyncio.create_task(delete_after(msg, 5))
            return

        quantity_to_sell = modal.value
        total_price = crop_info['price'] * quantity_to_sell
        
        try:
            await update_inventory(str(self.user.id), selected_crop, -quantity_to_sell)
            await update_wallet(self.user, total_price)

            new_wallet = await get_wallet(self.user.id)
            new_balance = new_wallet.get('balance', 0)
            success_message = f"✅ **{selected_crop}** {quantity_to_sell}개를 `{total_price:,}`{self.currency_icon}에 판매했습니다.\n(잔액: `{new_balance:,}`{self.currency_icon})"
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

class SellMineralView(ShopViewBase):
    def __init__(self, user: discord.Member):
        super().__init__(user)
        self.mineral_data_map: Dict[str, Dict[str, Any]] = {}

    async def refresh_view(self, interaction: discord.Interaction):
        embed = await self.build_embed()
        await self.build_components()
        await interaction.edit_original_response(embed=embed, view=self)
    
    async def build_embed(self) -> discord.Embed:
        wallet = await get_wallet(self.user.id)
        balance = wallet.get('balance', 0)
        embed = discord.Embed(title="💎 판매함 - 광물", description=f"현재 소지금: `{balance:,}`{self.currency_icon}\n판매할 광물을 아래 메뉴에서 선택해주세요.", color=0x607D8B)
        embed.set_footer(text="매일 00:05(KST)에 시세 변동")
        return embed

    async def build_components(self):
        self.clear_items()
        inventory = await get_inventory(self.user)
        item_db = get_item_database()
        self.mineral_data_map.clear()
        
        options = []
        mineral_items = {name: qty for name, qty in inventory.items() if item_db.get(name, {}).get('category', '').strip() == '광물'}

        if mineral_items:
            for name, qty in mineral_items.items():
                item_data = item_db.get(name, {})
                price = item_data.get('current_price', item_data.get('sell_price', 0))
                self.mineral_data_map[name] = {'price': price, 'name': name, 'max_qty': qty}
                
                options.append(discord.SelectOption(
                    label=f"{name} (보유: {qty}개)", 
                    value=name, 
                    description=f"개당: {price}{self.currency_icon}",
                    emoji=item_data.get('emoji', '💎')
                ))

        if options:
            select = ui.Select(placeholder="판매할 광물을 선택하세요...", options=options)
            select.callback = self.on_select
            self.add_item(select)
        
        back_button = ui.Button(label="카테고리 선택으로 돌아가기", style=discord.ButtonStyle.grey, row=1)
        back_button.callback = self.go_back
        self.add_item(back_button)

    async def on_select(self, interaction: discord.Interaction):
        selected_mineral = interaction.data['values'][0]
        mineral_info = self.mineral_data_map.get(selected_mineral)
        if not mineral_info: return

        modal = QuantityModal(f"'{selected_mineral}' 판매", mineral_info['max_qty'])
        await interaction.response.send_modal(modal)
        await modal.wait()

        if modal.value is None:
            msg = await interaction.followup.send("판매가 취소되었습니다.", ephemeral=True)
            asyncio.create_task(delete_after(msg, 5))
            return

        quantity_to_sell = modal.value
        total_price = mineral_info['price'] * quantity_to_sell
        
        try:
            await update_inventory(str(self.user.id), selected_mineral, -quantity_to_sell)
            await update_wallet(self.user, total_price)

            new_wallet = await get_wallet(self.user.id)
            new_balance = new_wallet.get('balance', 0)
            success_message = f"✅ **{selected_mineral}** {quantity_to_sell}개를 `{total_price:,}`{self.currency_icon}에 판매했습니다.\n(잔액: `{new_balance:,}`{self.currency_icon})"
            msg = await interaction.followup.send(success_message, ephemeral=True)
            asyncio.create_task(delete_after(msg, 10))
            
            await self.refresh_view(interaction)
        except Exception as e:
            logger.error(f"광물 판매 중 오류: {e}", exc_info=True)
            await self.handle_error(interaction, e)

    async def go_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = SellCategoryView(self.user)
        view.message = self.message
        await view.update_view(interaction)

class SellCategoryView(ShopViewBase):
    async def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="📦 판매함 - 카테고리 선택", description="판매할 아이템의 카테고리를 선택해주세요.", color=discord.Color.green())
        embed.set_footer(text="매일 00:05(KST)에 시세 변동")
        return embed

    async def build_components(self):
        self.clear_items()
        self.add_item(ui.Button(label="장비", custom_id="sell_category_gear", disabled=True))
        self.add_item(ui.Button(label="물고기", custom_id="sell_category_fish"))
        self.add_item(ui.Button(label="작물", custom_id="sell_category_crop"))
        self.add_item(ui.Button(label="광물", custom_id="sell_category_mineral"))
        self.add_item(ui.Button(label="음식", custom_id="sell_category_cooking"))
        for child in self.children:
            if isinstance(child, ui.Button):
                child.callback = self.on_button_click

    async def on_button_click(self, interaction: discord.Interaction):
        await interaction.response.defer()
        category = interaction.data['custom_id'].split('_')[-1]
        
        if category == "fish": view = SellFishView(self.user)
        elif category == "crop": view = SellCropView(self.user)
        elif category == "mineral": view = SellMineralView(self.user)
        
        elif category == "cooking": view = SellCookingView(self.user)
        
        else: return

        view.message = self.message
        await view.refresh_view(interaction)

class CommercePanelView(ui.View):
    def __init__(self, cog_instance: 'Commerce'):
        super().__init__(timeout=None)
        self.commerce_cog = cog_instance
        
        shop_button = ui.Button(label="구매함 (아이템 구매)", style=discord.ButtonStyle.success, emoji="🏪", custom_id="commerce_open_shop")
        shop_button.callback = self.open_shop
        self.add_item(shop_button)

        market_button = ui.Button(label="판매함 (아이템 판매)", style=discord.ButtonStyle.danger, emoji="📦", custom_id="commerce_open_market")
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
            market_updates_text = "오늘은 큰 가격 변동이 없었습니다."
        
        embed = format_embed_from_db(embed_data, market_updates=market_updates_text)
        embed.set_footer(text="매일 00:05(KST)에 시세 변동")
        view = CommercePanelView(self)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_name, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} 패널을 성공적으로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Cog):
    await bot.add_cog(Commerce(bot))
