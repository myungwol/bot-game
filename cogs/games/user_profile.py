# cogs/games/user_profile.py

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
import math
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone, timedelta
from utils.helpers import coerce_item_emoji  # ← 추가

from utils.database import (
    get_inventory, get_wallet, get_aquarium, set_user_gear, get_user_gear,
    save_panel_id, get_panel_id, get_id, get_embed_from_db,
    get_item_database, get_config, get_string, BARE_HANDS,
    supabase, get_farm_data, expand_farm_db, update_inventory, save_config_to_db
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

GEAR_CATEGORY = "장비"
BAIT_CATEGORY = "미끼"
FARM_TOOL_CATEGORY = "장비"
        
class ReasonModal(ui.Modal):
    def __init__(self, item_name: str):
        super().__init__(title="이벤트 우선 참여권 사용")
        self.reason_input = ui.TextInput(
            label="이벤트 양식",
            placeholder="이벤트 양식을 적어서 보내주세요.",
            style=discord.TextStyle.paragraph
        )
        self.add_item(self.reason_input)
        self.reason: Optional[str] = None

    async def on_submit(self, interaction: discord.Interaction):
        self.reason = self.reason_input.value
        await interaction.response.defer(ephemeral=True)
        self.stop()

class ItemUsageView(ui.View):
    def __init__(self, parent_view: 'ProfileView'):
        super().__init__(timeout=180)
        self.parent_view = parent_view
        self.user = parent_view.user
        self.message: Optional[discord.WebhookMessage] = None
    
    async def get_item_name_by_id_key(self, id_key: str) -> Optional[str]:
        try:
            res = await supabase.table('items').select('name').eq('id_key', id_key).single().execute()
            return res.data.get('name') if res.data else None
        except Exception:
            return None

    async def _update_warning_roles(self, member: discord.Member, total_count: int):
        guild = member.guild
        warning_thresholds = get_config("WARNING_THRESHOLDS", [])
        if not warning_thresholds:
            logger.error("DB에서 WARNING_THRESHOLDS 설정을 찾을 수 없어 역할 업데이트를 건너뜁니다.")
            return

        all_warning_role_ids = {get_id(t['role_key']) for t in warning_thresholds if get_id(t['role_key'])}
        current_warning_roles = [role for role in member.roles if role.id in all_warning_role_ids]
        
        target_role_id = None
        for threshold in sorted(warning_thresholds, key=lambda x: x['count'], reverse=True):
            if total_count >= threshold['count']:
                target_role_id = get_id(threshold['role_key'])
                break
        
        target_role = guild.get_role(target_role_id) if target_role_id else None

        try:
            roles_to_add = [target_role] if target_role and target_role not in current_warning_roles else []
            roles_to_remove = [role for role in current_warning_roles if not target_role or role.id != target_role.id]
            
            if roles_to_add: await member.add_roles(*roles_to_add, reason=f"누적 경고 {total_count}회 달성 (아이템 사용)")
            if roles_to_remove: await member.remove_roles(*roles_to_remove, reason="경고 역할 업데이트 (아이템 사용)")
        except discord.Forbidden:
            logger.error(f"경고 역할 업데이트 실패: {member.display_name}님의 역할을 변경할 권한이 없습니다.")
        except Exception as e:
            logger.error(f"경고 역할 업데이트 중 오류: {e}", exc_info=True)

    async def on_item_select(self, interaction: discord.Interaction):
        selected_item_key = interaction.data["values"][0]
        
        usable_items_config = get_config("USABLE_ITEMS", {})
        item_info = usable_items_config.get(selected_item_key)

        if not item_info:
            await interaction.response.defer()
            self.parent_view.status_message = get_string("profile_view.item_usage_view.error_invalid_item")
            return await self.on_back(interaction, reload_data=True)
            
        item_name_from_db = await self.get_item_name_by_id_key(selected_item_key)
        if not item_name_from_db:
            await interaction.response.defer()
            self.parent_view.status_message = "❌ 아이템 정보를 DB에서 찾을 수 없습니다."
            return await self.on_back(interaction, reload_data=True)

        item_type = item_info.get("type")
        
        if item_type == "consume_with_reason":
            if selected_item_key == "role_item_event_priority":
                is_active = get_config("event_priority_pass_active", False)
                if not is_active:
                    await interaction.response.send_message("❌ 현재 우선 참여권을 사용할 수 있는 이벤트가 없습니다.", ephemeral=True, delete_after=5)
                    return

                used_users = get_config("event_priority_pass_users", [])
                if self.user.id in used_users:
                    await interaction.response.send_message("❌ 이미 이 이벤트에 우선 참여권을 사용했습니다.", ephemeral=True, delete_after=5)
                    return

            modal = ReasonModal(item_name_from_db)
            await interaction.response.send_modal(modal)
            await modal.wait()
            
            if not modal.reason: return
            
            try:
                await self.log_item_usage(item_info, modal.reason)
                await update_inventory(self.user.id, item_name_from_db, -1)
                
                if selected_item_key == "role_item_event_priority":
                    used_users.append(self.user.id)
                    await save_config_to_db("event_priority_pass_users", used_users)

                self.parent_view.status_message = get_string("profile_view.item_usage_view.consume_success", item_name=item_name_from_db)
            except Exception as e:
                logger.error(f"아이템 사용 처리 중 오류 (아이템: {selected_item_key}): {e}", exc_info=True)
                self.parent_view.status_message = get_string("profile_view.item_usage_view.error_generic")

            return await self.on_back(None, reload_data=True)
        
        elif item_type == "job_reset":
            await interaction.response.defer()
            try:
                await supabase.rpc('reset_user_job_and_abilities', {'p_user_id': self.user.id}).execute()
                await update_inventory(self.user.id, item_name_from_db, -1)
                await self.log_item_usage(item_info, f"'{item_name_from_db}'을(를) 사용하여 직업을 초기화했습니다.")

                if handler_cog := self.parent_view.cog.bot.get_cog("JobAndTierHandler"):
                    await handler_cog.trigger_advancement_check(self.user)
                    self.parent_view.status_message = f"✅ 직업이 초기화되었습니다. 곧 전직 안내 스레드가 생성됩니다."
                else:
                    self.parent_view.status_message = f"✅ 직업이 초기화되었지만, 전직 시스템을 찾을 수 없습니다."

            except Exception as e:
                logger.error(f"직업 초기화 처리 중 오류: {e}", exc_info=True)
                self.parent_view.status_message = "❌ 직업 초기화 중 오류가 발생했습니다."
            
            return await self.on_back(interaction, reload_data=True)

        await interaction.response.defer()
        try:
            if item_type == "deduct_warning":
                current_warnings_res = await supabase.rpc('get_total_warnings', {'p_user_id': self.user.id, 'p_guild_id': self.user.guild.id}).execute()
                current_warnings = current_warnings_res.data

                if current_warnings <= 0:
                    self.parent_view.status_message = "ℹ️ 차감할 벌점이 없습니다. 아이템을 사용할 수 없습니다."
                    return await self.on_back(interaction, reload_data=False)

                rpc_params = {'p_guild_id': self.user.guild.id, 'p_user_id': self.user.id, 'p_moderator_id': self.user.id, 'p_reason': f"'{item_name_from_db}' 아이템 사용", 'p_amount': -1}
                response = await supabase.rpc('add_warning_and_get_total', rpc_params).execute()
                new_total = response.data

                await update_inventory(self.user.id, item_name_from_db, -1)
                await self.log_item_usage(item_info, f"'{item_name_from_db}'을(를) 사용하여 벌점을 1회 차감했습니다. (현재 벌점: {new_total}회)")
                await self._update_warning_roles(self.user, new_total)
                self.parent_view.status_message = f"✅ '{item_name_from_db}'을(를) 사용했습니다. (현재 벌점: {new_total}회)"
            
            elif item_type == "farm_expansion":
                farm_data = await get_farm_data(self.user.id)
                if not farm_data:
                    self.parent_view.status_message = get_string("profile_view.item_usage_view.farm_expand_fail_no_farm")
                else:
                    current_plots = len(farm_data.get('farm_plots', []))
                    if current_plots >= 25:
                        self.parent_view.status_message = get_string("profile_view.item_usage_view.farm_expand_fail_max")
                    else:
                        success = await expand_farm_db(farm_data['id'], current_plots)
                        if success:
                            await update_inventory(self.user.id, item_name_from_db, -1)
                            self.parent_view.status_message = get_string("profile_view.item_usage_view.farm_expand_success", plot_count=current_plots + 1)
                            
                            if farm_cog := self.parent_view.cog.bot.get_cog("Farm"):
                                await farm_cog.request_farm_ui_update(self.user.id)
                        else:
                            raise Exception("DB 농장 확장 실패")
        except Exception as e:
            logger.error(f"아이템 사용 처리 중 오류 (아이템: {selected_item_key}): {e}", exc_info=True)
            self.parent_view.status_message = get_string("profile_view.item_usage_view.error_generic")

        await self.on_back(interaction, reload_data=True)

    async def log_item_usage(self, item_info: dict, reason: str):
        if not (log_channel_key := item_info.get("log_channel_key")): return

        log_channel_id = get_id(log_channel_key)
        if not log_channel_id or not (log_channel := self.user.guild.get_channel(log_channel_id)):
            logger.warning(f"'{log_channel_key}'에 해당하는 로그 채널을 찾을 수 없습니다.")
            return

        log_embed_key = item_info.get("log_embed_key", "log_item_use")
        embed_data = await get_embed_from_db(log_embed_key)
        if not embed_data:
            logger.warning(f"DB에서 '{log_embed_key}' 임베드를 찾을 수 없습니다.")
            return
        
        embed = format_embed_from_db(embed_data, user_mention=self.user.mention)
        item_display_name = item_info.get('name', '알 수 없는 아이템')
        
        if item_info.get("type") == "consume_with_reason":
            embed.title = f"{self.user.display_name}님이 {item_display_name}을(를) 사용했습니다."
            embed.add_field(name="이벤트 양식", value=reason, inline=False)
        else:
            embed.description=f"{self.user.mention}님이 **'{item_display_name}'**을(를) 사용했습니다."
            embed.add_field(name="처리 내용", value=reason, inline=False)
            
        embed.set_author(name=self.user.display_name, icon_url=self.user.display_avatar.url if self.user.display_avatar else None)
        await log_channel.send(embed=embed)

        async def on_back(self, interaction: Optional[discord.Interaction], reload_data: bool = False):
            await self.parent_view.update_display(interaction, reload_data=reload_data)


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

    async def update_display(self, interaction: Optional[discord.Interaction], reload_data: bool = False):
        if interaction and not interaction.response.is_done():
            await interaction.response.defer()

        if reload_data:
            await self.load_data(self.user)
            
        embed = await self.build_embed()
        self.build_components()

        target_message_editor = None
        if interaction:
            target_message_editor = interaction.edit_original_response
        elif self.message:
            target_message_editor = self.message.edit

        if target_message_editor:
            try:
                await target_message_editor(embed=embed, view=self)
            except discord.NotFound:
                logger.warning("프로필 메시지를 수정하려 했으나 찾을 수 없습니다.")
        
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
            job_mention = "`없음`"
            job_system_config = get_config("JOB_SYSTEM_CONFIG", {})
            job_role_map = job_system_config.get("JOB_ROLE_MAP", {})
            try:
                job_res = await supabase.table('user_jobs').select('jobs(job_key, job_name)').eq('user_id', self.user.id).maybe_single().execute()
                if job_res and job_res.data and job_res.data.get('jobs'):
                    job_info = job_res.data['jobs']
                    job_key, job_name = job_info['job_key'], job_info['job_name']
                    if (role_key := job_role_map.get(job_key)) and (role_id := get_id(role_key)):
                        job_mention = f"<@&{role_id}>"
                    else:
                        job_mention = f"`{job_name}`"
            except Exception as e:
                logger.error(f"직업 정보 조회 중 오류 발생 (유저: {self.user.id}): {e}")
            embed.add_field(name="직업", value=job_mention, inline=True)
            user_rank_mention = get_string("profile_view.info_tab.default_rank_name", "새내기 주민")
            rank_roles_config = get_config("PROFILE_RANK_ROLES", []) 
            if rank_roles_config:
                user_role_ids = {role.id for role in self.user.roles}
                for rank_info in rank_roles_config:
                    if (role_key := rank_info.get("role_key")) and (rank_role_id := get_id(role_key)) and rank_role_id in user_role_ids:
                        user_rank_mention = f"<@&{rank_role_id}>"; break
            embed.add_field(name=get_string("profile_view.info_tab.field_rank", "등급"), value=user_rank_mention, inline=True)
            description += get_string("profile_view.info_tab.description", "아래 탭을 선택하여 상세 정보를 확인하세요.")
            embed.description = description
        
        elif self.current_page == "item":
            general_items = {
                name: count for name, count in inventory.items() 
                if item_db.get(name, {}).get('category') == '아이템'
            }
            
            item_list = [f"{item_db.get(n,{}).get('emoji','📦')} **{n}**: `{c}`개" for n, c in general_items.items()]
            embed.description = description + ("\n".join(item_list) or get_string("profile_view.item_tab.no_items", "보유 중인 아이템이 없습니다."))
        
        elif self.current_page == "gear":
            gear_categories = {
                "낚시": {"rod": "🎣 낚싯대", "bait": "🐛 미끼"},
                "농장": {"hoe": "🪓 괭이", "watering_can": "💧 물뿌리개"},
                "광산": {"pickaxe": "⛏️ 곡괭이"}
            }
            for category_name, items in gear_categories.items():
                field_lines = [f"**{label}:** `{gear.get(key, BARE_HANDS)}`" for key, label in items.items()]
                embed.add_field(name=f"**[ 현재 장비: {category_name} ]**", value="\n".join(field_lines), inline=False)
            equipped_gear_names = set(gear.values())
            owned_gear_categories = [GEAR_CATEGORY, BAIT_CATEGORY]
            owned_gear_items = {name: count for name, count in inventory.items() if item_db.get(name, {}).get('category') in owned_gear_categories and name not in equipped_gear_names}
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
                
        elif self.current_page == "mineral":
            mineral_items = {name: count for name, count in inventory.items() if item_db.get(name, {}).get('category') == "광물"}
            item_list = [f"{item_db.get(n,{}).get('emoji','💎')} **{n}**: `{c}`개" for n, c in mineral_items.items()]
            embed.description = description + ("\n".join(item_list) or get_string("profile_view.mineral_tab.no_items", "보유 중인 광물이 없습니다."))
        
        elif self.current_page == "seed":
            seed_items = {name: count for name, count in inventory.items() if item_db.get(name, {}).get('category') == "농장_씨앗"}
            item_list = [f"{item_db.get(n,{}).get('emoji','🌱')} **{n}**: `{c}`개" for n, c in seed_items.items()]
            embed.description = description + ("\n".join(item_list) or get_string("profile_view.seed_tab.no_items", "보유 중인 씨앗이 없습니다."))
        
        elif self.current_page == "crop":
            crop_items = {name: count for name, count in inventory.items() if item_db.get(name, {}).get('category') == "농장_작물"}
            item_list = [f"{item_db.get(n,{}).get('emoji','🌾')} **{n}**: `{c}`개" for n, c in crop_items.items()]
            embed.description = description + ("\n".join(item_list) or get_string("profile_view.crop_tab.no_items", "보유 중인 작물이 없습니다."))

        elif self.current_page == "food":
            food_items = {name: count for name, count in inventory.items() if item_db.get(name, {}).get('category') == "요리"}
            item_list = [f"{item_db.get(n,{}).get('emoji','🍲')} **{n}**: `{c}`개" for n, c in food_items.items()]
            embed.description = description + ("\n".join(item_list) or get_string("profile_view.food_tab.no_items", "보유 중인 음식이 없습니다."))
        
        else:
            embed.description = description + get_string("profile_view.wip_tab.description", "이 기능은 현재 준비 중입니다.")
        return embed

    def build_components(self):
        self.clear_items()
        tabs_config = get_string("profile_view.tabs", [])
        
        row_counter, tab_buttons_in_row = 0, 0
        for config in tabs_config:
            if not (key := config.get("key")): continue
            if tab_buttons_in_row >= 5: row_counter += 1; tab_buttons_in_row = 0
            style = discord.ButtonStyle.primary if self.current_page == key else discord.ButtonStyle.secondary
            self.add_item(ui.Button(label=config.get("label"), style=style, custom_id=f"profile_tab_{key}", emoji=config.get("emoji"), row=row_counter))
            tab_buttons_in_row += 1
        
        row_counter += 1
        if self.current_page == "item":
            use_item_label = get_string("profile_view.item_tab.use_item_button_label", "아이템 사용")
            self.add_item(ui.Button(label=use_item_label, style=discord.ButtonStyle.success, emoji="✨", custom_id="profile_use_item", row=row_counter))

        if self.current_page == "gear":
            self.add_item(ui.Button(label="낚싯대 변경", style=discord.ButtonStyle.blurple, custom_id="profile_change_rod", emoji="🎣", row=row_counter))
            self.add_item(ui.Button(label="미끼 변경", style=discord.ButtonStyle.blurple, custom_id="profile_change_bait", emoji="🐛", row=row_counter))
            row_counter += 1
            self.add_item(ui.Button(label="괭이 변경", style=discord.ButtonStyle.success, custom_id="profile_change_hoe", emoji="🪓", row=row_counter))
            self.add_item(ui.Button(label="물뿌리개 변경", style=discord.ButtonStyle.success, custom_id="profile_change_watering_can", emoji="💧", row=row_counter))
            row_counter += 1
            self.add_item(ui.Button(label="곡괭이 변경", style=discord.ButtonStyle.secondary, custom_id="profile_change_pickaxe", emoji="⛏️", row=row_counter))
            
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
            return await interaction.response.send_message("자신 전용 메뉴를 조작해주세요.", ephemeral=True, delete_after=5)
        
        custom_id = interaction.data['custom_id']
        if custom_id.startswith("profile_tab_"):
            self.current_page = custom_id.split("_")[-1]
            if self.current_page == 'fish': self.fish_page_index = 0
            await self.update_display(interaction) 
            
        elif custom_id == "profile_use_item":
            usage_view = ItemUsageView(self)
            usable_items_config = get_config("USABLE_ITEMS", {})
            user_inventory = await get_inventory(self.user)
            item_db = get_item_database()
            owned_usable_items = []
            for item_name, quantity in user_inventory.items():
                if quantity <= 0: continue
                item_data_from_db = item_db.get(item_name)
                if not item_data_from_db: continue
                item_id_key = item_data_from_db.get('id_key')
                if item_id_key and item_id_key in usable_items_config:
                    item_info_from_config = usable_items_config[item_id_key]
                    owned_usable_items.append({ "key": item_id_key, "name": item_info_from_config.get('name', item_name), "description": item_info_from_config.get('description', '설명 없음') })
            if not owned_usable_items:
                await interaction.response.send_message(get_string("profile_view.item_usage_view.no_usable_items"), ephemeral=True, delete_after=5)
                return
            options = [discord.SelectOption(label=item["name"], value=item["key"], description=item["description"]) for item in owned_usable_items]
            select = ui.Select(placeholder=get_string("profile_view.item_usage_view.select_placeholder"), options=options)
            select.callback = usage_view.on_item_select
            usage_view.add_item(select)
            back_button = ui.Button(label=get_string("profile_view.item_usage_view.back_button"), style=discord.ButtonStyle.grey)
            back_button.callback = usage_view.on_back
            usage_view.add_item(back_button)
            embed = discord.Embed(title=get_string("profile_view.item_usage_view.embed_title"), description=get_string("profile_view.item_usage_view.embed_description"), color=discord.Color.gold())
            await interaction.response.edit_message(embed=embed, view=usage_view)

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
            "pickaxe":      {"display_name": "곡괭이", "gear_type_db": "곡괭이", "unequip_label": "곡괭이 해제", "default_item": BARE_HANDS},
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
             # ▼▼▼ [수정] 아래 줄을 변경합니다 ▼▼▼
             options.append(discord.SelectOption(label=f"{name} ({count}개)", value=name, emoji=coerce_item_emoji(item_data.get('emoji'))))

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
