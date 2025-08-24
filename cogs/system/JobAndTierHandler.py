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

# [✅✅✅ 핵심 수정] 두 View 모두 완전한 영구 View로 재설계합니다.

class JobAdvancementView(ui.View):
    def __init__(self, bot: commands.Bot, user_id: int, jobs: List[Dict[str, Any]], level: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.user_id = user_id
        self.jobs_data = {job['job_key']: job for job in jobs}
        self.level = level
        
        # 컴포넌트 빌드
        self.build_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # custom_id에 user_id를 넣는 대신, View가 생성될 때 user_id를 알고 있으므로 여기서 체크합니다.
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("自分専用のメニューです。", ephemeral=True)
            return False
        return True

    def build_components(self, selected_job_key: str | None = None, selected_ability_key: str | None = None):
        """UI 컴포넌트를 현재 선택 상태에 맞게 다시 그립니다."""
        self.clear_items()

        job_options = [
            discord.SelectOption(label=job['job_name'], value=job['job_key'], description=job['description'][:100])
            for job in self.jobs_data.values()
        ]
        job_select = ui.Select(placeholder="① まずは職業を選択してください...", options=job_options, custom_id="job_adv_job_select")
        job_select.callback = self.on_job_select
        self.add_item(job_select)

        ability_options = []
        is_ability_disabled = True
        if selected_job_key:
            is_ability_disabled = False
            selected_job = self.jobs_data[selected_job_key]
            for ability in selected_job.get('abilities', []):
                ability_options.append(
                    discord.SelectOption(label=ability['ability_name'], value=ability['ability_key'], description=ability['description'][:100])
                )
        
        ability_placeholder = "② 次に能力を選択してください..." if selected_job_key else "先に職業を選択してください。"
        ability_select = ui.Select(placeholder=ability_placeholder, options=ability_options, disabled=is_ability_disabled, custom_id="job_adv_ability_select")
        ability_select.callback = self.on_ability_select
        self.add_item(ability_select)

        is_confirm_disabled = not (selected_job_key and selected_ability_key)
        confirm_button = ui.Button(label="確定する", style=discord.ButtonStyle.success, disabled=is_confirm_disabled, custom_id="job_adv_confirm")
        confirm_button.callback = self.on_confirm
        self.add_item(confirm_button)

    async def on_job_select(self, interaction: discord.Interaction):
        selected_job_key = interaction.data['values'][0]
        self.build_components(selected_job_key=selected_job_key)
        await interaction.response.edit_message(view=self)

    async def on_ability_select(self, interaction: discord.Interaction):
        # 능력 선택 시, 현재 직업 선택 드롭다운의 값을 가져와야 합니다.
        job_select = discord.utils.get(self.children, custom_id='job_adv_job_select')
        if not job_select or not job_select.values:
            # 이 경우는 거의 없지만, 안전장치
            await interaction.response.send_message("先に職業を選択してください。", ephemeral=True)
            return
        
        selected_job_key = job_select.values[0]
        selected_ability_key = interaction.data['values'][0]
        self.build_components(selected_job_key=selected_job_key, selected_ability_key=selected_ability_key)
        await interaction.response.edit_message(view=self)

    async def on_confirm(self, interaction: discord.Interaction):
        await interaction.response.defer()

        job_select = discord.utils.get(self.children, custom_id='job_adv_job_select')
        ability_select = discord.utils.get(self.children, custom_id='job_adv_ability_select')

        if not (job_select and job_select.values and ability_select and ability_select.values):
            await interaction.followup.send("職業と能力の両方を選択してください。", ephemeral=True)
            return

        selected_job_key = job_select.values[0]
        selected_ability_key = ability_select.values[0]
        
        for item in self.children: item.disabled = True
        await interaction.edit_original_response(content="しばらくお待ちください...", embed=None, view=self)
        self.stop()

        try:
            user = await interaction.guild.fetch_member(self.user_id)
            if not user:
                raise Exception("유저를 찾을 수 없습니다.")

            selected_job = self.jobs_data[selected_job_key]
            selected_ability = next(a for a in selected_job['abilities'] if a['ability_key'] == selected_ability_key)

            job_role_key = selected_job['role_key']
            all_job_role_keys = list(JOB_SYSTEM_CONFIG.get("JOB_ROLE_MAP", {}).values())
            
            roles_to_remove = [role for key in all_job_role_keys if (role_id := get_id(key)) and (role := interaction.guild.get_role(role_id)) and role in user.roles and key != job_role_key]
            if roles_to_remove: await user.remove_roles(*roles_to_remove, reason="전직으로 인한 이전 직업 역할 제거")

            if new_role_id := get_id(job_role_key):
                if new_role := interaction.guild.get_role(new_role_id):
                    await user.add_roles(new_role, reason="전직 완료")

            await supabase.rpc('set_user_job_and_ability', {'p_user_id': user.id, 'p_job_key': selected_job['job_key'], 'p_ability_key': selected_ability['ability_key']}).execute()

            if log_channel_id := get_id("job_log_channel_id"):
                if log_channel := self.bot.get_channel(log_channel_id):
                    if embed_data := await get_embed_from_db("log_job_advancement"):
                        log_embed = format_embed_from_db(embed_data, user_mention=user.mention, job_name=selected_job['job_name'], ability_name=selected_ability['ability_name'])
                        if user.display_avatar: log_embed.set_thumbnail(url=user.display_avatar.url)
                        await log_channel.send(embed=log_embed)

            await interaction.edit_original_response(content=f"🎉 **転職完了！**\nおめでとうございます！あなたは **{selected_job['job_name']}** になりました。", view=None)
            await asyncio.sleep(15)
            if isinstance(interaction.channel, discord.Thread): await interaction.channel.delete()
        except Exception as e:
            logger.error(f"전직 처리 중 오류 발생 (유저: {self.user_id}): {e}", exc_info=True)
            await interaction.edit_original_response(content="❌ 転職処理中にエラーが発生しました。管理者にお問い合わせください。", view=None)

class StartAdvancementView(ui.View):
    def __init__(self, bot: commands.Bot, user_id: int, jobs: List[Dict[str, Any]], level: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.user_id = user_id
        self.jobs_data = jobs
        self.level = level
        self.start_button.custom_id = f"start_advancement_{self.user_id}_{self.level}"

    @ui.button(label="転職を開始する", style=discord.ButtonStyle.primary, emoji="✨")
    async def start_button(self, interaction: discord.Interaction, button: ui.Button):
        self.stop()
        
        embed = discord.Embed(
            title=f"職業・能力選択 (レベル{self.level})",
            description="転職したい職業とその能力を一つずつ選択し、下の「確定する」ボタンを押してください。",
            color=0xFFD700
        )
        for job in self.jobs_data:
            ability_texts = [f"> **{ability['ability_name']}**: {ability['description']}" for ability in job.get('abilities', [])]
            embed.add_field(name=f"【{job['job_name']}】", value=f"```{job['description']}```\n" + "\n".join(ability_texts), inline=False)
        
        view = JobAdvancementView(self.bot, self.user_id, self.jobs_data, self.level)
        await interaction.response.edit_message(embed=embed, view=view)
        # 생성된 새 View도 봇 재시작에 대비해 등록합니다.
        self.bot.add_view(view)

class JobAndTierHandler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_views_loaded = False
        logger.info("JobAndTierHandler Cog (전직/등급 처리)가 성공적으로 초기화되었습니다.")
        
    @commands.Cog.listener()
    async def on_ready(self):
        """봇이 준비되면, 이전에 생성된 모든 전직 스레드를 찾아 View를 다시 등록합니다."""
        if self.active_views_loaded:
            return
        
        logger.info("이전에 활성화된 전직 View들을 다시 로드합니다...")
        channel_id = get_id("job_advancement_channel_id")
        if not (channel_id and (channel := self.bot.get_channel(channel_id))):
            logger.warning("전직소 채널을 찾을 수 없어 활성 View를 로드할 수 없습니다.")
            self.active_views_loaded = True
            return

        for thread in channel.threads:
            try:
                # 스레드 이름에서 유저 이름과 레벨을 파싱
                parts = thread.name.split('｜')
                if len(parts) != 2 or not parts[0] == "転職": continue
                
                user = thread.owner
                if not user:
                    # owner가 None일 경우, 아카이브된 스레드일 수 있으므로 fetch_members()를 시도
                    async for member in thread.fetch_members():
                        if member.id == thread.owner_id:
                            user = member
                            break
                if not user:
                    logger.warning(f"스레드 '{thread.name}'의 소유자를 찾을 수 없어 View를 로드할 수 없습니다.")
                    continue
                
                # 메시지 기록을 확인하여 어떤 View를 붙여야 할지 결정
                async for message in thread.history(limit=5, oldest_first=True):
                    if message.author.id == self.bot.user.id and message.components:
                        # View를 식별하기 위해 첫 번째 버튼의 custom_id를 확인
                        comp = message.components[0].children[0]
                        if isinstance(comp, discord.Button) and comp.custom_id and comp.custom_id.startswith("start_advancement_"):
                            level = int(comp.custom_id.split('_')[-1])
                            # 여기서 다시 View를 만들어 등록
                            advancement_data = JOB_ADVANCEMENT_DATA.get(level, [])
                            view = StartAdvancementView(self.bot, user.id, advancement_data, level)
                            self.bot.add_view(view, message_id=message.id)
                            logger.info(f"'{thread.name}' 스레드에서 StartAdvancementView를 다시 로드했습니다.")
                        elif isinstance(comp, discord.ui.Select) and comp.custom_id == "job_adv_job_select":
                            # 이미 전직 선택 화면으로 넘어간 경우
                            # 이 경우, 레벨과 직업 데이터를 다시 가져와야 함
                            level_res = await supabase.table('user_levels').select('level').eq('user_id', user.id).single().execute()
                            level = level_res.data['level']
                            advancement_data = JOB_ADVANCEMENT_DATA.get(level, [])
                            view = JobAdvancementView(self.bot, user.id, advancement_data, level)
                            self.bot.add_view(view, message_id=message.id)
                            logger.info(f"'{thread.name}' 스레드에서 JobAdvancementView를 다시 로드했습니다.")
                        break # 가장 오래된 봇 메시지 하나만 확인
            except Exception as e:
                logger.error(f"스레드 '{thread.name}'의 View를 다시 로드하는 중 오류 발생: {e}", exc_info=True)
        
        self.active_views_loaded = True
        logger.info("활성 전직 View 로드가 완료되었습니다.")

    async def start_advancement_process(self, member: discord.Member, level: int):
        try:
            channel_id = get_id("job_advancement_channel_id")
            if not (channel_id and (channel := self.bot.get_channel(channel_id))):
                logger.error(f"전직소 채널(job_advancement_channel_id)이 설정되지 않았거나 찾을 수 없습니다.")
                return

            if any(thread.name == f"転職｜{member.name}" for thread in channel.threads):
                logger.warning(f"{member.name}님의 전직 스레드가 이미 존재하여 생성을 건너뜁니다.")
                return

            user_job_res = await supabase.table('user_jobs').select('jobs(job_key)').eq('user_id', member.id).maybe_single().execute()
            current_job_key = None
            if user_job_res and user_job_res.data and user_job_res.data.get('jobs'):
                current_job_key = user_job_res.data['jobs']['job_key']

            all_advancement_jobs = JOB_ADVANCEMENT_DATA.get(level, [])
            
            filtered_jobs = [job_info for job_info in all_advancement_jobs if not (prerequisite := job_info.get("prerequisite_job")) or prerequisite == current_job_key]

            if not filtered_jobs:
                if level >= 100 and not current_job_key:
                    logger.warning(f"{member.name}님은 1차 전직을 하지 않아 2차 전직을 진행할 수 없습니다.")
                    try: await member.send(f"レベル{level}転職のご案内\n2次転職のためには、まずレベル50の転職を完了する必要があります。")
                    except discord.Forbidden: pass
                else: logger.warning(f"{member.name} (현재 직업: {current_job_key}) 님을 위한 레벨 {level} 상위 직업을 찾을 수 없습니다.")
                return

            thread = await channel.create_thread(name=f"転職｜{member.name}", type=discord.ChannelType.private_thread, invitable=False)
            await thread.add_user(member)
            
            embed = discord.Embed(
                title=f"🎉 レベル{level}達成！転職の時間です！",
                description=f"{member.mention}さん、新たな道へ進む時が来ました。\n\n"
                            "下のボタンを押して、転職手続きを開始してください。",
                color=0xFFD700
            )
            
            view = StartAdvancementView(self.bot, member.id, filtered_jobs, level)
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
            
            if roles_to_add: await member.add_roles(*roles_to_add, reason="레벨 달성 등급 역할 부여")
            if roles_to_remove: await member.remove_roles(*roles_to_remove, reason="레벨 변경 등급 역할 제거")

        except Exception as e:
            logger.error(f"{member.name}님의 등급 역할 업데이트 처리 중 오류: {e}", exc_info=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(JobAndTierHandler(bot))
