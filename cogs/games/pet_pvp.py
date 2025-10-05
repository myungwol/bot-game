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
            await interaction.response.send_message("他の人の対戦申請に応答することはできません。", ephemeral=True, delete_after=5)
            return False  # False를 반환하면 버튼 콜백이 실행되지 않습니다.
        return True  # 당사자일 경우에만 상호작용을 허용합니다.

    @ui.button(label="承諾", style=discord.ButtonStyle.success, emoji="⚔️")
    async def accept_button(self, interaction: discord.Interaction, button: ui.Button):
        # interaction_check를 통과했으므로 이 코드는 반드시 도전자 본인이 실행합니다.
        await self.cog.handle_accept(interaction, self.match_id)
        self.stop()

    @ui.button(label="拒否", style=discord.ButtonStyle.danger, emoji="✖️")
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

    @ui.button(label="挑戦する", style=discord.ButtonStyle.primary, emoji="⚔️", custom_id="pvp_challenge")
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
        
        # 5분 쿨타임 확인 (수정된 로직)
        cooldown_key = f"pet_pvp_challenge_{challenger.id}"
        cooldown_start_time = await get_cooldown(challenger.id, cooldown_key)

        if cooldown_start_time > 0:
            cooldown_duration_seconds = 300  # 5分
            cooldown_end_timestamp = int(cooldown_start_time + cooldown_duration_seconds)
            
            # 동적 시간 표시 생성 (예: <t:1672531200:R>)
            dynamic_timestamp = f"<t:{cooldown_end_timestamp}:R>"
            
            error_message = f"⏳ クールダウン中です。{dynamic_timestamp}に再度挑戦できます。"
            
            # delete_after를 늘려서 유저가 시간을 충분히 볼 수 있도록 합니다.
            return await interaction.response.send_message(error_message, ephemeral=True, delete_after=60)

        challenger_pet = await get_user_pet(challenger.id)
        if not challenger_pet:
            return await interaction.response.send_message("❌ 対戦に出すペットがいません。", ephemeral=True)
            
        select_view = ui.View(timeout=180)
        user_select = ui.UserSelect(placeholder="対戦相手を選択してください。")
        
        async def select_callback(select_interaction: discord.Interaction):
            await select_interaction.response.defer(ephemeral=True)
            
            opponent_id = int(select_interaction.data['values'][0])
            opponent = select_interaction.guild.get_member(opponent_id)

            if not opponent or opponent.bot or opponent.id == challenger.id:
                return await select_interaction.followup.send("❌ 無効な相手です。", ephemeral=True)
            
            opponent_pet = await get_user_pet(opponent.id)
            if not opponent_pet:
                return await select_interaction.followup.send("❌ 相手がペットを所有していません。", ephemeral=True)

            # 쿨타임 설정
            await set_cooldown(challenger.id, cooldown_key)

            # DB에 대전 기록 생성
            match = await create_pvp_match(challenger.id, opponent.id)
            if not match:
                return await select_interaction.followup.send("❌ 対戦情報の作成に失敗しました。", ephemeral=True)

            confirm_view = ChallengeConfirmView(self, match['id'], opponent.id)
            
            challenge_embed = discord.Embed(
                title="⚔️ ペット対戦申請到着！",
                description=f"{challenger.mention}さんのペット**'{challenger_pet['nickname']}'**が、あなたのペット**'{opponent_pet['nickname']}'**に挑戦を申請しました！",
                color=0xE91E63
            )
            challenge_embed.set_footer(text=f"{PVP_REQUEST_TIMEOUT_SECONDS // 60}分以内に承諾または拒否を選択してください。")
            
            challenge_message = await interaction.channel.send(
                content=opponent.mention,
                embed=challenge_embed,
                view=confirm_view
            )
            
            self.active_pvp[match['id']] = {"challenge_message": challenge_message}
            
            await select_interaction.followup.send(f"✅ {opponent.display_name}さんに挑戦申請を送りました。", ephemeral=True)

        user_select.callback = select_callback
        select_view.add_item(user_select)
        await interaction.response.send_message("誰に挑戦しますか？", view=select_view, ephemeral=True)

    async def handle_accept(self, interaction: discord.Interaction, match_id: int):
        match = await get_pvp_match(match_id)
        if not match or interaction.user.id != int(match['opponent_id']):
            return await interaction.response.send_message("❌ 無効な対戦申請です。", ephemeral=True, delete_after=5)

        await interaction.response.defer()

        # 대전 상태 업데이트
        await update_pvp_match(match_id, {'status': 'active'})
        
        # 도전 신청 메시지 수정/삭제
        if session := self.active_pvp.get(match_id):
            if msg := session.get("challenge_message"):
                try:
                    await msg.edit(content="対戦が承諾されました。まもなく戦闘が開始されます。", embed=None, view=None, delete_after=10)
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
            await interaction.followup.send("❌ 戦闘の開始中にエラーが発生しました。", ephemeral=True)
            await self.end_game(match_id, None) # 오류 시 게임 종료 처리

    async def handle_decline(self, interaction: discord.Interaction, match_id: int):
        match = await get_pvp_match(match_id)
        if not match or interaction.user.id != int(match['opponent_id']):
            return await interaction.response.send_message("❌ 無効な対戦申請です。", ephemeral=True, delete_after=5)
            
        await interaction.response.defer()
        await update_pvp_match(match_id, {'status': 'declined'})

        if session := self.active_pvp.pop(match_id, None):
            if msg := session.get("challenge_message"):
                try: await msg.edit(content=f"{interaction.user.mention}さんが挑戦を拒否しました。", embed=None, view=None, delete_after=10)
                except discord.NotFound: pass

    async def handle_timeout(self, match_id: int):
        if match := await get_pvp_match(match_id):
            if match['status'] == 'pending':
                await update_pvp_match(match_id, {'status': 'cancelled'})
                if session := self.active_pvp.pop(match_id, None):
                    if msg := session.get("challenge_message"):
                        try: await msg.edit(content="時間切れのため、対戦申請がキャンセルされました。", embed=None, view=None, delete_after=10)
                        except discord.NotFound: pass

    async def run_combat_simulation(self, thread: discord.Thread, match_id: int, p1: discord.Member, p2: discord.Member):
        combat_message = None
        try:
            p1_pet_task = get_user_pet(p1.id)
            p2_pet_task = get_user_pet(p2.id)
            p1_pet, p2_pet = await asyncio.gather(p1_pet_task, p2_pet_task)
            
            if not p1_pet or not p2_pet:
                await thread.send("エラー: ペット情報を読み込めず、対戦をキャンセルします。")
                return await self.end_game(match_id, None)
            
            p1_hp, p2_hp = p1_pet['current_hp'], p2_pet['current_hp']
            combat_logs = [f"**{p1.display_name}**の**{p1_pet['nickname']}**と**{p2.display_name}**の**{p2_pet['nickname']}**の対決が始まります！"]
            
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
                combat_logs.append(f"➡️ **{attacker['nickname']}**が`{damage_to_defender}`のダメージを与えました！")
                await combat_message.edit(embed=self._build_combat_embed(p1, p2, p1_pet, p2_pet, p1_hp, p2_hp, combat_logs))
                if p1_hp <= 0 or p2_hp <= 0: break
                
                # 2. 후공 펫의 공격
                damage_to_attacker = self._calculate_damage(defender, attacker)
                if p1_first: p1_hp -= damage_to_attacker
                else: p2_hp -= damage_to_attacker
                combat_logs.append(f"⬅️ **{defender['nickname']}**が`{damage_to_attacker}`のダメージを与えました！")
                await combat_message.edit(embed=self._build_combat_embed(p1, p2, p1_pet, p2_pet, p1_hp, p2_hp, combat_logs))
                if p1_hp <= 0 or p2_hp <= 0: break

            winner = None
            if p1_hp > p2_hp: winner = p1
            elif p2_hp > p1_hp: winner = p2
            elif turn_count >= 50: # 무승부 시 처리
                combat_logs.append("---")
                combat_logs.append("⚔️ 最大ターンに達したため、引き分けとなりました！")
                
            if winner:
                winner_pet = p1_pet if winner.id == p1.id else p2_pet
                combat_logs.append("---")
                combat_logs.append(f"🎉 **{winner_pet['nickname']}**の勝利！")
            
            await combat_message.edit(embed=self._build_combat_embed(p1, p2, p1_pet, p2_pet, p1_hp, p2_hp, combat_logs))
            await self.end_game(match_id, winner)

        except Exception as e:
            logger.error(f"PvP 전투 시뮬레이션 중 오류: {e}", exc_info=True)
            if combat_message:
                await combat_message.edit(content="戦闘中にエラーが発生しました。", embed=None, view=None)
            await self.end_game(match_id, None)

    def _calculate_damage(self, attacker: Dict, defender: Dict) -> int:
        """펫 PvP에 맞게 재조정된 데미지 공식"""
        # 1. 기본 공격력 계산 (랜덤 요소 포함)
        base_atk = attacker['current_attack'] * random.uniform(0.9, 1.2)

        # 2. 방어력에 기반한 피해 감소율(%) 계산
        #    - 방어력이 높을수록 감소율이 점근적으로 100%에 가까워지지만, 절대 100%는 넘지 않도록 설계
        #    - 방어력 50일 때 약 33%, 100일 때 50%의 피해 감소율을 가집니다.
        defense_efficiency = 100
        damage_reduction = defender['current_defense'] / (defender['current_defense'] + defense_efficiency)

        # 3. 기본 공격력에서 감소율만큼 피해량 차감
        mitigated_damage = base_atk * (1 - damage_reduction)
        
        # 4. 최종 데미지 보정
        #    - 펫의 평균 체력을 기준으로 데미지 스케일을 조정하여 전투 턴을 늘립니다.
        #    - 이 값이 작을수록 전투가 길어집니다. (현재는 평균 체력의 약 8~12% 데미지)
        final_damage_scaler = 8.0 
        final_damage = mitigated_damage / final_damage_scaler

        # 5. 최소 데미지는 1로 보장
        return max(1, int(final_damage))

    def _build_combat_embed(self, p1: discord.Member, p2: discord.Member, p1_pet: Dict, p2_pet: Dict, p1_hp: int, p2_hp: int, logs: List[str]) -> discord.Embed:
        embed = discord.Embed(title=f"⚔️ {p1_pet['nickname']} vs {p2_pet['nickname']}", color=0xC27C0E)
        
        p1_hp_bar = create_bar(p1_hp, p1_pet['current_hp'])
        p1_stats_text = (
            f"❤️ **HP:** `{max(0, p1_hp)} / {p1_pet['current_hp']}`\n{p1_hp_bar}\n"
            f"⚔️`{p1_pet['current_attack']}` 🛡️`{p1_pet['current_defense']}` 💨`{p1_pet['current_speed']}`"
        )
        embed.add_field(name=f"{p1.display_name}の{p1_pet['nickname']} (Lv.{p1_pet['level']})", value=p1_stats_text, inline=True)
        
        p2_hp_bar = create_bar(p2_hp, p2_pet['current_hp'])
        p2_stats_text = (
            f"❤️ **HP:** `{max(0, p2_hp)} / {p2_pet['current_hp']}`\n{p2_hp_bar}\n"
            f"⚔️`{p2_pet['current_attack']}` 🛡️`{p2_pet['current_defense']}` 💨`{p2_pet['current_speed']}`"
        )
        embed.add_field(name=f"{p2.display_name}の{p2_pet['nickname']} (Lv.{p2_pet['level']})", value=p2_stats_text, inline=True)
        
        log_text = "\n".join(f"> {line}" for line in logs[-10:])
        embed.add_field(name="--- 戦闘記録 ---", value=log_text, inline=False)
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
                    loser_mention=loser.mention if loser else "不明な相手",
                    winner_pet_name=winner_pet['nickname'] if winner_pet else "ペット"
                )

        # 스레드 정리 및 패널 재생성
        thread_id = match.get('thread_id')
        panel_channel_id = get_id("pet_pvp_panel_channel_id")
        
        if panel_channel_id and (panel_channel := self.bot.get_channel(panel_channel_id)):
            await self.regenerate_panel(panel_channel, last_log=log_embed)

        if thread_id and (thread := self.bot.get_channel(int(thread_id))):
            try:
                await thread.send("対戦が終了しました。このチャンネルは15秒後に自動的に削除されます。")
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
        logger.info(f"✅ {panel_key} パネルを正常に生成しました。(チャンネル: #{channel.name})")

async def setup(bot: commands.Bot):
    await bot.add_cog(PetPvP(bot))
