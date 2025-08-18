# cogs/economy/commerce.py (임시 메시지로 상점 UI를 보내도록 수정)

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

class BuyItemView(ui.View):
    """특정 카테고리의 아이템 목록을 보여주고 구매를 처리하는 View"""
    def __init__(self, user: discord.Member, category: str, parent_view: 'BuyCategoryView'):
        super().__init__(timeout=300)
        self.user = user
        self.category = category
        self.parent_view = parent_view
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")

    async def build_and_update(self, interaction: discord.Interaction):
        if not interaction.response.is_done(): await interaction.response.defer()
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

        # ephemeral=True가 동반되지 않은 defer는 thinking 상태를 표시
        # modal이 뜰 수도 있으므로 일단 thinking으로 응답
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True, ephemeral=True)
        
        wallet, inventory = await asyncio.gather(get_wallet(self.user.id), get_inventory(str(self.user.id)))
        balance = wallet.get('balance', 0)
        
        try:
            if item_data.get('is_upgrade_item'):
                hierarchy = get_config("ROD_HIERARCHY", [])
                if not hierarchy: raise Exception("ROD_HIERARCHY 설정이 DB에 없습니다.")
                
                current_rod, current_rank = None, -1
                for i, rod_in_hierarchy in enumerate(hierarchy):
                    if inventory.get(rod_in_hierarchy, 0) > 0:
                        current_rod, current_rank = rod_in_hierarchy, i
                
                target_rank = hierarchy.index(item_name)

                # [수정] 오류 처리 로직 강화
                if target_rank <= current_rank:
                    raise ValueError("error_already_have_better")
                
                # [신규] 바로 이전 등급의 낚싯대를 가지고 있는지 확인
                if target_rank > 0 and hierarchy[target_rank - 1] != current_rod:
                    raise ValueError("error_upgrade_needed")

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

            quantity = 1
            if item_data.get('max_ownable', 999) == 1:
                if inventory.get(item_name, 0) > 0 or ((id_key := item_data.get('id_key')) and (role_id := get_id(id_key)) and self.user.get_role(role_id)):
                     raise ValueError("error_already_owned")
            else:
                max_buyable = balance // item_data['price'] if item_data['price'] > 0 else 999
                if max_buyable == 0: raise ValueError("error_insufficient_funds")
                
                # [수정] Modal을 보내기 전에 defer()를 하면 안되므로, Modal을 보낼 때는 response를 직접 사용
                modal = QuantityModal(f"{item_name} 購入", "購入する数量", f"最大 {max_buyable}個まで", max_buyable)
                await interaction.response.send_modal(modal)
                await modal.wait()
                if modal.value is None:
                    # 사용자가 Modal을 닫았을 때, 이미 응답했으므로 followup으로 메시지 전송
                    await interaction.followup.send("購入がキャンセルされました。", ephemeral=True, delete_after=5)
                    return
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
            # [수정] 오류 메시지를 followup.send로 보내 안정성 확보
            error_key = str(e)
            if error_key.startswith("error_"):
                await interaction.followup.send(get_string(f"commerce.{error_key}"), ephemeral=True, delete_after=10)
            else: # 혹시 모를 다른 ValueError에 대비
                await interaction.followup.send(f"エラー: {error_key}", ephemeral=True, delete_after=10)
        except Exception as e:
            logger.error(f"구매 처리 중 오류: {e}", exc_info=True)
            await interaction.followup.send("❌ 購入処理中にエラーが発生しました。", ephemeral=True, delete_after=10)

    async def back_callback(self, interaction: discord.Interaction):
        await self.parent_view.build_and_update(interaction)
