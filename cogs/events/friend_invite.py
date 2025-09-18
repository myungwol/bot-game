# game-bot/cogs/events/friend_invite.py (수정된 코드)

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
from typing import Optional, Dict, Set

from utils.database import (
    supabase, get_id, get_embed_from_db, get_config,
    save_panel_id, get_panel_id, update_wallet
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

class FriendInvitePanelView(ui.View):
    """
    친구 초대 패널에 표시될 영구적인 View 클래스입니다.
    '초대 코드 만들기' 버튼을 포함합니다.
    """
    def __init__(self, cog_instance: 'FriendInvite'):
        super().__init__(timeout=None)
        self.cog = cog_instance
        
        create_invite_button = ui.Button(
            label="초대 코드 만들기",
            style=discord.ButtonStyle.success,
            emoji="💌",
            custom_id="create_friend_invite"
        )
        create_invite_button.callback = self.on_create_invite_click
        self.add_item(create_invite_button)

    async def on_create_invite_click(self, interaction: discord.Interaction):
        """'초대 코드 만들기' 버튼이 클릭되었을 때의 로직을 처리합니다."""
        await self.cog.handle_invite_creation(interaction)


class FriendInvite(commands.Cog):
    """
    친구 초대 이벤트와 관련된 모든 기능을 관리하는 Cog입니다.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.panel_key = "panel_friend_invite"
        # 서버의 모든 초대 코드를 {코드: 사용 횟수} 형태로 저장하는 캐시
        self.invite_cache: Dict[str, int] = {}
        # on_ready 리스너가 여러 번 실행되는 것을 방지하기 위한 플래그
        self.initial_cache_updated = False

    # ▼▼▼ [핵심 수정] cog_load 대신 on_ready 리스너를 사용합니다. ▼▼▼
    @commands.Cog.listener()
    async def on_ready(self):
        """봇이 준비되면 서버 정보를 안전하게 가져와 초기화 작업을 수행합니다."""
        if self.initial_cache_updated:
            return  # 이미 초기화가 완료되었다면 다시 실행하지 않습니다.

        server_id_str = get_config("SERVER_ID")
        if not server_id_str:
            logger.error("[FriendInvite] SERVER_ID가 설정되지 않아 초대 추적을 시작할 수 없습니다.")
            return
            
        guild = self.bot.get_guild(int(server_id_str))
        if guild:
            await self._update_invite_cache(guild)
            logger.info(f"[FriendInvite] '{guild.name}' 서버의 초대 코드 {len(self.invite_cache)}개를 캐시했습니다.")
            self.initial_cache_updated = True # 초기화 완료 플래그 설정
        else:
            logger.error(f"[FriendInvite] SERVER_ID({server_id_str})에 해당하는 서버를 찾을 수 없습니다.")
    # ▲▲▲ [핵심 수정] 여기까지 ▲▲▲

    async def _update_invite_cache(self, guild: discord.Guild):
        """서버의 현재 초대 목록을 가져와 캐시를 업데이트합니다."""
        try:
            invites = await guild.invites()
            self.invite_cache = {invite.code: invite.uses for invite in invites}
        except discord.Forbidden:
            logger.error(f"[FriendInvite] '{guild.name}' 서버의 초대 목록을 볼 권한이 없습니다.")

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        """새로운 초대가 생성되면 캐시를 업데이트합니다."""
        server_id_str = get_config("SERVER_ID")
        if not server_id_str or not invite.guild or invite.guild.id != int(server_id_str):
            return
        self.invite_cache[invite.code] = invite.uses
        logger.info(f"[FriendInvite] 새 초대({invite.code})가 생성되어 캐시를 업데이트했습니다.")

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        """초대가 삭제되면 캐시에서 제거합니다."""
        server_id_str = get_config("SERVER_ID")
        if not server_id_str or not invite.guild or invite.guild.id != int(server_id_str):
            return
        if invite.code in self.invite_cache:
            del self.invite_cache[invite.code]
            logger.info(f"[FriendInvite] 초대({invite.code})가 삭제되어 캐시를 업데이트했습니다.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """새로운 멤버가 서버에 참여했을 때, 사용된 초대를 추적하고 보상을 지급합니다."""
        server_id_str = get_config("SERVER_ID")
        if not server_id_str or member.guild.id != int(server_id_str):
            return

        # 봇이 캐시를 채울 시간을 약간 줍니다 (봇 재시작 직후 유저 입장 대비)
        await asyncio.sleep(5) 

        try:
            new_invites = await member.guild.invites()
            used_invite = None
            for invite in new_invites:
                # 캐시에 없는 새로운 코드이거나, 사용 횟수가 증가한 코드를 찾습니다.
                if self.invite_cache.get(invite.code, 0) < invite.uses:
                    used_invite = invite
                    break
            
            # 캐시는 항상 최신 상태로 유지합니다.
            self.invite_cache = {i.code: i.uses for i in new_invites}

            if not used_invite:
                logger.warning(f"{member.name} 님이 서버에 참여했지만, 사용된 초대 코드를 특정할 수 없습니다.")
                return

            # 사용된 코드가 이벤트 DB에 있는지 확인합니다.
            res = await supabase.table('friend_invites').select('*').eq('invite_code', used_invite.code).maybe_single().execute()

            if res and res.data:
                # 이벤트 초대 코드가 맞습니다! 보상 및 정리 절차를 시작합니다.
                invite_data = res.data
                inviter_id = int(invite_data['inviter_id'])
                thread_id = int(invite_data['thread_id'])
                
                inviter = member.guild.get_member(inviter_id)
                if not inviter:
                    logger.warning(f"초대자(ID: {inviter_id})를 서버에서 찾을 수 없어 보상 지급을 건너뜁니다.")
                else:
                    # 1. 보상 지급
                    reward = 500
                    await update_wallet(inviter, reward)

                    # 2. 성공 로그 전송
                    log_channel_id = get_id("friend_invite_log_channel_id")
                    if log_channel_id and (log_channel := self.bot.get_channel(log_channel_id)):
                        embed_data = await get_embed_from_db("log_friend_invite_success")
                        if embed_data:
                            currency_icon = get_config("GAME_CONFIG", {}).get("CURRENCY_ICON", "🪙")
                            log_embed = format_embed_from_db(
                                embed_data,
                                new_member_mention=member.mention,
                                inviter_mention=inviter.mention,
                                currency_icon=currency_icon
                            )
                            await log_channel.send(embed=log_embed)
                
                # 3. 개인 스레드 삭제
                if thread := self.bot.get_channel(thread_id):
                    try:
                        await thread.delete()
                    except discord.HTTPException as e:
                        logger.error(f"초대 스레드(ID: {thread_id}) 삭제 실패: {e}")
                
                # 4. 데이터베이스 기록 삭제
                await supabase.table('friend_invites').delete().eq('invite_code', used_invite.code).execute()

        except discord.Forbidden:
            logger.error("[on_member_join] 서버 초대 목록을 확인할 권한이 없습니다.")
        except Exception as e:
            logger.error(f"[on_member_join] 처리 중 오류 발생: {e}", exc_info=True)

    async def handle_invite_creation(self, interaction: discord.Interaction):
        """'초대 코드 만들기' 버튼 클릭 시 전체 프로세스를 처리합니다."""
        await interaction.response.defer(ephemeral=True)
        user = interaction.user

        # 유저가 이미 활성 초대 코드를 가지고 있는지 확인합니다.
        res = await supabase.table('friend_invites').select('thread_id').eq('inviter_id', str(user.id)).maybe_single().execute()
        if res and res.data:
            thread_id = res.data['thread_id']
            if thread := self.bot.get_channel(thread_id):
                 await interaction.followup.send(f"이미 생성한 초대 코드가 있습니다! {thread.mention}에서 확인해주세요.", ephemeral=True)
                 return
            else:
                # 스레드가 수동으로 삭제된 경우, DB 기록을 정리해줍니다.
                await supabase.table('friend_invites').delete().eq('inviter_id', str(user.id)).execute()

        try:
            # 1회용, 7일짜리 초대 코드를 생성합니다.
            invite = await interaction.channel.create_invite(
                max_age=604800,  # 7 days in seconds
                max_uses=1,
                unique=True,
                reason=f"Friend invite for {user.name}"
            )

            # 비공개 스레드를 생성합니다.
            thread = await interaction.channel.create_thread(
                name=f"💌｜{user.display_name}님의 초대",
                type=discord.ChannelType.private_thread,
                auto_archive_duration=10080 # 7 days
            )
            await thread.add_user(user)

            # 생성된 초대 정보를 데이터베이스에 저장합니다.
            await supabase.table('friend_invites').insert({
                'inviter_id': str(user.id),
                'invite_code': invite.code,
                'thread_id': thread.id
            }).execute()

            # 스레드에 안내 메시지를 보냅니다.
            embed_data = await get_embed_from_db("invite_thread_message")
            if embed_data:
                embed = format_embed_from_db(
                    embed_data,
                    user_mention=user.mention,
                    invite_code=invite.url  # 유저 편의를 위해 전체 URL을 전달
                )
                await thread.send(embed=embed)
            
            await interaction.followup.send(f"초대 코드를 생성했습니다! {thread.mention} 채널을 확인해주세요.", ephemeral=True)

        except Exception as e:
            logger.error(f"초대 코드 생성 중 오류: {e}", exc_info=True)
            await interaction.followup.send("초대 코드를 생성하는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.", ephemeral=True)

    async def register_persistent_views(self):
        """영구 View를 봇에 등록합니다."""
        self.bot.add_view(FriendInvitePanelView(self))
        logger.info("[FriendInvite] 친구 초대 패널의 영구 View가 등록되었습니다.")

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str):
        """관리자 명령어로 패널을 재생성합니다."""
        panel_name = panel_key.replace("panel_", "")
        if (panel_info := get_panel_id(panel_name)):
            if (old_ch_id := panel_info.get("channel_id")) and (old_ch := self.bot.get_channel(old_ch_id)):
                try:
                    old_message = await old_ch.fetch_message(panel_info["message_id"])
                    await old_message.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
        
        if not (embed_data := await get_embed_from_db(self.panel_key)):
            logger.error(f"DB에서 '{self.panel_key}' 임베드를 찾을 수 없어 패널을 생성할 수 없습니다.")
            return

        embed = discord.Embed.from_dict(embed_data)
        view = FriendInvitePanelView(self)
        
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_name, new_message.id, channel.id)
        logger.info(f"✅ {self.panel_key} 패널을 성공적으로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Bot):
    """Cog를 봇에 추가합니다."""
    await bot.add_cog(FriendInvite(bot))
