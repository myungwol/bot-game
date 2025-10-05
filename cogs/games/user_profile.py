# cogs/games/user_profile.py

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
import math
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone, timedelta
from utils.helpers import coerce_item_emoji

from utils.database import (
    get_inventory, get_wallet, get_aquarium, set_user_gear, get_user_gear,
    save_panel_id, get_panel_id, get_id, get_embed_from_db,
    get_item_database, get_config, get_string, BARE_HANDS,
    supabase, get_farm_data, expand_farm_db, update_inventory, save_config_to_db,
    open_boss_chest, update_wallet, add_xp_to_pet_db,
    clear_user_ability_cache # 💡 clear_user_ability_cache 임포트 추가
)
import time # time 모듈 import 추가
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

class ReasonModal(ui.Modal):
    def __init__(self, item_name: str):
        super().__init__(title="イベント優先参加券の使用")
        self.reason_input = ui.TextInput(label="イベント様式", placeholder="イベント様式を記入して送信してください。", style=discord.TextStyle.paragraph)
        self.add_item(self.reason_input); self.reason: Optional[str] = None
    async def on_submit(self, interaction: discord.Interaction):
        self.reason = self.reason_input.value; await interaction.response.defer(ephemeral=True); self.stop()

class ItemUsageView(ui.View):
    def __init__(self, parent_view: 'ProfileView'):
        super().__init__(timeout=180); self.parent_view = parent_view; self.user = parent_view.user; self.message: Optional[discord.WebhookMessage] = None
    async def get_item_name_by_id_key(self, id_key: str) -> Optional[str]:
        try: res = await supabase.table('items').select('name').eq('id_key', id_key).single().execute(); return res.data.get('name') if res.data else None
        except Exception: return None
    async def _update_warning_roles(self, member: discord.Member, total_count: int):
        guild = member.guild; warning_thresholds = get_config("WARNING_THRESHOLDS", [])
        if not warning_thresholds: logger.error("DB에서 WARNING_THRESHOLDS 설정을 찾을 수 없어 역할 업데이트를 건너뜁니다."); return
        all_warning_role_ids = {get_id(t['role_key']) for t in warning_thresholds if get_id(t['role_key'])}
        current_warning_roles = [role for role in member.roles if role.id in all_warning_role_ids]
        target_role_id = None
        for threshold in sorted(warning_thresholds, key=lambda x: x['count'], reverse=True):
            if total_count >= threshold['count']: target_role_id = get_id(threshold['role_key']); break
        target_role = guild.get_role(target_role_id) if target_role_id else None
        try:
            roles_to_add = [target_role] if target_role and target_role not in current_warning_roles else []; roles_to_remove = [role for role in current_warning_roles if not target_role or role.id != target_role.id]
            if roles_to_add: await member.add_roles(*roles_to_add, reason=f"累積警告{total_count}回達成（アイテム使用）")
            if roles_to_remove: await member.remove_roles(*roles_to_remove, reason="警告役職更新（アイテム使用）")
        except discord.Forbidden: logger.error(f"경고 역할 업데이트 실패: {member.display_name}님의 역할을 변경할 권한이 없습니다.")
        except Exception as e: logger.error(f"경고 역할 업데이트 중 오류: {e}", exc_info=True)
        
    async def on_item_select(self, interaction: discord.Interaction):
        selected_item_key = interaction.data["values"][0]
        usable_items_config = get_config("USABLE_ITEMS", {})
        item_info = usable_items_config.get(selected_item_key)
        
        if not item_info:
            await interaction.response.defer()
            self.parent_view.status_message = get_string("profile_view.item_usage_view.error_invalid_item")
            return await self.on_back(interaction, reload_data=True)
            
        item_name = item_info.get("name")
        if not item_name:
            await interaction.response.defer()
            self.parent_view.status_message = "❌ アイテム情報を設定で見つけられませんでした。"
            return await self.on_back(interaction, reload_data=True)

        item_type = item_info.get("type")

        # --- 보물 상자 열기 로직 강화 ---
        if item_type == "open_chest":
            await interaction.response.defer()
            
            # 1. 수정된 open_boss_chest 함수를 호출합니다.
            chest_contents = await open_boss_chest(self.user.id, item_name)
            
            if not chest_contents:
                self.parent_view.status_message = "❌ 開けられる宝箱がないか、処理中にエラーが発生しました。"
                return await self.on_back(interaction, reload_data=True)

            # 2. 결과 메시지를 생성하고 표시합니다.
            coins = chest_contents.get("coins", 0)
            xp = chest_contents.get("xp", 0)
            items = chest_contents.get("items", {})

            # 2-1. 획득한 재화를 DB에 실제로 반영합니다.
            db_tasks = []

            # ▼▼▼▼▼ 핵심 추가 ▼▼▼▼▼
            # 사용한 보물 상자 아이템을 인벤토리에서 1개 차감합니다.
            db_tasks.append(update_inventory(self.user.id, item_name, -1))
            # ▲▲▲▲▲ 핵심 추가 ▲▲▲▲▲

            if coins > 0:
                db_tasks.append(update_wallet(self.user, coins))
            if xp > 0:
                # 새로 만든 헬퍼 함수를 사용하여 안전하게 펫 경험치 추가
                db_tasks.append(add_xp_to_pet_db(self.user.id, xp))
            for item, qty in items.items():
                db_tasks.append(update_inventory(self.user.id, item, qty))
            
            # DB 작업 실행
            results = await asyncio.gather(*db_tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, Exception):
                    logger.error(f"보물상자 보상 지급 중 DB 오류 발생: {res}", exc_info=True)
                    # 여기서 사용자에게 오류 메시지를 보내는 것을 고려할 수 있습니다.
                    
            # 2-2. 결과 임베드를 생성합니다.
            reward_lines = []
            if coins > 0: reward_lines.append(f"🪙 **コイン**: `{coins:,}`")
            if xp > 0: reward_lines.append(f"✨ **ペット経験値**: `{xp:,}`")
            if items:
                reward_lines.append("\n**獲得アイテム:**")
                for item, qty in items.items():
                    reward_lines.append(f"📦 {item}: `{qty}`個")
            
            result_embed = discord.Embed(
                title=f"🎁 {item_name} 開封結果",
                description="\n".join(reward_lines) if reward_lines else "箱は空でした。",
                color=0xFFD700
            )
            await interaction.followup.send(embed=result_embed, ephemeral=True)
            
            # 3. 펫 레벨업/진화 확인 요청을 DB에 보냅니다.
            if xp > 0:
                await save_config_to_db(f"pet_levelup_request_{self.user.id}", {"xp_added": xp, "timestamp": time.time()})
                await save_config_to_db(f"pet_evolution_check_request_{self.user.id}", time.time())
            
            # 4. プロ필 UI를 새로고침하여 상자가 사라진 것을 반영합니다.
            return await self.on_back(interaction, reload_data=True)
        if item_type == "consume_with_reason":
            if selected_item_key == "role_item_event_priority":
                if not get_config("event_priority_pass_active", False): await interaction.response.send_message("❌ 現在、優先参加券を使用できるイベントはありません。", ephemeral=True, delete_after=5); return
                if self.user.id in get_config("event_priority_pass_users", []): await interaction.response.send_message("❌ すでにこのイベントに優先参加券を使用しています。", ephemeral=True, delete_after=5); return
            modal = ReasonModal(item_name); await interaction.response.send_modal(modal); await modal.wait()
            if not modal.reason: return
            try:
                await self.log_item_usage(item_info, modal.reason); await update_inventory(self.user.id, item_name, -1)
                if selected_item_key == "role_item_event_priority":
                    used_users = get_config("event_priority_pass_users", []); used_users.append(self.user.id); await save_config_to_db("event_priority_pass_users", used_users)
                self.parent_view.status_message = get_string("profile_view.item_usage_view.consume_success", item_name=item_name)
            except Exception as e: logger.error(f"아이템 사용 처리 중 오류 (아이템: {selected_item_key}): {e}", exc_info=True); self.parent_view.status_message = get_string("profile_view.item_usage_view.error_generic")
            return await self.on_back(None, reload_data=True)
        elif item_type == "job_reset":
            await interaction.response.defer()
            try:
                await supabase.rpc('reset_user_job_and_abilities', {'p_user_id': self.user.id}).execute()
                await update_inventory(self.user.id, item_name, -1)
                await self.log_item_usage(item_info, f"'{item_name}'を使用して職業をリセットしました。")
                
                # ▼▼▼▼▼ 핵심 추가 ▼▼▼▼▼
                # 직업이 초기화되었으므로, 이전 능력 캐시를 삭제합니다.
                clear_user_ability_cache(self.user.id)
                # ▲▲▲▲▲ 핵심 추가 ▲▲▲▲▲
                
                if handler_cog := self.parent_view.cog.bot.get_cog("JobAndTierHandler"):
                    await handler_cog.trigger_advancement_check(self.user)
                    self.parent_view.status_message = f"✅ 職業がリセットされました。まもなく転職案内のスレッドが作成されます。"
                else:
                    self.parent_view.status_message = f"✅ 職業はリセットされましたが、転職システムが見つかりません。"
            except Exception as e:
                logger.error(f"직업 초기화 처리 중 오류: {e}", exc_info=True)
                self.parent_view.status_message = "❌ 職業のリセット中にエラーが発生しました。"
            return await self.on_back(interaction, reload_data=True)
        await interaction.response.defer()
        try:
            if item_type == "deduct_warning":
                current_warnings = (await supabase.rpc('get_total_warnings', {'p_user_id': self.user.id, 'p_guild_id': self.user.guild.id}).execute()).data
                if current_warnings <= 0: self.parent_view.status_message = "ℹ️ 減点する罰点がありません。アイテムを使用できません。"; return await self.on_back(interaction, reload_data=False)
                new_total = (await supabase.rpc('add_warning_and_get_total', {'p_guild_id': self.user.guild.id, 'p_user_id': self.user.id, 'p_moderator_id': self.user.id, 'p_reason': f"'{item_name}' アイテム使用", 'p_amount': -1}).execute()).data
                await update_inventory(self.user.id, item_name, -1); await self.log_item_usage(item_info, f"'{item_name}'を使用して罰点を1回減点しました。(現在の罰点: {new_total}回)"); await self._update_warning_roles(self.user, new_total); self.parent_view.status_message = f"✅ '{item_name}'を使用しました。(現在の罰点: {new_total}回)"
            elif item_type == "farm_expansion":
                farm_data = await get_farm_data(self.user.id)
                if not farm_data: self.parent_view.status_message = get_string("profile_view.item_usage_view.farm_expand_fail_no_farm")
                else:
                    current_plots = len(farm_data.get('farm_plots', []))
                    if current_plots >= 25: self.parent_view.status_message = get_string("profile_view.item_usage_view.farm_expand_fail_max")
                    else:
                        if await expand_farm_db(farm_data['id'], current_plots):
                            await update_inventory(self.user.id, item_name, -1); self.parent_view.status_message = get_string("profile_view.item_usage_view.farm_expand_success", plot_count=current_plots + 1)
                            if farm_cog := self.parent_view.cog.bot.get_cog("Farm"): await farm_cog.request_farm_ui_update(self.user.id)
                        else: raise Exception("DB 농장 확장 실패")
        except Exception as e: logger.error(f"아이템 사용 처리 중 오류 (아이템: {selected_item_key}): {e}", exc_info=True); self.parent_view.status_message = get_string("profile_view.item_usage_view.error_generic")
        await self.on_back(interaction, reload_data=True)
        
    async def log_item_usage(self, item_info: dict, reason: str):
        if not (log_channel_key := item_info.get("log_channel_key")): return
        log_channel_id = get_id(log_channel_key)
        if not log_channel_id or not (log_channel := self.user.guild.get_channel(log_channel_id)): logger.warning(f"'{log_channel_key}'에 해당하는 로그 채널을 찾을 수 없습니다."); return
        log_embed_key = item_info.get("log_embed_key", "log_item_use"); embed_data = await get_embed_from_db(log_embed_key)
        if not embed_data: logger.warning(f"DB에서 '{log_embed_key}' 임베드를 찾을 수 없습니다."); return
        embed = format_embed_from_db(embed_data, user_mention=self.user.mention); item_display_name = item_info.get('name', '不明なアイテム')
        if item_info.get("type") == "consume_with_reason": embed.title = f"{self.user.display_name}さんが{item_display_name}を使用しました。"; embed.add_field(name="イベント様式", value=reason, inline=False)
        else: embed.description=f"{self.user.mention}さんが**'{item_display_name}'**を使用しました。"; embed.add_field(name="処理内容", value=reason, inline=False)
        embed.set_author(name=self.user.display_name, icon_url=self.user.display_avatar.url if self.user.display_avatar else None); await log_channel.send(embed=embed)
        
    async def on_back(self, interaction: Optional[discord.Interaction], reload_data: bool = False):
        await self.parent_view.update_display(interaction, reload_data=reload_data)

class ProfileView(ui.View):
    def __init__(self, user: discord.Member, cog_instance: 'UserProfile'):
        super().__init__(timeout=300); self.user: discord.Member = user; self.cog = cog_instance; self.message: Optional[discord.WebhookMessage] = None
        self.currency_icon = get_config("GAME_CONFIG", {}).get("CURRENCY_ICON", "🪙"); self.current_page = "info"; self.fish_page_index = 0
        self.cached_data = {}; self.status_message: Optional[str] = None

    async def build_and_send(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True); await self.load_data(self.user)
        embed = await self.build_embed(); self.build_components()
        self.message = await interaction.followup.send(embed=embed, view=self, ephemeral=True)

    async def update_display(self, interaction: Optional[discord.Interaction], reload_data: bool = False):
        if interaction and not interaction.response.is_done(): await interaction.response.defer()
        if reload_data: await self.load_data(self.user)
        embed = await self.build_embed(); self.build_components()
        target_message_editor = interaction.edit_original_response if interaction else (self.message.edit if self.message else None)
        if target_message_editor:
            try: await target_message_editor(embed=embed, view=self)
            except discord.NotFound: logger.warning("프로필 메시지를 수정하려 했으나 찾을 수 없습니다.")
        self.status_message = None
        
    async def load_data(self, user: discord.Member):
        wallet_data, inventory, aquarium, gear = await asyncio.gather(get_wallet(user.id), get_inventory(user), get_aquarium(str(user.id)), get_user_gear(user))
        self.cached_data = {"wallet": wallet_data, "inventory": inventory, "aquarium": aquarium, "gear": gear}

    def _get_current_tab_config(self) -> Dict:
        # ▼▼▼ [핵심 수정] 새로운 버튼 순서에 맞춰 strings.json 키 경로를 사용하도록 변경 ▼▼▼
        tabs_config = get_string("profile_view.tabs", [])
        return next((tab for tab in tabs_config if tab.get("key") == self.current_page), {})
        # ▲▲▲ 수정 완료 ▲▲▲

    async def build_embed(self) -> discord.Embed:
        inventory = self.cached_data.get("inventory", {}); gear = self.cached_data.get("gear", {}); balance = self.cached_data.get("wallet", {}).get('balance', 0)
        item_db = get_item_database(); base_title = get_string("profile_view.base_title", "{user_name}の所持品", user_name=self.user.display_name)
        
        # ▼▼▼ [핵심 수정] 탭 설정 가져오기 및 제목 변경 ▼▼▼
        tab_config = self._get_current_tab_config()
        title_suffix = tab_config.get("title_suffix", "")
        # ▲▲▲ 수정 완료 ▲▲▲

        embed = discord.Embed(title=f"{base_title}{title_suffix}", color=self.user.color or discord.Color.blue())
        if self.user.display_avatar: embed.set_thumbnail(url=self.user.display_avatar.url)
        description = f"**{self.status_message}**\n\n" if self.status_message else ""
        
        # ▼▼▼ [핵심 수정] 모든 탭에 대한 로직을 통합 및 재구성 ▼▼▼
        category_map = {
            "item": (["アイテム", "入場券"], "📦"), # 'アイテム' 탭이 '入場券'도 포함
            "gear": None, 
            "fish": None, 
            "seed": (["농장_씨앗"], "🌱"),
            "crop": (["농장_작물"], "🌾"), 
            "mineral": (["광물"], "💎"), 
            "food": (["요리"], "🍲"), 
            "loot": (["전리품"], "🏆"), 
            "pet": (["ペットアイテム", "卵"], "🐾") # 'ペットアイテム' 탭이 '卵'도 포함
        }
        
        if self.current_page == "info":
            embed.add_field(name=get_string("profile_view.info_tab.field_balance", "所持金"), value=f"`{balance:,}`{self.currency_icon}", inline=True)
            job_mention = "`なし`"; job_role_map = get_config("JOB_SYSTEM_CONFIG", {}).get("JOB_ROLE_MAP", {})
            try:
                job_res = await supabase.table('user_jobs').select('jobs(job_key, job_name)').eq('user_id', self.user.id).maybe_single().execute()
                if job_res and job_res.data and job_res.data.get('jobs'):
                    job_info = job_res.data['jobs']
                    if (role_key := job_role_map.get(job_info['job_key'])) and (role_id := get_id(role_key)):
                        job_mention = f"<@&{role_id}>"
            except Exception as e: logger.error(f"직업 정보 조회 중 오류 (유저: {self.user.id}): {e}")
            embed.add_field(name="職業", value=job_mention, inline=True)
            user_rank_mention = get_string("profile_view.info_tab.default_rank_name", "新入り住民")
            rank_roles_config = get_config("PROFILE_RANK_ROLES", []) 
            if rank_roles_config:
                user_role_ids = {role.id for role in self.user.roles}
                for rank_info in rank_roles_config:
                    if (role_key := rank_info.get("role_key")) and (rank_role_id := get_id(role_key)) and rank_role_id in user_role_ids:
                        user_rank_mention = f"<@&{rank_role_id}>"; break
            embed.add_field(name=get_string("profile_view.info_tab.field_rank", "等級"), value=user_rank_mention, inline=True)
            description += get_string("profile_view.info_tab.description", "下のタブを選択して詳細情報を確認してください。")
        elif self.current_page == "gear":
            gear_categories = {"釣り": {"rod": "釣り竿", "bait": "エサ"}, "農場": {"hoe": "クワ", "watering_can": "じょうろ"}, "鉱山": {"pickaxe": "ツルハシ"}}
            for category_name, items in gear_categories.items():
                field_lines = []
                for key, label in items.items():
                    item_name = gear.get(key, BARE_HANDS); item_data = item_db.get(item_name, {})
                    field_lines.append(f"{str(coerce_item_emoji(item_data.get('emoji', '')))} **{label}:** `{item_name}`")
                embed.add_field(name=f"**[ 現在の装備: {category_name} ]**", value="\n".join(field_lines), inline=False)
            equipped_gear_names = set(gear.values())
            owned_gear_items = {n: c for n, c in inventory.items() if item_db.get(n, {}).get('category') in ["装備", "エサ"] and n not in equipped_gear_names}
            if owned_gear_items:
                gear_list = [f"{str(coerce_item_emoji(item_db.get(n,{}).get('emoji','🔧')))} **{n}**: `{c}`個" for n, c in sorted(owned_gear_items.items())]
                embed.add_field(name="\n**[ 保有中の装備 ]**", value="\n".join(gear_list), inline=False)
            else:
                embed.add_field(name="\n**[ 保有中の装備 ]**", value=get_string("profile_view.gear_tab.no_owned_gear", "保有中の装備がありません。"), inline=False)
        elif self.current_page == "fish":
            aquarium = self.cached_data.get("aquarium", [])
            if not aquarium: description += get_string("profile_view.fish_tab.no_fish", "水槽に魚がいません。")
            else:
                total_pages = math.ceil(len(aquarium) / 10); self.fish_page_index = max(0, min(self.fish_page_index, total_pages - 1))
                fish_on_page = aquarium[self.fish_page_index * 10 : self.fish_page_index * 10 + 10]
                description += "\n".join([f"{str(coerce_item_emoji(f.get('emoji', '🐠')))} **{f['name']}**: `{f['size']}`cm" for f in fish_on_page])
                embed.set_footer(text=get_string("profile_view.fish_tab.pagination_footer", "ページ {current_page} / {total_pages}", current_page=self.fish_page_index + 1, total_pages=total_pages))
        elif self.current_page in category_map:
            category_info = category_map[self.current_page]
            if category_info:
                target_categories, default_emoji = category_info
                filtered_items = {n: c for n, c in inventory.items() if item_db.get(n, {}).get('category') in target_categories}
                
                if filtered_items:
                    item_list = [f"{str(coerce_item_emoji(item_db.get(n,{}).get('emoji', default_emoji)))} **{n}**: `{c}`個" for n, c in sorted(filtered_items.items())]
                    description += "\n".join(item_list)
                else:
                    # [버그 수정] 'loot' 같은 코드명 대신, 현재 탭의 표시 이름을 사용하도록 수정
                    tab_display_name = tab_config.get("label", self.current_page)
                    description += f"保有中の{tab_display_name}がありません。"
        
        embed.description = description
        return embed

    def build_components(self):
        self.clear_items()
        
        # ▼▼▼ [핵심 수정] 새로운 버튼 레이아웃 적용 ▼▼▼
        # DB의 strings 테이블에 저장된 순서와 설정을 그대로 따릅니다.
        tabs_config = get_string("profile_view.tabs", [])
        
        # 요청하신 레이아웃 (5개씩 2줄)
        layout_map = {0: 5, 1: 5} 
        current_row, buttons_in_row = 0, 0

        for config in tabs_config:
            key = config.get("key")
            if not key: continue

            # 레이아웃에 따라 행 자동 변경
            if buttons_in_row >= layout_map.get(current_row, 5):
                current_row += 1
                buttons_in_row = 0
            
            style = discord.ButtonStyle.primary if self.current_page == key else discord.ButtonStyle.secondary
            self.add_item(ui.Button(label=config.get("label"), style=style, custom_id=f"profile_tab_{key}", emoji=config.get("emoji"), row=current_row))
            buttons_in_row += 1
        
        # 맨 아래 줄에 기능 버튼 추가
        action_button_row = current_row + 1
        # ▲▲▲ 수정 완료 ▲▲▲
        
        if self.current_page == "item":
            self.add_item(ui.Button(label=get_string("profile_view.item_tab.use_item_button_label", "アイテムを使用"), style=discord.ButtonStyle.success, emoji="✨", custom_id="profile_use_item", row=action_button_row))
        if self.current_page == "gear":
            self.add_item(ui.Button(label="釣り竿変更", style=discord.ButtonStyle.blurple, custom_id="profile_change_rod", emoji="🎣", row=action_button_row))
            self.add_item(ui.Button(label="エサ変更", style=discord.ButtonStyle.blurple, custom_id="profile_change_bait", emoji="🐛", row=action_button_row))
            action_button_row += 1 # 다음 줄로
            self.add_item(ui.Button(label="クワ変更", style=discord.ButtonStyle.success, custom_id="profile_change_hoe", emoji="🪓", row=action_button_row))
            self.add_item(ui.Button(label="じょうろ変更", style=discord.ButtonStyle.success, custom_id="profile_change_watering_can", emoji="💧", row=action_button_row))
            self.add_item(ui.Button(label="ツルハシ変更", style=discord.ButtonStyle.secondary, custom_id="profile_change_pickaxe", emoji="⛏️", row=action_button_row))
        if self.current_page == "fish" and self.cached_data.get("aquarium"):
            total_pages = math.ceil(len(self.cached_data["aquarium"]) / 10)
            if total_pages > 1:
                self.add_item(ui.Button(label=get_string("profile_view.pagination_buttons.prev", "◀"), custom_id="profile_fish_prev", disabled=self.fish_page_index == 0, row=action_button_row))
                self.add_item(ui.Button(label=get_string("profile_view.pagination_buttons.next", "▶"), custom_id="profile_fish_next", disabled=self.fish_page_index >= total_pages - 1, row=action_button_row))
        
        for child in self.children:
            if isinstance(child, ui.Button): child.callback = self.button_callback
                
    async def button_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("自分専用のメニューを操作してください。", ephemeral=True, delete_after=5)
        
        custom_id = interaction.data['custom_id']
        if custom_id.startswith("profile_tab_"):
            self.current_page = custom_id.split("_")[-1]
            if self.current_page == 'fish': self.fish_page_index = 0
            await self.update_display(interaction) 
        elif custom_id == "profile_use_item":
            usage_view = ItemUsageView(self)
            usable_items_config = get_config("USABLE_ITEMS", {})
            user_inventory = await get_inventory(self.user); item_db = get_item_database()
            owned_usable_items = []
            for item_name, quantity in user_inventory.items():
                if quantity <= 0: continue
                item_data_from_db = item_db.get(item_name)
                if not item_data_from_db: continue
                if (item_id_key := item_data_from_db.get('id_key')) and item_id_key in usable_items_config:
                    item_info_from_config = usable_items_config[item_id_key]
                    owned_usable_items.append({ "key": item_id_key, "name": item_info_from_config.get('name', item_name), "description": item_info_from_config.get('description', '説明なし') })
            if not owned_usable_items:
                return await interaction.response.send_message(get_string("profile_view.item_usage_view.no_usable_items"), ephemeral=True, delete_after=5)
            options = [discord.SelectOption(label=item["name"], value=item["key"], description=item["description"]) for item in owned_usable_items]
            select = ui.Select(placeholder=get_string("profile_view.item_usage_view.select_placeholder"), options=options); select.callback = usage_view.on_item_select; usage_view.add_item(select)
            back_button = ui.Button(label=get_string("profile_view.item_usage_view.back_button"), style=discord.ButtonStyle.grey); back_button.callback = usage_view.on_back; usage_view.add_item(back_button)
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
        self.parent_view = parent_view; self.user = parent_view.user; self.gear_key = gear_key 
        settings = { "rod": {"display_name": "釣り竿", "gear_type_db": "釣り竿", "unequip_label": "釣り竿を外す", "default_item": BARE_HANDS}, "bait": {"display_name": "釣りエサ", "gear_type_db": "エサ", "unequip_label": "エサを外す", "default_item": "エサなし"}, "pickaxe": {"display_name": "ツルハシ", "gear_type_db": "ツルハシ", "unequip_label": "ツルハシを外す", "default_item": BARE_HANDS}, "hoe": {"display_name": "クワ", "gear_type_db": "クワ", "unequip_label": "クワを外す", "default_item": BARE_HANDS}, "watering_can": {"display_name": "じょうろ", "gear_type_db": "じょうろ", "unequip_label": "じょうろを外す", "default_item": BARE_HANDS} }.get(self.gear_key)
        if settings: self.display_name, self.gear_type_db, self.unequip_label, self.default_item = settings["display_name"], settings["gear_type_db"], settings["unequip_label"], settings["default_item"]
        else: self.display_name, self.gear_type_db, self.unequip_label, self.default_item = ("不明", "", "外す", "なし")
    async def setup_and_update(self, interaction: discord.Interaction):
        await interaction.response.defer()
        inventory, item_db = self.parent_view.cached_data.get("inventory", {}), get_item_database()
        options = [discord.SelectOption(label=f'{get_string("profile_view.gear_select_view.unequip_prefix", "✋")} {self.unequip_label}', value="unequip")]
        for name, count in inventory.items():
            item_data = item_db.get(name)
            if item_data and item_data.get('gear_type') == self.gear_type_db:
                 options.append(discord.SelectOption(label=f"{name} ({count}個)", value=name, emoji=coerce_item_emoji(item_data.get('emoji'))))
        select = ui.Select(placeholder=get_string("profile_view.gear_select_view.placeholder", "{category_name} 選択...", category_name=self.display_name), options=options); select.callback = self.select_callback; self.add_item(select)
        back_button = ui.Button(label=get_string("profile_view.gear_select_view.back_button", "戻る"), style=discord.ButtonStyle.grey, row=1); back_button.callback = self.back_callback; self.add_item(back_button)
        embed = discord.Embed(title=get_string("profile_view.gear_select_view.embed_title", "{category_name} 変更", category_name=self.display_name), description=get_string("profile_view.gear_select_view.embed_description", "装着するアイテムを選択してください。"), color=self.user.color)
        await interaction.edit_original_response(embed=embed, view=self)
    async def select_callback(self, interaction: discord.Interaction):
        selected_option = interaction.data['values'][0]
        if selected_option == "unequip": selected_item_name = self.default_item; self.parent_view.status_message = f"✅ {self.display_name}を外しました。"
        else: selected_item_name = selected_option; self.parent_view.status_message = f"✅ 装備を**{selected_item_name}**に変更しました。"
        await set_user_gear(self.user.id, **{self.gear_key: selected_item_name}); await self.go_back_to_profile(interaction, reload_data=True)
    async def back_callback(self, interaction: discord.Interaction): await self.go_back_to_profile(interaction)
    async def go_back_to_profile(self, interaction: discord.Interaction, reload_data: bool = False):
        self.parent_view.current_page = "gear"; await self.parent_view.update_display(interaction, reload_data=reload_data)

class UserProfilePanelView(ui.View):
    def __init__(self, cog_instance: 'UserProfile'):
        super().__init__(timeout=None); self.cog = cog_instance
        profile_button = ui.Button(label="所持品を見る", style=discord.ButtonStyle.primary, emoji="📦", custom_id="user_profile_open_button"); profile_button.callback = self.open_profile; self.add_item(profile_button)
    async def open_profile(self, interaction: discord.Interaction):
        view = ProfileView(interaction.user, self.cog); await view.build_and_send(interaction)

class UserProfile(commands.Cog):
    def __init__(self, bot: commands.Bot): self.bot = bot
    async def register_persistent_views(self): self.bot.add_view(UserProfilePanelView(self))
    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_profile"):
        panel_name = panel_key.replace("panel_", "")
        if (panel_info := get_panel_id(panel_name)) and (old_channel_id := panel_info.get("channel_id")) and (old_channel := self.bot.get_channel(old_channel_id)):
            try: await (await old_channel.fetch_message(panel_info["message_id"])).delete()
            except (discord.NotFound, discord.Forbidden): pass
        if not (embed_data := await get_embed_from_db(panel_key)): logger.warning(f"DB에서 '{panel_key}' 임베드 데이터를 찾을 수 없어 패널 생성을 건너뜁니다."); return
        embed = discord.Embed.from_dict(embed_data); view = UserProfilePanelView(self)
        new_message = await channel.send(embed=embed, view=view); await save_panel_id(panel_name, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。(チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(UserProfile(bot))
