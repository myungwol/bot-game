# cogs/economy/commerce.py (업그레이드 및 자동 판매 로직 추가)

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
from typing import Optional, Dict, List, Any

from utils.database import (
    get_inventory, get_wallet, get_aquarium, update_wallet,
    save_panel_id, get_panel_id, get_id, supabase, get_embed_from_db, get_panel_components_from_db,
    get_item_database, get_config, get_string
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

# --- 수량 입력을 위한 Modal ---
class QuantityModal(ui.Modal):
    quantity = ui.TextInput(label="数量", placeholder="例: 10", required=True, max_length=5)
    def __init__(self, title: str, label: str, placeholder: str, max_value: int):
        super().__init__(title=title)
        self.quantity.label, self.quantity.placeholder, self.max_value = label, placeholder, max_value
        self.value: Optional[int] = None
    async def on_submit(self, i: discord.Interaction):
        try:
            q_val = int(self.quantity.value)
            if not (1 <= q_val <= self.max_value):
                return await i.response.send_message(f"1から{self.max_value}までの数字を入力してください。", ephemeral=True, delete_after=10)
            self.value = q_val
            await i.response.defer()
        except ValueError: await i.response.send_message("数字のみ入力してください。", ephemeral=True, delete_after=10)
        except Exception: self.stop()

# --- 구매 흐름을 위한 View 클래스들 ---
class BuyItemView(ui.View):
    """특정 카테고리의 아이템 목록을 보여주고 구매를 처리하는 View"""
    def __init__(self, user: discord.Member, category: str, parent_view: 'BuyCategoryView'):
        super().__init__(timeout=300)
        self.user = user
        self.category = category
        self.parent_view = parent_view
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")

    async def build_and_update(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer()
        wallet = await get_wallet(self.user.id)
        balance = wallet.get('balance', 0)
        
        embed = discord.Embed(
            title=get_string("commerce.item_view_title", category=self.category),
            description=get_string("commerce.item_view_desc", balance=f"{balance:,}", currency_icon=self.currency_icon),
            color=discord.Color.blue()
        )
        
        self.clear_items()
        item_db = get_item_database()
        items_in_category = [(n, d) for n, d in item_db.items() if d.get('buyable') and d.get('category') == self.category]

        if not items_in_category:
            embed.description += f"\n\n{get_string('commerce.wip_category')}"
        else:
            options = [discord.SelectOption(label=n, value=n, description=f"{d['price']}{self.currency_icon} - {d.get('description', '')}"[:100], emoji=d.get('emoji')) for n, d in items_in_category]
            select = ui.Select(placeholder=f"「{self.category}」カテゴリの商品を選択", options=options)
            select.callback = self.select_callback
            self.add_item(select)

        back_button = ui.Button(label=get_string("commerce.back_button"), style=discord.ButtonStyle.grey, row=1)
        back_button.callback = self.back_callback
        self.add_item(back_button)
        await interaction.edit_original_response(embed=embed, view=self)

    async def select_callback(self, interaction: discord.Interaction):
        item_name = interaction.data['values'][0]
        item_data = get_item_database().get(item_name)
        if not item_data: return

        wallet, inventory = await asyncio.gather(get_wallet(self.user.id), get_inventory(str(self.user.id)))
        balance = wallet.get('balance', 0)
        
        try:
            # --- [핵심 수정] 업그레이드 아이템(낚싯대) 구매 로직 ---
            if item_data.get('is_upgrade_item'):
                hierarchy = get_config("ROD_HIERARCHY", [])
                if not hierarchy: raise Exception("ROD_HIERARCHY 설정이 DB에 없습니다.")
                
                current_rod, current_rank = None, -1
                for i, rod_in_hierarchy in enumerate(hierarchy):
                    if inventory.get(rod_in_hierarchy, 0) > 0:
                        current_rod, current_rank = rod_in_hierarchy, i
                
                target_rank = hierarchy.index(item_name)
                if target_rank <= current_rank: raise ValueError("error_already_have_better")
                
                sell_price = 100 if current_rod and "古い" not in current_rod else 0
                params = {
                    'p_user_id': str(self.user.id), 'p_new_rod_name': item_name,
                    'p_old_rod_name': current_rod, 'p_price': item_data['price'],
                    'p_sell_value': sell_price
                }
                res = await supabase.rpc('upgrade_rod_and_sell_old', params).execute()
                
                if not res.data or not res.data.get('success'):
                    if res.data.get('message') == 'insufficient_funds': raise ValueError("error_insufficient_funds")
                    raise Exception(f"Upgrade RPC failed: {res.data.get('message')}")
                
                await interaction.followup.send(
                    get_string("commerce.upgrade_success", new_item=item_name, old_item=current_rod, sell_price=sell_price, currency_icon=self.currency_icon),
                    ephemeral=True, delete_after=10
                )
                await self.build_and_update(interaction)
                return

            # --- [기존 로직] 일반 아이템 구매 ---
            quantity = 1
            if item_data.get('max_ownable', 999) == 1:
                if inventory.get(item_name, 0) > 0 or ((id_key := item_data.get('id_key')) and (role_id := get_id(id_key)) and self.user.get_role(role_id)):
                     raise ValueError("error_already_owned")
            else:
                max_buyable = balance // item_data['price'] if item_data['price'] > 0 else 999
                if max_buyable == 0: raise ValueError("error_insufficient_funds")
                modal = QuantityModal(f"{item_name} 購入", "購入する数量", f"最大 {max_buyable}個まで", max_buyable)
                await interaction.response.send_modal(modal)
                await modal.wait()
                if modal.value is None: return
                quantity = modal.value

            total_price = item_data['price'] * quantity
            if balance < total_price: raise ValueError("error_insufficient_funds")

            res = await supabase.rpc('buy_item', {'user_id_param': str(self.user.id), 'item_name_param': item_name, 'quantity_param': quantity, 'total_price_param': total_price}).execute()
            if not res.data: raise Exception("Buy RPC failed")
            
            if id_key := item_data.get('id_key'):
                if role_id := get_id(id_key):
                    if role := interaction.guild.get_role(role_id): await self.user.add_roles(role)

            await interaction.followup.send(get_string("commerce.purchase_success", item_name=item_name, quantity=quantity), ephemeral=True, delete_after=10)
            await self.build_and_update(interaction)

        except ValueError as e:
            await interaction.response.send_message(get_string(f"commerce.{e}"), ephemeral=True, delete_after=10)
        except Exception as e:
            logger.error(f"구매 처리 중 오류: {e}", exc_info=True)
            await interaction.followup.send("❌ 購入処理中にエラーが発生しました。", ephemeral=True, delete_after=10)
            
    async def back_callback(self, interaction: discord.Interaction):
        await self.parent_view.build_and_update(interaction)

# ... (BuyCategoryView, CommercePanelView, Commerce Cog, setup 함수는 이전과 동일) ...
class BuyCategoryView(ui.View):
    def __init__(self, user: discord.Member):
        super().__init__(timeout=300)
        self.user = user
    async def build_and_update(self, interaction: discord.Interaction):
        if not interaction.response.is_done(): await interaction.response.defer()
        embed = discord.Embed(title=get_string("commerce.category_view_title"), description=get_string("commerce.category_view_desc"), color=discord.Color.green())
        self.clear_items()
        categories = get_string("commerce.categories", {})
        for key, label in categories.items():
            button = ui.Button(label=label, custom_id=f"buy_category_{key}")
            button.callback = self.category_callback
            if "準備中" in label: button.disabled = True
            self.add_item(button)
        await interaction.edit_original_response(embed=embed, view=self)
    async def category_callback(self, interaction: discord.Interaction):
        category = interaction.data['custom_id'].split('_')[-1]
        await BuyItemView(self.user, category, self).build_and_update(interaction)

class CommercePanelView(ui.View):
    def __init__(self, cog_instance: 'Commerce'):
        super().__init__(timeout=None)
        self.commerce_cog = cog_instance
    async def setup_buttons(self):
        self.clear_items()
        components_data = await get_panel_components_from_db('commerce')
        for comp in components_data:
            if comp.get('component_type') == 'button' and (key := comp.get('component_key')):
                style = discord.ButtonStyle[comp.get('style', 'secondary')]
                button = ui.Button(label=comp.get('label'), style=style, emoji=comp.get('emoji'), custom_id=key)
                if key == 'open_shop': button.callback = self.open_shop
                elif key == 'open_market': button.callback = self.open_market
                self.add_item(button)
    async def open_shop(self, interaction: discord.Interaction):
        await BuyCategoryView(interaction.user).build_and_update(interaction)
    async def open_market(self, interaction: discord.Interaction):
        await interaction.response.send_message("販売機能は現在準備中です。", ephemeral=True)

class Commerce(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot, self.view_instance = bot, None
        logger.info("Commerce Cog가 성공적으로 초기화되었습니다.")
    async def register_persistent_views(self):
        self.view_instance = CommercePanelView(self)
        await self.view_instance.setup_buttons()
        self.bot.add_view(self.view_instance)
    async def regenerate_panel(self, channel: discord.TextChannel):
        panel_key, embed_key = "commerce", "panel_commerce"
        if (panel_info := get_panel_id(panel_key)) and (old_id := panel_info.get('message_id')):
            try: await (await channel.fetch_message(old_id)).delete()
            except (discord.NotFound, discord.Forbidden): pass
        if not (embed_data := await get_embed_from_db(embed_key)):
            return logger.error(f"DB에서 '{embed_key}' 임베드를 찾을 수 없어 패널 생성을 중단합니다.")
        embed = discord.Embed.from_dict(embed_data)
        self.view_instance = CommercePanelView(self)
        await self.view_instance.setup_buttons()
        self.bot.add_view(self.view_instance)
        new_message = await channel.send(embed=embed, view=self.view_instance)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ 상점 패널을 성공적으로 새로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Commerce(bot))
