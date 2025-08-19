import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
import random
import asyncio
import logging
from typing import Optional, Dict

from utils.database import (
    get_wallet, update_wallet,
    get_id, supabase, get_embed_from_db, get_config
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

# --- EconomyCore Cog ---
class EconomyCore(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.coin_log_channel_id: Optional[int] = None
        self.admin_role_id: Optional[int] = None
        self.currency_icon = "🪙"
        self._chat_cooldown = commands.CooldownMapping.from_cooldown(1, 3.0, commands.BucketType.user)
        self.voice_reward_loop.start()
        logger.info("EconomyCore Cog가 성공적으로 초기화되었습니다.")
        
    async def cog_load(self):
        await self.load_configs()
        
    async def load_configs(self):
        self.coin_log_channel_id = get_id("coin_log_channel_id")
        self.admin_role_id = get_id("role_admin_total")
        self.currency_icon = get_config("CURRENCY_ICON", "🪙")
        logger.info("[EconomyCore Cog] 데이터베이스로부터 설정을 성공적으로 로드했습니다.")
        
    def cog_unload(self):
        self.voice_reward_loop.cancel()
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or message.content.startswith('/'):
            return

        bucket = self._chat_cooldown.get_bucket(message)
        retry_after = bucket.update_rate_limit()
        if retry_after:
            return
        
        user = message.author
        chat_req = get_config("CHAT_MESSAGE_REQUIREMENT", 10)
        chat_reward_range = get_config("CHAT_REWARD_RANGE", [5, 10])
        if not chat_reward_range or len(chat_reward_range) != 2: chat_reward_range = [5, 10]

        try:
            params = {'p_user_id': str(user.id), 'p_chat_increment': 1}
            response = await supabase.rpc('increment_user_progress', params).execute()

            if response.data:
                current_progress = response.data[0]['new_chat_progress']
                
                if current_progress >= chat_req:
                    reward = random.randint(chat_reward_range[0], chat_reward_range[1])
                    await update_wallet(user, reward)
                    await self.log_coin_activity(user, reward, "チャット活動報酬")
                    reset_params = {'p_user_id': str(user.id), 'p_reset_chat': True}
                    await supabase.rpc('reset_user_progress', reset_params).execute()

        except Exception as e:
            logger.error(f"채팅 보상 처리 중 DB 오류 발생 (유저: {user.id}): {e}", exc_info=True)
            
    @tasks.loop(minutes=1)
    async def voice_reward_loop(self):
        try:
            voice_req_min = get_config("VOICE_TIME_REQUIREMENT_MINUTES", 1)
            voice_reward_range = get_config("VOICE_REWARD_RANGE", [10, 15])
            if not voice_reward_range or len(voice_reward_range) != 2: voice_reward_range = [10, 15]

            for guild in self.bot.guilds:
                afk_ch_id = guild.afk_channel.id if guild.afk_channel else None
                for vc in guild.voice_channels:
                    if vc.id == afk_ch_id: continue
                    
                    eligible_members = [m for m in vc.members if not m.bot]
                    
                    for member in eligible_members:
                        try:
                            params = {'p_user_id': str(member.id), 'p_voice_increment': 1}
                            response = await supabase.rpc('increment_user_progress', params).execute()
                            
                            if response.data:
                                current_progress = response.data[0]['new_voice_progress']
                                
                                if current_progress >= voice_req_min:
                                    reward = random.randint(voice_reward_range[0], voice_reward_range[1])
                                    await update_wallet(member, reward)
                                    await self.log_coin_activity(member, reward, "ボイスチャット活動報酬")
                                    
                                    reset_params = {'p_user_id': str(member.id), 'p_reset_voice': True}
                                    await supabase.rpc('reset_user_progress', reset_params).execute()

                        except Exception as e:
                            logger.error(f"음성 보상 처리 중 DB 오류 발생 (유저: {member.id}): {e}", exc_info=True)
        except Exception as e:
            logger.error(f"음성 보상 루프 중 오류: {e}", exc_info=True)
        
    @voice_reward_loop.before_loop
    async def before_voice_reward_loop(self):
        await self.bot.wait_until_ready()
    
    async def log_coin_activity(self, user: discord.Member, amount: int, reason: str):
        if not self.coin_log_channel_id or not (log_channel := self.bot.get_channel(self.coin_log_channel_id)): return
        
        if embed_data := await get_embed_from_db("log_coin_gain"):
            # 원본 embed_data를 복사하여 사용
            formatted_embed_data = embed_data.copy()
            
            # 보상 이유(reason)에 따라 제목과 설명을 동적으로 변경
            if reason == "チャット活動報酬":
                formatted_embed_data['title'] = "💬 チャット活動報酬"
                formatted_embed_data['description'] = f"{user.mention}さんがチャット活動でコインを獲得しました。"
            else: # 보이스 활동 보상 또는 기타
                formatted_embed_data['title'] = "🎙️ ボイスチャット活動報酬"
                formatted_embed_data['description'] = f"{user.mention}さんがVC活動でコインを獲得しました。"

            # 포맷팅 함수 호출
            embed = format_embed_from_db(
                formatted_embed_data, 
                user_mention=user.mention, 
                amount=f"{amount:,}", 
                currency_icon=self.currency_icon
            )

            # 썸네일 추가
            if user.display_avatar:
                embed.set_thumbnail(url=user.display_avatar.url)
            
            try: 
                # 유저를 태그하기 위해 content에 user.mention 추가
                await log_channel.send(content=user.mention, embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
            except Exception as e: 
                logger.error(f"코인 활동 로그 전송 실패: {e}", exc_info=True)
        
    @app_commands.command(name="コイン付与", description="[管理者専用] 特定のユーザーにコインを付与します。")
    @app_commands.checks.has_permissions(administrator=True)
    async def give_coin_command(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, None]):
        await interaction.response.defer(ephemeral=True)
        result = await update_wallet(user, amount)
        if result:
            await self.log_admin_action(interaction.user, user, amount, "付与")
            await interaction.followup.send(f"✅ {user.mention}さんへ `{amount:,}`{self.currency_icon}を付与しました。")
        else:
            await interaction.followup.send("❌ コイン付与中にエラーが発生しました。")
        
    @app_commands.command(name="コイン削減", description="[管理者専用] 特定のユーザーのコインを削減します。")
    @app_commands.checks.has_permissions(administrator=True)
    async def take_coin_command(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, None]):
        await interaction.response.defer(ephemeral=True)
        result = await update_wallet(user, -amount)
        if result:
            await self.log_admin_action(interaction.user, user, -amount, "削減")
            await interaction.followup.send(f"✅ {user.mention}さんの残高から `{amount:,}`{self.currency_icon}を削減しました。")
        else:
            await interaction.followup.send("❌ コイン削減中にエラーが発生しました。")

async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCore(bot))
