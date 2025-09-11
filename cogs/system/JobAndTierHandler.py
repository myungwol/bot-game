# bot-game/cogs/systems/JobAndTierHandler.py

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
from typing import Dict, Any, List

from utils.database import supabase, get_config, get_id, get_embed_from_db
# ▼ 아래 라인에서 JOB_SYSTEM_CONFIG와 JOB_ADVANCEMENT_DATA를 삭제합니다.
# from utils.game_config_defaults import JOB_SYSTEM_CONFIG, JOB_ADVANCEMENT_DATA
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

class JobAdvancementView(ui.View):
    def __init__(self, bot: commands.Bot, user_id: int, jobs: List[Dict[str, Any]], level: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.user_id = user_id
        self.jobs_data = {job['job_key']: job for job in jobs}
        self.level = level
        
        self.selected_job_key: str | None = None
        self.selected_ability_key: str | None = None
        
        self.build_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("이 메뉴는 본인만 사용할 수 있습니다.", ephemeral=True)
            return False
        return True

    def build_components(self):
        self.clear_items()

        job_options = []
        if self.jobs_data:
            job_options = [
                discord.SelectOption(label=job['job_name'], value=job['job_key'], description=job['description'][:100])
                for job in self.jobs_data.values()
            ]
        else:
            job_options.append(discord.SelectOption(label="선택할 수 있는 직업이 없습니다.", value="no_jobs_available", default=True))
        
        job_select = ui.Select(placeholder="① 먼저 직업을 선택해주세요...", options=job_options, custom_id="job_adv_job_select", disabled=(not self.jobs_data))
        if self.selected_job_key:
            job_select.placeholder = self.jobs_data[self.selected_job_key]['job_name']
        job_select.callback = self.on_job_select
        self.add_item(job_select)

        ability_options = []
        is_ability_disabled = True
        ability_placeholder = "먼저 직업을 선택해주세요."
        if self.selected_job_key and self.selected_job_key in self.jobs_data:
            is_ability_disabled = False
            ability_placeholder = "② 다음으로 능력을 선택해주세요..."
            selected_job = self.jobs_data[self.selected_job_key]
            for ability in selected_job.get('abilities', []):
                ability_options.append(discord.SelectOption(label=ability['ability_name'], value=ability['ability_key'], description=ability['description'][:100]))
        
        if not ability_options:
            ability_options.append(discord.SelectOption(label="선택 가능한 능력이 없습니다.", value="no_abilities_placeholder", default=True))

        ability_select = ui.Select(placeholder=ability_placeholder, options=ability_options, disabled=is_ability_disabled, custom_id="job_adv_ability_select")
        if self.selected_ability_key:
            selected_job = self.jobs_data[self.selected_job_key]
            ability_name = next((a['ability_name'] for a in selected_job['abilities'] if a['ability_key'] == self.selected_ability_key), "능력 선택")
            ability_select.placeholder = ability_name
        ability_select.callback = self.on_ability_select
        self.add_item(ability_select)

        is_confirm_disabled = not (self.selected_job_key and self.selected_ability_key)
        confirm_button = ui.Button(label="결정하기", style=discord.ButtonStyle.success, disabled=is_confirm_disabled, custom_id="job_adv_confirm")
        confirm_button.callback = self.on_confirm
        self.add_item(confirm_button)

    async def on_job_select(self, interaction: discord.Interaction):
        if interaction.data['values'][0] == "no_jobs_available": return await interaction.response.defer()
        
        self.selected_job_key = interaction.data['values'][0]
        self.selected_ability_key = None
        
        self.build_components()
        await interaction.response.edit_message(view=self)

    async def on_ability_select(self, interaction: discord.Interaction):
        if interaction.data['values'][0] == "no_abilities_placeholder": return await interaction.response.defer()
        
        self.selected_ability_key = interaction.data['values'][0]
        
        self.build_components()
        await interaction.response.edit_message(view=self)

    async def on_confirm(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not (self.selected_job_key and self.selected_ability_key):
            await interaction.followup.send("직업과 능력을 모두 선택해주세요.", ephemeral=True)
            return

        for item in self.children: item.disabled = True
        await interaction.edit_original_response(content="잠시만 기다려주세요... 전직 절차를 진행 중입니다.", embed=None, view=self)
        self.stop()

        try:
            user = await interaction.guild.fetch_member(self.user_id)
            if not user: raise Exception("유저를 찾을 수 없습니다.")

            selected_job_data = self.jobs_data[self.selected_job_key]
            selected_ability_data = next(a for a in selected_job_data['abilities'] if a['ability_key'] == self.selected_ability_key)

            job_res = await supabase.table('jobs').select('id').eq('job_key', self.selected_job_key).single().execute()
            ability_res = await supabase.table('abilities').select('id').eq('ability_key', self.selected_ability_key).single().execute()
            if not (job_res.data and ability_res.data):
                raise Exception(f"DB에서 직업 또는 능력 ID를 찾을 수 없습니다.")
            
            job_id, ability_id = job_res.data['id'], ability_res.data['id']
            
            job_role_key = selected_job_data['role_key']
            
            # ▼ DB에서 JOB_SYSTEM_CONFIG를 가져오도록 수정
            job_system_config = get_config("JOB_SYSTEM_CONFIG", {})
            all_job_role_keys = list(job_system_config.get("JOB_ROLE_MAP", {}).values())
            
            roles_to_remove = [role for key in all_job_role_keys if (role_id := get_id(key)) and (role := interaction.guild.get_role(role_id)) and role in user.roles and key != job_role_key]
            if roles_to_remove: await user.remove_roles(*roles_to_remove, reason="전직으로 인한 이전 직업 역할 제거")

            if new_role_id := get_id(job_role_key):
                if new_role := interaction.guild.get_role(new_role_id):
                    await user.add_roles(new_role, reason="전직 완료")

            await supabase.rpc('set_user_job_and_ability', {'p_user_id': user.id, 'p_job_id': job_id, 'p_ability_id': ability_id}).execute()

            if log_channel_id := get_id("job_log_channel_id"):
                if log_channel := self.bot.get_channel(log_channel_id):
                    if embed_data := await get_embed_from_db("log_job_advancement"):
                        log_embed = format_embed_from_db(embed_data, user_mention=user.mention, job_name=selected_job_data['job_name'], ability_name=selected_ability_data['ability_name'])
                        if user.display_avatar: log_embed.set_thumbnail(url=user.display_avatar.url)
                        await log_channel.send(embed=log_embed)
            
            job_name = selected_job_data['job_name']
            success_message = f"🎉 **전직 완료!**\n축하합니다! 이제부터 당신은 **{job_name}** 입니다."
            await interaction.edit_original_response(content=success_message, view=None)
            await asyncio.sleep(15)
            if isinstance(interaction.channel, discord.Thread): await interaction.channel.delete()
        except Exception as e:
            logger.error(f"전직 처리 중 오류가 발생했습니다 (유저: {self.user_id}): {e}", exc_info=True)
            await interaction.edit_original_response(content="❌ 전직 처리 중 오류가 발생했습니다. 관리자에게 문의해주세요.", view=None)

class StartAdvancementView(ui.View):
    def __init__(self, bot: commands.Bot, user_id: int, jobs: List[Dict[str, Any]], level: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.user_id = user_id
        self.jobs_data = jobs
        self.level = level
        self.start_button.custom_id = f"start_advancement_{self.user_id}_{self.level}"

    @ui.button(label="전직 시작하기", style=discord.ButtonStyle.primary, emoji="✨")
    async def start_button(self, interaction: discord.Interaction, button: ui.Button):
        self.stop()
        
        embed = discord.Embed(
            title=f"직업·능력 선택 (레벨 {self.level})",
            description="전직할 직업과 습득할 능력을 하나씩 선택한 후, '결정하기' 버튼을 눌러주세요.",
            color=0xFFD700
        )
        for job in self.jobs_data:
            ability_texts = [f"> **{ability['ability_name']}**: {ability['description']}" for ability in job.get('abilities', [])]
            embed.add_field(name=f"【 {job['job_name']} 】", value=f"```{job['description']}```\n" + "\n".join(ability_texts), inline=False)
        
        view = JobAdvancementView(self.bot, interaction.user.id, self.jobs_data, self.level)
        await interaction.response.edit_message(embed=embed, view=view)
        self.bot.add_view(view)

class JobAndTierHandler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_views_loaded = False
        logger.info("JobAndTierHandler Cog (전직/등급 처리)가 성공적으로 초기화되었습니다.")
        
    @commands.Cog.listener()
    async def on_ready(self):
        if self.active_views_loaded: return
        logger.info("이전에 활성화된 전직 View를 다시 로드합니다...")
        channel_id = get_id("job_advancement_channel_id")
        if not (channel_id and (channel := self.bot.get_channel(channel_id))):
            logger.warning("'전직소' 채널을 찾을 수 없어 활성 View를 로드할 수 없습니다.")
            self.active_views_loaded = True
            return

        active_threads = channel.threads
        try:
            archived_threads = [t async for t in channel.archived_threads(limit=None)]
            active_threads.extend(archived_threads)
        except Exception as e:
            logger.error(f"아카이브된 스레드를 가져오는 중 오류가 발생했습니다: {e}")

        for thread in active_threads:
            try:
                if not thread.name.startswith("전직｜"): continue
                
                owner_id = thread.owner_id
                if not owner_id: continue

                async for message in thread.history(limit=5, oldest_first=True):
                    if message.author.id == self.bot.user.id and message.components:
                        comp = message.components[0].children[0]
                        if isinstance(comp, discord.Button) and comp.custom_id and comp.custom_id.startswith("start_advancement_"):
                            level = int(comp.custom_id.split('_')[-1])
                            # ▼ DB에서 JOB_ADVANCEMENT_DATA를 가져오도록 수정
                            advancement_data = get_config("JOB_ADVANCEMENT_DATA", {}).get(str(level), [])
                            view = StartAdvancementView(self.bot, owner_id, advancement_data, level)
                            self.bot.add_view(view, message_id=message.id)
                            logger.info(f"'{thread.name}' 스레드에서 StartAdvancementView를 다시 로드했습니다.")
                        elif isinstance(comp, discord.ui.Select) and comp.custom_id == "job_adv_job_select":
                            # TODO: JobAdvancementView 복구 로직 (필요 시 구현)
                            pass
                        break
            except Exception as e:
                logger.error(f"스레드 '{thread.name}'의 View를 다시 로드하는 중 오류가 발생했습니다: {e}", exc_info=True)
        
        self.active_views_loaded = True
        logger.info("활성 전직 View 로드가 완료되었습니다.")

    async def start_advancement_process(self, member: discord.Member, level: int):
        try:
            channel_id = get_id("job_advancement_channel_id")
            if not (channel_id and (channel := self.bot.get_channel(channel_id))):
                logger.error(f"'전직소' 채널(job_advancement_channel_id)이 설정되지 않았거나 찾을 수 없습니다.")
                return

            if any(thread.name == f"전직｜{member.name}" for thread in channel.threads):
                logger.warning(f"{member.name}님의 전직 스레드가 이미 존재하여 생성을 건너뜁니다.")
                return

            user_job_res = await supabase.table('user_jobs').select('jobs(job_key)').eq('user_id', member.id).maybe_single().execute()
            current_job_key = user_job_res.data['jobs']['job_key'] if user_job_res and user_job_res.data and user_job_res.data.get('jobs') else None

            all_advancement_data = get_config("JOB_ADVANCEMENT_DATA", {})
            
            # ▼▼▼ [핵심 수정] 전직 가능 직업 필터링 및 따라잡기 로직 ▼▼▼
            target_level_for_advancement = 0
            advancement_title = ""
            advancement_description = ""

            if level >= 100 and not current_job_key:
                # 100레벨 이상이지만 1차 전직을 안 한 경우
                target_level_for_advancement = 50
                advancement_title = "🎉 100레벨 달성! 2차 전직을 위한 준비!"
                advancement_description = f"{member.mention}님, 100레벨 달성을 축하합니다!\n2차 전직을 진행하기 전에, 먼저 당신의 길을 결정할 1차 전직을 완료해야 합니다.\n\n아래 버튼을 눌러 1차 전직 절차를 시작해주세요."
            elif level >= 100 and current_job_key:
                # 정상적인 2차 전직
                target_level_for_advancement = 100
            elif level >= 50 and not current_job_key:
                # 정상적인 1차 전직
                target_level_for_advancement = 50

            # target_level_for_advancement가 0이면 전직 대상이 아님
            if target_level_for_advancement == 0:
                logger.info(f"{member.name} (레벨: {level}, 직업: {current_job_key})님은 현재 전직 대상이 아닙니다.")
                return

            available_jobs = all_advancement_data.get(str(target_level_for_advancement), [])
            filtered_jobs = [
                job_info for job_info in available_jobs 
                if not (prerequisite := job_info.get("prerequisite_job")) or prerequisite == current_job_key
            ]
            # ▲▲▲ [핵심 수정] 종료 ▲▲▲

            if not filtered_jobs:
                logger.warning(f"{member.name} (현재 직업: {current_job_key}) 님을 위한 레벨 {target_level_for_advancement} 상위 직업을 찾을 수 없습니다.")
                return

            thread = await channel.create_thread(name=f"전직｜{member.name}", type=discord.ChannelType.private_thread, invitable=False)
            await thread.add_user(member)
            
            # [수정] 동적 제목 및 설명 사용
            if not advancement_title:
                advancement_title = f"🎉 레벨 {target_level_for_advancement} 달성! 새로운 길을 개척할 시간입니다!"
            if not advancement_description:
                advancement_description = f"{member.mention}님, 새로운 능력을 얻을 때가 왔습니다.\n\n아래 버튼을 눌러 전직 절차를 시작해주세요."

            embed = discord.Embed(title=advancement_title, description=advancement_description, color=0xFFD700)
            
            view = StartAdvancementView(self.bot, member.id, filtered_jobs, target_level_for_advancement)
            await thread.send(embed=embed, view=view)
            
            self.bot.add_view(view)
            logger.info(f"{member.name}님의 레벨 {target_level_for_advancement} 전직 스레드를 성공적으로 생성하고 View를 등록했습니다.")
        except Exception as e:
            logger.error(f"{member.name}님의 전직 절차 시작 중 오류가 발생했습니다: {e}", exc_info=True)
            
    async def update_tier_role(self, member: discord.Member, level: int):
        try:
            guild = member.guild
            # ▼ DB에서 JOB_SYSTEM_CONFIG를 가져오도록 수정
            job_system_config = get_config("JOB_SYSTEM_CONFIG", {})
            tier_roles_config = sorted(job_system_config.get("LEVEL_TIER_ROLES", []), key=lambda x: x['level'], reverse=True)
            
            target_role_key = next((tier['role_key'] for tier in tier_roles_config if level >= tier['level']), None)
            if not target_role_key: return

            all_tier_role_ids = {get_id(tier['role_key']) for tier in tier_roles_config if get_id(tier['role_key'])}
            target_role_id = get_id(target_role_key)

            roles_to_add = []
            roles_to_remove = []

            if target_role_id and not member.get_role(target_role_id):
                if role_obj := guild.get_role(target_role_id):
                    roles_to_add.append(role_obj)
            
            for role in member.roles:
                if role.id in all_tier_role_ids and role.id != target_role_id:
                    roles_to_remove.append(role)
            
            if roles_to_add: await member.add_roles(*roles_to_add, reason=f"레벨 {level} 달성, 등급 역할 부여")
            if roles_to_remove: await member.remove_roles(*roles_to_remove, reason="레벨 변경으로 인한 등급 역할 조정")

        except Exception as e:
            logger.error(f"{member.name}님의 등급 역할 업데이트 처리 중 오류가 발생했습니다: {e}", exc_info=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(JobAndTierHandler(bot))
