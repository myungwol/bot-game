# game-bot/cogs/systems/LevelSystem.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
import time
import math
from typing import Optional, Dict, List, Any

# [수정] 게임 봇의 database와 helpers를 참조하도록 경로 변경
from utils.database import supabase, get_panel_id, save_panel_id, get_id, get_config, get_cooldown, set_cooldown
from utils.helpers import format_embed_from_db, calculate_xp_for_level

logger = logging.getLogger(__name__)

# --- Helper Functions ---
def create_xp_bar(current_xp: int, required_xp: int, length: int = 10) -> str:
    if required_xp <= 0: return "▓" * length
    progress = min(current_xp / required_xp, 1.0)
    filled_length = int(length * progress)
    bar = '▓' * filled_length + '░' * (length - filled_length)
    return f"[{bar}]"

# --- UI Views ---
class RankingView(ui.View):
    def __init__(self, user: discord.Member, total_users: int):
        super().__init__(timeout=180)
        self.user = user
        self.current_page = 0
        self.users_per_page = 10
        self.total_pages = math.ceil(total_users / self.users_per_page)

    async def update_view(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.build_embed()
        self.update_buttons()
        await interaction.edit_original_response(embed=embed, view=self)
        
    def update_buttons(self):
        prev_button = next((child for child in self.children if isinstance(child, ui.Button) and child.custom_id == "prev_page"), None)
        next_button = next((child for child in self.children if isinstance(child, ui.Button) and child.custom_id == "next_page"), None)
        
        if prev_button: prev_button.disabled = self.current_page == 0
        if next_button: next_button.disabled = self.current_page >= self.total_pages - 1

    async def build_embed(self) -> discord.Embed:
        offset = self.current_page * self.users_per_page
        res = await supabase.table('user_levels').select('user_id, level, xp', count='exact').order('xp', desc=True).range(offset, offset + self.users_per_page - 1).execute()

        embed = discord.Embed(title="👑 サーバーランキング", color=0xFFD700)
        
        rank_list = []
        if res and res.data:
            for i, user_data in enumerate(res.data):
                rank = offset + i + 1
                member = self.user.guild.get_member(int(user_data['user_id']))
                name = member.display_name if member else f"ID: {user_data['user_id']}"
                rank_list.append(f"`{rank}.` {name} - **Lv.{user_data['level']}** (`{user_data['xp']:,} XP`)")
        
        embed.description = "\n".join(rank_list) if rank_list else "まだランキング情報がありません。"
        embed.set_footer(text=f"ページ {self.current_page + 1} / {self.total_pages}")
        return embed

    @ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="prev_page", disabled=True)
    async def prev_page(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_page > 0: self.current_page -= 1
        await self.update_view(interaction)

    @ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="next_page")
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_page < self.total_pages - 1: self.current_page += 1
        await self.update_view(interaction)

    @ui.button(label="自分の順位へ", style=discord.ButtonStyle.primary, emoji="👤", custom_id="my_rank")
    async def go_to_my_rank(self, interaction: discord.Interaction, button: ui.Button):
        my_rank_res = await supabase.rpc('get_user_rank', {'p_user_id': self.user.id}).execute()
        if my_rank_res and my_rank_res.data:
            my_rank = my_rank_res.data
            self.current_page = (my_rank - 1) // self.users_per_page
            await self.update_view(interaction)
        else:
            await interaction.response.send_message("❌ 自分の順位情報を取得できませんでした。", ephemeral=True)


class LevelPanelView(ui.View):
    def __init__(self, cog_instance: 'LevelSystem'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    @ui.button(label="ステータス確認", style=discord.ButtonStyle.primary, emoji="📊", custom_id="level_check_button")
    async def check_level_button(self, interaction: discord.Interaction, button: ui.Button):
        user = interaction.user
        user_id_str = str(user.id)
        cooldown_key = "level_check_cooldown"
        cooldown_seconds = 60

        last_used = await get_cooldown(user_id_str, cooldown_key)
        if time.time() - last_used < cooldown_seconds:
            remaining = int(cooldown_seconds - (time.time() - last_used))
            await interaction.response.send_message(f"⏳ このボタンはクールダウン中です。あと`{remaining}`秒お待ちください。", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        try:
            await set_cooldown(user_id_str, cooldown_key)
            
            level_res_task = supabase.table('user_levels').select('*').eq('user_id', user.id).maybe_single().execute()
            job_res_task = supabase.table('user_jobs').select('jobs(*)').eq('user_id', user.id).maybe_single().execute()
            xp_logs_res_task = supabase.table('xp_logs').select('source, xp_amount').eq('user_id', user.id).execute()
            level_res, job_res, xp_logs_res = await asyncio.gather(level_res_task, job_res_task, xp_logs_res_task)

            user_level_data = level_res.data if level_res and level_res.data else {'level': 1, 'xp': 0}
            current_level, total_xp = user_level_data['level'], user_level_data['xp']

            xp_for_next_level = calculate_xp_for_level(current_level + 1)
            xp_at_level_start = calculate_xp_for_level(current_level)
            
            xp_in_current_level = total_xp - xp_at_level_start
            required_xp_for_this_level = xp_for_next_level - xp_at_level_start
            
            job_system_config = get_config("JOB_SYSTEM_CONFIG", {})
            job_name = "なし"
            job_role_mention = ""
            job_role_map = job_system_config.get("JOB_ROLE_MAP", {})
            if job_res and job_res.data and job_res.data.get('jobs'):
                job_data = job_res.data['jobs']
                job_name = job_data['job_name']
                if role_key := job_role_map.get(job_data['job_key']):
                    if role_id := get_id(role_key):
                        job_role_mention = f"<@&{role_id}>"
            level_tier_roles = job_system_config.get("LEVEL_TIER_ROLES", [])
            tier_role_mention = ""
            for tier in sorted(level_tier_roles, key=lambda x: x['level'], reverse=True):
                if current_level >= tier['level']:
                    if role_id := get_id(tier['role_key']):
                        tier_role_mention = f"<@&{role_id}>"
                        break
            
            source_map = {'chat': '💬 チャット', 'voice': '🎙️ VC参加', 'fishing': '🎣 釣り', 'farming': '🌾 農業'}
            aggregated_xp = {v: 0 for v in source_map.values()}
            if xp_logs_res and xp_logs_res.data:
                for log in xp_logs_res.data:
                    source_name = source_map.get(log['source'], log['source'])
                    if source_name in aggregated_xp:
                        aggregated_xp[source_name] += log['xp_amount']
            
            details = [f"> {source}: `{amount:,} XP`" for source, amount in aggregated_xp.items()]
            xp_details_text = "\n".join(details)
            
            xp_bar = create_xp_bar(xp_in_current_level, required_xp_for_this_level)
            embed = discord.Embed(color=user.color or discord.Color.blue())
            if user.display_avatar:
                embed.set_thumbnail(url=user.display_avatar.url)

            description_parts = [ f"## {user.mention}のステータス\n", f"**レベル**: **Lv. {current_level}**", f"**等級**: {tier_role_mention or '`かけだし住民`'}\n**職業**: {job_role_mention or '`なし`'}\n", f"**経験値**\n`{xp_in_current_level:,} / {required_xp_for_this_level:,}`", f"{xp_bar}\n", f"**🏆 総獲得経験値**\n`{total_xp:,} XP`\n", f"**📊 経験値獲得の内訳**\n{xp_details_text}" ]
            embed.description = "\n".join(description_parts)
            
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"레벨 확인 중 오류 발생 (유저: {user.id}): {e}", exc_info=True)
            await interaction.followup.send("❌ ステータス情報の読み込み中にエラーが発生しました。", ephemeral=True)

    @ui.button(label="ランキング確認", style=discord.ButtonStyle.secondary, emoji="👑", custom_id="show_ranking_button")
    async def show_ranking_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            count_res = await supabase.table('user_levels').select('user_id', count='exact').execute()
            total_users = count_res.count if count_res and count_res.count is not None else 0

            if total_users == 0: await interaction.followup.send("まだランキング情報がありません。", ephemeral=True); return
            view = RankingView(interaction.user, total_users)
            embed = await view.build_embed()
            view.update_buttons()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            logger.error(f"랭킹 표시 중 오류: {e}", exc_info=True)
            await interaction.followup.send("❌ ランキング情報の読み込み中にエラーが発生しました。", ephemeral=True)

class JobSelectionView(ui.View):
    def __init__(self, cog: 'LevelSystem', user: discord.Member, level: int, thread: discord.Thread):
        super().__init__(timeout=86400)
        self.cog, self.user, self.level, self.thread = cog, user, level, thread
        self.selected_job_id: Optional[int] = None; self.selected_job_name: Optional[str] = None
        self.selected_ability_id: Optional[int] = None; self.selected_ability_name: Optional[str] = None
        self.jobs_at_level: List[Dict] = []; self.abilities_for_job: List[Dict] = []

    async def initialize(self):
        await self.load_data(); self.build_components()
        
    # [개선] 상위 직업 필터링 로직 추가
    async def load_data(self):
        # 1. 현재 레벨에 맞는 모든 직업을 가져옵니다.
        res = await supabase.table('jobs').select('*, abilities(*)').eq('required_level', self.level).execute()
        if not (res and res.data):
            self.jobs_at_level = []
            return

        all_jobs_at_level = [job for job in res.data if job.get('abilities')]

        # 2. 1차 전직(Lv.50 이하)인 경우, 모든 직업을 보여줍니다.
        if self.level <= 50:
            self.jobs_at_level = all_jobs_at_level
            return
            
        # 3. 2차 전직 이상인 경우, 유저의 현재 직업을 확인합니다.
        user_job_res = await supabase.table('user_jobs').select('jobs(job_key)').eq('user_id', self.user.id).maybe_single().execute()
        
        # 유저가 직업이 없으면 아무것도 보여주지 않습니다.
        if not (user_job_res and user_job_res.data and user_job_res.data.get('jobs')):
            self.jobs_at_level = []
            return
        
        current_job_key = user_job_res.data['jobs']['job_key']
        
        # 4. 직업 계보를 정의하고, 현재 직업에 맞는 상위 직업만 필터링합니다.
        progression_map = {
            'fisherman': 'master_angler', # 낚시꾼 -> 태공망
            'farmer': 'master_farmer'      # 농부 -> 대농부
        }

        next_job_key = progression_map.get(current_job_key)
        if not next_job_key:
            self.jobs_at_level = []
            return
        
        # 5. 필터링된 직업만 최종 목록으로 설정합니다.
        self.jobs_at_level = [
            job for job in all_jobs_at_level if job.get('job_key') == next_job_key
        ]

    def build_components(self):
        self.clear_items()
        if not self.jobs_at_level: 
            self.add_item(ui.Button(label="選択可能な職業がありません。", disabled=True))
            return

        job_options = [discord.SelectOption(label=j['job_name'], value=str(j['id']), description=j['description']) for j in self.jobs_at_level]
        job_select = ui.Select(placeholder="新しい職業を選択してください...", options=job_options, custom_id="job_select")
        job_select.callback = self.on_job_select
        self.add_item(job_select)
        
        ability_select = ui.Select(placeholder="まず職業を選択してください。", disabled=True, custom_id="ability_select", options=[discord.SelectOption(label="placeholder", value="placeholder")])
        ability_select.callback = self.on_ability_select
        self.add_item(ability_select)
        
        confirm_button = ui.Button(label="転職確定", style=discord.ButtonStyle.success, disabled=True, custom_id="confirm_advancement")
        confirm_button.callback = self.on_confirm
        self.add_item(confirm_button)

    async def on_job_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.selected_job_id = int(interaction.data['values'][0])
        selected_job_data = next((j for j in self.jobs_at_level if j['id'] == self.selected_job_id), None)
        if not selected_job_data: return
        self.selected_job_name, self.abilities_for_job = selected_job_data['job_name'], selected_job_data.get('abilities', [])
        self.selected_ability_id, self.selected_ability_name = None, None
        
        ability_select = discord.utils.get(self.children, custom_id="ability_select")
        if isinstance(ability_select, ui.Select):
            ability_options = [discord.SelectOption(label=a['ability_name'], value=str(a['id']), description=a['description']) for a in self.abilities_for_job]
            if ability_options:
                ability_select.placeholder, ability_select.disabled = "能力を選択してください...", False
                ability_select.options = ability_options
            else:
                ability_select.placeholder, ability_select.disabled = "選択できる能力がありません。", True
                ability_select.options = [discord.SelectOption(label="placeholder", value="placeholder")]

        confirm_button = discord.utils.get(self.children, custom_id="confirm_advancement")
        if isinstance(confirm_button, ui.Button): confirm_button.disabled = True
        
        await interaction.edit_original_response(view=self)

    async def on_ability_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.selected_ability_id = int(interaction.data['values'][0])
        ability_data = next((a for a in self.abilities_for_job if a['id'] == self.selected_ability_id), None)
        if ability_data: self.selected_ability_name = ability_data['ability_name']
        confirm_button = discord.utils.get(self.children, custom_id="confirm_advancement")
        if isinstance(confirm_button, ui.Button): confirm_button.disabled = False
        await interaction.edit_original_response(view=self)

    async def on_confirm(self, interaction: discord.Interaction):
        if not all([self.selected_job_id, self.selected_ability_id]):
            await interaction.response.send_message("職業と能力を両方選択してください。", ephemeral=True); return
        await interaction.response.defer()
        await supabase.rpc('set_user_job_and_ability', {'p_user_id': self.user.id, 'p_job_id': self.selected_job_id, 'p_ability_id': self.selected_ability_id}).execute()
        await self.cog.update_job_roles(self.user, self.selected_job_id)
        await self.cog.finalize_advancement(interaction, self.user, self.selected_job_name, self.selected_ability_name, self.thread)
        self.stop()

class LevelSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_advancement_threads: Dict[int, JobSelectionView] = {}
        logger.info("LevelSystem Cog가 성공적으로 초기화되었습니다.")
    
    async def register_persistent_views(self):
        self.bot.add_view(LevelPanelView(self))
        logger.info("✅ 레벨 시스템의 영구 View가 성공적으로 등록되었습니다.")
        
    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("[LevelSystem] 봇이 준비되었습니다. 기존 전직 스레드를 확인하고 태스크를 시작합니다.")
        await self.reconnect_advancement_views()
        if not self.check_advancement_requests.is_running(): self.check_advancement_requests.start()
        if not self.check_level_tier_updates.is_running(): self.check_level_tier_updates.start()

    def cog_unload(self):
        self.check_advancement_requests.cancel()
        self.check_level_tier_updates.cancel()
        
    async def load_configs(self):
        pass

    @tasks.loop(seconds=15.0)
    async def check_advancement_requests(self):
        try:
            res = await supabase.table('bot_configs').select('config_key, config_value').like('config_key', 'job_advancement_request_%').execute()
            if not res or not res.data: return
            keys_to_delete = [req['config_key'] for req in res.data]
            for req in res.data:
                user_id, new_level = int(req['config_key'].split('_')[-1]), req['config_value'].get('level', 0)
                user_member = discord.utils.find(lambda m: m.id == user_id, self.bot.get_all_members())
                if user_member: await self.start_advancement_process(user_member, new_level)
            if keys_to_delete: await supabase.table('bot_configs').delete().in_('config_key', keys_to_delete).execute()
        except Exception as e:
            logger.error(f"전직 요청 확인 중 오류: {e}", exc_info=True)

    @check_advancement_requests.before_loop
    async def before_check_advancement_requests(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=20.0)
    async def check_level_tier_updates(self):
        try:
            res = await supabase.table('bot_configs').select('config_key, config_value').like('config_key', 'level_tier_update_request_%').execute()
            if not res or not res.data: return
            keys_to_delete = [req['config_key'] for req in res.data]
            for req in res.data:
                user_id, new_level = int(req['config_key'].split('_')[-1]), req['config_value'].get('level', 0)
                user_member = discord.utils.find(lambda m: m.id == user_id, self.bot.get_all_members())
                if user_member: await self.update_level_tier_role(user_member, new_level)
            if keys_to_delete: await supabase.table('bot_configs').delete().in_('config_key', keys_to_delete).execute()
        except Exception as e:
            logger.error(f"레벨 등급 업데이트 확인 중 오류: {e}", exc_info=True)
            
    @check_level_tier_updates.before_loop
    async def before_check_level_tier_updates(self):
        await self.bot.wait_until_ready()
        
    async def update_level_tier_role(self, user: discord.Member, current_level: int):
        job_system_config = get_config("JOB_SYSTEM_CONFIG", {})
        level_tier_roles = job_system_config.get("LEVEL_TIER_ROLES", [])
        if not level_tier_roles: return
        role_key_to_add = next((tier['role_key'] for tier in sorted(level_tier_roles, key=lambda x: x['level'], reverse=True) if current_level >= tier['level']), None)
        if not role_key_to_add: return
        all_tier_role_ids = {get_id(tier['role_key']) for tier in level_tier_roles if get_id(tier['role_key'])}
        roles_to_remove = [role for role in user.roles if role.id in all_tier_role_ids]
        if roles_to_remove: await user.remove_roles(*roles_to_remove, reason="レベル等級の変更")
        if (new_role_id := get_id(role_key_to_add)) and (new_role := user.guild.get_role(new_role_id)) and new_role not in user.roles:
            await user.add_roles(new_role, reason="レベル等級の変更")
            logger.info(f"{user.display_name}さんの等級役割を「{new_role.name}」に更新しました。")

    async def regenerate_panel(self, channel: discord.TextChannel) -> bool:
        panel_key = "level_check"
        try:
            panel_info = get_panel_id(panel_key)
            if panel_info and panel_info.get('channel_id') and panel_info.get('message_id'):
                if (ch := self.bot.get_channel(panel_info['channel_id'])):
                    try: 
                        msg = await ch.fetch_message(panel_info['message_id'])
                        await msg.delete()
                    except (discord.NotFound, discord.Forbidden): pass
            embed = discord.Embed(title="📊 レベル＆ランキング", description="下のボタンでご自身のレベルを確認したり、サーバーのランキングを見ることができます。", color=0x5865F2)
            message = await channel.send(embed=embed, view=LevelPanelView(self))
            await save_panel_id(panel_key, message.id, channel.id)
            logger.info(f"✅ 「{panel_key}」パネルを #{channel.name} に再設置しました。")
            return True
        except Exception as e:
            logger.error(f"「{panel_key}」パネルの再設置中にエラー: {e}", exc_info=True)
            return False

    async def start_advancement_process(self, user: discord.Member, level: int):
        channel_id = get_id("job_advancement_channel_id")
        if not channel_id or not (channel := self.bot.get_channel(channel_id)):
            logger.error("전직소 채널이 설정되지 않았거나 찾을 수 없어 전직 프로세스를 시작할 수 없습니다.")
            return
        try:
            if any(v.user.id == user.id for v in self.active_advancement_threads.values()):
                logger.warning(f"{user.display_name}님의 전직 프로세스가 이미 진행 중입니다.")
                return

            thread = await channel.create_thread(
                name=f"⚜️ {user.display_name}さんのLv.{level}転職",
                type=discord.ChannelType.private_thread,
                reason=f"{user.display_name}さんの転職進行"
            )
            
            await thread.add_user(user)
            await asyncio.sleep(1)

            view = JobSelectionView(self, user, level, thread)
            await view.initialize()
            
            self.active_advancement_threads[thread.id] = view
            
            embed = discord.Embed(
                title=f"🎉 レベル{level}達成！転職の時間です！",
                description=f"{user.mention}さん、おめでとうございます！\n\n下のメニューから新しい職業と能力を選択し、「転職確定」ボタンを押してください。",
                color=0xFFD700
            )
            await thread.send(embed=embed, view=view)

        except Exception as e:
            logger.error(f"{user.display_name}님의 전직 스레드 생성 중 오류: {e}", exc_info=True)
    
    async def update_job_roles(self, user: discord.Member, new_job_id: int):
        all_jobs_res = await supabase.table('jobs').select('role_key').execute()
        if not all_jobs_res.data: return
        all_job_role_keys = {job['role_key'] for job in all_jobs_res.data}
        all_job_role_ids = {get_id(key) for key in all_job_role_keys if get_id(key)}
        roles_to_remove = [role for role in user.roles if role.id in all_job_role_ids]
        if roles_to_remove: await user.remove_roles(*roles_to_remove, reason="転職による役割変更")
        new_job_res = await supabase.table('jobs').select('role_key').eq('id', new_job_id).single().execute()
        if new_job_res.data:
            if (new_role_id := get_id(new_job_res.data['role_key'])) and (new_role := user.guild.get_role(new_role_id)):
                await user.add_roles(new_role, reason="転職による役割変更")
            else: logger.warning(f"새로운 직업 역할({new_job_res.data['role_key']})을 찾을 수 없습니다.")

    async def finalize_advancement(self, interaction: discord.Interaction, user: discord.Member, job_name: str, ability_name: str, thread: discord.Thread):
        await thread.send(f"✅ **{job_name}**への転職が完了しました！\nこのスレッドは10秒後に自動で削除されます。")
        if (log_channel_id := get_id("job_log_channel_id")) and (log_channel := self.bot.get_channel(log_channel_id)):
            if (embed_data_res := await supabase.table('embeds').select('embed_data').eq('embed_key', 'log_job_advancement').single().execute()).data:
                embed = format_embed_from_db(embed_data_res.data['embed_data'], user_mention=user.mention, job_name=job_name, ability_name=ability_name)
                if user.display_avatar: embed.set_thumbnail(url=user.display_avatar.url)
                await log_channel.send(embed=embed)
        await asyncio.sleep(10)
        try: await thread.delete()
        except discord.NotFound: pass
        self.active_advancement_threads.pop(thread.id, None)

    async def reconnect_advancement_views(self):
        channel_id = get_id("job_advancement_channel_id")
        if not channel_id or not (channel := self.bot.get_channel(channel_id)): return
        logger.info("기존 전직 스레드를 확인하고 View를 다시 연결합니다...")
        count = 0
        for thread in channel.threads:
            if "転職" in thread.name:
                try:
                    if thread.archived or thread.locked: continue
                    name_parts = thread.name.split("さんのLv.")
                    if len(name_parts) != 2: continue
                    level_part = name_parts[1].split("転職")[0]
                    level = int(level_part)
                    thread_members = await thread.fetch_members()
                    user = next((m for m in thread_members if not m.bot), None)

                    if user:
                        view = JobSelectionView(self, user, level, thread)
                        await view.initialize()
                        # [개선] 메시지에 View를 다시 연결하여 상호작용이 가능하게 합니다.
                        # message_id는 DB에 저장하지 않으므로, 최근 메시지를 가져와서 연결합니다.
                        async for message in thread.history(limit=5):
                            if message.author.id == self.bot.user.id and message.components:
                                await message.edit(view=view)
                                self.active_advancement_threads[thread.id] = view
                                count += 1
                                break
                except Exception as e:
                    logger.warning(f"스레드 '{thread.name}'의 View 재연결 실패: {e}")
        if count > 0:
            logger.info(f"{count}개의 활성 전직 스레드에 View를 성공적으로 다시 연결했습니다.")

async def setup(bot: commands.Bot):
    await bot.add_cog(LevelSystem(bot))
