# game-bot/cogs/systems/LevelSystem.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
import time
import math
from typing import Optional, Dict, List, Any

from utils.database import (
    supabase, get_panel_id, save_panel_id, get_id, get_config, 
    get_cooldown, set_cooldown, save_config_to_db,
    get_embed_from_db
)
from utils.helpers import format_embed_from_db, calculate_xp_for_level

logger = logging.getLogger(__name__)

# --- Helper Functions ---
def create_xp_bar(current_xp: int, required_xp: int, length: int = 10) -> str:
    if required_xp <= 0: return "▓" * length
    progress = min(current_xp / required_xp, 1.0)
    filled_length = int(length * progress)
    bar = '▓' * filled_length + '░' * (length - filled_length)
    return f"[{bar}]"

async def build_level_embed(user: discord.Member) -> discord.Embed:
    """사용자의 레벨 정보를 담은 Embed 객체를 생성합니다."""
    try:
        level_res_task = supabase.table('user_levels').select('*').eq('user_id', user.id).maybe_single().execute()
        job_res_task = supabase.table('user_jobs').select('jobs(*)').eq('user_id', user.id).maybe_single().execute()
        xp_logs_res_task = supabase.table('xp_logs').select('source, xp_amount').eq('user_id', user.id).execute()
        level_res, job_res, xp_logs_res = await asyncio.gather(level_res_task, job_res_task, xp_logs_res_task)

        user_level_data = level_res.data if level_res and level_res.data else {'level': 1, 'xp': 0}
        current_level, total_xp = user_level_data['level'], user_level_data['xp']

        xp_for_next_level = calculate_xp_for_level(current_level + 1)
        xp_at_level_start = calculate_xp_for_level(current_level)
        
        xp_in_current_level = total_xp - xp_at_level_start
        required_xp_for_this_level = xp_for_next_level - xp_at_level_start if xp_for_next_level > xp_at_level_start else 1
        
        job_system_config = get_config("JOB_SYSTEM_CONFIG", {})
        job_role_mention = "`なし`"
        job_role_map = job_system_config.get("JOB_ROLE_MAP", {})
        if job_res and job_res.data and job_res.data.get('jobs'):
            job_data = job_res.data['jobs']
            if role_key := job_role_map.get(job_data['job_key']):
                if role_id := get_id(role_key):
                    job_role_mention = f"<@&{role_id}>"
        
        level_tier_roles = job_system_config.get("LEVEL_TIER_ROLES", [])
        tier_role_mention = "`かけだし住民`"
        user_roles = {role.id for role in user.roles}
        for tier in sorted(level_tier_roles, key=lambda x: x['level'], reverse=True):
            if role_id := get_id(tier['role_key']):
                if role_id in user_roles:
                    tier_role_mention = f"<@&{role_id}>"
                    break
        
        source_map = {'chat': '💬 チャット', 'voice': '🎙️ VC参加', 'fishing': '🎣 釣り', 'farming': '🌾 農業', 'admin': '⚙️ 管理者'}
        aggregated_xp = {v: 0 for v in source_map.values()}
        if xp_logs_res and xp_logs_res.data:
            for log in xp_logs_res.data:
                source_name = source_map.get(log['source'], log['source'])
                if source_name in aggregated_xp:
                    aggregated_xp[source_name] += log['xp_amount']
        
        details = []
        for display_name in source_map.values():
            amount = aggregated_xp.get(display_name, 0)
            details.append(f"> {display_name}: `{amount:,} XP`")
        xp_details_text = "\n".join(details)
        
        xp_bar = create_xp_bar(xp_in_current_level, required_xp_for_this_level)
        embed = discord.Embed(color=user.color or discord.Color.blue())
        if user.display_avatar:
            embed.set_thumbnail(url=user.display_avatar.url)

        description_parts = [
            f"## {user.mention}のステータス\n",
            f"**レベル**: **Lv. {current_level}**",
            f"**等級**: {tier_role_mention}\n**職業**: {job_role_mention}\n",
            f"**経験値**\n`{xp_in_current_level:,} / {required_xp_for_this_level:,}`",
            f"{xp_bar}\n",
            f"**🏆 総獲得経験値**\n`{total_xp:,} XP`\n",
            f"**📊 経験値獲得の内訳**\n{xp_details_text}"
        ]
        embed.description = "\n".join(description_parts)
        
        return embed
    except Exception as e:
        logger.error(f"레벨 임베드 생성 중 오류 (유저: {user.id}): {e}", exc_info=True)
        return discord.Embed(title="エラー", description="ステータス情報の読み込み中にエラーが発生しました。", color=discord.Color.red())

# --- [✅ 신규 추가] 전직 UI ---
class JobAdvancementView(ui.View):
    def __init__(self, user: discord.Member, level: int, cog_instance: 'LevelSystem'):
        super().__init__(timeout=86400) # 24시간
        self.user = user
        self.level = level
        self.cog = cog_instance
        self.selected_job_key: Optional[str] = None
        self.jobs_for_level = get_config("JOB_ADVANCEMENT_DATA", {}).get(str(level), [])

        self.add_item(self.create_job_select())

    def create_job_select(self) -> ui.Select:
        options = []
        for job in self.jobs_for_level:
            options.append(discord.SelectOption(
                label=job['job_name'],
                description=job['description'],
                value=job['job_key']
            ))
        select = ui.Select(placeholder="転職したい職業を選択してください...", options=options, custom_id="job_select")
        select.callback = self.on_job_select
        return select

    def create_ability_select(self) -> ui.Select:
        job_data = next((j for j in self.jobs_for_level if j['job_key'] == self.selected_job_key), None)
        if not job_data: return None

        options = []
        for ability in job_data['abilities']:
            options.append(discord.SelectOption(
                label=ability['ability_name'],
                description=ability['description'],
                value=ability['ability_key']
            ))
        select = ui.Select(placeholder="習得する能力を選択してください...", options=options, custom_id="ability_select")
        select.callback = self.on_ability_select
        return select

    async def on_job_select(self, interaction: discord.Interaction):
        self.selected_job_key = interaction.data['values'][0]
        
        self.clear_items()
        ability_select = self.create_ability_select()
        if ability_select:
            self.add_item(ability_select)
            await interaction.response.edit_message(content="次に、習得したい能力を選択してください。", view=self)
        else:
            await interaction.response.edit_message(content="エラー: 職業情報が見つかりません。", view=None)

    async def on_ability_select(self, interaction: discord.Interaction):
        selected_ability_key = interaction.data['values'][0]
        
        await interaction.response.defer()
        
        job_data = next((j for j in self.jobs_for_level if j['job_key'] == self.selected_job_key), None)
        ability_data = next((a for a in job_data['abilities'] if a['ability_key'] == selected_ability_key), None)

        if not job_data or not ability_data:
            return await interaction.edit_message(content="❌ エラーが発生しました。もう一度お試しください。", view=None)

        try:
            # DB에 전직 및 능력 정보 저장
            job_res = await supabase.table('jobs').select('id').eq('job_key', self.selected_job_key).single().execute()
            ability_res = await supabase.table('abilities').select('id').eq('ability_key', selected_ability_key).single().execute()
            
            if not (job_res.data and ability_res.data):
                raise Exception("DB에서 직업 또는 능력 ID를 찾을 수 없습니다.")

            job_id, ability_id = job_res.data['id'], ability_res.data['id']
            
            await supabase.table('user_jobs').upsert({'user_id': self.user.id, 'job_id': job_id}).execute()
            await supabase.table('user_abilities').upsert({'user_id': self.user.id, 'ability_id': ability_id}).execute()
            
            # 관리 봇에 역할 부여 요청
            role_key = job_data.get('role_key')
            if role_key:
                await save_config_to_db(f"job_role_update_request_{self.user.id}", {'role_key': role_key, 'timestamp': time.time()})

            # 로그 채널에 알림
            log_channel_id = get_id("job_log_channel_id")
            if log_channel_id and (log_channel := self.cog.bot.get_channel(log_channel_id)):
                embed_data = await get_embed_from_db("log_job_advancement")
                if embed_data:
                    embed = format_embed_from_db(
                        embed_data, 
                        user_mention=self.user.mention,
                        job_name=job_data['job_name'],
                        ability_name=ability_data['ability_name']
                    )
                    if self.user.display_avatar:
                        embed.set_thumbnail(url=self.user.display_avatar.url)
                    await log_channel.send(embed=embed)

            await interaction.edit_message(
                content=f"🎉 **{job_data['job_name']}**に転職し、**{ability_data['ability_name']}**の能力を習得しました！",
                view=None
            )
            self.stop()
        except Exception as e:
            logger.error(f"전직 처리 중 DB 오류 (유저: {self.user.id}): {e}", exc_info=True)
            await interaction.edit_message(content="❌ 転職処理中にエラーが発生しました。管理者にお問い合わせください。", view=None)

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
        
        cooldown_key = f"level_check_public_{user.id}"
        cooldown_seconds = 60

        last_used = await get_cooldown(user.id, cooldown_key)
        if time.time() - last_used < cooldown_seconds:
            can_use_time = int(last_used + cooldown_seconds)
            await interaction.response.send_message(f"⏳ このボタンは <t:{can_use_time}:R> に再度使用できます。", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        
        try:
            await set_cooldown(user.id, cooldown_key)
            
            public_embed = await build_level_embed(user)
            await interaction.channel.send(embed=public_embed)

            await self.cog.regenerate_panel(interaction.channel, "panel_level_check")

            await interaction.followup.send("✅ レベル情報を表示しました。", ephemeral=True)

        except Exception as e:
            logger.error(f"공개 레벨 확인 중 오류 발생 (유저: {user.id}): {e}", exc_info=True)
            await interaction.followup.send("❌ ステータス情報の表示中にエラーが発生しました。", ephemeral=True)

    @ui.button(label="ランキング確認", style=discord.ButtonStyle.secondary, emoji="👑", custom_id="show_ranking_button")
    async def show_ranking_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            count_res = await supabase.table('user_levels').select('user_id', count='exact').execute()
            total_users = count_res.count if count_res and count_res.count is not None else 0

            if total_users == 0:
                await interaction.followup.send("まだランキング情報がありません。", ephemeral=True)
                return
            
            view = RankingView(interaction.user, total_users)
            embed = await view.build_embed()
            view.update_buttons()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            logger.error(f"랭킹 표시 중 오류: {e}", exc_info=True)
            await interaction.followup.send("❌ ランキング情報の読み込み中にエラーが発生しました。", ephemeral=True)

class LevelSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("LevelSystem Cog (게임봇)가 성공적으로 초기화되었습니다.")
    
    async def register_persistent_views(self):
        """봇이 재시작되어도 View가 동작하도록 등록합니다."""
        self.bot.add_view(LevelPanelView(self))
        logger.info("✅ 레벨 시스템의 영구 View가 성공적으로 등록되었습니다。")
        
    async def load_configs(self):
        pass
            
    async def handle_level_up_event(self, user: discord.Member, result_data: Dict):
        """레벨업 이벤트를 중앙에서 처리하는 함수 (전직 안내, 역할 부여 요청 등)"""
        if not result_data or not result_data.get('leveled_up'):
            return
        
        new_level = result_data.get('new_level')
        logger.info(f"유저 {user.display_name}(ID: {user.id})가 레벨 {new_level}(으)로 레벨업했습니다.")
        
        # 1. 서버 관리 봇에 등급 역할 업데이트 요청 (항상 실행)
        await save_config_to_db(f"level_tier_update_request_{user.id}", {"level": new_level, "timestamp": time.time()})
        logger.info(f"유저의 레벨이 변경되어 관리 봇에게 등급 역할 업데이트 요청을 보냈습니다.")

        # 2. 전직 가능 레벨인지 확인하고, DM으로 안내 전송
        game_config = get_config("GAME_CONFIG", {})
        # [수정] 키를 문자열로 변환하여 JSONB와 호환되도록 함
        job_advancement_levels = [str(lvl) for lvl in game_config.get("JOB_ADVANCEMENT_LEVELS", [])]

        if str(new_level) in job_advancement_levels:
            logger.info(f"유저가 전직 가능 레벨({new_level})에 도달하여 DM으로 안내를 보냅니다.")
            try:
                embed = discord.Embed(
                    title="🎉 転職のご案内",
                    description=f"**Lv.{new_level}**達成、おめでとうございます！\n新しい職業に転職できます。\n\n下のメニューから転職したい職業を選択してください。",
                    color=0xFFD700
                )
                view = JobAdvancementView(user, new_level, self)
                await user.send(embed=embed, view=view)
            except discord.Forbidden:
                logger.warning(f"유저 {user.display_name}(ID: {user.id})에게 DM을 보낼 수 없어 전직 안내에 실패했습니다.")
            except Exception as e:
                logger.error(f"전직 안내 DM 발송 중 오류: {e}", exc_info=True)

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_level_check") -> bool:
        try:
            panel_info = get_panel_id(panel_key)
            if panel_info and panel_info.get('channel_id') and panel_info.get('message_id'):
                target_channel_id = panel_info['channel_id']
                if isinstance(channel, discord.TextChannel) and channel.id == target_channel_id:
                    try: 
                        msg = await channel.fetch_message(panel_info['message_id'])
                        await msg.delete()
                    except (discord.NotFound, discord.Forbidden):
                        logger.warning(f"이전 레벨 패널(ID: {panel_info['message_id']})을 찾지 못했지만, 계속 진행합니다.")
            
            embed_data = await get_embed_from_db("panel_level_check")
            if not embed_data:
                embed_data = {
                    "title": "📊 レベル＆ランキング",
                    "description": "下のボタンでご自身のレベルを確認したり、サーバーのランキングを見ることができます。",
                    "color": 0x5865F2
                }
                logger.warning(f"DB에서 'panel_level_check' 임베드를 찾을 수 없어 기본값으로 패널을 생성합니다.")

            embed = discord.Embed.from_dict(embed_data)
            
            message = await channel.send(embed=embed, view=LevelPanelView(self))
            await save_panel_id(panel_key, message.id, channel.id)
            
            logger.info(f"✅ 「{panel_key}」パネルを #{channel.name} に再設置しました。")
            return True
        except Exception as e:
            logger.error(f"「{panel_key}」パネルの再設置中にエラー: {e}", exc_info=True)
            return False

async def setup(bot: commands.Bot):
    await bot.add_cog(LevelSystem(bot))
