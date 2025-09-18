# game-bot/cogs/events/friend_invite.py (새로운 전체 코드)

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
from typing import Optional, Dict

from utils.database import (
    supabase, get_id, get_embed_from_db, get_config,
    save_panel_id, get_panel_id, update_wallet
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

class FriendInvitePanelView(ui.View):
    def __init__(self, cog_instance: 'FriendInvite'):
        super().__init__(timeout=None)
        self.cog = cog_instance
        
        button = ui.Button(
            label="내 초대 코드 확인/생성",
            style=discord.ButtonStyle.success,
            emoji="💌",
            custom_id="create_friend_invite"
        )
        button.callback = self.on_create_invite_click
        self.add_item(button)

    async def on_create_invite_click(self, interaction: discord.Interaction):
        await self.cog.handle_invite_creation(interaction)


class FriendInvite(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.panel_key = "panel_friend_invite"
        self.invite_cache: Dict[str, int] = {}
        self.initial_cache_updated = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self.initial_cache_updated: return

        server_id_str = get_config("SERVER_ID")
        if not server_id_str:
            logger.error("[FriendInvite] SERVER_ID가 설정되지 않아 초대 추적을 시작할 수 없습니다.")
            return
            
        guild = self.bot.get_guild(int(server_id_str))
        if guild:
            await self._update_invite_cache(guild)
            logger.info(f"[FriendInvite] '{guild.name}' 서버의 초대 코드 {len(self.invite_cache)}개를 캐시했습니다.")
            self.initial_cache_updated = True
        else:
            logger.error(f"[FriendInvite] SERVER_ID({server_id_str})에 해당하는 서버를 찾을 수 없습니다.")

    async def _update_invite_cache(self, guild: discord.Guild):
        try:
            invites = await guild.invites()
            self.invite_cache = {invite.code: invite.uses for invite in invites}
        except discord.Forbidden:
            logger.error(f"[FriendInvite] '{guild.name}' 서버의 초대 목록을 볼 권한이 없습니다.")

    # [핵심 변경] on_invite_create/delete 리스너는 캐시 관리를 위해 그대로 둡니다.
    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        server_id_str = get_config("SERVER_ID")
        if not server_id_str or not invite.guild or invite.guild.id != int(server_id_str): return
        self.invite_cache[invite.code] = invite.uses

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        server_id_str = get_config("SERVER_ID")
        if not server_id_str or not invite.guild or invite.guild.id != int(server_id_str): return
        if invite.code in self.invite_cache: del self.invite_cache[invite.code]

    # [핵심 변경] on_member_join은 이제 '기록'만 합니다.
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        server_id_str = get_config("SERVER_ID")
        if not server_id_str or member.guild.id != int(server_id_str): return
        
        await asyncio.sleep(5) 

        try:
            invites_before = self.invite_cache.copy()
            invites_after = await member.guild.invites()
            self.invite_cache = {i.code: i.uses for i in invites_after}

            used_invite_code = None
            for invite in invites_after:
                if invite.uses > invites_before.get(invite.code, 0):
                    used_invite_code = invite.code; break
            
            if not used_invite_code:
                deleted_codes = set(invites_before.keys()) - {i.code for i in invites_after}
                if deleted_codes: used_invite_code = deleted_codes.pop()

            if not used_invite_code: return

            # 사용된 코드가 우리 시스템에 등록된 코드인지 확인
            res = await supabase.table('user_invites').select('inviter_id').eq('invite_code', used_invite_code).maybe_single().execute()

            if res and res.data:
                inviter_id = int(res.data['inviter_id'])
                # 보상 대기열에 추가
                await supabase.table('pending_invites').insert({
                    'new_member_id': member.id,
                    'inviter_id': inviter_id
                }).execute()
                logger.info(f"[FriendInvite] {member.name}님이 {inviter_id}님의 초대로 참여. 보상 대기열에 추가됨.")

        except Exception as e:
            logger.error(f"[on_member_join] 처리 중 오류 발생: {e}", exc_info=True)

    # [핵심 변경] on_member_update가 '보상 지급'을 담당합니다.
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        resident_role_id = get_id('role_resident')
        if not resident_role_id: return

        # '주민' 역할이 새로 부여되었는지 확인
        before_roles = {r.id for r in before.roles}
        after_roles = {r.id for r in after.roles}
        if resident_role_id in before_roles or resident_role_id not in after_roles:
            return

        # 보상 대기열에 이 멤버가 있는지 확인
        pending_res = await supabase.table('pending_invites').select('*').eq('new_member_id', after.id).maybe_single().execute()
        
        if pending_res and pending_res.data:
            pending_data = pending_res.data
            inviter_id = int(pending_data['inviter_id'])
            
            inviter = after.guild.get_member(inviter_id)
            if not inviter:
                logger.warning(f"초대자(ID: {inviter_id})를 찾을 수 없어 보상 지급을 건너뜁니다.")
            else:
                reward = 500
                await update_wallet(inviter, reward)

                # 초대 횟수 1 증가시키고 새로운 카운트 받아오기
                count_res = await supabase.rpc('increment_invite_count', {'p_inviter_id': inviter_id}).execute()
                new_count = count_res.data if count_res.data is not None else 0

                # 성공 로그 전송
                log_channel_id = get_id("friend_invite_log_channel_id")
                if log_channel_id and (log_channel := self.bot.get_channel(log_channel_id)):
                    embed_data = await get_embed_from_db("log_friend_invite_success")
                    if embed_data:
                        currency_icon = get_config("GAME_CONFIG", {}).get("CURRENCY_ICON", "🪙")
                        log_embed = format_embed_from_db(
                            embed_data, new_member_mention=after.mention,
                            inviter_mention=inviter.mention, currency_icon=currency_icon,
                            invite_count=new_count
                        )
                        await log_channel.send(embed=log_embed)
            
            # 대기열에서 제거
            await supabase.table('pending_invites').delete().eq('new_member_id', after.id).execute()
            logger.info(f"[FriendInvite] {after.name}님이 주민 역할 획득. {inviter_id}님에게 보상 지급 완료.")

    # [핵심 변경] 초대 코드 생성/확인 로직
    async def handle_invite_creation(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user

        res = await supabase.table('user_invites').select('invite_code').eq('inviter_id', str(user.id)).maybe_single().execute()
        
        if res and res.data:
            # 이미 코드가 있는 경우
            invite_code = res.data['invite_code']
            await interaction.followup.send(f"✅ 당신의 영구 초대 코드는 다음과 같습니다:\nhttps://discord.gg/{invite_code}", ephemeral=True)
        else:
            # 새 코드를 생성해야 하는 경우
            try:
                invite = await interaction.channel.create_invite(
                    max_age=0, max_uses=0, unique=True, reason=f"Permanent invite for {user.name}"
                )
                
                await supabase.table('user_invites').insert({
                    'inviter_id': user.id,
                    'invite_code': invite.code
                }).execute()

                await interaction.followup.send(f"🎉 당신의 영구 초대 코드가 생성되었습니다!\n> 이 코드를 친구에게 공유해주세요.\nhttps://discord.gg/{invite.code}", ephemeral=True)
            except Exception as e:
                logger.error(f"영구 초대 코드 생성 중 오류: {e}", exc_info=True)
                await interaction.followup.send("❌ 초대 코드를 생성하는 중 오류가 발생했습니다.", ephemeral=True)

    async def register_persistent_views(self):
        self.bot.add_view(FriendInvitePanelView(self))

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str):
        panel_name = panel_key.replace("panel_", "")
        if (panel_info := get_panel_id(panel_name)):
            if (old_ch := self.bot.get_channel(panel_info.get("channel_id"))):
                try:
                    msg = await old_ch.fetch_message(panel_info["message_id"])
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden): pass
        
        if not (embed_data := await get_embed_from_db(self.panel_key)): return

        embed = discord.Embed.from_dict(embed_data)
        view = FriendInvitePanelView(self)
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_name, new_message.id, channel.id)

async def setup(bot: commands.Bot):
    await bot.add_cog(FriendInvite(bot))
