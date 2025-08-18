# cogs/games/user_profile.py (버튼 기반 장비 장착 기능 포함)

import discord
from discord.ext import commands
from discord import app_commands, ui
import logging
import asyncio
from typing import Optional, Dict, List, Any

from utils.database import (
    get_inventory, get_wallet, get_aquarium, set_user_gear, get_user_gear,
    save_panel_id, get_panel_id, get_id, get_embed_from_db, get_panel_components_from_db,
    get_item_database, get_config
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

# --- 전역 변수 ---
# 아이템 DB에서 카테고리 이름을 가져와서 사용
# 이 값들은 Supabase 'items' 테이블의 'category' 컬럼 값과 일치해야 합니다.
ROD_CATEGORY = "釣竿"
BAIT_CATEGORY = "釣りエサ"


class ProfileView(ui.View):
    """
    유저의 프로필(소지품, 수족관, 장비)을 보여주고 상호작용하는 기본 View.
    """
    def __init__(self, user: discord.Member, cog_instance: 'UserProfile'):
        super().__init__(timeout=300)
        self.user = user
        self.cog = cog_instance
        self.message: Optional[discord.WebhookMessage] = None
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.current_page = "inventory" # 시작 페이지

    async def build_and_send(self, interaction: discord.Interaction):
        """View를 처음 생성하고 보낼 때 사용"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await self.build_embed()
        self.build_components()
        self.message = await interaction.followup.send(embed=embed, view=self, ephemeral=True)

    async def update_display(self, interaction: discord.Interaction):
        """버튼 클릭 등으로 View를 새로고침할 때 사용"""
        embed = await self.build_embed()
        self.build_components()
        await interaction.response.edit_message(embed=embed, view=self)

    async def build_embed(self) -> discord.Embed:
        """현재 페이지에 맞는 Embed를 생성"""
        wallet_data, inventory, aquarium, gear = await asyncio.gather(
            get_wallet(self.user.id),
            get_inventory(str(self.user.id)),
            get_aquarium(str(self.user.id)),
            get_user_gear(str(self.user.id))
        )
        balance = wallet_data.get('balance', 0)

        embed = discord.Embed(
            title=f"{self.user.display_name}님의 프로필",
            color=self.user.color or discord.Color.default()
        )
        if self.user.display_avatar:
            embed.set_thumbnail(url=self.user.display_avatar.url)
        embed.add_field(name="💰 所持金", value=f"`{balance:,}`{self.currency_icon}", inline=False)
        
        item_db = get_item_database()
        if self.current_page == "inventory":
            embed.title += " - 持ち物"
            inv_text = "\n".join(f"{item_db.get(name,{}).get('emoji','📦')} **{name}**: `{count}`個" for name, count in inventory.items()) or "持ち物がありません。"
            embed.add_field(name="🎒 持ち物リスト", value=inv_text, inline=False)
        elif self.current_page == "aquarium":
            embed.title += " - 水槽"
            aqua_text = "\n".join(f"{fish['emoji']} **{fish['name']}**: `{fish['size']}`cm" for fish in aquarium) or "水槽に魚がいません。"
            embed.add_field(name="🐠 水槽の中", value=aqua_text, inline=False)
        elif self.current_page == "gear":
            embed.title += " - 装備"
            rod_name = gear.get('rod', '古い釣竿')
            bait_name = gear.get('bait', 'エサなし')
            rod_emoji = item_db.get(rod_name, {}).get('emoji', '🎣')
            bait_emoji = item_db.get(bait_name, {}).get('emoji', '🐛')
            embed.add_field(name="⚙️ 装備中のアイテム", value=f"{rod_emoji} **釣竿**: {rod_name}\n{bait_emoji} **エサ**: {bait_name}", inline=False)
        return embed

    def build_components(self):
        """현재 페이지에 맞는 버튼들을 동적으로 생성"""
        self.clear_items()
        
        # 1. 상단 탭 버튼들
        self.add_item(ui.Button(label="持ち物", style=discord.ButtonStyle.primary if self.current_page == "inventory" else discord.ButtonStyle.secondary, custom_id="profile_inventory", emoji="🎒", row=0))
        self.add_item(ui.Button(label="水槽", style=discord.ButtonStyle.primary if self.current_page == "aquarium" else discord.ButtonStyle.secondary, custom_id="profile_aquarium", emoji="🐠", row=0))
        self.add_item(ui.Button(label="装備", style=discord.ButtonStyle.primary if self.current_page == "gear" else discord.ButtonStyle.secondary, custom_id="profile_gear", emoji="⚙️", row=0))

        # 2. '장비' 탭일 경우, 장비 변경 버튼 추가
        if self.current_page == "gear":
            self.add_item(ui.Button(label="釣竿を変更", style=discord.ButtonStyle.success, custom_id="profile_change_rod", emoji="🎣", row=1))
            self.add_item(ui.Button(label="エサを変更", style=discord.ButtonStyle.success, custom_id="profile_change_bait", emoji="🐛", row=1))
        
        # 3. 모든 버튼에 콜백 함수 연결
        for child in self.children:
            if isinstance(child, ui.Button):
                child.callback = self.button_callback

    async def button_callback(self, interaction: discord.Interaction):
        """모든 버튼의 상호작용을 처리하는 중앙 콜백"""
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("自分専用のメニューを操作してください。", ephemeral=True)
        
        custom_id = interaction.data['custom_id']

        if custom_id.startswith("profile_change_"):
            # '장비 변경' 버튼을 눌렀을 경우
            gear_type = custom_id.split("_")[-1] # 'rod' 또는 'bait'
            gear_select_view = GearSelectView(self.user, self.cog, gear_type)
            await gear_select_view.setup_components()
            await interaction.response.edit_message(view=gear_select_view)
        else:
            # 상단 탭 버튼을 눌렀을 경우
            self.current_page = custom_id.split("_")[1] # 'inventory', 'aquarium', 'gear'
            await self.update_display(interaction)

class GearSelectView(ui.View):
    """
    장비를 변경하기 위한 드롭다운 메뉴를 보여주는 View
    """
    def __init__(self, user: discord.Member, cog_instance: 'UserProfile', gear_type: str):
        super().__init__(timeout=180)
        self.user = user
        self.cog = cog_instance
        self.gear_type = gear_type # 'rod' 또는 'bait'

    async def setup_components(self):
        """인벤토리를 읽어 드롭다운 메뉴의 옵션을 설정"""
        inventory = await get_inventory(str(self.user.id))
        item_db = get_item_database()
        
        target_category = ROD_CATEGORY if self.gear_type == 'rod' else BAIT_CATEGORY
        
        options = []
        # '장비 해제' 옵션 추가
        unequip_label = "釣竿を外す" if self.gear_type == 'rod' else "エサを外す"
        unequip_value = get_config("DEFAULT_ROD", "古い釣竿") if self.gear_type == 'rod' else "エサなし"
        options.append(discord.SelectOption(label=f"✋ {unequip_label}", value=unequip_value))

        # 인벤토리에서 해당 카테고리의 아이템만 필터링하여 옵션에 추가
        for name, count in inventory.items():
            item_data = item_db.get(name)
            if item_data and item_data.get('category') == target_category:
                options.append(discord.SelectOption(label=name, value=name, emoji=item_data.get('emoji')))

        select = ui.Select(placeholder=f"새로운 {self.gear_type}를 선택하세요...", options=options)
        select.callback = self.select_callback
        self.add_item(select)

        back_button = ui.Button(label="戻る", style=discord.ButtonStyle.grey, row=1)
        back_button.callback = self.back_callback
        self.add_item(back_button)

    async def select_callback(self, interaction: discord.Interaction):
        """드롭다운에서 아이템을 선택했을 때 호출"""
        selected_item = interaction.data['values'][0]
        
        update_data = {self.gear_type: selected_item}
        await set_user_gear(str(self.user.id), **update_data)
        
        # 선택 후, 다시 이전 프로필 화면으로 돌아감
        await self.go_back_to_profile(interaction)
    
    async def back_callback(self, interaction: discord.Interaction):
        """'뒤로가기' 버튼을 눌렀을 때 호출"""
        await self.go_back_to_profile(interaction)

    async def go_back_to_profile(self, interaction: discord.Interaction):
        """프로필 View를 다시 생성하여 화면을 되돌림"""
        profile_view = ProfileView(self.user, self.cog)
        profile_view.current_page = "gear" # '장비' 탭으로 고정
        await profile_view.update_display(interaction)

class UserProfilePanelView(ui.View):
    """
    서버에 영구적으로 고정되는 패널 View
    """
    def __init__(self, cog_instance: 'UserProfile'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    async def setup_buttons(self):
        self.clear_items()
        components = await get_panel_components_from_db("profile")
        if not components: return
        
        for button_info in components:
            button = ui.Button(
                label=button_info.get('label', '持ち物を開く'),
                style=discord.ButtonStyle.primary,
                emoji=button_info.get('emoji', '📦'),
                custom_id=button_info.get('component_key', 'open_inventory')
            )
            button.callback = self.open_profile
            self.add_item(button)

    async def open_profile(self, interaction: discord.Interaction):
        # 유저 프로필 View를 새로 생성하여 ephemeral 메시지로 보여줌
        view = ProfileView(interaction.user, self.cog)
        await view.build_and_send(interaction)

class UserProfile(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.view_instance: Optional[UserProfilePanelView] = None
        logger.info("UserProfile Cog가 성공적으로 초기화되었습니다.")
    
    async def register_persistent_views(self):
        self.view_instance = UserProfilePanelView(self)
        await self.view_instance.setup_buttons()
        self.bot.add_view(self.view_instance)
        
    async def regenerate_panel(self, channel: discord.TextChannel):
        """요청에 의해 프로필 패널을 재생성합니다."""
        panel_key = "profile"
        embed_key = "panel_profile"

        panel_info = get_panel_id(panel_key)
        if panel_info and (old_id := panel_info.get('message_id')):
            try:
                old_message = await channel.fetch_message(old_id)
                await old_message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
        
        embed_data = await get_embed_from_db(embed_key)
        if not embed_data:
            logger.warning(f"DB에서 '{embed_key}' 임베드 데이터를 찾을 수 없어, 패널 생성을 건너뜁니다.")
            return

        embed = discord.Embed.from_dict(embed_data)
        
        self.view_instance = UserProfilePanelView(self)
        await self.view_instance.setup_buttons()
        # 봇 재시작 시, view_instance가 None일 수 있으므로 add_view를 다시 호출
        self.bot.add_view(self.view_instance)

        new_message = await channel.send(embed=embed, view=self.view_instance)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ 프로필 패널을 성공적으로 새로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(UserProfile(bot))
