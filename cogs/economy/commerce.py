# cogs/economy/commerce.py

import discord
from discord.ext import commands
from discord import app_commands, ui
import logging
import asyncio
from typing import Optional, Dict, List, Any

# [수정] get_config 함수를 임포트합니다.
from utils.database import (
    get_inventory, get_wallet, get_aquarium, update_wallet,
    save_panel_id, get_panel_id, get_id, supabase, get_embed_from_db, get_panel_components_from_db,
    get_item_database, get_fishing_loot, get_config
)
from cogs.server.system import format_embed_from_db

logger = logging.getLogger(__name__)

# --- [삭제] 하드코딩된 변수들 ---
# BUTTON_STYLES_MAP, SELL_CATEGORIES, BUY_CATEGORIES 등은 get_config로 대체


# --- UI 클래스 (Modals, Views) ---
class QuantityModal(ui.Modal):
    quantity = ui.TextInput(label="数量", placeholder="例: 10", required=True, max_length=5)

    def __init__(self, title: str, label: str, placeholder: str, max_value: int):
        super().__init__(title=title)
        self.quantity.label = label
        self.quantity.placeholder = placeholder
        self.max_value = max_value
        self.value: Optional[int] = None

    async def on_submit(self, interaction: discord.Interaction):
        try:
            q_val = int(self.quantity.value)
            if not (1 <= q_val <= self.max_value):
                await interaction.response.send_message(f"1から{self.max_value}までの数字を入力してください。", ephemeral=True)
                return
            self.value = q_val
            await interaction.response.defer()
        except ValueError:
            await interaction.response.send_message("数字のみ入力してください。", ephemeral=True)

class ShopViewBase(ui.View):
    def __init__(self, user: discord.Member):
        super().__init__(timeout=300)
        self.user = user
        self.message: Optional[discord.WebhookMessage] = None
        self.wallet_balance = 0
        self.inventory: Dict[str, int] = {}
        self.aquarium: List[Dict[str, Any]] = []
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("自分専用のメニューを操作してください。", ephemeral=True)
            return False
        return True

    async def fetch_data(self):
        wallet_data, self.inventory, self.aquarium = await asyncio.gather(
            get_wallet(self.user.id), get_inventory(str(self.user.id)), get_aquarium(str(self.user.id))
        )
        self.wallet_balance = wallet_data.get('balance', 0)

    async def update_view(self, interaction: discord.Interaction, temp_footer: Optional[str] = None):
        embed = await self._build_embed()
        self._build_components()
        
        original_footer = embed.footer.text if embed.footer and embed.footer.text else ""
        if temp_footer:
            embed.set_footer(text=temp_footer)
            
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        except (discord.NotFound, discord.HTTPException):
            pass # 메시지가 삭제된 경우 등 무시
            
        if temp_footer:
            await asyncio.sleep(5)
            embed.set_footer(text=original_footer)
            try:
                await interaction.edit_original_response(embed=embed)
            except (discord.NotFound, discord.HTTPException):
                pass

class SellItemView(ShopViewBase):
    def __init__(self, user: discord.Member):
        super().__init__(user)
        self.sell_categories = get_config("SHOP_SELL_CATEGORIES", ["魚", "アイテム"])
        self.current_category = self.sell_categories[0]

    async def _build_embed(self) -> discord.Embed:
        embed_data = await get_embed_from_db("embed_shop_sell")
        if not embed_data:
            return discord.Embed(title=f"📦 販売所 - 「{self.current_category}」", description=f"現在の所持金: `{self.wallet_balance:,}`{self.currency_icon}", color=discord.Color.orange())
        return format_embed_from_db(embed_data, category=self.current_category, balance=f"{self.wallet_balance:,}", currency_icon=self.currency_icon)

    def _build_components(self):
        self.clear_items()
        for cat in self.sell_categories:
            btn = ui.Button(label=cat, style=discord.ButtonStyle.success if self.current_category == cat else discord.ButtonStyle.secondary, custom_id=f"sell_cat_{cat}")
            btn.callback = self.category_callback
            self.add_item(btn)
        
        options = []
        if self.current_category == "魚":
            for fish in self.aquarium:
                proto = next((item for item in get_fishing_loot() if item['name'] == fish['name']), None)
                if not proto or proto.get("base_value") is None: continue
                price = int(proto.get("base_value", 0) + (fish.get('size', 0) * proto.get("size_multiplier", 0)))
                options.append(discord.SelectOption(label=f"{fish.get('emoji','🐟')} {fish['name']} ({fish['size']}cm)", value=f"fish_{fish['id']}", description=f"売却価格: {price}{self.currency_icon}"))
        elif self.current_category == "アイテム":
            for name, count in self.inventory.items():
                if not (proto := get_item_database().get(name, {})).get('sellable'): continue
                options.append(discord.SelectOption(label=f"{proto.get('emoji','❓')} {name} ({count}個)", value=f"item_{name}", description=f"単価: {proto.get('sell_price', 0)}{self.currency_icon}"))
        
        select = ui.Select(placeholder=f"売却したい{self.current_category}を選択..." if options else "販売できるものがありません。", options=options or [discord.SelectOption(label="...")], disabled=not options, row=1)
        select.callback = self.sell_callback
        self.add_item(select)

    async def category_callback(self, interaction: discord.Interaction):
        self.current_category = interaction.data['custom_id'].split('_')[-1]
        await self.update_view(interaction)

    async def sell_callback(self, interaction: discord.Interaction):
        sell_type, target = interaction.data['values'][0].split('_', 1)
        uid_str = str(self.user.id)
        footer_msg = ""
        try:
            if sell_type == "fish":
                await interaction.response.defer()
                fish_id = int(target)
                fish_data = next((f for f in self.aquarium if f.get('id') == fish_id), None)
                if not fish_data: raise ValueError("選択された魚が見つかりません。")
                
                proto = next((it for it in get_fishing_loot() if it['name'] == fish_data['name']), None)
                if not proto: raise ValueError("魚の基本データが見つかりません。")
                
                price = int(proto.get("base_value", 0) + (fish_data.get('size', 0) * proto.get("size_multiplier", 0)))
                response = await supabase.rpc('sell_fish', {'user_id_param': uid_str, 'fish_id_param': fish_id, 'fish_value_param': price}).execute()
                if not response.data: raise Exception("RPC failed to sell fish.")
                
                self.aquarium = [f for f in self.aquarium if f.get('id') != fish_id]
                self.wallet_balance += price
                footer_msg = f"✅ {fish_data['name']}を売却し、{price}{self.currency_icon}を獲得しました！"

            elif sell_type == "item":
                item_name = target
                max_qty = self.inventory.get(item_name, 0)
                if max_qty <= 0: raise ValueError("所持していないアイテムです。")
                
                modal = QuantityModal("販売数量入力", f"{item_name}の販売数量", f"最大 {max_qty}個まで入力できます", max_qty)
                await interaction.response.send_modal(modal)
                await modal.wait()
                
                if modal.value is None:
                    await self.update_view(interaction, "❌ 販売がキャンセルされました。")
                    return
                    
                qty = modal.value
                price = get_item_database().get(item_name, {}).get('sell_price', 0) * qty
                response = await supabase.rpc('sell_item', {'user_id_param': uid_str, 'item_name_param': item_name, 'quantity_param': qty, 'total_value_param': price}).execute()
                if not response.data: raise Exception("RPC failed to sell item.")

                self.inventory[item_name] -= qty
                if self.inventory[item_name] <= 0: del self.inventory[item_name]
                self.wallet_balance += price
                footer_msg = f"✅ {item_name} {qty}個を売却し、{price}{self.currency_icon}を獲得しました！"
        except Exception as e:
            logger.error(f"판매 처리 중 오류: {e}", exc_info=True)
            footer_msg = f"❌ 販売に失敗しました。"
            await self.fetch_data() # 에러 발생 시 데이터 다시 동기화
            
        await self.update_view(interaction, footer_msg)

class BuyItemView(ShopViewBase):
    def __init__(self, user: discord.Member):
        super().__init__(user)
        self.buy_categories = get_config("SHOP_BUY_CATEGORIES", ["里の役職", "釣り"])
        self.current_category_index = 0

    async def _build_embed(self) -> discord.Embed:
        category = self.buy_categories[self.current_category_index]
        embed_data = await get_embed_from_db("embed_shop_buy")
        if not embed_data:
            return discord.Embed(title=f"🏪 Dico森商店 - 「{category}」", description=f"現在の所持金: `{self.wallet_balance:,}`{self.currency_icon}", color=discord.Color.blue())
        
        embed = format_embed_from_db(embed_data, category=category, balance=f"{self.wallet_balance:,}", currency_icon=self.currency_icon)
        embed.set_footer(text=f"ページ {self.current_category_index + 1}/{len(self.buy_categories)}")
        return embed

    def _build_components(self):
        self.clear_items()
        is_first = self.current_category_index == 0
        is_last = self.current_category_index >= len(self.buy_categories) - 1
        
        prev_btn = ui.Button(label="◀ 前", disabled=is_first, row=0, custom_id="buy_cat_prev")
        next_btn = ui.Button(label="次 ▶", disabled=is_last, row=0, custom_id="buy_cat_next")
        prev_btn.callback = self.nav_category_callback
        next_btn.callback = self.nav_category_callback
        self.add_item(prev_btn)
        self.add_item(next_btn)
        
        category = self.buy_categories[self.current_category_index]
        items = {n: d for n, d in get_item_database().items() if d.get('category') == category and d.get("buyable", False)}
        options = [
            discord.SelectOption(label=n, value=n, description=f"{d['price']}{self.currency_icon} - {d.get('description', '')}"[:100], emoji=d.get('emoji'))
            for n, d in items.items()
        ]
        
        select = ui.Select(placeholder=f"「{category}」カテゴリの商品を選択" if options else "商品準備中...", options=options or [discord.SelectOption(label="...")], disabled=not options, row=1)
        select.callback = self.select_callback
        self.add_item(select)

    async def nav_category_callback(self, i: discord.Interaction):
        if i.data['custom_id'] == 'buy_cat_prev' and self.current_category_index > 0:
            self.current_category_index -= 1
        elif i.data['custom_id'] == 'buy_cat_next' and self.current_category_index < len(self.buy_categories) - 1:
            self.current_category_index += 1
        await self.update_view(i)

    async def select_callback(self, interaction: discord.Interaction):
        name = interaction.data['values'][0]
        data = get_item_database().get(name)
        user = interaction.user
        uid_str = str(user.id)
        footer_msg = ""

        if not data:
            footer_msg = "❌ エラー：商品データが見つかりません。"
            return await self.update_view(interaction, footer_msg)
            
        price = data['price']
        if self.wallet_balance < price:
            footer_msg = "❌ 残高が不足しています。"
            return await self.update_view(interaction, footer_msg)
            
        try:
            if data['category'] == '里の役職':
                await interaction.response.defer()
                role_id = get_id(data['id_key'])
                if not role_id or not isinstance(user, discord.Member) or not (role := interaction.guild.get_role(role_id)):
                    raise ValueError("Role not found.")
                if role in user.roles:
                    footer_msg = f"❌ すでにその役職をお持ちです。"
                    return await self.update_view(interaction, footer_msg)
                    
                await update_wallet(user, -price)
                await user.add_roles(role)
                self.wallet_balance -= price
                footer_msg = f"✅ 「{role.name}」役職を購入しました！"

            elif data.get("is_upgrade_item"):
                await interaction.response.defer()
                rod_hierarchy = get_config("ROD_HIERARCHY", [])
                current_rank = -1
                for idx, rod in enumerate(rod_hierarchy):
                    if self.inventory.get(rod, 0) > 0 or rod == get_config("DEFAULT_ROD", "古い釣竿"):
                        current_rank = idx
                        
                if rod_hierarchy.index(name) <= current_rank:
                    footer_msg = "❌ すでにその装備またはより良い装備を持っています。"
                    return await self.update_view(interaction, footer_msg)
                    
                response = await supabase.rpc('buy_item', {'user_id_param': uid_str, 'item_name_param': name, 'quantity_param': 1, 'total_price_param': price}).execute()
                if not response.data: raise Exception("RPC failed to buy item.")

                self.wallet_balance -= price
                self.inventory[name] = self.inventory.get(name, 0) + 1
                footer_msg = f"✅ **{name}**にアップグレードしました！"

            else:
                max_buyable = self.wallet_balance // price if price > 0 else 999
                if max_buyable == 0:
                    footer_msg = "❌ 残高が不足しています。"
                    return await self.update_view(interaction, footer_msg)
                    
                modal = QuantityModal("購入数量入力", f"{name}の購入数量", f"最大 {max_buyable}個まで購入可能です", max_buyable)
                await interaction.response.send_modal(modal)
                await modal.wait()
                
                if modal.value is None:
                    await self.update_view(interaction, "❌ 購入がキャンセルされました。")
                    return
                    
                qty = modal.value
                total_price = price * qty
                if self.wallet_balance < total_price:
                    footer_msg = "❌ 残高が不足しています。"
                    return await self.update_view(interaction, footer_msg)
                    
                response = await supabase.rpc('buy_item', {'user_id_param': uid_str, 'item_name_param': name, 'quantity_param': qty, 'total_price_param': total_price}).execute()
                if not response.data: raise Exception("RPC failed to buy item.")

                self.wallet_balance -= total_price
                self.inventory[name] = self.inventory.get(name, 0) + qty
                footer_msg = f"✅ **{name}**を{qty}個購入し、持ち物に入れました。"
        except Exception as e:
            logger.error(f"구매 처리 중 오류: {e}", exc_info=True)
            footer_msg = "❌ 購入処理中にエラーが発生しました。"
            await self.fetch_data() # 에러 발생 시 데이터 다시 동기화
            
        await self.update_view(interaction, footer_msg)

class CommercePanelView(ui.View):
    def __init__(self, cog_instance: 'Commerce'):
        super().__init__(timeout=None)
        self.commerce_cog = cog_instance
        
    async def setup_buttons(self):
        button_styles = get_config("DISCORD_BUTTON_STYLES_MAP", {})
        components_data = await get_panel_components_from_db('commerce')
        if not components_data:
            logger.warning("'commerce' 패널에 대한 컴포넌트 데이터가 DB에 없습니다.")
            return
            
        for comp in components_data:
            if comp.get('component_type') == 'button' and comp.get('component_key'):
                style = button_styles.get(comp.get('style', 'secondary'), discord.ButtonStyle.secondary)
                button = ui.Button(label=comp.get('label'), style=style, emoji=comp.get('emoji'), row=comp.get('row'), custom_id=comp.get('component_key'))
                if comp.get('component_key') == 'open_shop':
                    button.callback = self.open_shop
                elif comp.get('component_key') == 'open_market':
                    button.callback = self.open_market
                self.add_item(button)

    async def open_shop(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True, thinking=True)
        view = BuyItemView(i.user)
        await view.fetch_data()
        embed = await view._build_embed()
        view._build_components()
        view.message = await i.followup.send(embed=embed, view=view, ephemeral=True)

    async def open_market(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True, thinking=True)
        view = SellItemView(i.user)
        await view.fetch_data()
        embed = await view._build_embed()
        view._build_components()
        view.message = await i.followup.send(embed=embed, view=view, ephemeral=True)

class Commerce(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.commerce_panel_channel_id: Optional[int] = None
        self.view_instance = None
        logger.info("Commerce Cog가 성공적으로 초기화되었습니다.")
        
    async def register_persistent_views(self):
        self.view_instance = CommercePanelView(self)
        await self.view_instance.setup_buttons()
        self.bot.add_view(self.view_instance)
        
    # [수정] 함수 이름 변경
    async def cog_load(self):
        await self.load_configs()
        
    async def load_configs(self):
        self.commerce_panel_channel_id = get_id("commerce_panel_channel_id")
        
    async def regenerate_panel(self, channel: Optional[discord.TextChannel] = None):
        target_channel = channel
        if target_channel is None:
            channel_id = get_id("commerce_panel_channel_id")
            if channel_id:
                target_channel = self.bot.get_channel(channel_id)
            else:
                logger.info("ℹ️ 상점 패널 채널이 설정되지 않아, 자동 생성을 건너뜁니다.")
                return
        if not target_channel:
            logger.warning("❌ Commerce panel channel could not be found.")
            return
            
        panel_info = get_panel_id("commerce")
        if panel_info and (old_id := panel_info.get('message_id')):
            try:
                await (await target_channel.fetch_message(old_id)).delete()
            except (discord.NotFound, discord.Forbidden): pass
            
        embed_data = await get_embed_from_db("panel_commerce")
        if not embed_data:
            logger.warning("DB에서 'panel_commerce' 임베드 데이터를 찾을 수 없어, 패널 생성을 건너뜁니다.")
            return
            
        embed = discord.Embed.from_dict(embed_data)
        self.view_instance = CommercePanelView(self)
        await self.view_instance.setup_buttons()
        new_message = await target_channel.send(embed=embed, view=self.view_instance)
        await save_panel_id("commerce", new_message.id, target_channel.id)
        logger.info(f"✅ 상점 패널을 성공적으로 새로 생성했습니다. (채널: #{target_channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(Commerce(bot))
