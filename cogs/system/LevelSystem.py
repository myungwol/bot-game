# bot-management/cogs/server/LevelSystem.py

import discord
from discord.ext import commands
from discord import ui
import logging
from utils.database import supabase, get_panel_id, save_panel_id, get_embed_from_db
from utils.helpers import format_embed_from_db # helpers가 있다면 사용

logger = logging.getLogger(__name__)

def create_xp_bar(current_xp, required_xp, length=10):
    if required_xp == 0: return "Lv.MAX"
    progress = min(current_xp / required_xp, 1.0)
    filled_length = int(length * progress)
    bar = '█' * filled_length + '░' * (length - filled_length)
    return f"[{bar}]"

class LevelCheckView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @ui.button(label="自分のレベルを確認", style=discord.ButtonStyle.primary, emoji="📊", custom_id="level_check_button")
    async def check_level_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=False) # 모두에게 보이도록 ephemeral=False
        
        user = interaction.user
        
        # 1. 유저 레벨 정보 가져오기
        level_res = await supabase.table('user_levels').select('*').eq('user_id', user.id).maybe_single().execute()
        user_level_data = level_res.data or {'level': 1, 'xp': 0}
        
        # 2. 다음 레벨 필요 경험치 가져오기
        xp_res = await supabase.rpc('get_xp_for_level', {'target_level': user_level_data['level']}).execute()
        xp_for_next = xp_res.data
        
        # 3. 유저 직업 정보 가져오기
        job_res = await supabase.table('user_jobs').select('jobs(job_name)').eq('user_id', user.id).maybe_single().execute()
        job_name = job_res.data['jobs']['job_name'] if job_res.data and job_res.data.get('jobs') else "一般住民"

        # 4. 임베드 생성
        embed = discord.Embed(
            title=f"{user.display_name}のステータス",
            color=user.color
        )
        if user.display_avatar:
            embed.set_thumbnail(url=user.display_avatar.url)
            
        embed.add_field(name="レベル", value=f"**Lv. {user_level_data['level']}**", inline=True)
        embed.add_field(name="職業", value=f"**{job_name}**", inline=True)
        
        xp_bar = create_xp_bar(user_level_data['xp'], xp_for_next)
        embed.add_field(
            name="経験値",
            value=f"`{user_level_data['xp']:,} / {xp_for_next:,}`\n{xp_bar}",
            inline=False
        )
        
        await interaction.followup.send(embed=embed)


class LevelSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 봇 재시작 시 View가 계속 동작하도록 등록
        self.bot.add_view(LevelCheckView())
        logger.info("LevelSystem Cog가 성공적으로 초기화되었습니다.")
        
    async def regenerate_panel(self, channel: discord.TextChannel):
        # 기존 패널 삭제 로직
        if panel_info := get_panel_id("panel_level_check"):
            try:
                msg = await self.bot.get_channel(panel_info['channel_id']).fetch_message(panel_info['message_id'])
                await msg.delete()
            except (discord.NotFound, AttributeError):
                pass
        
        embed = discord.Embed(
            title="📊 レベル確認",
            description="下のボタンを押して、ご自身の現在のレベルと経験値を確認できます。",
            color=0x5865F2
        )
        view = LevelCheckView()
        
        message = await channel.send(embed=embed, view=view)
        await save_panel_id("panel_level_check", message.id, channel.id)
        logger.info(f"✅ レベル確認パネルを #{channel.name} に設置しました。")

# (전직 시스템은 이 Cog에 계속해서 추가됩니다)
