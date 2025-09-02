# cogs/games/user_profile.py

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
import math
from typing import Optional, Dict, List, Any

from utils.database import (
    get_inventory, get_wallet, get_aquarium, set_user_gear, get_user_gear,
    save_panel_id, get_panel_id, get_id, get_embed_from_db,
    get_item_database, get_config, get_string, BARE_HANDS,
    supabase
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

# 아이템 카테고리를 상수로 정의
GEAR_CATEGORY = "장비"
BAIT_CATEGORY = "미끼"
FARM_TOOL_CATEGORY = "농장_도구"

class ProfileView(ui.View):
    def __init__(self, user: discord.Member, cog_instance: 'UserProfile'):
        super().__init__(timeout=300)
        self.user: discord.Member = user
        self.cog = cog_instance
        self.message: Optional[discord.WebhookMessage] = None
        self.currency_icon = get_config("GAME_CONFIG", {}).get("CURRENCY_ICON", "🪙")
        self.current_page = "info"
        self.fish_page_index = 0
        self.cached_data = {}
        self.status_message: Optional[str] = None

    async def build_and_send(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.load_data(self.user)
        embed = await self.build_embed()
        self.build_components()
        self.message = await interaction.followup.send(embed=embed, view=self, ephemeral=True)

    async def update_display(self, interaction: discord.Interaction, reload_data: bool = False):
        await interaction.response.defer()
        if reload_data:
            await self.load_data(self.user)
        embed = await self.build_embed()
        self.build_components()
        await interaction.edit_original_response(embed=embed, view=self)
        self.status_message = None

    async def load_data(self, user: discord.Member):
        wallet_data, inventory, aquarium, gear = await asyncio.gather(
            get_wallet(user.id),
            get_inventory(user),
            get_aquarium(str(user.id)),
            get_user_gear(user)
        )
        self.cached_data = {"wallet": wallet_data, "inventory": inventory, "aquarium": aquarium, "gear": gear}

    def _get_current_tab_config(self) -> Dict:
        tabs_config = get_string("profile_view.tabs", [])
        return next((tab for tab in tabs_config if tab.get("key") == self.current_page), {})

    async def build_embed(self) -> discord.Embed:
        inventory = self.cached_data.get("inventory", {})
        gear = self.cached_data.get("gear", {})
        balance = self.cached_data.get("wallet", {}).get('balance', 0)
        item_db = get_item_database()
        
        base_title = get_string("profile_view.base_title", "{user_name}의 소지품", user_name=self.user.display_name)
        
        current_tab_config = self._get_current_tab_config()
        title_suffix = current_tab_config.get("title_suffix", "")

        embed = discord.Embed(title=f"{base_title}{title_suffix}", color=self.user.color or discord.Color.blue())
        if self.user.display_avatar:
            embed.set_thumbnail(url=self.user.display_avatar.url)
        description = ""
        if self.status_message:
            description += f"**{self.status_message}**\n\n"
        
        if self.current_page == "info":
            embed.add_field(name=get_string("profile_view.info_tab.field_balance", "소지금"), value=f"`{balance:,}`{self.currency_icon}", inline=True)
            
            job_name = "일반 주민"
            try:
                job_res = await supabase.table('user_jobs').select('jobs(job_name)').eq('user_id', self.user.id).maybe_single().execute()
                if job_res and job_res.data and job_res.data.get('jobs'):
                    job_name = job_res.data['jobs']['job_name']
            except Exception as e:
                logger.error(f"직업 정보 조회 중 오류 발생 (유저: {self.user.id}): {e}")
            embed.add_field(name="직업", value=f"`{job_name}`", inline=True)

            user_rank_mention = get_string("profile_view.info_tab.default_rank_name", "새내기 주민")
            job_system_config = get_config("JOB_SYSTEM_CONFIG", {})
            level_tier_roles = job_system_config.get("LEVEL_TIER_ROLES", [])
            sorted_tier_roles = sorted(level_tier_roles, key=lambda x: x.get('level', 0), reverse=True)
            user_role_ids = {role.id for role in self.user.roles}
            
            for tier in sorted_tier_roles:
                if (role_key := tier.get('role_key')) and (rank_role_id := get_id(role_key)) and rank_role_id in user_role_ids:
                    if rank_role := self.user.guild.get_role(rank_role_id):
                        user_rank_mention = rank_role.mention
                        break
            
            embed.add_field(name=get_string("profile_view.info_tab.field_rank", "등급"), value=user_rank_mention, inline=True)
            description += get_string("profile_view.info_tab.description", "아래 탭을 선택하여 상세 정보를 확인하세요.")
            embed.description = description
        
        elif self.current_page == "item":
            excluded_categories = [GEAR_CATEGORY, FARM_TOOL_CATEGORY, "농장_씨앗", "농장_작물", BAIT_CATEGORY]
            general_items = {name: count for name, count in inventory.items() if item_db.get(name, {}).get('category') not in excluded_categories}
            item_list = [f"{item_db.get(n,{}).get('emoji','📦')} **{n}**: `{c}`개" for n, c in general_items.items()]
            embed.description = description + ("\n".join(item_list) or get_string("profile_view.item_tab.no_items", "보유 중인 아이템이 없습니다."))
        
        elif self.current_page == "gear":
            gear_categories = {"낚시": {"rod": "🎣 낚싯대", "bait": "🐛 미끼"}, "농장": {"hoe": "🪓 괭이", "watering_can": "💧 물뿌리개"}}
            for category_name, items in gear_categories.items():
                field_lines = [f"**{label}:** `{gear.get(key, BARE_HANDS)}`" for key, label in items.items()]
                embed.add_field(name=f"**[ 현재 장비: {category_name} ]**", value="\n".join(field_lines), inline=False)
            
            # [핵심 수정] '보유 중인 장비'를 필터링할 때 '장비'와 '미끼' 카테고리를 모두 포함하도록 변경
            owned_gear_categories = [GEAR_CATEGORY, BAIT_CATEGORY]
            owned_gear_items = {name: count for name, count in inventory.items() if item_db.get(name, {}).get('category') in owned_gear_categories}

            if owned_gear_items:
                gear_list = [f"{item_db.get(n,{}).get('emoji','🔧')} **{n}**: `{c}`개" for n, c in sorted(owned_gear_items.items())]
                embed.add_field(name="\n**[ 보유 중인 장비 ]**", value="\n".join(gear_list), inline=False)
            else:
                embed.add_field(name="\n**[ 보유 중인 장비 ]**", value=get_string("profile_view.gear_tab.no_owned_gear", "보유 중인 장비가 없습니다."), inline=False)
            embed.description = description
        
        elif self.current_page == "fish":
            aquarium = self.cached_data.get("aquarium", [])
            if not aquarium:
                embed.description = description + get_string("profile_view.fish_tab.no_fish", "어항에 물고기가 없습니다.")
            else:
                total_pages = math.ceil(len(aquarium) / 10)
                self.fish_page_index = max(0, min(self.fish_page_index, total_pages - 1))
                fish_on_page = aquarium[self.fish_page_index * 10 : self.fish_page_index * 10 + 10]
                embed.description = description + "\n".join([f"{f['emoji']} **{f['name']}**: `{f['size']}`cm" for f in fish_on_page])
                embed.set_footer(text=get_string("profile_view.fish_tab.pagination_footer", "페이지 {current_page} / {total_pages}", current_page=self.fish_page_index + 1, total_pages=total_pages))
        
        elif self.current_page == "seed":
            seed_items = {name: count for name, count in inventory.items() if item_db.get(name, {}).get('category') == "농장_씨앗"}
            item_list = [f"{item_db.get(n,{}).get('emoji','🌱')} **{n}**: `{c}`개" for n, c in seed_items.items()]
            embed.description = description + ("\n".join(item_list) or get_string("profile_view.seed_tab.no_items", "보유 중인 씨앗이 없습니다."))
        
        elif self.current_page == "crop":
            crop_items = {name: count for name, count in inventory.items() if item_db.get(name, {}).get('category') == "농장_작물"}
            item_list = [f"{item_db.get(n,{}).get('emoji','🌾')} **{n}**: `{c}`개" for n, c in crop_items.items()]
            embed.description = description + ("\n".join(item_list) or get_string("profile_view.crop_tab.no_items", "보유 중인 작물이 없습니다."))
        
        else:
            embed.description = description + get_string("profile_view.wip_tab.description", "이 기능은 현재 준비 중입니다.")
        return embed

    def build_components(self):
        self.clear_items()
        tabs_config = get_string("profile_view.tabs", [])
        
        row_counter, tab_buttons_in_row = 0, 0
        for config in tabs_config:
            if not (key := config.get("key")): continue
            if tab_buttons_in_row >= 5:
                row_counter += 1; tab_buttons_in_row = 0
            style = discord.ButtonStyle.primary if self.current_page == key else discord.ButtonStyle.secondary
            self.add_item(ui.Button(label=config.get("label"), style=style, custom_id=f"profile_tab_{key}", emoji=config.get("emoji"), row=row_counter))
            tab_buttons_in_row += 1
        
        row_counter += 1
        if self.current_page == "gear":
            self.add_item(ui.Button(label="낚싯대 변경", style=discord.ButtonStyle.blurple, custom_id="profile_change_rod", emoji="🎣", row=row_counter))
            self.add_item(ui.Button(label="미끼 변경", style=discord.ButtonStyle.blurple, custom_id="profile_change_bait", emoji="🐛", row=row_counter))
            row_counter += 1
            self.add_item(ui.Button(label="괭이 변경", style=discord.ButtonStyle.success, custom_id="profile_change_hoe", emoji="🪓", row=row_counter))
            self.add_item(ui.Button(label="물뿌리개 변경", style=discord.ButtonStyle.success, custom_id="profile_change_watering_can", emoji="💧", row=row_counter))
        
        row_counter += 1
        if self.current_page == "fish" and self.cached_data.get("aquarium"):
            total_pages = math.ceil(len(self.cached_data["aquarium"]) / 10)
            if total_pages > 1:
                prev_label = get_string("profile_view.pagination_buttons.prev", "◀")
                next_label = get_string("profile_view.pagination_buttons.next", "▶")
                self.add_item(ui.Button(label=prev_label, custom_id="profile_fish_prev", disabled=self.fish_page_index == 0, row=row_counter))
                self.add_item(ui.Button(label=next_label, custom_id="profile_fish_next", disabled=self.fish_page_index >= total_pages - 1, row=row_counter))
        
        for child in self.children:
            if isinstance(child, ui.Button):
                child.callback = self.button_callback
                
    async def button_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("자신 전용 메뉴를 조작해주세요.", ephemeral=True, delete_after=5)
            return
        
        custom_id = interaction.data['custom_id']
        if custom_id.startswith("profile_tab_"):
            self.current_page = custom_id.split("_")[-1]
            if self.current_page == 'fish': self.fish_page_index = 0
            await self.update_display(interaction) 
        elif custom_id.startswith("profile_change_"):
            gear_key = custom_id.replace("profile_change_", "", 1)
            await GearSelectView(self, gear_key).setup_and_update(interaction)
        elif custom_id.startswith("profile_fish_"):
            if custom_id.endswith("prev"): self.fish_page_index -= 1
            else: self.fish_page_index += 1
            await self.update_display(interaction)
            
class GearSelectView(ui.View):
    def __init__(self, parent_view: ProfileView, gear_key: str):
        super().__init__(timeout=180)
        self.parent_view = parent_view
        self.user = parent_view.user
        self.gear_key = gear_key
        
        GEAR_SETTINGS = {
            "rod":          {"display_name": "낚싯대", "gear_type_db": "낚싯대", "unequip_label": "낚싯대 해제", "default_item": BARE_HANDS},
            "bait":         {"display_name": "낚시 미끼", "gear_type_db": "미끼", "unequip_label": "미끼 해제", "default_item": "미끼 없음"},
            "hoe":          {"display_name": "괭이", "gear_type_db": "괭이", "unequip_label": "괭이 해제", "default_item": BARE_HANDS},
            "watering_can": {"display_name": "물뿌리개", "gear_type_db": "물뿌리개", "unequip_label": "물뿌리개 해제", "default_item": BARE_HANDS}
        }
        
        settings = GEAR_SETTINGS.get(self.gear_key)
        if settings:
            self.display_name = settings["display_name"]
            self.gear_type_db = settings["gear_type_db"]
            self.unequip_label = settings["unequip_label"]
            self.default_item = settings["default_item"]
        else:
            self.display_name, self.gear_type_db, self.unequip_label, self.default_item = ("알 수 없음", "", "해제", "없음")

    async def setup_and_update(self, interaction: discord.Interaction):
        await interaction.response.defer()
        inventory, item_db = self.parent_view.cached_data.get("inventory", {}), get_item_database()
        
        options = [discord.SelectOption(label=f'{get_string("profile_view.gear_select_view.unequip_prefix", "✋")} {self.unequip_label}', value="unequip")]
        
        for name, count in inventory.items():
            item_data = item_db.get(name)
            if item_data and item_data.get('gear_type') == self.gear_type_db:
                 options.append(discord.SelectOption(label=f"{name} ({count}개)", value=name, emoji=item_data.get('emoji')))

        select = ui.Select(placeholder=get_string("profile_view.gear_select_view.placeholder", "{category_name} 선택...", category_name=self.display_name), options=options)
        select.callback = self.select_callback
        self.add_item(select)

        back_button = ui.Button(label=get_string("profile_view.gear_select_view.back_button", "뒤로"), style=discord.ButtonStyle.grey, row=1)
        back_button.callback = self.back_callback
        self.add_item(back_button)

        embed = discord.Embed(
            title=get_string("profile_view.gear_select_view.embed_title", "{category_name} 변경", category_name=self.display_name), 
            description=get_string("profile_view.gear_select_view.embed_description", "장착할 아이템을 선택하세요."), 
            color=self.user.color
        )
        await interaction.edit_original_response(embed=embed, view=self)

    async def select_callback(self, interaction: discord.Interaction):
        selected_option = interaction.data['values'][0]
        if selected_option == "unequip":
            selected_item_name = self.default_item
            self.parent_view.status_message = f"✅ {self.display_name}을(를) 해제했습니다."
        else:
            selected_item_name = selected_option
            self.parent_view.status_message = f"✅ 장비를 **{selected_item_name}**(으)로 변경했습니다."
        await set_user_gear(self.user.id, **{self.gear_key: selected_item_name})
        await self.go_back_to_profile(interaction, reload_data=True)

    async def back_callback(self, interaction: discord.Interaction):
        await self.go_back_to_profile(interaction)

    async def go_back_to_profile(self, interaction: discord.Interaction, reload_data: bool = False):
        self.parent_view.current_page = "gear"
        await self.parent_view.update_display(interaction, reload_data=reload_data)

class UserProfilePanelView(ui.View):
    def __init__(self, cog_instance: 'UserProfile'):
        super().__init__(timeout=None)
        self.cog = cog_instance
        profile_button = ui.Button(label="소지품 보기", style=discord.ButtonStyle.primary, emoji="📦", custom_id="user_profile_open_button")
        profile_button.callback = self.open_profile
        self.add_item(profile_button)

    async def open_profile(self, interaction: discord.Interaction):
        view = ProfileView(interaction.user, self.cog)
        await view.build_and_send(interaction)

class UserProfile(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def register_persistent_views(self):
        self.bot.add_view(UserProfilePanelView(self))

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_profile"):
        panel_name = panel_key.replace("panel_", "")
        if (panel_info := get_panel_id(panel_name)):
            if (old_channel_id := panel_info.get("channel_id")) and (old_channel := self.bot.get_channel(old_channel_id)):
                try:
                    old_message = await old_channel.fetch_message(panel_info["message_id"])
                    await old_message.delete()
                except (discord.NotFound, discord.Forbidden): pass
        
        if not (embed_data := await get_embed_from_db(panel_key)): 
            logger.warning(f"DB에서 '{panel_key}' 임베드 데이터를 찾지 못해 패널 생성을 건너뜁니다.")
            return
            
        embed = discord.Embed.from_dict(embed_data)
        view = UserProfilePanelView(self)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_name, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} 패널을 성공적으로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(UserProfile(bot))
