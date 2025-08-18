# cogs/economy/commerce.py (재정 정보 표시 및 메시지 자동삭제 최종 완성본)

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)

from utils.database import (
    get_inventory, get_wallet, supabase, get_id, get_item_database, 
    get_config, get_string, get_panel_components_from_db,
    get_aquarium, get_fishing_loot, sell_fish_from_db
)

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
                # ephemeral 메시지는 delete_after를 지원하지 않아 별도 처리 필요
                msg = await i.response.send_message(f"1から{self.max_value}までの数字を入力してください。", ephemeral=True)
                # asyncio.create_task(delete_after(msg, 5)) # 이 방식은 webhook 메시지에만 가능
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
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.message: Optional[discord.WebhookMessage] = None
    
    async def handle_error(self, interaction: discord.Interaction, error: Exception, custom_message: str = ""):
        logger.error(f"상점 처리 중 오류 발생: {error}", exc_info=False)
        message_content = custom_message or "❌ 処理中にエラーが発生しました。"
        if interaction.response.is_done():
            msg = await interaction.followup.send(message_content, ephemeral=True)
            asyncio.create_task(delete_after(msg, 5))
        else:
            await interaction.response.send_message(message_content, ephemeral=True)

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
                    msg = await interaction.followup.send("購入がキャンセルされました。", ephemeral=True)
                    asyncio.create_task(delete_after(msg, 5))
                    return

                quantity, total_price = modal.value, item_data['price'] * modal.value
                wallet_after_modal = await get_wallet(self.user.id)
                if wallet_after_modal.get('balance', 0) < total_price:
                     raise ValueError("error_insufficient_funds")

                await supabase.rpc('buy_item', {'user_id_param': str(self.user.id), 'item_name_param': item_name, 'quantity_param': quantity, 'total_price_param': total_price}).execute()
                
                # [🔴 핵심 수정] 잔액을 다시 가져와서 메시지 생성
                new_wallet = await get_wallet(self.user.id)
                new_balance = new_wallet.get('balance', 0)
                success_message = f"✅ **{item_name}** {quantity}個を`{total_price:,}`{self.currency_icon}で購入しました。\n(残高: `{new_balance:,}`{self.currency_icon})"
                msg = await interaction.followup.send(success_message, ephemeral=True)
                asyncio.create_task(delete_after(msg, 5))
            else:
                await interaction.response.defer(ephemeral=True)
                wallet, inventory = await asyncio.gather(get_wallet(self.user.id), get_inventory(str(self.user.id)))
                
                if inventory.get(item_name, 0) > 0 and item_data.get('max_ownable', 1) == 1:
                    raise ValueError("error_already_owned")
                
                total_price, quantity = item_data['price'], 1
                if wallet.get('balance', 0) < total_price:
                    raise ValueError("error_insufficient_funds")
                
                await supabase.rpc('buy_item', {'user_id_param': str(self.user.id), 'item_name_param': item_name, 'quantity_param': quantity, 'total_price_param': total_price}).execute()
                
                if id_key := item_data.get('id_key'):
                    if role_id := get_id(id_key):
                        if role := interaction.guild.get_role(role_id): await self.user.add_roles(role)
                
                # [🔴 핵심 수정] 잔액을 다시 가져와서 메시지 생성
                new_wallet = await get_wallet(self.user.id)
                new_balance = new_wallet.get('balance', 0)
                success_message = f"✅ **{item_name}**を`{total_price:,}`{self.currency_icon}で購入しました。\n(残高: `{new_balance:,}`{self.currency_icon})"
                msg = await interaction.followup.send(success_message, ephemeral=True)
                asyncio.create_task(delete_after(msg, 5))

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
        embed = category_view.build_embed()
        view = await category_view.build_components()
        await self.message.edit(embed=embed, view=view)

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

class SellFishView(ShopViewBase):
    def __init__(self, user: discord.Member):
        super().__init__(user)
        self.fish_data_map: Dict[str, Dict[str, Any]] = {}

    async def refresh_view(self):
        embed = await self.build_embed()
        await self.build_components()
        if self.message:
            await self.message.edit(embed=embed, view=self)
    
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
                base_value = loot_info.get('base_value', 0)
                size_multiplier = loot_info.get('size_multiplier', 0)
                price = int(base_value + (fish['size'] * size_multiplier))
                self.fish_data_map[fish_id] = {'price': price, 'name': fish['name']}
                options.append(discord.SelectOption(label=f"{fish['name']} ({fish['size']}cm)", value=fish_id, description=f"{price}{self.currency_icon}", emoji=fish['emoji']))

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
        sell_button = next(c for c in self.children if isinstance(c, ui.Button) and c.custom_id == "sell_fish_confirm")
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
            
            # [🔴 핵심 수정] 잔액을 다시 가져와서 메시지 생성
            new_wallet = await get_wallet(self.user.id)
            new_balance = new_wallet.get('balance', 0)
            sold_fish_count = len(fish_ids_to_sell)
            
            success_message = f"✅ 魚{sold_fish_count}匹を`{total_price:,}`{self.currency_icon}で売却しました。\n(残高: `{new_balance:,}`{self.currency_icon})"
            msg = await interaction.followup.send(success_message, ephemeral=True)
            asyncio.create_task(delete_after(msg, 5))
            
            await self.refresh_view()
        except Exception as e:
            logger.error(f"물고기 판매 중 오류: {e}", exc_info=True)
            await self.handle_error(interaction, e)
    
    async def go_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = SellCategoryView(self.user)
        view.message = self.message
        embed = view.build_embed()
        view.build_components()
        await self.message.edit(embed=embed, view=view)

class SellCategoryView(ShopViewBase):
    def build_embed(self) -> discord.Embed:
        return discord.Embed(title="📦 買取ボックス - カテゴリー選択", description="売却したいアイテムのカテゴリーを選択してください。", color=discord.Color.green())
    def build_components(self):
        self.clear_items()
        self.add_item(ui.Button(label="装備", custom_id="sell_category_gear", disabled=True))
        self.add_item(ui.Button(label="魚", custom_id="sell_category_fish"))
        self.add_item(ui.Button(label="作物", custom_id="sell_category_crop", disabled=True))
        for child in self.children:
            if isinstance(child, ui.Button):
                child.callback = self.on_button_click

    async def on_button_click(self, interaction: discord.Interaction):
        await interaction.response.defer()
        category = interaction.data['custom_id'].split('_')[-1]
        if category == "fish":
            view = SellFishView(self.user)
            view.message = self.message # 기존 메시지를 넘겨줌
            await view.refresh_view() # 새 View의 내용으로 기존 메시지를 수정

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
        embed = view.build_embed()
        view = await view.build_components()
        message = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = message

    async def open_market(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        view = SellCategoryView(interaction.user)
        embed = view.build_embed()
        view.build_components()
        message = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = message

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
