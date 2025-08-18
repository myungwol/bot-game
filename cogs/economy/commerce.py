# cogs/economy/commerce.py (판매 기능 및 메시지 자동 삭제 추가)

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

# [추가] 메시지 자동 삭제를 위한 헬퍼 함수
async def delete_after(message: discord.WebhookMessage, delay: int):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden):
        pass # 메시지가 이미 삭제되었거나 권한이 없는 경우 무시

# ... (QuantityModal 클래스는 이전과 동일) ...

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
            await interaction.response.send_message(message_content, ephemeral=True, delete_after=5)

# ... (BuyItemView, BuyCategoryView 는 이전과 동일, 단 메시지 삭제 로직 추가) ...

# [수정] 메시지 자동 삭제 기능이 추가된 BuyItemView
class BuyItemView(ShopViewBase):
    # ... (init, build_embed, build_components, back_callback 등은 이전과 동일) ...
    async def select_callback(self, interaction: discord.Interaction):
        # ... (이전 최종 코드와 동일) ...
        # [수정] followup 메시지 전송 부분을 모두 아래와 같이 변경
        # 예시: 
        # await interaction.followup.send(...)
        # ->
        # msg = await interaction.followup.send(...)
        # asyncio.create_task(delete_after(msg, 5))

# --- [추가] 판매 기능 관련 UI 클래스 ---

class SellFishView(ShopViewBase):
    def __init__(self, user: discord.Member):
        super().__init__(user)
        self.fish_data_map: Dict[str, Dict[str, Any]] = {}
    
    async def build_and_send(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.update_view(interaction)

    async def update_view(self, interaction: discord.Interaction):
        embed = await self.build_embed()
        await self.build_components()
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            self.message = await interaction.followup.send(embed=embed, view=self, ephemeral=True)

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
                options.append(discord.SelectOption(
                    label=f"{fish['name']} ({fish['size']}cm)",
                    value=fish_id,
                    description=f"{price}{self.currency_icon}",
                    emoji=fish['emoji']
                ))

        if options:
            # 디스코드 최대 선택 개수는 25개
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
        # 선택이 변경되면 판매 버튼 활성화
        sell_button = next(c for c in self.children if isinstance(c, ui.Button) and c.custom_id == "sell_fish_confirm")
        sell_button.disabled = False
        await interaction.response.edit_message(view=self)

    async def sell_fish(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        select_menu = next(c for c in self.children if isinstance(c, ui.Select))
        if not select_menu.values:
            msg = await interaction.followup.send("❌ 売却する魚が選択されていません。", ephemeral=True)
            asyncio.create_task(delete_after(msg, 5))
            return
            
        fish_ids_to_sell = [int(val) for val in select_menu.values]
        total_price = sum(self.fish_data_map[val]['price'] for val in select_menu.values)
        
        try:
            await sell_fish_from_db(str(self.user.id), fish_ids_to_sell, total_price)
            
            sold_fish_names = ", ".join([self.fish_data_map[val]['name'] for val in select_menu.values])
            msg = await interaction.followup.send(f"✅ **{sold_fish_names}** を売却し、`{total_price:,}`{self.currency_icon} を獲得しました！", ephemeral=True)
            asyncio.create_task(delete_after(msg, 5))
            
            # 판매 후 View 새로고침
            await self.update_view(interaction)
        except Exception as e:
            logger.error(f"물고기 판매 중 오류: {e}", exc_info=True)
            await self.handle_error(interaction, e, "❌ 売却処理中にエラーが発生しました。")
    
    async def go_back(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = SellCategoryView(self.user)
        await view.update_view(interaction, self.message)


class SellCategoryView(ShopViewBase):
    async def update_view(self, interaction: discord.Interaction, message: discord.WebhookMessage = None):
        self.message = message or self.message
        embed = self.build_embed()
        self.build_components()
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            self.message = await interaction.followup.send(embed=embed, view=self, ephemeral=True)

    def build_embed(self) -> discord.Embed:
        return discord.Embed(title="📦 買取ボックス - カテゴリー選択", description="売却したいアイテムのカテゴリーを選択してください。", color=discord.Color.green())

    def build_components(self):
        self.clear_items()
        # [수정] 판매할 수 있는 카테고리만 버튼으로 생성
        # 현재는 물고기만 구현
        self.add_item(ui.Button(label="装備", custom_id="sell_category_gear", disabled=True)) # 장비 판매는 추후 구현
        self.add_item(ui.Button(label="魚", custom_id="sell_category_fish"))
        self.add_item(ui.Button(label="作物", custom_id="sell_category_crop", disabled=True)) # 작물 판매는 추후 구현
        
        for child in self.children:
            if isinstance(child, ui.Button):
                child.callback = self.on_button_click

    async def on_button_click(self, interaction: discord.Interaction):
        custom_id = interaction.data['custom_id']
        category = custom_id.split('_')[-1]
        
        if category == "fish":
            view = SellFishView(self.user)
            await view.build_and_send(interaction)
            # 현재 메시지는 더 이상 필요 없으므로 삭제
            await interaction.delete_original_response()


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

    # [수정] 판매 기능 콜백 구현
    async def open_market(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        view = SellCategoryView(interaction.user)
        await view.update_view(interaction)

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
