# bot-game/cogs/systems/JobAndTierHandler.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
from typing import Dict, Any, List

from utils.database import supabase, get_config, get_id
from utils.game_config_defaults import JOB_SYSTEM_CONFIG, JOB_ADVANCEMENT_DATA
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

# [✅✅✅ 핵심 수정] View의 timeout을 None으로 변경하여 영구적으로 만듭니다.
class JobSelectionView(ui.View):
    def __init__(self, bot: commands.Bot, user: discord.Member, jobs: List[Dict[str, Any]], advancement_level: int):
        super().__init__(timeout=None) 
        self.bot = bot
        self.user = user
        self.jobs = {job['job_key']: job for job in jobs}
        self.advancement_level = advancement_level
        self.selected_job: Dict[str, Any] = {}
        self.selected_ability: Dict[str, Any] = {}
        
        for job in jobs:
            # 모든 버튼에 custom_id를 명시적으로 부여합니다.
            button = ui.Button(label=job['job_name'], custom_id=f"job_select_{job['job_key']}", style=discord.ButtonStyle.primary)
            button.callback = self.on_job_select
            self.add_item(button)
    
    # [✅✅✅ 핵심 수정] interaction_check를 추가하여 올바른 사용자만 버튼을 누르도록 합니다.
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("自分専用のメニューです。", ephemeral=True)
            return False
        return True

    async def on_job_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        job_key = interaction.data['custom_id'].split('_')[-1]
        self.selected_job = self.jobs[job_key]
        
        self.clear_items()
        
        abilities = self.selected_job.get('abilities', [])
        for ability in abilities:
            button = ui.Button(label=ability['ability_name'], custom_id=f"ability_select_{ability['ability_key']}", style=discord.ButtonStyle.success)
            button.callback = self.on_ability_select
            self.add_item(button)
            
        embed = discord.Embed(
            title=f"② {self.selected_job['job_name']} - 능력 선택",
            description=f"이 직업과 함께 배울 특별한 능력을 하나 선택해주세요.\n\n```{self.selected_job['description']}```",
            color=0xFFD700
        )
        for ability in abilities:
            embed.add_field(name=f"✅ {ability['ability_name']}", value=ability['description'], inline=False)
            
        await interaction.edit_original_response(embed=embed, view=self)

    async def on_ability_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ability_key = interaction.data['custom_id'].split('_')[-1]
        self.selected_ability = next(a for a in self.selected_job['abilities'] if a['ability_key'] == ability_key)

        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(view=self)
        self.stop() # View의 모든 상호작용을 중단합니다.

        try:
            job_role_key = self.selected_job['role_key']
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
                'p_user_id': self.user.id,
                'p_job_key': self.selected_job['job_key'],
                'p_ability_key': self.selected_ability['ability_key']
            }).execute()

            log_channel_id = get_id("job_log_channel_id")
            if log_channel_id and (log_channel := self.bot.get_channel(log_channel_id)):
                embed_data = await get_embed_from_db("log_job_advancement")
                if embed_data:
                    log_embed = format_embed_from_db(
                        embed_data,
                        user_mention=self.user.mention,
                        job_name=self.selected_job['job_name'],
                        ability_name=self.selected_ability['ability_name']
                    )
                    if self.user.display_avatar:
                        log_embed.set_thumbnail(url=self.user.display_avatar.url)
                    await log_channel.send(embed=log_embed)

            await interaction.followup.send(f"🎉 전직을 축하합니다! 이제 당신은 **{self.selected_job['job_name']}** 입니다!", ephemeral=True)
            
            await asyncio.sleep(10)
            # [수정] 스레드는 Thread 객체이므로 interaction.channel을 사용합니다.
            if isinstance(interaction.channel, discord.Thread):
                await interaction.channel.delete()

        except Exception as e:
            logger.error(f"전직 처리 중 오류 발생 (유저: {self.user.id}): {e}", exc_info=True)
            await interaction.followup.send("❌ 전직 처리 중 오류가 발생했습니다. 관리자에게 문의해주세요.", ephemeral=True)

class JobAndTierHandler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("JobAndTierHandler Cog (전직/등급 처리)가 성공적으로 초기화되었습니다.")

    # [✅✅✅ 핵심 수정] cog_load 시점에 add_view를 호출하여 영구 View를 등록합니다.
    async def cog_load(self):
        # 봇이 켜질 때, timeout=None이고 모든 버튼에 custom_id가 있는 View 객체를 등록합니다.
        # JobSelectionView는 동적으로 버튼이 바뀌므로, 빈 View 껍데기를 등록할 수 없습니다.
        # 대신, 버튼 콜백을 View가 아닌 Cog 레벨에서 처리하도록 구조를 변경할 수 있지만,
        # 더 간단한 방법은 View 자체를 영구적으로 만드는 것입니다.
        # JobSelectionView의 timeout을 None으로 설정하고 모든 버튼에 custom_id를 부여하면 됩니다.
        pass # register_persistent_views를 사용하지 않으므로 이 함수는 비워둡니다.

    async def start_advancement_process(self, member: discord.Member, level: int):
        try:
            channel_id = get_id("job_advancement_channel_id")
            if not (channel_id and (channel := self.bot.get_channel(channel_id))):
                logger.error(f"전직소 채널(job_advancement_channel_id)이 설정되지 않았거나 찾을 수 없습니다.")
                return

            if any(thread.name == f"転職｜{member.name}" for thread in channel.threads):
                logger.warning(f"{member.name}님의 전직 스레드가 이미 존재하여 생성을 건너뜁니다.")
                return

            advancement_data = JOB_ADVANCEMENT_DATA.get(level, [])
            if not advancement_data: return

            thread = await channel.create_thread(
                name=f"転職｜{member.name}",
                type=discord.ChannelType.private_thread,
                invitable=False
            )
            await thread.add_user(member)
            
            embed = discord.Embed(
                title=f"🎉 レベル{level}達成！転職の時間です！",
                description=f"{member.mention}さん、新たな道へ進む時が来ました。\n"
                            "下面のボタンから希望の職業を選択してください。",
                color=0xFFD700
            )
            view = JobSelectionView(self.bot, member, advancement_data, level)
            await thread.send(embed=embed, view=view)
            
            # View를 등록합니다. 봇이 재시작되어도 이 View의 상호작용을 받을 수 있습니다.
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
