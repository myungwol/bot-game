# cogs/economy/commerce.py (상호작용 응답 로직 수정)

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
    # ... (이전과 동일, 변경 없음) ...
    quantity = ui.TextInput(label="数量", placeholder="例: 10", required=True, max_length=5)
    def __init__(self, title: str, label: str, placeholder: str, max_value: int):
        super().__init__(title=title)
        self.quantity.label, self.quantity.placeholder, self.max_value = label, placeholder, max_value
        self.value: Optional[int] = None
    async def on_submit(self, i: discord.Interaction):
        try:
            q_val = int(self.quantity.value)
            if not (1 <= q_val <= self.max_value):
                # Modal 안에서는 followup을 사용할 수 없으므로, response.send_message 사용
                return await i.response.send_message(f"1から{self.max_value}までの数字を入力してください。", ephemeral=True, delete_after=10)
            self.value = q_val
            # Modal 제출 자체로 상호작용이 응답되므로 defer 불필요
            await i.response.defer(ephemeral=True, thinking=False) # thinking=False로 즉시 닫힘
        except ValueError: await i.response.send_message("数字のみ入力してください。", ephemeral=True, delete_after=10)
        except Exception: self.stop()

# --- 구매 흐름을 위한 View 클래스들 ---
class BuyItemView(ui.View):
    def __init__(self, user: discord.Member, category: str, parent_view: 'BuyCategoryView'):
        super().__init__(timeout=300)
        self.user = user
        self.category = category
        self.parent_view = parent_view
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")

    async def build_and_update(self, interaction: discord.Interaction):
        """상호작용이 이미 응답되었다고 가정하고, 기존 메시지를 수정합니다."""
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

        # --- [핵심 수정] ---
        # Modal이 필요한 아이템인지 먼저 확인합니다.
        is_modal_needed = item_data.get('max_ownable', 999) > 1 and item_data.get('price', 0) > 0

        # Modal이 필요 없는 모든 경우, 즉시 defer()를 호출하여 상호작용 시간 초과를 방지합니다.
        if not is_modal_needed:
            await interaction.response.defer(ephemeral=True)

        wallet, inventory = await asyncio.gather(get_wallet(self.user.id), get_inventory(str(self.user.id)))
        balance = wallet.get('balance', 0)
        
        try:
            if item_data.get('is_upgrade_item'):
                hierarchy = get_config("ROD_HIERARCHY", [])
                if not hierarchy: raise Exception("ROD_HIERARCHY 설정이 DB에 없습니다.")
                current_rod, current_rank = None, -1
                for i, rod in enumerate(hierarchy):
                    if inventory.get(rod, 0) > 0: current_rod, current_rank = rod, i
                target_rank = hierarchy.index(item_name)
                if target_rank <= current_rank: raise ValueError("error_already_have_better")
                if target_rank > 0 and hierarchy[target_rank - 1] != current_rod: raise ValueError("error_upgrade_needed")
                sell_price = 100 if current_rod and "古い" not in current_rod else 0
                params = {'p_user_id': str(self.user.id), 'p_new_rod_name': item_name, 'p_old_rod_name': current_rod, 'p_price': item_data['price'], 'p_sell_value': sell_price}
                res = await supabase.rpc('upgrade_rod_and_sell_old', params).execute()
                if not res.data or not res.data.get('success'):
                    if res.data.get('message') == 'insufficient_funds': raise ValueError("error_insufficient_funds")
                    raise Exception(f"Upgrade RPC failed: {res.data.get('message')}")
                await interaction.followup.send(get_string("commerce.upgrade_success", new_item=item_name, old_item=current_rod, sell_price=sell_price, currency_icon=self.currency_icon), ephemeral=True, delete_after=10)

            elif item_data.get('max_ownable', 999) == 1: # 단일 소유 아이템
                if inventory.get(item_name, 0) > 0 or ((id_key := item_data.get('id_key')) and (role_id := get_id(id_key)) and self.user.get_role(role_id)):
                     raise ValueError("error_already_owned")
                total_price, quantity = item_data['price'], 1
                if balance < total_price: raise ValueError("error_insufficient_funds")
                res = await supabase.rpc('buy_item', {'user_id_param': str(self.user.id), 'item_name_param': item_name, 'quantity_param': quantity, 'total_price_param': total_price}).execute()
                if not res.data: raise Exception("Buy RPC failed")
                if id_key := item_data.get('id_key'):
                    if role_id := get_id(id_key):
                        if role := interaction.guild.get_role(role_id): await self.user.add_roles(role)
                await interaction.followup.send(get_string("commerce.purchase_success", item_name=item_name, quantity=quantity), ephemeral=True, delete_after=10)

            else: # 수량 구매 아이템 (Modal)
                max_buyable = balance // item_data['price'] if item_data['price'] > 0 else 999
                if max_buyable == 0:
                    await interaction.response.send_message(get_string("commerce.error_insufficient_funds"), ephemeral=True, delete_after=10)
                    return
                modal = QuantityModal(f"{item_name} 購入", "購入する数量", f"最大 {max_buyable}個まで", max_buyable)
                await interaction.response.send_modal(modal)
                await modal.wait()
                if modal.value is None:
                    try: await interaction.followup.send("購入がキャンセルされました。", ephemeral=True, delete_after=5)
                    except discord.NotFound: pass
                    return
                quantity = modal.value
                total_price = item_data['price'] * quantity
                if balance < total_price: raise ValueError("error_insufficient_funds")
                res = await supabase.rpc('buy_item', {'user_id_param': str(self.user.id), 'item_name_param': item_name, 'quantity_param': quantity, 'total_price_param': total_price}).execute()
                if not res.data: raise Exception("Buy RPC failed")
                await interaction.followup.send(get_string("commerce.purchase_success", item_name=item_name, quantity=quantity), ephemeral=True, delete_after=10)

            await self.build_and_update(interaction)

        except ValueError as e:
            error_key = str(e)
            message = get_string(f"commerce.{error_key}", f"エラー: {error_key}")
            if interaction.response.is_done(): await interaction.followup.send(message, ephemeral=True, delete_after=10)
            else: await interaction.response.send_message(message, ephemeral=True, delete_after=10)
        except Exception as e:
            logger.error(f"구매 처리 중 오류: {e}", exc_info=True)
            message = "❌ 購入処理中にエラーが発生しました。"
            if interaction.response.is_done(): await interaction.followup.send(message, ephemeral=True, delete_after=10)
            else: await interaction.response.send_message(message, ephemeral=True, delete_after=10)

    async def back_callback(self, interaction: discord.Interaction):
        await self.parent_view.build_and_update(interaction)

class BuyCategoryView(ui.View):
    # ... (이전과 동일, 변경 없음) ...
    def __init__(self, user: discord.Member):
        super().__init__(timeout=300)
        self.user = user
    def _build_embed(self) -> discord.Embed:
        return discord.Embed(title=get_string("commerce.category_view_title"), description=get_string("commerce.category_view_desc"), color=discord.Color.green())
    def _build_components(self):
        self.clear_items()
        categories = get_string("commerce.categories", {})
        for key, label in categories.items():
            button = ui.Button(label=label, custom_id=f"buy_category_{key}")
            button.callback = self.category_callback
            if "準備中" in label: button.disabled = True
            self.add_item(button)
    async def send_initial_message(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        embed = self._build_embed()
        self._build_components()
        await interaction.followup.send(embed=embed, view=self, ephemeral=True)
    async def build_and_update(self, interaction: discord.Interaction):
        if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
        embed = self._build_embed()
        self._build_components()
        await interaction.edit_original_response(embed=embed, view=self)
    async def category_callback(self, interaction: discord.Interaction):
        category = interaction.data['custom_id'].split('_')[-1]
        await BuyItemView(self.user, category, self).build_and_update(interaction)

class CommercePanelView(ui.View):
    # ... (이전과 동일, 변경 없음) ...
    def __init__(self, cog_instance: 'Commerce'):
        super().__init__(timeout=None)
        self.commerce_cog = cog_instance
    async def setup_buttons(self):
        self.clear_items()
        components_data = await get_panel_components_from_db('commerce')
        for comp in components_data:
            if comp.get('component_type') == 'button' and (key := comp.get('component_key')):
                style_str = comp.get('style', 'secondary')
                style = discord.ButtonStyle[style_str] if hasattr(discord.ButtonStyle, style_str) else discord.ButtonStyle.secondary
                button = ui.Button(label=comp.get('label'), style=style, emoji=comp.get('emoji'), custom_id=key)
                if key == 'open_shop': button.callback = self.open_shop
                elif key == 'open_market': button.callback = self.open_market
                self.add_item(button)
    async def open_shop(self, interaction: discord.Interaction):
        await BuyCategoryView(interaction.user).send_initial_message(interaction)
    async def open_market(self, interaction: discord.Interaction):
        await interaction.response.send_message("販売機能は現在準備中です。", ephemeral=True)

class Commerce(commands.Cog):
    # ... (이전과 동일, 변경 없음) ...
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

async def setup(bot: commands.Cog):
    await bot.add_cog(Commerce(bot))
