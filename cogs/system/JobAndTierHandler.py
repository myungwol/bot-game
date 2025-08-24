# bot-game/cogs/systems/JobAndTierHandler.py

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
from typing import Dict, Any, List

from utils.database import supabase, get_config, get_id
from utils.game_config_defaults import JOB_SYSTEM_CONFIG, JOB_ADVANCEMENT_DATA
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

class JobAdvancementView(ui.View):
    def __init__(self, bot: commands.Bot, user: discord.Member, jobs: List[Dict[str, Any]]):
        super().__init__(timeout=3600)
        self.bot = bot
        self.user = user
        self.jobs_data = {job['job_key']: job for job in jobs}
        
        self.selected_job_key: str | None = None
        self.selected_ability_key: str | None = None

        self.build_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("自分専用のメニューです。", ephemeral=True)
            return False
        return True

    def build_components(self):
        self.clear_items()

        job_options = [
            discord.SelectOption(label=job['job_name'], value=job['job_key'], description=job['description'][:100])
            for job in self.jobs_data.values()
        ]
        job_select = ui.Select(placeholder="① まずは職業を選択してください...", options=job_options, custom_id="job_select")
        job_select.callback = self.on_job_select
        self.add_item(job_select)

        ability_options = []
        is_ability_disabled = True
        if self.selected_job_key:
            is_ability_disabled = False
            selected_job = self.jobs_data[self.selected_job_key]
            for ability in selected_job.get('abilities', []):
                ability_options.append(
                    discord.SelectOption(label=ability['ability_name'], value=ability['ability_key'], description=ability['description'][:100])
                )
        
        ability_placeholder = "② 次に能力を選択してください..." if self.selected_job_key else "先に職業を選択してください。"
        ability_select = ui.Select(placeholder=ability_placeholder, options=ability_options, disabled=is_ability_disabled, custom_id="ability_select")
        ability_select.callback = self.on_ability_select
        self.add_item(ability_select)

        is_confirm_disabled = not (self.selected_job_key and self.selected_ability_key)
        confirm_button = ui.Button(label="確定する", style=discord.ButtonStyle.success, disabled=is_confirm_disabled, custom_id="confirm_advancement")
        confirm_button.callback = self.on_confirm
        self.add_item(confirm_button)

    async def update_view(self, interaction: discord.Interaction):
        self.build_components()
        await interaction.response.edit_message(view=self)

    async def on_job_select(self, interaction: discord.Interaction):
        self.selected_job_key = interaction.data['values'][0]
        self.selected_ability_key = None
        await self.update_view(interaction)

    async def on_ability_select(self, interaction: discord.Interaction):
        self.selected_ability_key = interaction.data['values'][0]
        await self.update_view(interaction)

    async def on_confirm(self, interaction: discord.Interaction):
        await interaction.response.defer()

        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(content="しばらくお待ちください...", embed=None, view=self)
        self.stop()

        try:
            selected_job = self.jobs_data[self.selected_job_key]
            selected_ability = next(a for a in selected_job['abilities'] if a['ability_key'] == self.selected_ability_key)

            job_role_key = selected_job['role_key']
            all_job_role_keys = list(JOB_SYSTEM_CONFIG.get("JOB_ROLE_MAP", {}).values())
            
            roles_to_remove = []
            for key in all_job_role_keys:
                if (role_id := get_id(key)) and (role := interaction.guild.get_role(role_id)):
                    if role in self.user.roles and key != job_role_key:
                        roles_to_remove.append(role)
            if roles_to_remove:
                await self.user.remove_roles(*roles_to_remove, reason="전직으로 인한 이전 직업 역할 제거")

            if new_role_id := get_id(job_role_key):
                if new_role := interaction.guild.get_role(new_role_id):
                    await self.user.add_roles(new_role, reason="전직 완료")

            await supabase.rpc('set_user_job_and_ability', {
                'p_user_id': self.user.id, 'p_job_key': selected_job['job_key'], 'p_ability_key': selected_ability['ability_key']
            }).execute()

            log_channel_id = get_id("job_log_channel_id")
            if log_channel_id and (log_channel := self.bot.get_channel(log_channel_id)):
                embed_data = await get_embed_from_db("log_job_advancement")
                if embed_data:
                    log_embed = format_embed_from_db(
                        embed_data, user_mention=self.user.mention, job_name=selected_job['job_name'], ability_name=selected_ability['ability_name']
                    )
                    if self.user.display_avatar: log_embed.set_thumbnail(url=self.user.display_avatar.url)
                    await log_channel.send(embed=log_embed)

            await interaction.edit_original_response(content=f"🎉 **転職完了！**\nおめでとうございます！あなたは **{selected_job['job_name']}** になりました。", view=None)
            
            await asyncio.sleep(15)
            if isinstance(interaction.channel, discord.Thread):
                await interaction.channel.delete()

        except Exception as e:
            logger.error(f"전직 처리 중 오류 발생 (유저: {self.user.id}): {e}", exc_info=True)
            await interaction.edit_original_response(content="❌ 転職処理中にエラーが発生しました。管理者にお問い合わせください。", view=None)

class StartAdvancementView(ui.View):
    def __init__(self, bot: commands.Bot, user: discord.Member, jobs: List[Dict[str, Any]], level: int):
        super().__init__(timeout=3600)
        self.bot = bot
        self.user = user
        self.jobs_data = jobs
        self.level = level

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("自分専用のメニューです。", ephemeral=True)
            return False
        return True

    @ui.button(label="転職を開始する", style=discord.ButtonStyle.primary, emoji="✨")
    async def start_button(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(
            title=f"職業・能力選択 (レベル{self.level})",
            description="転職したい職業とその能力を一つずつ選択し、下の「確定する」ボタンを押してください。",
            color=0xFFD700
        )
        for job in self.jobs_data:
            ability_texts = []
            for ability in job.get('abilities', []):
                ability_texts.append(f"> **{ability['ability_name']}**: {ability['description']}")
            
            embed.add_field(
                name=f"【{job['job_name']}】",
                value=f"```{job['description']}```\n" + "\n".join(ability_texts),
                inline=False
            )
        
        view = JobAdvancementView(self.bot, self.user, self.jobs_data)
        await interaction.response.edit_message(embed=embed, view=view)
        self.stop()

class JobAndTierHandler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("JobAndTierHandler Cog (전직/등급 처리)가 성공적으로 초기화되었습니다.")

    # [✅✅✅ 핵심 수정] 전직 절차 시작 시, 유저의 현재 직업을 확인하고 상위 직업을 필터링합니다.
    async def start_advancement_process(self, member: discord.Member, level: int):
        try:
            channel_id = get_id("job_advancement_channel_id")
            if not (channel_id and (channel := self.bot.get_channel(channel_id))):
                logger.error(f"전직소 채널(job_advancement_channel_id)이 설정되지 않았거나 찾을 수 없습니다.")
                return

            if any(thread.name == f"転職｜{member.name}" for thread in channel.threads):
                logger.warning(f"{member.name}님의 전직 스레드가 이미 존재하여 생성을 건너뜁니다.")
                return

            # DB에서 유저의 현재 직업 정보를 가져옵니다.
            user_job_res = await supabase.table('user_jobs').select('jobs(job_key)').eq('user_id', member.id).maybe_single().execute()
            current_job_key = None
            if user_job_res and user_job_res.data and user_job_res.data.get('jobs'):
                current_job_key = user_job_res.data['jobs']['job_key']

            # 설정 파일에서 해당 레벨의 모든 전직 정보를 가져옵니다.
            all_advancement_jobs = JOB_ADVANCEMENT_DATA.get(level, [])
            
            # 유저의 현재 직업에 맞는 상위 직업만 필터링합니다.
            filtered_jobs = []
            for job_info in all_advancement_jobs:
                prerequisite = job_info.get("prerequisite_job")
                # 전직 조건이 없거나(Lv.50), 전직 조건이 현재 직업과 일치하는 경우에만 목록에 추가합니다.
                if not prerequisite or prerequisite == current_job_key:
                    filtered_jobs.append(job_info)

            # 표시할 상위 직업이 없으면 함수를 종료합니다.
            if not filtered_jobs:
                logger.warning(f"{member.name} (현재 직업: {current_job_key}) 님을 위한 레벨 {level} 상위 직업을 찾을 수 없습니다.")
                return

            # 필터링된 직업 목록으로 전직 절차를 시작합니다.
            thread = await channel.create_thread(name=f"転職｜{member.name}", type=discord.ChannelType.private_thread, invitable=False)
            await thread.add_user(member)
            
            embed = discord.Embed(
                title=f"🎉 レベル{level}達成！転職の時間です！",
                description=f"{member.mention}さん、新たな道へ進む時が来ました。\n\n"
                            "下のボタンを押して、転職手続きを開始してください。",
                color=0xFFD700
            )
            view = StartAdvancementView(self.bot, member, filtered_jobs, level)
            await thread.send(embed=embed, view=view)
            
            self.bot.add_view(view)
            logger.info(f"{member.name}님의 레벨 {level} 전직 스레드를 성공적으로 생성하고 View를 등록했습니다.")

        except Exception as e:
            logger.error(f"{member.name}님의 전직 절차 시작 중 오류 발생: {e}", exc_info=True)

    async def update_tier_role(self, member: discord.Member, level: int):
        try:
            guild = member.guild
            tier_roles_config = sorted(JOB_SYSTEM_CONFIG.get("LEVEL_TIER_ROLES", []), key=lambda x: x['level'], reverse=True)
            
            target_role_key = None
            for tier in tier_roles_config:
                if level >= tier['level']:
                    target_role_key = tier['role_key']
                    break
            
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
            
            if roles_to_add: 
                await member.add_roles(*roles_to_add, reason="레벨 달성 등급 역할 부여")
                logger.info(f"{member.name}님에게 등급 역할 '{roles_to_add[0].name}'을(를) 부여했습니다.")
            if roles_to_remove: 
                await member.remove_roles(*roles_to_remove, reason="레벨 변경 등급 역할 제거")
                logger.info(f"{member.name}님에게서 이전 등급 역할 {len(roles_to_remove)}개를 제거했습니다.")

        except Exception as e:
            logger.error(f"{member.name}님의 등급 역할 업데이트 처리 중 오류: {e}", exc_info=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(JobAndTierHandler(bot))
