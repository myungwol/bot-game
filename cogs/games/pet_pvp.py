# cogs/games/pet_pvp.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
import random
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

from utils.database import (
    supabase, get_user_pet, get_config, get_id,
    save_panel_id, get_panel_id, get_embed_from_db,
    get_cooldown, set_cooldown, create_pvp_match, get_pvp_match, update_pvp_match
)
from utils.helpers import format_embed_from_db, create_bar

logger = logging.getLogger(__name__)

PVP_REQUEST_TIMEOUT_SECONDS = 300 # 5분

class ChallengeConfirmView(ui.View):
    """도전 수락/거절을 위한 View"""
    def __init__(self, cog_instance: 'PetPvP', match_id: int, opponent_id: int):
        super().__init__(timeout=PVP_REQUEST_TIMEOUT_SECONDS)
        self.cog = cog_instance
        self.match_id = match_id
        self.opponent_id = opponent_id  # 도전자 ID를 View에 저장

    # 이 메서드가 버튼 콜백보다 먼저 실행됩니다.
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent_id:
            # 도전을 받은 당사자가 아니면, 비공개 메시지를 보내고 상호작용을 차단합니다.
            await interaction.response.send_message("다른 사람의 대전 신청에 응답할 수 없습니다.", ephemeral=True, delete_after=5)
            return False  # False를 반환하면 버튼 콜백이 실행되지 않습니다.
        return True  # 당사자일 경우에만 상호작용을 허용합니다.

    @ui.button(label="수락", style=discord.ButtonStyle.success, emoji="⚔️")
    async def accept_button(self, interaction: discord.Interaction, button: ui.Button):
        # interaction_check를 통과했으므로 이 코드는 반드시 도전자 본인이 실행합니다.
        await self.cog.handle_accept(interaction, self.match_id)
        self.stop()

    @ui.button(label="거절", style=discord.ButtonStyle.danger, emoji="✖️")
    async def decline_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.handle_decline(interaction, self.match_id)
        self.stop()

    async def on_timeout(self):
        await self.cog.handle_timeout(self.match_id)


class PetPvPGameView(ui.View):
    """전투 진행 중 표시될 View (버튼 없음)"""
    def __init__(self):
        super().__init__(timeout=None)


class PetPvPPanelView(ui.View):
    """대전장 패널의 메인 View"""
    def __init__(self, cog_instance: 'PetPvP'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    @ui.button(label="도전하기", style=discord.ButtonStyle.primary, emoji="⚔️", custom_id="pvp_challenge")
    async def challenge_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.handle_challenge_start(interaction)


class PetPvP(commands.Cog, name="PetPvP"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_pvp: Dict[int, Dict] = {} # Key: match_id

    async def register_persistent_views(self):
        self.bot.add_view(PetPvPPanelView(self))
        logger.info("✅ 펫 대전장의 영구 View가 성공적으로 등록되었습니다.")
        
    async def handle_challenge_start(self, interaction: discord.Interaction):
        challenger = interaction.user
        
        # 5분 쿨타임 확인
        cooldown_key = f"pet_pvp_challenge_{challenger.id}"
        if await get_cooldown(challenger.id, cooldown_key) > 0:
            return await interaction.response.send_message("❌ 도전 신청 후 5분이 지나야 다시 신청할 수 있습니다.", ephemeral=True, delete_after=10)

        challenger_pet = await get_user_pet(challenger.id)
        if not challenger_pet:
            return await interaction.response.send_message("❌ 대결에 내보낼 펫이 없습니다.", ephemeral=True)
            
        select_view = ui.View(timeout=180)
        user_select = ui.UserSelect(placeholder="대결할 상대를 선택하세요.")
        
        async def select_callback(select_interaction: discord.Interaction):
            await select_interaction.response.defer(ephemeral=True)
            
            opponent_id = int(select_interaction.data['values'][0])
            opponent = select_interaction.guild.get_member(opponent_id)

            if not opponent or opponent.bot or opponent.id == challenger.id:
                return await select_interaction.followup.send("❌ 유효하지 않은 상대입니다.", ephemeral=True)
            
            opponent_pet = await get_user_pet(opponent.id)
            if not opponent_pet:
                return await select_interaction.followup.send("❌ 상대방이 펫을 소유하고 있지 않습니다.", ephemeral=True)

            # 쿨타임 설정
            await set_cooldown(challenger.id, cooldown_key)

            # DB에 대전 기록 생성
            match = await create_pvp_match(challenger.id, opponent.id)
            if not match:
                return await select_interaction.followup.send("❌ 대전 정보를 생성하는 데 실패했습니다.", ephemeral=True)

            confirm_view = ChallengeConfirmView(self, match['id'], opponent.id)
            
            challenge_embed = discord.Embed(
                title="⚔️ 펫 대전 신청 도착!",
                description=f"{challenger.mention}님의 펫 **'{challenger_pet['nickname']}'**(이)가 당신의 펫 **'{opponent_pet['nickname']}'**에게 도전을 신청했습니다!",
                color=0xE91E63
            )
            challenge_embed.set_footer(text=f"{PVP_REQUEST_TIMEOUT_SECONDS // 60}분 내에 수락 또는 거절을 선택해주세요.")
            
            challenge_message = await interaction.channel.send(
                content=opponent.mention,
                embed=challenge_embed,
                view=confirm_view
            )
            
            self.active_pvp[match['id']] = {"challenge_message": challenge_message}
            
            await select_interaction.followup.send(f"✅ {opponent.display_name}님에게 도전 신청을 보냈습니다.", ephemeral=True)

        user_select.callback = select_callback
        select_view.add_item(user_select)
        await interaction.response.send_message("누구에게 도전하시겠습니까?", view=select_view, ephemeral=True)

    async def handle_accept(self, interaction: discord.Interaction, match_id: int):
        match = await get_pvp_match(match_id)
        if not match or interaction.user.id != int(match['opponent_id']):
            return await interaction.response.send_message("❌ 유효하지 않은 대전 신청입니다.", ephemeral=True, delete_after=5)

        await interaction.response.defer()

        # 대전 상태 업데이트
        await update_pvp_match(match_id, {'status': 'active'})
        
        # 도전 신청 메시지 수정/삭제
        if session := self.active_pvp.get(match_id):
            if msg := session.get("challenge_message"):
                try:
                    await msg.edit(content="대전이 수락되었습니다. 잠시 후 전투가 시작됩니다.", embed=None, view=None, delete_after=10)
                except discord.NotFound: pass
        
        # 전투 스레드 생성
        challenger = interaction.guild.get_member(int(match['challenger_id']))
        opponent = interaction.user
        
        try:
            thread_name = f"⚔️｜{challenger.display_name} vs {opponent.display_name}"
            thread = await interaction.channel.create_thread(name=thread_name, type=discord.ChannelType.private_thread, invitable=False)
            await thread.add_user(challenger)
            await thread.add_user(opponent)

            await update_pvp_match(match_id, {'thread_id': thread.id})
            
            # 전투 시뮬레이션 시작
            await self.run_combat_simulation(thread, match_id, challenger, opponent)

        except Exception as e:
            logger.error(f"PvP 스레드 생성 또는 전투 시작 중 오류: {e}", exc_info=True)
            await interaction.followup.send("❌ 전투를 시작하는 중 오류가 발생했습니다.", ephemeral=True)
            await self.end_game(match_id, None) # 오류 시 게임 종료 처리

    async def handle_decline(self, interaction: discord.Interaction, match_id: int):
        match = await get_pvp_match(match_id)
        if not match or interaction.user.id != int(match['opponent_id']):
            return await interaction.response.send_message("❌ 유효하지 않은 대전 신청입니다.", ephemeral=True, delete_after=5)
            
        await interaction.response.defer()
        await update_pvp_match(match_id, {'status': 'declined'})

        if session := self.active_pvp.pop(match_id, None):
            if msg := session.get("challenge_message"):
                try: await msg.edit(content=f"{interaction.user.mention}님이 도전을 거절했습니다.", embed=None, view=None, delete_after=10)
                except discord.NotFound: pass

    async def handle_timeout(self, match_id: int):
        if match := await get_pvp_match(match_id):
            if match['status'] == 'pending':
                await update_pvp_match(match_id, {'status': 'cancelled'})
                if session := self.active_pvp.pop(match_id, None):
                    if msg := session.get("challenge_message"):
                        try: await msg.edit(content="시간이 초과되어 대전 신청이 취소되었습니다.", embed=None, view=None, delete_after=10)
                        except discord.NotFound: pass

    async def run_combat_simulation(self, thread: discord.Thread, match_id: int, p1: discord.Member, p2: discord.Member):
        combat_message = None
        try:
            p1_pet_task = get_user_pet(p1.id)
            p2_pet_task = get_user_pet(p2.id)
            p1_pet, p2_pet = await asyncio.gather(p1_pet_task, p2_pet_task)
            
            if not p1_pet or not p2_pet:
                await thread.send("오류: 펫 정보를 불러올 수 없어 대전을 취소합니다.")
                return await self.end_game(match_id, None)
            
            p1_hp, p2_hp = p1_pet['current_hp'], p2_pet['current_hp']
            combat_logs = [f"**{p1.display_name}**의 **{p1_pet['nickname']}**와(과) **{p2.display_name}**의 **{p2_pet['nickname']}**의 대결이 시작됩니다!"]
            
            view = PetPvPGameView()
            embed = self._build_combat_embed(p1, p2, p1_pet, p2_pet, p1_hp, p2_hp, combat_logs)
            combat_message = await thread.send(embed=embed, view=view)

            turn_count = 0
            while p1_hp > 0 and p2_hp > 0 and turn_count < 50:
                turn_count += 1
                await asyncio.sleep(2.5)
                
                # 속도 비교로 선공 결정
                p1_first = p1_pet['current_speed'] > p2_pet['current_speed']
                if p1_pet['current_speed'] == p2_pet['current_speed']:
                    p1_first = random.choice([True, False])

                attacker, defender = (p1_pet, p2_pet) if p1_first else (p2_pet, p1_pet)
                attacker_hp_ref, defender_hp_ref = (p1_hp, p2_hp) if p1_first else (p2_hp, p1_hp)
                
                # 1. 선공 펫의 공격
                damage_to_defender = self._calculate_damage(attacker, defender)
                if p1_first: p2_hp -= damage_to_defender
                else: p1_hp -= damage_to_defender
                combat_logs.append(f"➡️ **{attacker['nickname']}**이(가) `{damage_to_defender}`의 피해를 입혔습니다!")
                await combat_message.edit(embed=self._build_combat_embed(p1, p2, p1_pet, p2_pet, p1_hp, p2_hp, combat_logs))
                if p1_hp <= 0 or p2_hp <= 0: break
                
                # 2. 후공 펫의 공격
                damage_to_attacker = self._calculate_damage(defender, attacker)
                if p1_first: p1_hp -= damage_to_attacker
                else: p2_hp -= damage_to_attacker
                combat_logs.append(f"⬅️ **{defender['nickname']}**이(가) `{damage_to_attacker}`의 피해를 입혔습니다.")
                await combat_message.edit(embed=self._build_combat_embed(p1, p2, p1_pet, p2_pet, p1_hp, p2_hp, combat_logs))
                if p1_hp <= 0 or p2_hp <= 0: break

            winner = None
            if p1_hp > p2_hp: winner = p1
            elif p2_hp > p1_hp: winner = p2
            elif turn_count >= 50: # 무승부 시 처리
                combat_logs.append("---")
                combat_logs.append("⚔️ 최대 턴에 도달하여 무승부로 처리되었습니다!")
                
            if winner:
                winner_pet = p1_pet if winner.id == p1.id else p2_pet
                combat_logs.append("---")
                combat_logs.append(f"🎉 **{winner_pet['nickname']}**의 승리!")
            
            await combat_message.edit(embed=self._build_combat_embed(p1, p2, p1_pet, p2_pet, p1_hp, p2_hp, combat_logs))
            await self.end_game(match_id, winner)

        except Exception as e:
            logger.error(f"PvP 전투 시뮬레이션 중 오류: {e}", exc_info=True)
            if combat_message:
                await combat_message.edit(content="전투 중 오류가 발생했습니다.", embed=None, view=None)
            await self.end_game(match_id, None)

    def _calculate_damage(self, attacker: Dict, defender: Dict) -> int:
        """보스전 데미지 공식을 펫 PvP에 맞게 적용"""
        defense_reduction_constant = 100 # 펫 대전은 스탯이 낮으므로 상수 조정
        defense_factor = defender['current_defense'] / (defender['current_defense'] + defense_reduction_constant)
        base_damage = attacker['current_attack'] * random.uniform(0.9, 1.1)
        damage = max(1, int(base_damage * (1 - defense_factor)))
        return damage

    def _build_combat_embed(self, p1: discord.Member, p2: discord.Member, p1_pet: Dict, p2_pet: Dict, p1_hp: int, p2_hp: int, logs: List[str]) -> discord.Embed:
        embed = discord.Embed(title=f"⚔️ {p1_pet['nickname']} vs {p2_pet['nickname']}", color=0xC27C0E)
        
        p1_hp_bar = create_bar(p1_hp, p1_pet['current_hp'])
        p1_stats_text = f"❤️ **HP:** `{max(0, p1_hp)} / {p1_pet['current_hp']}`\n{p1_hp_bar}"
        embed.add_field(name=f"{p1.display_name}의 {p1_pet['nickname']} (Lv.{p1_pet['level']})", value=p1_stats_text, inline=True)
        
        p2_hp_bar = create_bar(p2_hp, p2_pet['current_hp'])
        p2_stats_text = f"❤️ **HP:** `{max(0, p2_hp)} / {p2_pet['current_hp']}`\n{p2_hp_bar}"
        embed.add_field(name=f"{p2.display_name}의 {p2_pet['nickname']} (Lv.{p2_pet['level']})", value=p2_stats_text, inline=True)
        
        log_text = "\n".join(f"> {line}" for line in logs[-10:])
        embed.add_field(name="--- 전투 기록 ---", value=log_text, inline=False)
        return embed

    async def end_game(self, match_id: int, winner: Optional[discord.Member]):
        match = await update_pvp_match(match_id, {
            'status': 'completed',
            'winner_id': winner.id if winner else None,
            'completed_at': datetime.now(timezone.utc).isoformat()
        })
        if not match: return
        
        self.active_pvp.pop(match_id, None)
        
        # 결과 로그 전송
        log_embed = None
        if winner:
            loser_id = match['challenger_id'] if int(match['opponent_id']) == winner.id else match['opponent_id']
            loser = self.bot.get_user(int(loser_id))
            winner_pet = await get_user_pet(winner.id)
            
            if embed_data := await get_embed_from_db("log_pet_pvp_result"):
                log_embed = format_embed_from_db(
                    embed_data, 
                    winner_mention=winner.mention, 
                    loser_mention=loser.mention if loser else "알 수 없는 상대",
                    winner_pet_name=winner_pet['nickname'] if winner_pet else "펫"
                )

        # 스레드 정리 및 패널 재생성
        thread_id = match.get('thread_id')
        panel_channel_id = get_id("pet_pvp_panel_channel_id")
        
        if panel_channel_id and (panel_channel := self.bot.get_channel(panel_channel_id)):
            await self.regenerate_panel(panel_channel, last_log=log_embed)

        if thread_id and (thread := self.bot.get_channel(int(thread_id))):
            try:
                await thread.send("대전이 종료되었습니다. 이 채널은 15초 후에 자동으로 삭제됩니다.")
                await asyncio.sleep(15)
                await thread.delete()
            except (discord.NotFound, discord.Forbidden): pass
            
    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_pet_pvp", last_log: Optional[discord.Embed] = None):
        if last_log:
            try:
                await channel.send(embed=last_log)
            except discord.HTTPException as e:
                logger.error(f"PvP 로그 전송 실패: {e}")

        panel_name = panel_key.replace("panel_", "")
        if panel_info := get_panel_id(panel_name):
            try:
                if old_channel := self.bot.get_channel(panel_info['channel_id']):
                    msg = await old_channel.fetch_message(panel_info['message_id'])
                    await msg.delete()
            except (discord.NotFound, discord.Forbidden): pass
        
        embed_data = await get_embed_from_db(panel_key)
        if not embed_data:
            return logger.error(f"DB에서 '{panel_key}' 임베드를 찾을 수 없습니다.")
        
        embed = discord.Embed.from_dict(embed_data)
        view = PetPvPPanelView(self)
        new_message = await channel.send(embed=embed, view=view)
        await save_panel_id(panel_name, new_message.id, channel.id)
        logger.info(f"✅ {panel_key} 패널을 성공적으로 생성했습니다. (채널: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(PetPvP(bot))
