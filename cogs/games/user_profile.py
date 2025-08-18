# cogs/games/user_profile.py (상호작용 실패 최종 해결)

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
import math
from typing import Optional, Dict, List, Any

from utils.database import (
    get_inventory, get_wallet, get_aquarium, set_user_gear, get_user_gear,
    save_panel_id, get_panel_id, get_id, get_embed_from_db, get_panel_components_from_db,
    get_item_database, get_config, get_string
)

logger = logging.getLogger(__name__)

GEAR_CATEGORY = "釣り" 

class ProfileView(ui.View):
    def __init__(self, user: discord.Member, cog_instance: 'UserProfile'):
        super().__init__(timeout=300)
        self.user: discord.Member = user
        self.cog = cog_instance
        self.message: Optional[discord.WebhookMessage] = None
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        self.current_page = "info"
        self.fish_page_index = 0
        self.cached_data = {}
        self.status_message: Optional[str] = None

    async def build_and_send(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.load_data()
        embed = await self.build_embed()
        self.build_components()
        self.message = await interaction.followup.send(embed=embed, view=self, ephemeral=True)

    # [🔴 핵심 수정] 응답 방식을 edit_original_response로 통일
    async def update_display(self, interaction: discord.Interaction, reload_data: bool = False):
        if reload_data:
            await self.load_data()
        embed = await self.build_embed()
        self.build_components()
        await interaction.edit_original_response(embed=embed, view=self)
        self.status_message = None

    async def load_data(self):
        wallet_data, inventory, aquarium, gear = await asyncio.gather(
            get_wallet(self.user.id),
            get_inventory(str(self.user.id)),
            get_aquarium(str(self.user.id)),
            get_user_gear(str(self.user.id))
        )
        self.cached_data = {"wallet": wallet_data, "inventory": inventory, "aquarium": aquarium, "gear": gear}

    async def build_embed(self) -> discord.Embed:
        wallet_data = self.cached_data.get("wallet", {})
        inventory = self.cached_data.get("inventory", {})
        aquarium = self.cached_data.get("aquarium", [])
        gear = self.cached_data.get("gear", {})
        
        balance = wallet_data.get('balance', 0)
        item_db = get_item_database()
        
        base_title = get_string("profile_view.base_title", user_name=self.user.display_name)
        title_suffix = get_string(f"profile_view.tabs.{self.current_page}.title_suffix")
        embed = discord.Embed(title=f"{base_title}{title_suffix}", color=self.user.color)
        
        if self.user.display_avatar:
            embed.set_thumbnail(url=self.user.display_avatar.url)

        description = ""
        if self.status_message:
            description += f"**{self.status_message}**\n\n"
        
        if self.current_page == "info":
            embed.add_field(name=get_string("profile_view.info_tab.field_balance"), value=f"`{balance:,}`{self.currency_icon}", inline=True)
            resident_role_keys = ["role_resident_elder", "role_resident_veteran", "role_resident_regular", "role_resident_rookie", "role_resident"]
            user_role_ids = {role.id for role in self.user.roles}
            user_rank_name = get_string("profile_view.info_tab.default_rank_name")
            for key in resident_role_keys:
                if (rank_role_id := get_id(key)) and rank_role_id in user_role_ids:
                    if rank_role := self.user.guild.get_role(rank_role_id):
                        user_rank_name = rank_role.name; break
            embed.add_field(name=get_string("profile_view.info_tab.field_rank"), value=f"`{user_rank_name}`", inline=True)
            description += get_string("profile_view.info_tab.description")
            embed.description = description

        elif self.current_page == "item":
            general_items = {
                name: count for name, count in inventory.items()
                if item_db.get(name, {}).get('category') != GEAR_CATEGORY
            }
            item_list = [f"{item_db.get(n,{}).get('emoji','📦')} **{n}**: `{c}`個" for n, c in general_items.items()]
            embed.description = description + ("\n".join(item_list) or get_string("profile_view.item_tab.no_items"))

        elif self.current_page == "gear":
            rod_name, bait_name = gear.get('rod', '古い釣竿'), gear.get('bait', 'エサなし')
            rod_emoji, bait_emoji = item_db.get(rod_name, {}).get('emoji', '🎣'), item_db.get(bait_name, {}).get('emoji', '🐛')
            embed.add_field(name=get_string("profile_view.gear_tab.current_gear_field"), value=f"{rod_emoji} **釣竿**: {rod_name}\n{bait_emoji} **エサ**: {bait_name}", inline=False)
            
            gear_items = {
                name: count for name, count in inventory.items()
                if item_db.get(name, {}).get('category') == GEAR_CATEGORY
            }
            gear_list = [f"{item_db.get(n,{}).get('emoji','🔧')} **{n}**: `{c}`個" for n, c in gear_items.items()]
            embed.add_field(name=get_string("profile_view.gear_tab.owned_gear_field"), value="\n".join(gear_list) or get_string("profile_view.gear_tab.no_owned_gear"), inline=False)
            embed.description = description

        elif self.current_page == "fish":
            if not aquarium:
                embed.description = description + get_string("profile_view.fish_tab.no_fish")
            else:
                total_pages = math.ceil(len(aquarium) / 10)
                self.fish_page_index = max(0, min(self.fish_page_index, total_pages - 1))
                fish_on_page = aquarium[self.fish_page_index * 10 : self.fish_page_index * 10 + 10]
                embed.description = description + "\n".join([f"{f['emoji']} **{f['name']}**: `{f['size']}`cm" for f in fish_on_page])
                embed.set_footer(text=get_string("profile_view.fish_tab.pagination_footer", current_page=self.fish_page_index + 1, total_pages=total_pages))
        
        elif self.current_page in get_string("profile_view.tabs", {}):
            embed.description = description + get_string("profile_view.wip_tab.description")
            
        return embed

    def build_components(self):
        self.clear_items()
        tabs_config = get_string("profile_view.tabs", {})
        row_counter = 0
        for i, (key, config) in enumerate(tabs_config.items()):
            current_row = i // 4
            style = discord.ButtonStyle.primary if self.current_page == key else discord.ButtonStyle.secondary
            self.add_item(ui.Button(label=config.get("label"), style=style, custom_id=f"profile_tab_{key}", emoji=config.get("emoji"), row=current_row))
            row_counter = max(row_counter, current_row)
        
        row_counter += 1
        if self.current_page == "gear":
            self.add_item(ui.Button(label=get_string("profile_view.gear_tab.change_rod_button"), style=discord.ButtonStyle.success, custom_id="profile_change_rod", emoji="🎣", row=row_counter))
            self.add_item(ui.Button(label=get_string("profile_view.gear_tab.change_bait_button"), style=discord.ButtonStyle.success, custom_id="profile_change_bait", emoji="🐛", row=row_counter))
        
        if self.current_page == "fish" and self.cached_data.get("aquarium"):
            if math.ceil(len(self.cached_data["aquarium"]) / 10) > 1:
                total_pages = math.ceil(len(self.cached_data["aquarium"]) / 10)
                self.add_item(ui.Button(label=get_string("profile_view.pagination_buttons.prev"), custom_id="profile_fish_prev", disabled=self.fish_page_index == 0, row=row_counter))
                self.add_item(ui.Button(label=get_string("profile_view.pagination_buttons.next"), custom_id="profile_fish_next", disabled=self.fish_page_index >= total_pages - 1, row=row_counter))

        for child in self.children:
            if isinstance(child, ui.Button): child.callback = self.button_callback

    async def button_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("自分専用のメニューを操作してください。", ephemeral=True)
        
        # [🔴 핵심 수정] 모든 콜백 시작 시 defer() 호출
        await interaction.response.defer()
        
        custom_id = interaction.data['custom_id']
        
        if custom_id.startswith("profile_tab_"):
            self.current_page = custom_id.split("_")[-1]
            if self.current_page == 'fish': self.fish_page_index = 0
            await self.update_display(interaction, reload_data=True) 

        elif custom_id.startswith("profile_change_"):
            gear_type = custom_id.split("_")[-1]
            # GearSelectView로 넘길 때 interaction을 그대로 전달
            await GearSelectView(self, gear_type).setup_and_update(interaction)
        
        elif custom_id.startswith("profile_fish_"):
            if custom_id.endswith("prev"): self.fish_page_index -= 1
            else: self.fish_page_index += 1
            await self.update_display(interaction)

class GearSelectView(ui.View):
    def __init__(self, parent_view: ProfileView, gear_type: str):
        super().__init__(timeout=180)
        self.parent_view = parent_view
        self.user = parent_view.user
        self.gear_type = gear_type

    # [🔴 핵심 수정] interaction을 받아 화면을 수정하도록 변경
    async def setup_and_update(self, interaction: discord.Interaction):
        inventory = self.parent_view.cached_data.get("inventory", {})
        item_db = get_item_database()
        is_rod_change = self.gear_type == 'rod'
        category_name = "釣竿" if is_rod_change else "釣りエサ"
        
        options = []
        unequip_label_key = "gear_select_view.unequip_rod_label" if is_rod_change else "gear_select_view.unequip_bait_label"
        unequip_value = get_config("DEFAULT_ROD", "古い釣竿") if is_rod_change else "エサなし"
        options.append(discord.SelectOption(label=f'{get_string("gear_select_view.unequip_prefix")} {get_string(unequip_label_key)}', value=unequip_value))
        
        for name, count in inventory.items():
            item_data = item_db.get(name)
            if item_data and item_data.get('category') == GEAR_CATEGORY:
                is_rod_item = '竿' in name
                if (is_rod_change and is_rod_item) or (not is_rod_change and not is_rod_item):
                     options.append(discord.SelectOption(label=f"{name} ({count}個)", value=name, emoji=item_data.get('emoji')))

        select = ui.Select(placeholder=get_string("gear_select_view.placeholder", category_name=category_name), options=options)
        select.callback = self.select_callback
        self.add_item(select)
        
        back_button = ui.Button(label=get_string("gear_select_view.back_button"), style=discord.ButtonStyle.grey, row=1, custom_id="back")
        back_button.callback = self.back_callback
        self.add_item(back_button)
        
        embed = discord.Embed(title=get_string("gear_select_view.embed_title", category_name=category_name), description=get_string("gear_select_view.embed_description"), color=self.user.color)
        # 부모의 interaction을 사용해 화면을 수정
        await interaction.edit_original_response(embed=embed, view=self)

    async def select_callback(self, interaction: discord.Interaction):
        # [🔴 핵심 수정] 콜백 시작 시 defer()
        await interaction.response.defer()
        selected_item = interaction.data['values'][0]
        await set_user_gear(str(self.user.id), **{self.gear_type: selected_item})
        
        self.parent_view.status_message = f"✅ 装備を**{selected_item}**に変更しました。"
        await self.go_back_to_profile(interaction, reload_data=True)

    async def back_callback(self, interaction: discord.Interaction):
        # [🔴 핵심 수정] 콜백 시작 시 defer()
        await interaction.response.defer()
        await self.go_back_to_profile(interaction)

    async def go_back_to_profile(self, interaction: discord.Interaction, reload_data: bool = False):
        self.parent_view.current_page = "gear"
        await self.parent_view.update_display(interaction, reload_data=reload_data)

class UserProfilePanelView(ui.View):
    def __init__(self, cog_instance: 'UserProfile'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    async def setup_buttons(self):
        self.clear_items()
        components = await get_panel_components_from_db("profile")
        if not components: return
        for button_info in components:
            button = ui.Button(label=button_info.get('label'), style=discord.ButtonStyle.primary, emoji=button_info.get('emoji'), custom_id=button_info.get('component_key'))
            button.callback = self.open_profile
            self.add_item(button)

    async def open_profile(self, interaction: discord.Interaction):
        view = ProfileView(interaction.user, self.cog)
        await view.build_and_send(interaction)

class UserProfile(commands.Cog):
    def __init__(self, bot: commands.Cog):
        self.bot = bot

    async def register_persistent_views(self):
        view = UserProfilePanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)

    async def regenerate_panel(self, channel: discord.TextChannel):
        panel_key, embed_key = "profile", "panel_profile"
        if (panel_info := get_panel_id(panel_key)) and (old_id := panel_info.get('message_id')):
            try: await (await channel.fetch_message(old_id)).delete()
            except (discord.NotFound, discord.Forbidden): pass
        
        if not (embed_data := await get_embed_from_db(embed_key)):
            return logger.warning(f"DB에서 '{embed_key}' 임베드 데이터를 찾을 수 없어, 패널 생성을 건너뜁니다.")
        
        embed = discord.Embed.from_dict(embed_data)
        view = UserProfilePanelView(self)
        await view.setup_buttons()
        self.bot.add_view(view)
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_key, new_message.id, channel.id)
        logger.info(f"✅ プロフィール 패널을 성공적으로 새로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Cog):
    await bot.add_cog(UserProfile(bot))
