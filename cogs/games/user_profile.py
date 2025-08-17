# cogs/games/user_profile.py

import discord
from discord.ext import commands
from discord import app_commands, ui
from math import ceil
import logging
import asyncio
from typing import Optional, Dict, List, Any

# [수정] get_config 함수를 임포트합니다.
from utils.database import (
    get_wallet, get_inventory, get_aquarium, get_user_gear, set_user_gear,
    save_panel_id, get_panel_id, get_id, get_embed_from_db,
    get_panel_components_from_db, get_item_database, get_config
)

logger = logging.getLogger(__name__)

# --- [삭제] 하드코딩된 변수들 ---
# BUTTON_STYLES_MAP, CATEGORIES, FISH_PER_PAGE 등은 get_config로 대체


# --- UI 클래스 (InventoryView, InventoryPanelView) ---
class InventoryView(ui.View):
    def __init__(self, user: discord.Member):
        super().__init__(timeout=300)
        self.user = user
        self.message: Optional[discord.WebhookMessage] = None
        
        # 데이터 영역
        self.wallet_data: Dict[str, Any] = {}
        self.inventory_data: Dict[str, int] = {}
        self.aquarium_data: List[Dict[str, Any]] = {}
        self.gear_data: Dict[str, str] = {}
        
        # UI 상태 영역
        self.categories = get_config("PROFILE_CATEGORIES", ["プロフィール", "装備", "アイテム", "魚"])
        self.current_category = self.categories[0]
        self.fish_page = 1
        self.fish_per_page = get_config("PROFILE_FISH_PER_PAGE", 5)
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("自分専用のメニューを操作してください。", ephemeral=True)
            return False
        return True

    async def fetch_all_data(self):
        """사용자의 모든 게임 관련 데이터를 DB에서 비동기적으로 불러옵니다."""
        uid_str = str(self.user.id)
        self.wallet_data, self.inventory_data, self.aquarium_data, self.gear_data = await asyncio.gather(
            get_wallet(self.user.id), get_inventory(uid_str), get_aquarium(uid_str), get_user_gear(uid_str)
        )

    async def _update_embed_and_view(self, interaction: discord.Interaction):
        """Embed와 View 컴포넌트를 현재 상태에 맞게 다시 만들어 메시지를 수정합니다."""
        embed = self._build_embed()
        self._update_view_components()
        
        try:
            # 상호작용에 응답이 이미 되었는지 확인
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        except (discord.NotFound, discord.HTTPException) as e:
            logger.warning(f"인벤토리 메시지 업데이트 실패: {e}")

    def _build_embed(self) -> discord.Embed:
        """현재 선택된 카테고리에 맞는 Embed를 생성합니다."""
        embed = discord.Embed(title=f"📦 {self.user.display_name}様の持ち物 - 「{self.current_category}」", color=0xC8C8C8)
        if self.user.display_avatar:
            embed.set_thumbnail(url=self.user.display_avatar.url)
        
        # getattr을 사용하여 동적으로 함수 호출 (e.g., _build_プロフィール_embed)
        builder = getattr(self, f"_build_{self.current_category}_embed", self._build_default_embed)
        builder(embed)
        return embed

    def _update_view_components(self):
        """현재 상태에 맞게 View의 버튼과 선택 메뉴를 다시 구성합니다."""
        self.clear_items()
        
        # 카테고리 버튼 생성
        for cat_name in self.categories:
            is_active = self.current_category == cat_name
            btn = ui.Button(label=cat_name, style=discord.ButtonStyle.success if is_active else discord.ButtonStyle.secondary, custom_id=f"inv_cat_{cat_name}", row=0)
            btn.callback = self.category_button_callback
            self.add_item(btn)
        
        # 특정 카테고리에 맞는 추가 컴포넌트 생성
        if self.current_category == "装備": self._add_gear_selects()
        if self.current_category == "魚": self._add_fish_pagination()

    # --- 각 카테고리별 Embed 생성 함수 ---
    def _build_プロフィール_embed(self, embed: discord.Embed):
        balance = self.wallet_data.get('balance', 0)
        embed.add_field(name="💰 所持金", value=f"`{balance:,}` {self.currency_icon}", inline=False)
        
        prefix_hierarchy = get_config("NICKNAME_PREFIX_HIERARCHY", [])
        prefix = "役職なし"
        member_role_names = {role.name for role in self.user.roles}
        for prefix_name in prefix_hierarchy:
            if prefix_name in member_role_names:
                prefix = prefix_name
                break
        embed.add_field(name="📜 等級", value=f"`{prefix}`", inline=False)

    def _build_装備_embed(self, embed: discord.Embed):
        rod = self.gear_data.get('rod', '素手')
        rod_count = self.inventory_data.get(rod, 1) if rod in ["素手", get_config("DEFAULT_ROD", "古い釣竿")] else self.inventory_data.get(rod, 0)
        
        bait = self.gear_data.get('bait', 'エサなし')
        bait_count = self.inventory_data.get(bait, 0)
        
        embed.add_field(name="🎣 装備中の釣竿", value=f"`{rod}` (`{rod_count}`個所持)", inline=False)
        embed.add_field(name="🐛 装備中のエサ", value=f"`{bait}` (`{bait_count}`個所持)", inline=False)
        embed.set_footer(text="ここで装備したアイテムが釣りの際に自動で使用されます。")

    def _build_アイテム_embed(self, embed: discord.Embed):
        if not self.inventory_data:
            embed.description = "アイテムがありません。"
            return
        
        lines = []
        for name, count in self.inventory_data.items():
            item_info = get_item_database().get(name, {})
            emoji = item_info.get('emoji', '❓')
            lines.append(f"{emoji} **{name}** : `{count}`個")
        embed.description = "\n".join(lines)

    def _build_魚_embed(self, embed: discord.Embed):
        total_fishes = len(self.aquarium_data)
        total_pages = ceil(total_fishes / self.fish_per_page) if total_fishes > 0 else 1
        if self.fish_page > total_pages: self.fish_page = total_pages
        
        start_index = (self.fish_page - 1) * self.fish_per_page
        end_index = start_index + self.fish_per_page
        page_fishes = self.aquarium_data[start_index:end_index]
        
        if page_fishes:
            embed.description = "".join([f"{f.get('emoji', '❓')} **{f['name']}** - `{f['size']}`cm\n" for f in page_fishes])
        else:
            embed.description = "水槽に魚がいません。"
            
        embed.set_footer(text=f"ページ {self.fish_page}/{total_pages} | 合計 {total_fishes}匹")

    def _build_default_embed(self, embed: discord.Embed):
        embed.description = f"現在、このカテゴリの情報はありません。"

    # --- 각 카테고리별 View 컴포넌트 추가 함수 ---
    def _add_gear_selects(self):
        rod_hierarchy = get_config("ROD_HIERARCHY", [])
        
        rod_options = [discord.SelectOption(label=get_config("DEFAULT_ROD", "古い釣竿"), emoji="🎣")]
        rod_options.extend(
            discord.SelectOption(label=r, emoji=get_item_database().get(r, {}).get('emoji', '🎣'))
            for r in rod_hierarchy if r != get_config("DEFAULT_ROD", "古い釣竿") and self.inventory_data.get(r, 0) > 0
        )
        rod_select = ui.Select(placeholder="装備する釣竿を選択...", options=rod_options, custom_id="gear_rod_select", row=2)
        rod_select.callback = self.gear_select_callback
        self.add_item(rod_select)
        
        bait_options = [discord.SelectOption(label="エサなし", value="エサなし", emoji="🚫")]
        bait_items = get_config("BAIT_ITEMS", ["一般の釣りエサ", "高級釣りエサ"])
        bait_options.extend(
            discord.SelectOption(label=i, emoji=get_item_database().get(i, {}).get('emoji', '🐛'))
            for i in bait_items if self.inventory_data.get(i, 0) > 0
        )
        bait_select = ui.Select(placeholder="装備するエサを選択...", options=bait_options, custom_id="gear_bait_select", row=3)
        bait_select.callback = self.gear_select_callback
        self.add_item(bait_select)

    def _add_fish_pagination(self):
        total_fishes = len(self.aquarium_data)
        total_pages = ceil(total_fishes / self.fish_per_page) if total_fishes > 0 else 1
        
        prev_btn = ui.Button(label="◀", style=discord.ButtonStyle.grey, disabled=(self.fish_page <= 1), row=1, custom_id="inv_fish_prev")
        next_btn = ui.Button(label="▶", style=discord.ButtonStyle.grey, disabled=(self.fish_page >= total_pages), row=1, custom_id="inv_fish_next")
        
        prev_btn.callback = self.prev_fish_page
        next_btn.callback = self.next_fish_page
        
        self.add_item(prev_btn)
        self.add_item(next_btn)

    # --- 콜백 함수들 ---
    async def gear_select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        gear_type = "rod" if "rod" in interaction.data["custom_id"] else "bait"
        selected = interaction.data["values"][0]
        
        try:
            uid_str = str(self.user.id)
            if gear_type == "rod":
                await set_user_gear(uid_str, rod=selected)
                self.gear_data['rod'] = selected
            else:
                await set_user_gear(uid_str, bait=selected)
                self.gear_data['bait'] = selected
                
            self.inventory_data = await get_inventory(uid_str) # 인벤토리 데이터 갱신
            await self._update_embed_and_view(interaction)
        except Exception as e:
            logger.error(f"장비 변경 중 오류: {e}", exc_info=True)
            if interaction.response.is_done():
                await interaction.followup.send("❌ エラーが発生しました。", ephemeral=True)

    async def category_button_callback(self, interaction: discord.Interaction):
        self.current_category = interaction.data['custom_id'].split('_')[-1]
        self.fish_page = 1
        await self._update_embed_and_view(interaction)

    async def prev_fish_page(self, interaction: discord.Interaction):
        if self.fish_page > 1:
            self.fish_page -= 1
        await self._update_embed_and_view(interaction)

    async def next_fish_page(self, interaction: discord.Interaction):
        total_pages = ceil(len(self.aquarium_data) / self.fish_per_page) if self.aquarium_data else 1
        if self.fish_page < total_pages:
            self.fish_page += 1
        await self._update_embed_and_view(interaction)

class InventoryPanelView(ui.View):
    def __init__(self, cog_instance: 'UserProfile'):
        super().__init__(timeout=None)
        self.user_profile_cog = cog_instance
        
    async def setup_buttons(self):
        self.clear_items()
        button_styles = get_config("DISCORD_BUTTON_STYLES_MAP", {})
        components_data = await get_panel_components_from_db('profile')
        
        if not components_data:
            default_button = ui.Button(label="📦 持ち物を開く", custom_id="open_inventory", style=discord.ButtonStyle.primary)
            default_button.callback = self.open_inventory
            self.add_item(default_button)
            return
            
        for comp in components_data:
            if comp.get('component_type') == 'button' and comp.get('component_key'):
                style = button_styles.get(comp.get('style', 'secondary'), discord.ButtonStyle.secondary)
                button = ui.Button(label=comp.get('label'), style=style, emoji=comp.get('emoji'), row=comp.get('row'), custom_id=comp.get('component_key'))
                if comp.get('component_key') == 'open_inventory':
                    button.callback = self.open_inventory
                self.add_item(button)

    async def open_inventory(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            view = InventoryView(interaction.user)
            await view.fetch_all_data()
            
            embed = view._build_embed()
            view._update_view_components()
            
            view.message = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            logger.error(f"{interaction.user.display_name}의 인벤토리 열기 중 오류: {e}", exc_info=True)
            await interaction.followup.send(f"❌エラーが発生しました。\n`{e}`", ephemeral=True)

class UserProfile(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.inventory_panel_channel_id: Optional[int] = None
        self.view_instance = None
        logger.info("UserProfile Cog가 성공적으로 초기화되었습니다.")

    async def register_persistent_views(self):
        self.view_instance = InventoryPanelView(self)
        await self.view_instance.setup_buttons()
        self.bot.add_view(self.view_instance)
        
    # [수정] 함수 이름 변경
    async def cog_load(self):
        await self.load_configs()
        
    async def load_configs(self):
        self.inventory_panel_channel_id = get_id("inventory_panel_channel_id")
        
    async def regenerate_panel(self, channel: Optional[discord.TextChannel] = None):
        target_channel = channel
        if target_channel is None:
            channel_id = get_id("inventory_panel_channel_id")
            if channel_id:
                target_channel = self.bot.get_channel(channel_id)
            else:
                logger.info("ℹ️ 프로필 패널 채널이 설정되지 않아, 자동 생성을 건너뜁니다.")
                return
        if not target_channel:
            logger.warning("❌ Inventory panel channel could not be found.")
            return
        
        panel_info = get_panel_id("profile")
        if panel_info and (old_id := panel_info.get('message_id')):
            try:
                await (await target_channel.fetch_message(old_id)).delete()
            except (discord.NotFound, discord.Forbidden): pass
            
        embed_data = await get_embed_from_db("panel_profile")
        if not embed_data:
            logger.warning("DB에서 'panel_profile' 임베드 데이터를 찾을 수 없어, 패널 생성을 건너뜁니다.")
            return
            
        embed = discord.Embed.from_dict(embed_data)
        
        self.view_instance = InventoryPanelView(self)
        await self.view_instance.setup_buttons()
        new_message = await target_channel.send(embed=embed, view=self.view_instance)
        await save_panel_id("profile", new_message.id, target_channel.id)
        logger.info(f"✅ 프로필 패널을 성공적으로 새로 생성했습니다. (채널: #{target_channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(UserProfile(bot))
