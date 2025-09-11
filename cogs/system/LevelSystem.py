# cogs/system/LevelSystem.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
import time
import math
from typing import Optional, Dict, List, Any
from datetime import time as dt_time, timezone, timedelta
from collections import defaultdict
from types import SimpleNamespace # <--- [추가] 이 라인을 파일 상단에 추가해주세요.

from utils.database import (
    supabase, get_panel_id, save_panel_id, get_id, get_config, 
    get_cooldown, set_cooldown, save_config_to_db,
    get_embed_from_db, log_activity
)
from utils.helpers import format_embed_from_db, calculate_xp_for_level, format_timedelta_minutes_seconds

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
KST_MIDNIGHT_UPDATE = dt_time(hour=0, minute=5, tzinfo=KST)

def create_xp_bar(current_xp: int, required_xp: int, length: int = 10) -> str:
    if required_xp <= 0: return "▓" * length
    progress = min(current_xp / required_xp, 1.0)
    filled_length = int(length * progress)
    bar = '▓' * filled_length + '░' * (length - filled_length)
    return f"[{bar}]"

async def build_level_embed(user: discord.Member) -> discord.Embed:
    try:
        level_res_task = supabase.table('user_levels').select('*').eq('user_id', user.id).maybe_single().execute()
        job_res_task = supabase.table('user_jobs').select('jobs(*)').eq('user_id', user.id).maybe_single().execute()
        xp_logs_res_task = supabase.table('user_activities').select('activity_type, xp_earned').eq('user_id', user.id).gt('xp_earned', 0).execute()
        
        level_res, job_res, xp_logs_res = await asyncio.gather(level_res_task, job_res_task, xp_logs_res_task)

        user_level_data = level_res.data if level_res and hasattr(level_res, 'data') and level_res.data else {'level': 1, 'xp': 0}
        current_level, total_xp = user_level_data['level'], user_level_data['xp']

        xp_for_next_level = calculate_xp_for_level(current_level + 1)
        xp_at_level_start = calculate_xp_for_level(current_level)
        
        xp_in_current_level = total_xp - xp_at_level_start
        required_xp_for_this_level = xp_for_next_level - xp_at_level_start if xp_for_next_level > xp_at_level_start else 1
        
        job_system_config = get_config("JOB_SYSTEM_CONFIG", {})
        job_role_mention = "`없음`"; job_role_map = job_system_config.get("JOB_ROLE_MAP", {})
        if job_res and hasattr(job_res, 'data') and job_res.data and job_res.data.get('jobs'):
            job_data = job_res.data['jobs']
            if role_key := job_role_map.get(job_data['job_key']):
                if role_id := get_id(role_key): job_role_mention = f"<@&{role_id}>"
        
        level_tier_roles = job_system_config.get("LEVEL_TIER_ROLES", [])
        tier_role_mention = "`새내기 주민`"; user_roles = {role.id for role in user.roles}
        for tier in sorted(level_tier_roles, key=lambda x: x['level'], reverse=True):
            if role_id := get_id(tier['role_key']):
                if role_id in user_roles: tier_role_mention = f"<@&{role_id}>"; break
        
        source_map = {
            'chat': '💬 채팅', 
            'voice': '🎙️ 음성채팅', 
            'fishing_catch': '🎣 낚시', 
            'farm_harvest': '🌾 농사',
            'mining': '⛏️ 채광',
            'quest': '📜 퀘스트',
            'admin': '⚙️ 관리자'
        }
        
        aggregated_xp = {v: 0 for v in source_map.values()}
        
        if xp_logs_res and hasattr(xp_logs_res, 'data') and xp_logs_res.data:
            for log in xp_logs_res.data:
                source_key = next((key for key in source_map.keys() if log['activity_type'].startswith(key)), None)
                if source_key:
                    display_name = source_map[source_key]
                    aggregated_xp[display_name] += log['xp_earned']
        
        details = [f"> {display_name}: `{amount:,} XP`" for display_name, amount in aggregated_xp.items()]
        xp_details_text = "\n".join(details)
        
        xp_bar = create_xp_bar(xp_in_current_level, required_xp_for_this_level)
        embed = discord.Embed(color=user.color or discord.Color.blue())
        if user.display_avatar: embed.set_thumbnail(url=user.display_avatar.url)

        description_parts = [
            f"## {user.mention}의 상태\n",
            f"**레벨**: **Lv. {current_level}**",
            f"**등급**: {tier_role_mention}\n**직업**: {job_role_mention}\n",
            f"**경험치**\n`{xp_in_current_level:,} / {required_xp_for_this_level:,}`",
            f"{xp_bar}\n",
            f"**🏆 총 획득 경험치**\n`{total_xp:,} XP`\n",
            f"**📊 경험치 획득 내역**\n{xp_details_text}"
        ]
        embed.description = "\n".join(description_parts)
        return embed
    except Exception as e:
        logger.error(f"레벨 임베드 생성 중 오류 (유저: {user.id}): {e}", exc_info=True)
        return discord.Embed(title="오류", description="상태 정보를 불러오는 중 오류가 발생했습니다.", color=discord.Color.red())

class RankingView(ui.View):
    def __init__(self, user: discord.Member):
        super().__init__(timeout=300)
        self.user = user
        self.current_page = 0
        self.users_per_page = 10
        self.total_pages = 1
        
        self.current_category = "level"
        self.current_period = "total"

        self.highlight_user_id: Optional[int] = None

        self.category_map = {
            "level": {"column": "xp", "name": "레벨", "unit": "XP"},
            "voice": {"column": "voice_minutes", "name": "음성채팅", "unit": "분"},
            "chat": {"column": "chat_count", "name": "채팅", "unit": "회"},
            "fishing": {"column": "fishing_count", "name": "낚시", "unit": "마리"},
            "harvest": {"column": "harvest_count", "name": "수확", "unit": "회"},
            "mining": {"column": "mining_count", "name": "채광", "unit": "회"},
        }
        
        self.period_map = {
            "daily": "오늘",
            "weekly": "이번 주",
            "monthly": "이번 달",
            "total": "종합",
        }

    async def start(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        embed = await self.build_embed()
        self.build_components()
        await interaction.followup.send(embed=embed, view=self, ephemeral=True)

    async def update_display(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.build_embed()
        self.build_components()
        await interaction.edit_original_response(embed=embed, view=self)

    def build_components(self):
        self.clear_items()

        category_options = [
            discord.SelectOption(label=info["name"], value=key, emoji=e)
            for key, info, e in [
                ("level", self.category_map["level"], "👑"),
                ("voice", self.category_map["voice"], "🎙️"),
                ("chat", self.category_map["chat"], "💬"),
                ("fishing", self.category_map["fishing"], "🎣"),
                ("harvest", self.category_map["harvest"], "🌾"),
                ("mining", self.category_map["mining"], "⛏️"),
            ]
        ]
        category_select = ui.Select(
            placeholder="랭킹 카테고리를 선택하세요...",
            options=category_options,
            custom_id="ranking_category_select"
        )
        for option in category_options:
            if option.value == self.current_category:
                option.default = True
        category_select.callback = self.on_select_change
        self.add_item(category_select)
        
        period_options = [
            discord.SelectOption(label=name, value=key, emoji=e)
            for key, name, e in [
                ("daily", self.period_map["daily"], "📅"),
                ("weekly", self.period_map["weekly"], "🗓️"),
                ("monthly", self.period_map["monthly"], "🈷️"),
                ("total", self.period_map["total"], "🏆"),
            ]
        ]
        period_select = ui.Select(
            placeholder="랭킹 기간을 선택하세요...",
            options=period_options,
            custom_id="ranking_period_select",
            disabled=(self.current_category == "level")
        )
        for option in period_options:
            if option.value == self.current_period:
                option.default = True
        period_select.callback = self.on_select_change
        self.add_item(period_select)

        prev_button = ui.Button(label="◀", style=discord.ButtonStyle.secondary, custom_id="prev_page", disabled=(self.current_page == 0))
        next_button = ui.Button(label="▶", style=discord.ButtonStyle.secondary, custom_id="next_page", disabled=(self.current_page >= self.total_pages - 1))
        
        prev_button.callback = self.on_pagination_click
        next_button.callback = self.on_pagination_click
        self.add_item(prev_button)
        self.add_item(next_button)

        my_rank_button = ui.Button(label="내 순위", style=discord.ButtonStyle.success, emoji="📍", custom_id="my_rank_button")
        my_rank_button.callback = self.on_my_rank_click
        self.add_item(my_rank_button)

    async def on_select_change(self, interaction: discord.Interaction):
        custom_id = interaction.data['custom_id']
        selected_value = interaction.data['values'][0]

        if custom_id == "ranking_category_select":
            self.current_category = selected_value
            if self.current_category == "level":
                self.current_period = "total"
        elif custom_id == "ranking_period_select":
            self.current_period = selected_value
        
        self.current_page = 0
        await self.update_display(interaction)

    async def on_pagination_click(self, interaction: discord.Interaction):
        if interaction.data['custom_id'] == "next_page":
            self.current_page += 1
        else:
            self.current_page -= 1
        await self.update_display(interaction)
    
    async def on_my_rank_click(self, interaction: discord.Interaction):
        category_info = self.category_map[self.current_category]
        column_name = category_info["column"]
        table_name = 'user_levels' if self.current_category == 'level' else f"{self.current_period}_stats"
        
        try:
            res = await supabase.rpc('get_user_rank', {
                'p_user_id': self.user.id,
                'p_table_name': table_name,
                'p_column_name': column_name
            }).execute()

            if res.data:
                rank = res.data
                self.current_page = (rank - 1) // self.users_per_page
                self.highlight_user_id = self.user.id
                await self.update_display(interaction)
            else:
                await interaction.response.send_message("아직 랭킹에 등록되지 않았습니다.", ephemeral=True, delete_after=5)

        except Exception as e:
            logger.error(f"내 순위 조회 중 오류: {e}", exc_info=True)
            await interaction.response.send_message("❌ 순위를 가져오는 중 오류가 발생했습니다.", ephemeral=True, delete_after=5)

    async def build_embed(self) -> discord.Embed:
        offset = self.current_page * self.users_per_page
        
        category_info = self.category_map[self.current_category]
        column_name = category_info["column"]
        unit = category_info["unit"]

        table_name = 'user_levels' if self.current_category == 'level' else f"{self.current_period}_stats"

        query = supabase.table(table_name).select('user_id', column_name, count='exact').order(column_name, desc=True).range(offset, offset + self.users_per_page - 1)
        res = await query.execute()

        total_users = res.count if res and res.count is not None else 0
        self.total_pages = math.ceil(total_users / self.users_per_page)
        
        title = f"👑 {self.period_map[self.current_period]} {category_info['name']} 랭킹"
        embed = discord.Embed(title=title, color=0xFFD700)

        rank_list = []
        if res and hasattr(res, 'data') and res.data:
            for i, user_data in enumerate(res.data):
                rank = offset + i + 1
                user_id_int = int(user_data['user_id'])
                member = self.user.guild.get_member(user_id_int)
                name = member.display_name if member else f"ID: {user_id_int}"
                value = user_data.get(column_name, 0)
                
                line = f"`{rank}.` {name} - **`{value:,}`** {unit}"
                if self.highlight_user_id == user_id_int:
                    line = f"➡️ **{line}** ⬅️"
                
                rank_list.append(line)

        self.highlight_user_id = None

        embed.description = "\n".join(rank_list) if rank_list else "아직 랭킹 정보가 없습니다."
        embed.set_footer(text=f"페이지 {self.current_page + 1} / {self.total_pages}")
        return embed
        
class LevelPanelView(ui.View):
    def __init__(self, cog_instance: 'LevelSystem'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    @ui.button(label="상태 확인", style=discord.ButtonStyle.primary, emoji="📊", custom_id="level_check_button")
    async def check_level_button(self, interaction: discord.Interaction, button: ui.Button):
        # ▼▼▼ [핵심 수정] 쿨타임 시스템이 요구하는 형식에 맞게 임시 객체를 생성 ▼▼▼
        # CooldownMapping이 message.author.id를 찾으므로,
        # author 속성이 interaction.user를 가리키는 임시 객체를 만들어 전달합니다.
        dummy_message = SimpleNamespace(author=interaction.user)
        bucket = self.cog.level_check_cooldown.get_bucket(dummy_message)
        # ▲▲▲ [핵심 수정] 종료 ▲▲▲
        
        retry_after = bucket.update_rate_limit()

        if retry_after:
            available_at = discord.utils.utcnow() + timedelta(seconds=retry_after)
            
            await interaction.response.send_message(
                f"⏳ 잠시 후 다시 시도해주세요. (사용 가능: {discord.utils.format_dt(available_at, style='R')})",
                ephemeral=True,
                delete_after=10
            )
            return

        try:
            await interaction.response.defer(ephemeral=False, thinking=True)
            
            level_embed = await build_level_embed(interaction.user)
            
            await interaction.followup.send(embed=level_embed, ephemeral=False)
            
            panel_info = get_panel_id(self.cog.panel_key.replace("panel_", ""))
            if panel_info and (panel_channel := self.cog.bot.get_channel(panel_info['channel_id'])):
                await self.cog.regenerate_panel(panel_channel, panel_key=self.cog.panel_key)
            
        except Exception as e:
            logger.error(f"개인 레벨 확인 및 패널 재생성 중 오류 발생 (유저: {interaction.user.id}): {e}", exc_info=True)
            error_message = "❌ 상태 정보를 불러오는 중 오류가 발생했습니다."
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(error_message, ephemeral=True)
                except discord.InteractionResponded:
                    await interaction.followup.send(error_message, ephemeral=True)
            else:
                await interaction.followup.send(error_message, ephemeral=True)

    @ui.button(label="랭킹 확인", style=discord.ButtonStyle.secondary, emoji="👑", custom_id="show_ranking_button")
    async def show_ranking_button(self, interaction: discord.Interaction, button: ui.Button):
        view = RankingView(interaction.user)
        await view.start(interaction)

class LevelSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.panel_key = "panel_level_check"
        self.channel_id_key = "level_check_panel_channel_id"
        logger.info("LevelSystem Cog (게임봇)가 성공적으로 초기화되었습니다.")
        self.level_check_cooldown = commands.CooldownMapping.from_cooldown(1, 60.0, commands.BucketType.user)
    
    async def cog_load(self):
        self.update_champion_panel.start()
        
    def cog_unload(self):
        self.update_champion_panel.cancel()
        
    @tasks.loop(time=KST_MIDNIGHT_UPDATE)
    async def update_champion_panel(self):
        logger.info("[LevelSystem] 챔피언 보드 패널 새로고침을 시작합니다.")
        try:
            channel_id = get_id(self.channel_id_key)
            if not (channel_id and (channel := self.bot.get_channel(channel_id))):
                logger.warning("레벨/챔피언 패널 채널이 설정되지 않아 자동 업데이트를 건너뜁니다.")
                return
            
            await self.regenerate_panel(channel, panel_key=self.panel_key)
            logger.info("[LevelSystem] 챔피언 보드 패널을 성공적으로 새로고침했습니다.")
        except Exception as e:
            logger.error(f"챔피언 패널 업데이트 중 오류: {e}", exc_info=True)

    @update_champion_panel.before_loop
    async def before_champion_update(self):
        await self.bot.wait_until_ready()

    async def _build_champion_embed(self) -> discord.Embed:
        categories = {
            "level": {"column": "xp", "name": "종합 레벨", "unit": "XP", "table": "user_levels"},
            "voice": {"column": "voice_minutes", "name": "음성채팅", "unit": "분", "table": "total_stats"},
            "chat": {"column": "chat_count", "name": "채팅", "unit": "회", "table": "total_stats"},
            "fishing": {"column": "fishing_count", "name": "낚시", "unit": "마리", "table": "total_stats"},
            "harvest": {"column": "harvest_count", "name": "수확", "unit": "회", "table": "total_stats"},
            "mining": {"column": "mining_count", "name": "채광", "unit": "회", "table": "total_stats"},
        }
        
        tasks = []
        for key, info in categories.items():
            query = supabase.table(info["table"]).select('user_id', info["column"])
            tasks.append(query.order(info["column"], desc=True).limit(1).maybe_single().execute())
        
        results = await asyncio.gather(*tasks)

        champion_data = {}
        category_keys = list(categories.keys())
        server_id = get_config("SERVER_ID")
        if not server_id:
            logger.error("SERVER_ID가 설정되지 않아 챔피언 보드 멤버를 찾을 수 없습니다.")
            return discord.Embed(title="오류", description="SERVER_ID가 설정되지 않았습니다.")
            
        guild = self.bot.get_guild(int(server_id))

        for i, res in enumerate(results):
            key = category_keys[i]
            info = categories[key]
            
            if res and hasattr(res, 'data') and res.data and res.data.get(info["column"], 0) > 0:
                user_id = int(res.data['user_id'])
                value = res.data[info["column"]]
                member = guild.get_member(user_id) if guild else None
                name = member.mention if member else f"ID: {user_id}"
                champion_data[f"{key}_champion"] = f"🏆 **{name}** (`{value:,}` {info['unit']})"
            else:
                champion_data[f"{key}_champion"] = "아직 기록이 없습니다."

        embed_template = await get_embed_from_db("panel_champion_board")
        if not embed_template:
            return discord.Embed(title="오류", description="챔피언 보드 템플릿을 찾을 수 없습니다.")

        return format_embed_from_db(embed_template, **champion_data)

    async def register_persistent_views(self):
        self.bot.add_view(LevelPanelView(self))
        logger.info("✅ 레벨 시스템의 영구 View가 성공적으로 등록되었습니다.")
        
    async def load_configs(self):
        pass
    
    async def handle_level_up_event(self, user: discord.Member, result_data: List[Dict]):
        if not result_data or not result_data[0].get('leveled_up'): return
        
        new_level = result_data[0].get('new_level')
        logger.info(f"유저 {user.display_name}(ID: {user.id})가 레벨 {new_level}(으)로 레벨업했습니다.")
        
        # 'level_tier_update_request'와 'job_advancement_request'를 DB에 저장
        await save_config_to_db(f"level_tier_update_request_{user.id}", {"level": new_level, "timestamp": time.time()})
        
        game_config = get_config("GAME_CONFIG", {})
        job_advancement_levels = game_config.get("JOB_ADVANCEMENT_LEVELS", [50, 100])
        
        if new_level in job_advancement_levels:
            await save_config_to_db(f"job_advancement_request_{user.id}", {"level": new_level, "timestamp": time.time()})

    async def process_level_requests(self, requests_by_prefix: Dict[str, List]):
        server_id_str = get_config("SERVER_ID")
        if not server_id_str: return
        guild = self.bot.get_guild(int(server_id_str))
        if not guild: return
            
        handler_cog = self.bot.get_cog("JobAndTierHandler")
        if not handler_cog: return

        user_updates = defaultdict(lambda: {"level": None, "advancement_level": None})

        for req in requests_by_prefix.get("level_tier_update", []):
            user_id = int(req['config_key'].split('_')[-1])
            user_updates[user_id]["level"] = req['config_value'].get('level')

        for req in requests_by_prefix.get("job_advancement", []):
            user_id = int(req['config_key'].split('_')[-1])
            user_updates[user_id]["advancement_level"] = req['config_value'].get('level')

        for user_id, updates in user_updates.items():
            member = guild.get_member(user_id)
            if not member: continue

            if new_level := updates.get("level"):
                await handler_cog.update_tier_role(member, new_level)
            
            if advancement_level := updates.get("advancement_level"):
                await handler_cog.start_advancement_process(member, advancement_level)

    async def update_user_xp_and_level_from_admin(self, user: discord.Member, xp_to_add: int = 0, exact_level: Optional[int] = None) -> bool:
        try:
            if xp_to_add > 0:
                await log_activity(user.id, 'admin', xp_earned=xp_to_add)

            res = await supabase.table('user_levels').select('level, xp').eq('user_id', user.id).maybe_single().execute()
            
            # [핵심 수정] res가 None인 경우를 처리하여 AttributeError 방지
            if res and res.data:
                current_data = res.data
            else:
                # DB에서 데이터를 가져오지 못했거나 유저가 없는 경우 기본값 사용
                current_data = {'level': 1, 'xp': 0}
            
            new_total_xp = current_data['xp']
            leveled_up = False

            if exact_level is not None:
                new_level = exact_level
                new_total_xp = calculate_xp_for_level(new_level)
                if new_level > current_data['level']: leveled_up = True
            else:
                new_total_xp += xp_to_add
                new_level = current_data['level']
                # [수정] 레벨 1부터 시작하도록 보장
                while new_level > 0 and new_total_xp >= calculate_xp_for_level(new_level + 1):
                    new_level += 1
                if new_level > current_data['level']: leveled_up = True
            
            await supabase.table('user_levels').upsert({'user_id': user.id, 'level': new_level, 'xp': new_total_xp}).execute()
            
            if leveled_up:
                await self.handle_level_up_event(user, [{"leveled_up": True, "new_level": new_level}])
            
            logger.info(f"관리자 요청으로 {user.display_name}님의 레벨/XP가 성공적으로 업데이트되었습니다.")
            return True # 성공 시 True 반환
        
        except Exception as e:
            logger.error(f"관리자 요청으로 레벨/XP 업데이트 중 오류 발생 (유저: {user.id}): {e}", exc_info=True)
            return False # 실패 시 False 반환

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_level_check") -> bool:
        try:
            panel_name = panel_key.replace("panel_", "")
            panel_info = get_panel_id(panel_name)
            
            if panel_info and panel_info.get('message_id') and panel_info.get('channel_id'):
                try:
                    old_channel = self.bot.get_channel(panel_info['channel_id'])
                    if old_channel:
                        msg_to_delete = await old_channel.fetch_message(panel_info['message_id'])
                        await msg_to_delete.delete()
                        logger.info(f"이전 '{panel_key}' 패널(ID: {panel_info['message_id']})을 채널 '{old_channel.name}'에서 삭제했습니다.")
                except (discord.NotFound, discord.Forbidden):
                    logger.warning(f"이전 '{panel_key}' 패널(ID: {panel_info.get('message_id')})을 찾을 수 없거나 삭제할 수 없습니다. 계속 진행합니다.")
                except Exception as e:
                    logger.error(f"이전 패널 삭제 중 예기치 않은 오류 발생: {e}", exc_info=True)

            embed = await self._build_champion_embed()
            message = await channel.send(embed=embed, view=LevelPanelView(self))

            await save_panel_id(panel_name, message.id, channel.id)
            
            logger.info(f"✅ '{panel_key}' 패널을 #{channel.name} 에 재설치했습니다.")
            return True
        except Exception as e:
            logger.error(f"'{panel_key}' 패널 재설치 중 오류: {e}", exc_info=True)
            return False

async def setup(bot: commands.Bot):
    await bot.add_cog(LevelSystem(bot))
