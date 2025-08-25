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

GEAR_CATEGORY = "装備"
BAIT_CATEGORY = "エサ"

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
        # [✅✅✅ 핵심 수정] get_string 대신 get_config를 사용하여 전체 UI 텍스트 딕셔너리를 가져옵니다.
        all_ui_strings = get_config("strings", {})
        # 가져온 딕셔너리에서 profile_view.tabs 경로로 직접 접근합니다.
        tabs_config = all_ui_strings.get("profile_view", {}).get("tabs", [])
        return next((tab for tab in tabs_config if tab.get("key") == self.current_page), {})

    async def build_embed(self) -> discord.Embed:
        inventory = self.cached_data.get("inventory", {})
        gear = self.cached_data.get("gear", {})
        balance = self.cached_data.get("wallet", {}).get('balance', 0)
        item_db = get_item_database()
        
        # [✅ 수정] 여기도 get_config를 통해 텍스트를 가져오도록 수정합니다.
        all_ui_strings = get_config("strings", {})
        profile_strings = all_ui_strings.get("profile_view", {})

        base_title = profile_strings.get("base_title", "{user_name}の持ち物").format(user_name=self.user.display_name)
        
        current_tab_config = self._get_current_tab_config()
        title_suffix = current_tab_config.get("title_suffix", "")

        embed = discord.Embed(title=f"{base_title}{title_suffix}", color=self.user.color or discord.Color.blue())
        if self.user.display_avatar:
            embed.set_thumbnail(url=self.user.display_avatar.url)
        description = ""
        if self.status_message:
            description += f"**{self.status_message}**\n\n"
        
        if self.current_page == "info":
            info_tab_strings = profile_strings.get("info_tab", {})
            embed.add_field(name=info_tab_strings.get("field_balance", "所持金"), value=f"`{balance:,}`{self.currency_icon}", inline=True)
            
            job_name = "一般住民"
            try:
                job_res = await supabase.table('user_jobs').select('jobs(job_name)').eq('user_id', self.user.id).maybe_single().execute()
                if job_res and job_res.data and job_res.data.get('jobs'):
                    job_name = job_res.data['jobs']['job_name']
            except Exception as e:
                logger.error(f"직업 정보 조회 중 오류 발생 (유저: {self.user.id}): {e}")
            embed.add_field(name="職業", value=f"`{job_name}`", inline=True)

            user_rank_mention = info_tab_strings.get("default_rank_name", "かけだし住民")
            
            job_system_config = get_config("JOB_SYSTEM_CONFIG", {})
            level_tier_roles = job_system_config.get("LEVEL_TIER_ROLES", [])
            
            sorted_tier_roles = sorted(level_tier_roles, key=lambda x: x.get('level', 0), reverse=True)
            
            user_role_ids = {role.id for role in self.user.roles}
            
            for tier in sorted_tier_roles:
                role_key = tier.get('role_key')
                if not role_key: continue
                
                if (rank_role_id := get_id(role_key)) and rank_role_id in user_role_ids:
                    if rank_role := self.user.guild.get_role(rank_role_id):
                        user_rank_mention = rank_role.mention
                        break
            
            embed.add_field(name=info_tab_strings.get("field_rank", "等級"), value=user_rank_mention, inline=True)
            
            description += info_tab_strings.get("description", "下のタブを選択して、詳細情報を確認できます。")
            embed.description = description
        
        elif self.current_page == "item":
            excluded_categories = [GEAR_CATEGORY, "農場_種", "農場_作物", BAIT_CATEGORY]
            general_items = {name: count for name, count in inventory.items() if item_db.get(name, {}).get('category') not in excluded_categories}
            item_list = [f"{item_db.get(n,{}).get('emoji','📦')} **{n}**: `{c}`個" for n, c in general_items.items()]
            embed.description = description + ("\n".join(item_list) or profile_strings.get("item_tab", {}).get("no_items", "所持しているアイテムがありません。"))
        
        elif self.current_page == "gear":
            gear_categories = {"釣り": {"rod": "🎣 釣竿", "bait": "🐛 エサ"}, "農場": {"hoe": "🪓 クワ", "watering_can": "💧 じょうろ"}}
            for category_name, items in gear_categories.items():
                field_lines = [f"**{label}:** `{gear.get(key, BARE_HANDS)}`" for key, label in items.items()]
                embed.add_field(name=f"**[ 現在の装備: {category_name} ]**", value="\n".join(field_lines), inline=False)
            owned_gear_items = {name: count for name, count in inventory.items() if item_db.get(name, {}).get('category') == GEAR_CATEGORY}
            if owned_gear_items:
                gear_list = [f"{item_db.get(n,{}).get('emoji','🔧')} **{n}**: `{c}`個" for n, c in owned_gear_items.items()]
                embed.add_field(name="\n**[ 所持している装備 ]**", value="\n".join(gear_list), inline=False)
            else:
                embed.add_field(name="\n**[ 所持している装備 ]**", value=profile_strings.get("gear_tab", {}).get("no_owned_gear", "所持している装備がありません。"), inline=False)
            embed.description = description
        
        elif self.current_page == "fish":
            fish_tab_strings = profile_strings.get("fish_tab", {})
            aquarium = self.cached_data.get("aquarium", [])
            if not aquarium:
                embed.description = description + fish_tab_strings.get("no_fish", "水槽に魚がいません。")
            else:
                total_pages = math.ceil(len(aquarium) / 10)
                self.fish_page_index = max(0, min(self.fish_page_index, total_pages - 1))
                fish_on_page = aquarium[self.fish_page_index * 10 : self.fish_page_index * 10 + 10]
                embed.description = description + "\n".join([f"{f['emoji']} **{f['name']}**: `{f['size']}`cm" for f in fish_on_page])
                embed.set_footer(text=fish_tab_strings.get("pagination_footer", "ページ {current_page} / {total_pages}").format(current_page=self.fish_page_index + 1, total_pages=total_pages))
        
        elif self.current_page == "seed":
            seed_items = {name: count for name, count in inventory.items() if item_db.get(name, {}).get('category') == "農場_種"}
            item_list = [f"{item_db.get(n,{}).get('emoji','🌱')} **{n}**: `{c}`個" for n, c in seed_items.items()]
            embed.description = description + ("\n".join(item_list) or profile_strings.get("seed_tab", {}).get("no_items", "所持している種がありません。"))
        
        elif self.current_page == "crop":
            crop_items = {name: count for name, count in inventory.items() if item_db.get(name, {}).get('category') == "農場_作物"}
            item_list = [f"{item_db.get(n,{}).get('emoji','🌾')} **{n}**: `{c}`個" for n, c in crop_items.items()]
            embed.description = description + ("\n".join(item_list) or profile_strings.get("crop_tab", {}).get("no_items", "所持している作物がありません。"))
        
        else:
            embed.description = description + profile_strings.get("wip_tab", {}).get("description", "この機能は現在準備中です。")
        return embed

    def build_components(self):
        self.clear_items()
        all_ui_strings = get_config("strings", {})
        profile_strings = all_ui_strings.get("profile_view", {})
        tabs_config = profile_strings.get("tabs", [])
        
        row_counter, tab_buttons_in_row = 0, 0
        for config in tabs_config:
            key = config.get("key")
            if not key: continue

            if tab_buttons_in_row >= 5:
                row_counter += 1
                tab_buttons_in_row = 0
            style = discord.ButtonStyle.primary if self.current_page == key else discord.ButtonStyle.secondary
            self.add_item(ui.Button(label=config.get("label"), style=style, custom_id=f"profile_tab_{key}", emoji=config.get("emoji"), row=row_counter))
            tab_buttons_in_row += 1
        
        row_counter += 1
        if self.current_page == "gear":
            self.add_item(ui.Button(label="釣竿を変更", style=discord.ButtonStyle.blurple, custom_id="profile_change_rod", emoji="🎣", row=row_counter))
            self.add_item(ui.Button(label="エサを変更", style=discord.ButtonStyle.blurple, custom_id="profile_change_bait", emoji="🐛", row=row_counter))
            
            row_counter += 1
            self.add_item(ui.Button(label="クワを変更", style=discord.ButtonStyle.success, custom_id="profile_change_hoe", emoji="🪓", row=row_counter))
            self.add_item(ui.Button(label="じょうろを変更", style=discord.ButtonStyle.success, custom_id="profile_change_watering_can", emoji="💧", row=row_counter))
        
        row_counter += 1
        if self.current_page == "fish" and self.cached_data.get("aquarium"):
            if math.ceil(len(self.cached_data["aquarium"]) / 10) > 1:
                total_pages = math.ceil(len(self.cached_data["aquarium"]) / 10)
                pagination_buttons = profile_strings.get("pagination_buttons", {})
                self.add_item(ui.Button(label=pagination_buttons.get("prev", "◀"), custom_id="profile_fish_prev", disabled=self.fish_page_index == 0, row=row_counter))
                self.add_item(ui.Button(label=pagination_buttons.get("next", "▶"), custom_id="profile_fish_next", disabled=self.fish_page_index >= total_pages - 1, row=row_counter))
        
        for child in self.children:
            if isinstance(child, ui.Button):
                child.callback = self.button_callback
                
    async def button_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("自分専用のメニューを操作してください。", ephemeral=True)
            return
        
        custom_id = interaction.data['custom_id']
        if custom_id.startswith("profile_tab_"):
            self.current_page = custom_id.split("_")[-1]
            if self.current_page == 'fish': self.fish_page_index = 0
            await self.update_display(interaction, reload_data=False) 
        elif custom_id.startswith("profile_change_"):
            gear_type = custom_id.replace("profile_change_", "", 1)
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
        
        GEAR_SETTINGS = {
            "rod":          (GEAR_CATEGORY, "釣竿", "釣竿を外す", BARE_HANDS),
            "hoe":          (GEAR_CATEGORY, "クワ", "クワを外す", BARE_HANDS),
            "watering_can": (GEAR_CATEGORY, "じょうろ", "じょうろを外す", BARE_HANDS),
            "bait":         (BAIT_CATEGORY, "釣りエサ", "エサを外す", "エサなし")
        }
        
        settings = GEAR_SETTINGS.get(self.gear_type)
        if settings:
            self.db_category, self.category_name, self.unequip_label, self.default_item = settings
        else:
            self.db_category, self.category_name, self.unequip_label, self.default_item = ("不明", "不明", "外す", "なし")

    async def setup_and_update(self, interaction: discord.Interaction):
        await interaction.response.defer()
        inventory, item_db = self.parent_view.cached_data.get("inventory", {}), get_item_database()
        
        all_ui_strings = get_config("strings", {})
        gear_select_strings = all_ui_strings.get("profile_view", {}).get("gear_select_view", {})

        options = [discord.SelectOption(label=f'{gear_select_strings.get("unequip_prefix", "✋")} {self.unequip_label}', value="unequip")]
        
        for name, count in inventory.items():
            item_data = item_db.get(name)
            if item_data and item_data.get('category') == self.db_category and item_data.get('gear_type') == self.gear_type:
                 options.append(discord.SelectOption(label=f"{name} ({count}個)", value=name, emoji=item_data.get('emoji')))

        select = ui.Select(placeholder=gear_select_strings.get("placeholder", "{category_name}を選択...").format(category_name=self.category_name), options=options)
        select.callback = self.select_callback
        self.add_item(select)

        back_button = ui.Button(label=gear_select_strings.get("back_button", "戻る"), style=discord.ButtonStyle.grey, row=1)
        back_button.callback = self.back_callback
        self.add_item(back_button)

        embed = discord.Embed(
            title=gear_select_strings.get("embed_title", "{category_name} 変更").format(category_name=self.category_name), 
            description=gear_select_strings.get("embed_description", "装備したいアイテムを選択してください。"), 
            color=self.user.color
        )
        await interaction.edit_original_response(embed=embed, view=self)

    async def select_callback(self, interaction: discord.Interaction):
        selected_option = interaction.data['values'][0]
        if selected_option == "unequip":
            selected_item_name = self.default_item
            self.parent_view.status_message = f"✅ {self.category_name}を外しました。"
        else:
            selected_item_name = selected_option
            self.parent_view.status_message = f"✅ 装備を**{selected_item_name}**に変更しました。"
        await set_user_gear(str(self.user.id), **{self.gear_type: selected_item_name})
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
        profile_button = ui.Button(label="持ち物を見る", style=discord.ButtonStyle.primary, emoji="📦", custom_id="user_profile_open_button")
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
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。 (チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(UserProfile(bot))
