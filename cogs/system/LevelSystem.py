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
    # [✅ 추가] 패널 생성을 위해 임베드 DB 함수를 가져옵니다.
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
        
        # [수정] cooldown key에 유저 ID 포함
        cooldown_key = f"level_check_cooldown_{user.id}"
        cooldown_seconds = 60

        last_used = await get_cooldown(user.id, cooldown_key)
        if time.time() - last_used < cooldown_seconds:
            can_use_time = int(last_used + cooldown_seconds)
            await interaction.response.send_message(f"⏳ このボタンは <t:{can_use_time}:R> に再度使用できます。", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        try:
            # [수정] cooldown key에 유저 ID 포함
            await set_cooldown(user.id, cooldown_key)
            
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
            
            details = [f"> {source}: `{amount:,} XP`" for source, amount in aggregated_xp.items() if amount > 0]
            xp_details_text = "\n".join(details)
            
            xp_bar = create_xp_bar(xp_in_current_level, required_xp_for_this_level)
            embed = discord.Embed(color=user.color or discord.Color.blue())
            if user.display_avatar:
                embed.set_thumbnail(url=user.display_avatar.url)

            description_parts = [
                f"## {user.mention}のステータス\n",
                f"**レベル**: **Lv. {current_level}**",
                f"**等級**: {tier_role_mention or '`かけだし住民`'}\n**職業**: {job_role_mention or '`なし`'}\n",
                f"**経験値**\n`{xp_in_current_level:,} / {required_xp_for_this_level:,}`",
                f"{xp_bar}\n",
                f"**🏆 総獲得経験値**\n`{total_xp:,} XP`\n",
            ]
            if xp_details_text:
                description_parts.extend([f"**📊 経験値獲得の内訳**\n{xp_details_text}"])

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

class LevelSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("LevelSystem Cog (게임봇)가 성공적으로 초기화되었습니다.")
    
    async def register_persistent_views(self):
        self.bot.add_view(LevelPanelView(self))
        logger.info("✅ 레벨 시스템의 영구 View가 성공적으로 등록되었습니다.")
        
    async def load_configs(self):
        pass
            
    async def handle_level_up_event(self, user: discord.Member, result_data: Dict):
        if not result_data: return
        
        new_level = result_data.get('new_level')
        logger.info(f"유저 {user.display_name}(ID: {user.id})가 레벨 {new_level}(으)로 레벨업했습니다.")
        
        game_config = get_config("GAME_CONFIG", {})
        job_advancement_levels = game_config.get("JOB_ADVANCEMENT_LEVELS", [50, 100])
        
        timestamp = time.time()
        
        if new_level in job_advancement_levels:
            await save_config_to_db(f"job_advancement_request_{user.id}", {"level": new_level, "timestamp": timestamp})
            logger.info(f"유저가 전직 가능 레벨({new_level})에 도달하여 관리 봇에게 전직 요청을 보냈습니다.")

        await save_config_to_db(f"level_tier_update_request_{user.id}", {"level": new_level, "timestamp": timestamp})
        logger.info(f"유저의 레벨이 변경되어 관리 봇에게 등급 역할 업데이트 요청을 보냈습니다.")

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_level_check") -> bool:
        try:
            # 이전 패널 메시지 삭제
            panel_info = get_panel_id(panel_key)
            if panel_info and panel_info.get('channel_id') and panel_info.get('message_id'):
                if (ch := self.bot.get_channel(panel_info['channel_id'])):
                    try: 
                        msg = await ch.fetch_message(panel_info['message_id'])
                        await msg.delete()
                    except (discord.NotFound, discord.Forbidden): pass
            
            # [✅✅✅ 핵심 수정 ✅✅✅]
            # DB에서 패널용 임베드 정보를 가져옵니다.
            embed_data = await get_embed_from_db("panel_level_check")
            if not embed_data:
                # DB에 정보가 없을 경우를 대비한 기본값
                embed_data = {
                    "title": "📊 レベル＆ランキング",
                    "description": "下のボタンでご自身のレベルを確認したり、サーバーのランキングを見ることができます。",
                    "color": 0x5865F2
                }
                logger.warning(f"DB에서 'panel_level_check' 임베드를 찾을 수 없어 기본값으로 패널을 생성합니다.")

            embed = discord.Embed.from_dict(embed_data)
            
            # 새로운 패널 메시지 전송 및 DB에 ID 저장
            message = await channel.send(embed=embed, view=LevelPanelView(self))
            await save_panel_id(panel_key, message.id, channel.id)
            
            logger.info(f"✅ 「{panel_key}」パネルを #{channel.name} に再設置しました。")
            return True
        except Exception as e:
            logger.error(f"「{panel_key}」パネルの再設置中にエラー: {e}", exc_info=True)
            return False

async def setup(bot: commands.Bot):
    await bot.add_cog(LevelSystem(bot))
