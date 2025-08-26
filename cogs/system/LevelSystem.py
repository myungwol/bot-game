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
    get_embed_from_db, log_activity
)
from utils.helpers import format_embed_from_db, calculate_xp_for_level
from utils.game_config_defaults import JOB_ADVANCEMENT_DATA, GAME_CONFIG

logger = logging.getLogger(__name__)

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
        job_role_mention = "`なし`"; job_role_map = job_system_config.get("JOB_ROLE_MAP", {})
        if job_res and hasattr(job_res, 'data') and job_res.data and job_res.data.get('jobs'):
            job_data = job_res.data['jobs']
            if role_key := job_role_map.get(job_data['job_key']):
                if role_id := get_id(role_key): job_role_mention = f"<@&{role_id}>"
        
        level_tier_roles = job_system_config.get("LEVEL_TIER_ROLES", [])
        tier_role_mention = "`かけだし住民`"; user_roles = {role.id for role in user.roles}
        for tier in sorted(level_tier_roles, key=lambda x: x['level'], reverse=True):
            if role_id := get_id(tier['role_key']):
                if role_id in user_roles: tier_role_mention = f"<@&{role_id}>"; break
        
        source_map = {
            'chat': '💬 チャット', 
            'voice': '🎙️ VC参加', 
            'fishing_catch': '🎣 釣り', 
            'farm_harvest': '🌾 農業', 
            'quest': '📜 クエスト',
            'admin': '⚙️ 管理者'
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

# [✅✅✅ 핵심 수정 ✅✅✅]
# 기존의 RankingView를 완전히 새로운, 더 강력한 버전으로 교체합니다.
class RankingView(ui.View):
    def __init__(self, user: discord.Member):
        super().__init__(timeout=300)
        self.user = user
        self.current_page = 0
        self.users_per_page = 10
        self.total_pages = 1
        
        # 랭킹의 기준이 되는 '카테고리'와 '기간'을 상태로 저장합니다.
        self.current_category = "level"  # level, voice, chat, fishing, harvest
        self.current_period = "total"   # daily, weekly, monthly, total

        # 각 카테고리에 대한 정보 (DB 컬럼명, 표시 이름, 단위)
        self.category_map = {
            "level": {"column": "xp", "name": "レベル", "unit": "XP"},
            "voice": {"column": "voice_minutes", "name": "ボイス", "unit": "分"},
            "chat": {"column": "chat_count", "name": "チャット", "unit": "回"},
            "fishing": {"column": "fishing_count", "name": "釣り", "unit": "匹"},
            "harvest": {"column": "harvest_count", "name": "収穫", "unit": "回收"},
        }
        
        self.period_map = {
            "daily": "今日",
            "weekly": "今週",
            "monthly": "今月",
            "total": "総合",
        }

    async def start(self, interaction: discord.Interaction):
        """View를 시작하고 첫 메시지를 보냅니다."""
        await interaction.response.defer(ephemeral=True)
        embed = await self.build_embed()
        self.build_components()
        await interaction.followup.send(embed=embed, view=self, ephemeral=True)

    async def update_display(self, interaction: discord.Interaction):
        """인터랙션에 대한 응답으로 View를 업데이트합니다."""
        await interaction.response.defer()
        embed = await self.build_embed()
        self.build_components()
        await interaction.edit_original_response(embed=embed, view=self)

    def build_components(self):
        """현재 상태에 맞게 드롭다운 메뉴와 버튼을 구성합니다."""
        self.clear_items()

        # 1. 카테고리 선택 드롭다운
        category_options = [
            discord.SelectOption(label=info["name"], value=key, emoji=e)
            for key, info, e in [
                ("level", self.category_map["level"], "👑"),
                ("voice", self.category_map["voice"], "🎙️"),
                ("chat", self.category_map["chat"], "💬"),
                ("fishing", self.category_map["fishing"], "🎣"),
                ("harvest", self.category_map["harvest"], "🌾"),
            ]
        ]
        category_select = ui.Select(
            placeholder="ランキングのカテゴリーを選択...",
            options=category_options,
            custom_id="ranking_category_select"
        )
        # 현재 선택된 값을 기본값으로 설정
        for option in category_options:
            if option.value == self.current_category:
                option.default = True
        category_select.callback = self.on_select_change
        self.add_item(category_select)
        
        # 2. 기간 선택 드롭다운
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
            placeholder="ランキングの期間を選択...",
            options=period_options,
            custom_id="ranking_period_select",
            # '레벨' 랭킹은 '종합'만 가능하므로, 이 경우 비활성화합니다.
            disabled=(self.current_category == "level")
        )
        for option in period_options:
            if option.value == self.current_period:
                option.default = True
        period_select.callback = self.on_select_change
        self.add_item(period_select)

        # 3. 페이지네이션 버튼
        prev_button = ui.Button(label="◀", style=discord.ButtonStyle.secondary, custom_id="prev_page", disabled=(self.current_page == 0))
        next_button = ui.Button(label="▶", style=discord.ButtonStyle.secondary, custom_id="next_page", disabled=(self.current_page >= self.total_pages - 1))
        
        prev_button.callback = self.on_pagination_click
        next_button.callback = self.on_pagination_click
        self.add_item(prev_button)
        self.add_item(next_button)

    async def on_select_change(self, interaction: discord.Interaction):
        """드롭다운 메뉴의 값이 변경되었을 때 호출됩니다."""
        # 어떤 메뉴가 변경되었는지 확인하고 상태를 업데이트합니다.
        custom_id = interaction.data['custom_id']
        selected_value = interaction.data['values'][0]

        if custom_id == "ranking_category_select":
            self.current_category = selected_value
            # 카테고리가 '레벨'로 바뀌면 기간을 '종합'으로 강제합니다.
            if self.current_category == "level":
                self.current_period = "total"
        elif custom_id == "ranking_period_select":
            self.current_period = selected_value
        
        # 페이지를 처음으로 리셋하고 화면을 다시 그립니다.
        self.current_page = 0
        await self.update_display(interaction)

    async def on_pagination_click(self, interaction: discord.Interaction):
        """페이지네이션 버튼이 클릭되었을 때 호출됩니다."""
        if interaction.data['custom_id'] == "next_page":
            self.current_page += 1
        else:
            self.current_page -= 1
        await self.update_display(interaction)
        
    async def build_embed(self) -> discord.Embed:
        """현재 상태에 맞는 랭킹 데이터를 DB에서 가져와 임베드를 만듭니다."""
        offset = self.current_page * self.users_per_page
        
        # 선택된 카테고리와 기간에 따라 쿼리할 테이블과 컬럼을 결정합니다.
        category_info = self.category_map[self.current_category]
        column_name = category_info["column"]
        unit = category_info["unit"]

        if self.current_category == 'level':
            table_name = 'user_levels'
        else:
            table_name = f"{self.current_period}_stats"

        # 데이터베이스에서 랭킹 데이터를 가져옵니다.
        query = supabase.table(table_name).select('user_id', column_name, count='exact').order(column_name, desc=True).range(offset, offset + self.users_per_page - 1)
        res = await query.execute()

        # 총 페이지 수를 계산합니다.
        total_users = res.count if res and res.count is not None else 0
        self.total_pages = math.ceil(total_users / self.users_per_page)
        
        # 임베드 제목을 설정합니다.
        title = f"👑 {self.period_map[self.current_period]} {category_info['name']} ランキング"
        embed = discord.Embed(title=title, color=0xFFD700)

        # 랭킹 목록을 만듭니다.
        rank_list = []
        if res and hasattr(res, 'data') and res.data:
            for i, user_data in enumerate(res.data):
                rank = offset + i + 1
                user_id_int = int(user_data['user_id'])
                member = self.user.guild.get_member(user_id_int)
                name = member.display_name if member else f"ID: {user_id_int}"
                
                value = user_data.get(column_name, 0)
                
                # 레벨 랭킹일 경우, XP를 레벨로 변환하여 표시 (선택적, 현재는 XP로 표시)
                if self.current_category == 'level':
                    rank_list.append(f"`{rank}.` {name} - **`{value:,}`** {unit}")
                else:
                    rank_list.append(f"`{rank}.` {name} - **`{value:,}`** {unit}")

        embed.description = "\n".join(rank_list) if rank_list else "まだランキング情報がありません。"
        embed.set_footer(text=f"ページ {self.current_page + 1} / {self.total_pages}")
        return embed


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

    # [✅ 수정] '랭킹 확인' 버튼을 누르면 새로운 RankingView를 시작하도록 변경합니다.
    @ui.button(label="ランキング確認", style=discord.ButtonStyle.secondary, emoji="👑", custom_id="show_ranking_button")
    async def show_ranking_button(self, interaction: discord.Interaction, button: ui.Button):
        view = RankingView(interaction.user)
        await view.start(interaction)

class LevelSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("LevelSystem Cog (게임봇)가 성공적으로 초기화되었습니다.")
    
    async def register_persistent_views(self):
        self.bot.add_view(LevelPanelView(self))
        logger.info("✅ 레벨 시스템의 영구 View가 성공적으로 등록되었습니다。")
        
    async def load_configs(self):
        pass
    
    async def handle_level_up_event(self, user: discord.Member, result_data: List[Dict]):
        if not result_data or not result_data[0].get('leveled_up'): return
        
        new_level = result_data[0].get('new_level')
        logger.info(f"유저 {user.display_name}(ID: {user.id})가 레벨 {new_level}(으)로 레벨업했습니다.")
        
        handler_cog = self.bot.get_cog("JobAndTierHandler")
        if not handler_cog:
            logger.error("JobAndTierHandler Cog를 찾을 수 없습니다. 전직/등급 처리를 건너뜁니다.")
            return

        await handler_cog.update_tier_role(user, new_level)
        logger.info(f"{user.name}님의 등급 역할 업데이트를 요청했습니다.")

        job_advancement_levels = GAME_CONFIG.get("JOB_ADVANCEMENT_LEVELS", [50, 100])
        
        if new_level in job_advancement_levels:
            await handler_cog.start_advancement_process(user, new_level)
            logger.info(f"유저가 전직 가능 레벨({new_level})에 도달하여 전직 절차를 시작합니다.")

    async def update_user_xp_and_level_from_admin(self, user: discord.Member, xp_to_add: int = 0, exact_level: Optional[int] = None):
        try:
            if xp_to_add > 0:
                await log_activity(user.id, 'admin', xp_earned=xp_to_add)

            res = await supabase.table('user_levels').select('level, xp').eq('user_id', user.id).maybe_single().execute()
            current_data = res.data if res.data else {'level': 1, 'xp': 0}
            
            new_total_xp = current_data['xp']
            leveled_up = False

            if exact_level is not None:
                new_level = exact_level
                new_total_xp = calculate_xp_for_level(new_level)
                if new_level > current_data['level']: leveled_up = True
            else:
                new_total_xp += xp_to_add
                new_level = current_data['level']
                while new_total_xp >= calculate_xp_for_level(new_level + 1):
                    new_level += 1
                if new_level > current_data['level']: leveled_up = True
            
            await supabase.table('user_levels').upsert({'user_id': user.id, 'level': new_level, 'xp': new_total_xp}).execute()
            
            if leveled_up:
                await self.handle_level_up_event(user, [{"leveled_up": True, "new_level": new_level}])
        
        except Exception as e:
            logger.error(f"관리자 요청으로 레벨/XP 업데이트 중 오류 발생 (유저: {user.id}): {e}", exc_info=True)

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
                embed_data = {"title": "📊 レベル＆ランキング", "description": "下のボタンでご自身のレベルを確認したり、サーバーのランキングを見ることができます。", "color": 0x5865F2}
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
