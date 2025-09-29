# cogs/games/boss_raid.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
import random
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

# --- [필수] utils 폴더에서 필요한 함수들을 가져옵니다 ---
from utils.database import (
    supabase, get_user_pet, get_config, get_id,
    update_wallet, update_inventory
)
from utils.helpers import format_embed_from_db

# 'create_bar' 함수는 helpers.py에 있어야 합니다.
# 이 파일에 없다면 다른 Cog(LevelSystem.py 등)에서 사용되므로 helpers.py에 이미 있을 가능성이 높습니다.
try:
    from utils.helpers import create_bar
except ImportError:
    # 만약을 위한 임시 함수 정의
    def create_bar(current: int, required: int, length: int = 10, full_char: str = '▓', empty_char: str = '░') -> str:
        if required <= 0: return full_char * length
        progress = min(current / required, 1.0)
        filled_length = int(length * progress)
        return f"[{full_char * filled_length}{empty_char * (length - filled_length)}]"

logger = logging.getLogger(__name__)

# --- [상수] 설정 값들을 정의합니다 ---
WEEKLY_BOSS_CHANNEL_KEY = "weekly_boss_channel_id"
MONTHLY_BOSS_CHANNEL_KEY = "monthly_boss_channel_id"
WEEKLY_BOSS_PANEL_MSG_KEY = "weekly_boss_panel_msg_id"
MONTHLY_BOSS_PANEL_MSG_KEY = "monthly_boss_panel_msg_id"
COMBAT_LOG_CHANNEL_KEY = "boss_log_channel_id" # 주요 이벤트 공지용

KST = timezone(timedelta(hours=9))

def get_week_start_utc() -> datetime:
    """현재 KST 기준 이번 주 월요일 00:00을 UTC datetime 객체로 반환합니다."""
    now_kst = datetime.now(KST)
    start_of_week_kst = now_kst - timedelta(days=now_kst.weekday())
    start_of_week_kst = start_of_week_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_of_week_kst.astimezone(timezone.utc)

def get_month_start_utc() -> datetime:
    """현재 KST 기준 이번 달 1일 00:00을 UTC datetime 객체로 반환합니다."""
    now_kst = datetime.now(KST)
    start_of_month_kst = now_kst.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start_of_month_kst.astimezone(timezone.utc)


class BossPanelView(ui.View):
    """
    각 보스 채널에 위치할 영구 패널의 View입니다.
    '도전하기', '현재 랭킹' 버튼을 포함합니다.
    """
    def __init__(self, cog_instance: 'BossRaid', boss_type: str, is_combat_locked: bool, is_defeated: bool):
        super().__init__(timeout=None)
        self.cog = cog_instance
        self.boss_type = boss_type

        challenge_label = "⚔️ 도전하기"
        if is_combat_locked:
            challenge_label = "🔴 전투 진행 중..."
        elif is_defeated:
            challenge_label = "✅ 처치 완료"

        challenge_button = ui.Button(
            label=challenge_label,
            style=discord.ButtonStyle.success,
            custom_id=f"boss_challenge:{self.boss_type}",
            disabled=(is_combat_locked or is_defeated)
        )
        challenge_button.callback = self.on_challenge_click
        self.add_item(challenge_button)

        ranking_button = ui.Button(
            label="🏆 현재 랭킹",
            style=discord.ButtonStyle.secondary,
            custom_id=f"boss_ranking:{self.boss_type}",
            disabled=is_defeated
        )
        ranking_button.callback = self.on_ranking_click
        self.add_item(ranking_button)

    async def on_challenge_click(self, interaction: discord.Interaction):
        await self.cog.handle_challenge(interaction, self.boss_type)

    async def on_ranking_click(self, interaction: discord.Interaction):
        await self.cog.handle_ranking(interaction, self.boss_type)

class BossCombatView(ui.View):
    """
    실시간 전투 UI에 사용될 View입니다. 현재는 버튼이 없지만,
    향후 '도망가기' 등의 기능을 추가할 수 있습니다.
    """
    def __init__(self):
        super().__init__(timeout=None)


class BossRaid(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_combats: Dict[str, asyncio.Task] = {} # key: boss_type ('weekly'/'monthly')
        self.combat_lock = asyncio.Lock()

        self.panel_updater_loop.start()
        # self.combat_engine_loop.start() # -> 실시간 턴제 방식으로 변경되어 이 루프는 불필요
        self.boss_reset_loop.start()

    def cog_unload(self):
        self.panel_updater_loop.cancel()
        self.boss_reset_loop.cancel()
        for task in self.active_combats.values():
            task.cancel()

    @tasks.loop(minutes=2)
    async def panel_updater_loop(self):
        logger.info("[BossRaid] 패널 자동 업데이트 시작...")
        await self.update_all_boss_panels()
        logger.info("[BossRaid] 패널 자동 업데이트 완료.")

    @tasks.loop(hours=1)
    async def boss_reset_loop(self):
        """
        매시간 실행하여 보스를 리셋하거나 생성할 시간인지 확인합니다.
        KST(UTC+9) 기준 자정을 감지하여 동작합니다.
        """
        now_utc = datetime.now(timezone.utc)
        now_kst = now_utc.astimezone(KST)

        # --- 주간 보스 리셋/생성 로직 ---
        # 조건: 월요일 00:00 ~ 00:59 사이
        if now_kst.weekday() == 0 and now_kst.hour == 0:
            # 현재 활성화된 주간 보스가 있는지 확인
            active_weekly_raid_res = await supabase.table('boss_raids').select('id').eq('status', 'active').eq('bosses.type', 'weekly').maybe_single().execute()

            if not active_weekly_raid_res.data:
                logger.info("[BossRaid] 새로운 주간 보스를 생성합니다.")
                # 1. 만료시킬 이전 보스가 있다면 'expired'로 상태 변경
                await supabase.table('boss_raids').update({'status': 'expired'}).eq('status', 'active').eq('bosses.type', 'weekly').execute()

                # 2. 새로운 주간 보스 생성
                await self.create_new_raid('weekly')

        # --- 월간 보스 리셋/생성 로직 ---
        # 조건: 매월 1일 00:00 ~ 00:59 사이
        if now_kst.day == 1 and now_kst.hour == 0:
            active_monthly_raid_res = await supabase.table('boss_raids').select('id').eq('status', 'active').eq('bosses.type', 'monthly').maybe_single().execute()
            
            if not active_monthly_raid_res.data:
                logger.info("[BossRaid] 새로운 월간 보스를 생성합니다.")
                await supabase.table('boss_raids').update({'status': 'expired'}).eq('status', 'active').eq('bosses.type', 'monthly').execute()
                await self.create_new_raid('monthly')
    
    @boss_reset_loop.before_loop
    async def before_boss_reset_loop(self):
        # 봇이 준비될 때까지 기다립니다.
        await self.bot.wait_until_ready()
        logger.info("[BossRaid] 보스 리셋 루프가 시작 대기 중입니다...")
        # 루프가 즉시 실행되지 않도록 약간의 딜레이를 줍니다.
        await asyncio.sleep(5)



class BossRaid(commands.Cog):

    async def create_new_raid(self, boss_type: str, force: bool = False):
        """
        DB에서 해당 타입의 보스 정보를 찾아 새로운 레이드를 생성하고 공지합니다.
        `force=True`이면 기존 레이드를 강제로 종료시킵니다.
        """
        try:
            if force:
                logger.info(f"[{boss_type.upper()}] 관리자 요청으로 기존 레이드를 강제 종료/만료시킵니다.")
                raids_to_expire_res = await supabase.table('boss_raids').select('id, bosses!inner(type)').eq('bosses.type', boss_type).eq('status', 'active').execute()
                
                if raids_to_expire_res.data:
                    raid_ids_to_expire = [raid['id'] for raid in raids_to_expire_res.data]
                    if raid_ids_to_expire:
                        await supabase.table('boss_raids').update({'status': 'expired'}).in_('id', raid_ids_to_expire).execute()
                        logger.info(f"[{boss_type.upper()}] {len(raid_ids_to_expire)}개의 활성 레이드를 'expired' 상태로 변경했습니다.")

            boss_template_res = await supabase.table('bosses').select('*').eq('type', boss_type).limit(1).single().execute()
            if not boss_template_res.data:
                logger.error(f"[{boss_type.upper()}] DB에 생성할 보스 정보가 없습니다.")
                return

            boss_template = boss_template_res.data

            # ▼▼▼ [핵심 수정] .select().single() 부분을 제거합니다. ▼▼▼
            new_raid_res = await supabase.table('boss_raids').insert({
                'boss_id': boss_template['id'],
                'current_hp': boss_template['max_hp'],
                'status': 'active'
            }).execute()
            # ▲▲▲ [핵심 수정] 완료 ▲▲▲

            if not new_raid_res.data:
                logger.error(f"[{boss_type.upper()}] 새로운 레이드를 DB에 생성하는 데 실패했습니다.")
                return
            
            channel_key = WEEKLY_BOSS_CHANNEL_KEY if boss_type == 'weekly' else MONTHLY_BOSS_CHANNEL_KEY
            channel_id = get_id(channel_key)
            if channel_id and (channel := self.bot.get_channel(channel_id)):
                embed = discord.Embed(
                    title=f"‼️ 새로운 {boss_template['name']}이(가) 나타났습니다!",
                    description="마을의 평화를 위해 힘을 합쳐 보스를 물리치세요!",
                    color=0xF1C40F
                )
                if boss_template.get('image_url'):
                    embed.set_thumbnail(url=boss_template['image_url'])
                
                await channel.send(embed=embed, delete_after=86400)

            await self.regenerate_panel(boss_type)

        except Exception as e:
            logger.error(f"[{boss_type.upper()}] 신규 레이드 생성 중 오류 발생: {e}", exc_info=True)
            
    async def update_all_boss_panels(self, boss_type_to_update: Optional[str] = None):
        types_to_process = [boss_type_to_update] if boss_type_to_update else ['weekly', 'monthly']
        for boss_type in types_to_process:
            await self.regenerate_panel(boss_type=boss_type)
            await asyncio.sleep(1) # API 제한 방지를 위한 짧은 딜레이

    async def regenerate_panel(self, boss_type: str, channel: Optional[discord.TextChannel] = None):
        """
        특정 타입의 보스 패널을 (재)생성하거나 업데이트합니다.
        이 함수는 Cog의 핵심적인 UI 관리 역할을 합니다.
        """
        logger.info(f"[{boss_type.upper()}] 패널 재생성 시작...")
        
        channel_key = WEEKLY_BOSS_CHANNEL_KEY if boss_type == 'weekly' else MONTHLY_BOSS_CHANNEL_KEY
        msg_key = WEEKLY_BOSS_PANEL_MSG_KEY if boss_type == 'weekly' else MONTHLY_BOSS_PANEL_MSG_KEY
        
        if not channel:
            channel_id = get_id(channel_key)
            if not channel_id or not (channel := self.bot.get_channel(channel_id)):
                logger.warning(f"[{boss_type.upper()}] 보스 채널이 설정되지 않았거나 찾을 수 없습니다.")
                return

        # ▼▼▼ [핵심 수정] 쿼리 결과를 raid_res 변수에 저장 ▼▼▼
        raid_res = await supabase.table('boss_raids').select('*, bosses(*)').eq('status', 'active').eq('bosses.type', boss_type).maybe_single().execute()
        
        # ▼▼▼ [핵심 수정] raid_res가 None이 아닌지, 그리고 .data 속성이 유효한지 먼저 확인합니다. ▼▼▼
        raid_data = raid_res.data if raid_res and hasattr(raid_res, 'data') else None

        is_combat_locked = self.combat_lock.locked()
        # raid_data가 유효하고, 그 안의 status가 'active'인지를 확인합니다.
        is_defeated = not (raid_data and raid_data.get('status') == 'active')

        view = BossPanelView(self, boss_type, is_combat_locked, is_defeated)
        
        if raid_data:
            # 보스가 활성화된 경우
            embed = self.build_boss_panel_embed(raid_data)
        else:
            # 보스가 없는 경우 (리셋 대기 중)
            embed = discord.Embed(
                title=f"👑 다음 {('주간' if boss_type == 'weekly' else '월간')} 보스를 기다리는 중...",
                description="새로운 보스가 곧 나타납니다!\n리셋 시간: " + ("매주 월요일 00시" if boss_type == 'weekly' else "매월 1일 00시"),
                color=0x34495E
            )
            for item in view.children:
                item.disabled = True

        message_id = get_id(msg_key)
        try:
            if message_id:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=embed, view=view)
                logger.info(f"[{boss_type.upper()}] 패널 메시지(ID: {message_id})를 성공적으로 수정했습니다.")
            else:
                # [수정] channel_configs 테이블에 직접 접근하는 대신 save_id_to_db 헬퍼 함수 사용
                from utils.database import save_id_to_db
                await channel.purge(limit=100)
                new_message = await channel.send(embed=embed, view=view)
                await save_id_to_db(msg_key, new_message.id)
                await new_message.pin()
                logger.info(f"[{boss_type.upper()}] 새로운 패널 메시지(ID: {new_message.id})를 생성하고 고정했습니다.")
        except discord.NotFound:
            logger.warning(f"[{boss_type.upper()}] 패널 메시지(ID: {message_id})를 찾을 수 없어 새로 생성합니다.")
            from utils.database import save_id_to_db
            await channel.purge(limit=100)
            new_message = await channel.send(embed=embed, view=view)
            await save_id_to_db(msg_key, new_message.id)
            await new_message.pin()
        except (discord.Forbidden, discord.HTTPException) as e:
            logger.error(f"[{boss_type.upper()}] 패널 메시지를 수정/생성/고정하는 데 실패했습니다: {e}")
            
    def build_boss_panel_embed(self, raid_data: Dict[str, Any]) -> discord.Embed:
        boss_info = raid_data['bosses']
        
        recent_logs = raid_data.get('recent_logs', [])
        log_text = "\n".join(recent_logs) if recent_logs else "아직 전투 기록이 없습니다."

        hp_bar = create_bar(raid_data['current_hp'], boss_info['max_hp'])
        hp_text = f"`{raid_data['current_hp']:,} / {boss_info['max_hp']:,}`\n{hp_bar}"
        stats_text = f"**속성:** `{boss_info.get('element', '무')}` | **공격력:** `{boss_info['attack']:,}` | **방어력:** `{boss_info['defense']:,}`"
        
        embed = discord.Embed(title=f"👑 {boss_info['name']} 현황", color=0xE74C3C)
        if boss_info.get('image_url'):
            embed.set_thumbnail(url=boss_info['image_url'])

        embed.add_field(name="--- 최근 전투 기록 (최대 10개) ---", value=log_text, inline=False)
        embed.add_field(name="--- 보스 정보 ---", value=f"{stats_text}\n\n**체력:**\n{hp_text}", inline=False)
        
        embed.set_footer(text="패널은 2분마다 자동으로 업데이트됩니다.")
        return embed

    # --- [핸들러] 버튼 상호작용 처리 ---
    async def handle_challenge(self, interaction: discord.Interaction, boss_type: str):
        """'도전하기' 버튼 클릭을 처리하는 로직"""
        user = interaction.user
        
        if self.combat_lock.locked():
            await interaction.response.send_message("❌ 다른 유저가 전투 중입니다. 잠시 후 다시 시도해주세요.", ephemeral=True, delete_after=5)
            return

        # 1. 도전 조건 확인
        # [수정] bosses 테이블 조인 쿼리 수정
        raid_res = await supabase.table('boss_raids').select('id, bosses!inner(type)').eq('status', 'active').eq('bosses.type', boss_type).maybe_single().execute()
        if not raid_res.data:
            await interaction.response.send_message("❌ 현재 도전할 수 있는 보스가 없습니다.", ephemeral=True)
            return # <--- 올바른 들여쓰기
        
        raid_id = raid_res.data['id']
        
        pet = await get_user_pet(user.id)
        if not pet:
            await interaction.response.send_message("❌ 전투에 참여할 펫이 없습니다.", ephemeral=True)
            return # <--- 올바른 들여쓰기
        
        # 2. 도전 횟수 확인
        start_time_utc = get_week_start_utc() if boss_type == 'weekly' else get_month_start_utc()
        
        part_res = await supabase.table('boss_participants').select('last_fought_at').eq('raid_id', raid_id).eq('user_id', user.id).maybe_single().execute()
        
        if part_res.data and part_res.data.get('last_fought_at'):
            last_fought_dt = datetime.fromisoformat(part_res.data['last_fought_at'].replace('Z', '+00:00'))
            if last_fought_dt >= start_time_utc:
                 await interaction.response.send_message(f"❌ 이번 {('주' if boss_type == 'weekly' else '달')}에는 이미 보스에게 도전했습니다.", ephemeral=True)
                 return # <--- 올바른 들여쓰기
        
        # 3. 전투 시작
        async with self.combat_lock:
            await interaction.response.send_message("✅ 전투를 준비합니다... 잠시만 기다려주세요.", ephemeral=True, delete_after=3)
            await self.update_all_boss_panels() # 도전하기 버튼을 비활성화하기 위해 패널 업데이트

            combat_task = asyncio.create_task(self.run_combat_simulation(interaction, user, pet, raid_id, boss_type))
            self.active_combats[boss_type] = combat_task
            try:
                await combat_task
            finally:
                self.active_combats.pop(boss_type, None)
        
        # 4. 전투 종료 후 패널 즉시 업데이트
        await self.update_all_boss_panels()

    async def run_combat_simulation(self, interaction: discord.Interaction, user: discord.Member, pet: Dict, raid_id: int, boss_type: str):
        """실시간 턴제 전투를 시뮬레이션하고 UI를 업데이트합니다."""
        combat_message = None
        try:
            # 1. 전투 정보 초기화
            raid_res = await supabase.table('boss_raids').select('*, bosses(*)').eq('id', raid_id).single().execute()
            raid_data = raid_res.data
            boss = raid_data['bosses']

            # 펫의 모든 스탯을 가져옵니다.
            pet_hp = pet.get('current_hp', 100)
            pet_attack = pet.get('current_attack', 10)
            pet_defense = pet.get('current_defense', 10)
            pet_speed = pet.get('current_speed', 10)
            
            boss_hp = raid_data['current_hp']
            boss_attack = boss['attack']
            boss_defense = boss['defense']
            # 보스 스피드는 DB에 없으므로 임의로 설정하거나, DB에 추가해야 합니다. 여기서는 임의로 설정합니다.
            boss_speed = int(boss_attack * 0.5) # 예시: 공격력의 50%를 스피드로 설정
            
            combat_logs = [f"**{user.display_name}**님이 **{pet['nickname']}**와(과) 함께 전투를 시작합니다!"]
            total_damage_dealt = 0

            # 2. 전투 UI 생성
            view = BossCombatView()
            embed = self.build_combat_embed(user, pet, boss, pet_hp, boss_hp, combat_logs)
            combat_message = await interaction.channel.send(embed=embed, view=view)

            # 3. 턴제 전투 루프
            turn_count = 0
            while pet_hp > 0 and boss_hp > 0 and turn_count < 50: # 무한 루프 방지를 위해 최대 턴 수 제한
                turn_count += 1
                await asyncio.sleep(2.5)

                # 선제공격 결정 (스피드 기반)
                pet_first = pet_speed > boss_speed

                # 턴 진행
                if pet_first:
                    # 펫의 턴
                    if pet_hp > 0:
                        # 피해량 계산 (방어력에 따른 피해 감소 적용)
                        damage_reduction = boss_defense / (boss_defense + 200) # 방어력이 높을수록 1에 가까워짐
                        base_damage = pet_attack * random.uniform(0.9, 1.1)
                        pet_damage = max(1, int(base_damage * (1 - damage_reduction)))
                        
                        boss_hp -= pet_damage
                        total_damage_dealt += pet_damage
                        combat_logs.append(f"🔥 **{pet['nickname']}**이(가) `{pet_damage}`의 피해를 입혔습니다!")
                        await combat_message.edit(embed=self.build_combat_embed(user, pet, boss, pet_hp, boss_hp, combat_logs))
                        if boss_hp <= 0: break
                    
                    # 보스의 턴
                    if boss_hp > 0:
                        damage_reduction = pet_defense / (pet_defense + 200)
                        base_damage = boss_attack * random.uniform(0.9, 1.1)
                        boss_damage = max(1, int(base_damage * (1 - damage_reduction)))

                        # 스피드 기반 회피 로직
                        speed_diff = pet_speed - boss_speed
                        dodge_chance = min(0.3, max(0, speed_diff / 100)) # 스피드 차이가 100일때 회피율 30% (최대)
                        if random.random() < dodge_chance:
                            combat_logs.append(f"💨 **{pet['nickname']}**이(가) 보스의 공격을 회피했습니다!")
                        else:
                            pet_hp -= boss_damage
                            combat_logs.append(f"💧 **{boss['name']}**이(가) `{boss_damage}`의 피해를 입혔습니다.")
                        
                        await combat_message.edit(embed=self.build_combat_embed(user, pet, boss, pet_hp, boss_hp, combat_logs))
                        if pet_hp <= 0: break
                
                else: # 보스 선제 공격
                    # 보스의 턴
                    if boss_hp > 0:
                        damage_reduction = pet_defense / (pet_defense + 200)
                        base_damage = boss_attack * random.uniform(0.9, 1.1)
                        boss_damage = max(1, int(base_damage * (1 - damage_reduction)))

                        speed_diff = pet_speed - boss_speed
                        dodge_chance = min(0.3, max(0, speed_diff / 100))
                        if random.random() < dodge_chance:
                            combat_logs.append(f"💨 **{pet['nickname']}**이(가) 보스의 공격을 회피했습니다!")
                        else:
                            pet_hp -= boss_damage
                            combat_logs.append(f"💧 **{boss['name']}**이(가) `{boss_damage}`의 피해를 입혔습니다.")

                        await combat_message.edit(embed=self.build_combat_embed(user, pet, boss, pet_hp, boss_hp, combat_logs))
                        if pet_hp <= 0: break

                    # 펫의 턴
                    if pet_hp > 0:
                        damage_reduction = boss_defense / (boss_defense + 200)
                        base_damage = pet_attack * random.uniform(0.9, 1.1)
                        pet_damage = max(1, int(base_damage * (1 - damage_reduction)))
                        
                        boss_hp -= pet_damage
                        total_damage_dealt += pet_damage
                        combat_logs.append(f"🔥 **{pet['nickname']}**이(가) `{pet_damage}`의 피해를 입혔습니다!")
                        await combat_message.edit(embed=self.build_combat_embed(user, pet, boss, pet_hp, boss_hp, combat_logs))
                        if boss_hp <= 0: break

            # 4. 전투 종료 처리 (이하 로직은 기존과 동일)
            combat_logs.append("---")
            if boss_hp <= 0:
                combat_logs.append(f"🎉 **{boss['name']}**을(를) 쓰러뜨렸습니다!")
            else:
                combat_logs.append(f"☠️ **{pet['nickname']}**이(가) 쓰러졌습니다.")
            
            combat_logs.append(f"✅ 전투 종료! 총 `{total_damage_dealt:,}`의 피해를 입혔습니다.")
            await combat_message.edit(embed=self.build_combat_embed(user, pet, boss, pet_hp, boss_hp, combat_logs))

            # 5. DB 업데이트
            final_boss_hp = max(0, raid_data['current_hp'] - total_damage_dealt)
            
            new_log_entry = f"`[{datetime.now(KST).strftime('%H:%M')}]` ⚔️ **{user.display_name}** 님이 `{total_damage_dealt:,}`의 피해를 입혔습니다. (남은 HP: `{final_boss_hp:,}`)"
            recent_logs = raid_data.get('recent_logs', [])
            recent_logs.insert(0, new_log_entry)
            
            await supabase.table('boss_raids').update({
                'current_hp': final_boss_hp,
                'recent_logs': recent_logs[:10]
            }).eq('id', raid_id).execute()

            await supabase.rpc('upsert_boss_participant', {
                'p_raid_id': raid_id,
                'p_user_id': user.id,
                'p_pet_id': pet['id'],
                'p_damage_to_add': total_damage_dealt
            })
            
            if final_boss_hp <= 0 and raid_data['status'] == 'active':
                 await self.handle_boss_defeat(interaction.channel, raid_id)

        except Exception as e:
            logger.error(f"보스 전투 시뮬레이션 중 오류: {e}", exc_info=True)
            if combat_message:
                await combat_message.edit(content="전투 중 오류가 발생했습니다.", embed=None, view=None)
        finally:
            if combat_message:
                await asyncio.sleep(10)
                try:
                    await combat_message.delete()
                except discord.NotFound:
                    pass

    def build_combat_embed(self, user: discord.Member, pet: Dict, boss: Dict, pet_hp: int, boss_hp: int, logs: List[str]) -> discord.Embed:
        """실시간 전투 UI 임베드를 생성합니다."""
        embed = discord.Embed(title=f"⚔️ {boss['name']}와(과)의 전투", color=0xC27C0E)
        embed.set_author(name=f"{user.display_name}님의 도전", icon_url=user.display_avatar.url if user.display_avatar else None)
        
        # 펫 정보 필드 - 모든 스탯 표시
        pet_stats_text = (
            f"❤️ **HP:** `{max(0, pet_hp)} / {pet['current_hp']}`\n"
            f"⚔️ **공격력:** `{pet['current_attack']}`\n"
            f"🛡️ **방어력:** `{pet['current_defense']}`\n"
            f"💨 **스피드:** `{pet['current_speed']}`"
        )
        embed.add_field(
            name=f"내 펫: {pet['nickname']} (Lv.{pet['level']})",
            value=pet_stats_text,
            inline=True
        )
        
        # 보스 정보 필드 - 모든 스탯 표시
        boss_speed = int(boss['attack'] * 0.5) # 예시 스피드
        boss_stats_text = (
            f"❤️ **HP:** `{max(0, boss_hp):,} / {boss['max_hp']:,}`\n"
            f"⚔️ **공격력:** `{boss['attack']}`\n"
            f"🛡️ **방어력:** `{boss['defense']}`\n"
            f"💨 **스피드:** `{boss_speed}`"
        )
        embed.add_field(
            name=f"보스: {boss['name']}",
            value=boss_stats_text,
            inline=True
        )
        
        log_text = "\n".join(f"> {line}" for line in logs[-10:]) # 최근 10줄만 표시
        embed.add_field(name="--- 전투 기록 ---", value=log_text, inline=False)
        return embed

    async def handle_boss_defeat(self, channel: discord.TextChannel, raid_id: int):
        """보스 처치 시 공지 및 보상 지급 로직"""
        
        # 1. 레이드 상태를 'defeated'로 변경
        raid_update_res = await supabase.table('boss_raids').update({
            'status': 'defeated',
            'defeat_time': datetime.now(timezone.utc).isoformat()
        }).eq('id', raid_id).eq('status', 'active').execute()
        
        # 이미 다른 프로세스에 의해 처리된 경우 중복 실행 방지
        if not raid_update_res.data:
            logger.warning(f"Raid ID {raid_id}는 이미 처치되었거나 활성 상태가 아닙니다. 보상 지급을 건너뜁니다.")
            return

        raid_data = raid_update_res.data[0]
        boss_info_res = await supabase.table('bosses').select('name').eq('id', raid_data['boss_id']).single().execute()
        boss_name = boss_info_res.data['name'] if boss_info_res.data else "보스"
        
        # 2. 보스 채널에 처치 공지 (24시간 후 삭제)
        defeat_embed = discord.Embed(
            title=f"🎉 {boss_name} 처치 성공!",
            description="용감한 모험가들의 활약으로 보스를 물리쳤습니다!\n\n참가자들에게 곧 보상이 지급됩니다...",
            color=0x2ECC71
        )
        await channel.send(embed=defeat_embed, delete_after=86400)
        
        # 3. 보상 지급 로직 호출 (다음 단계에서 구현)
        await self.distribute_rewards(channel, raid_id, boss_name)

    async def handle_ranking(self, interaction: discord.Interaction, boss_type: str):
        """'현재 랭킹' 버튼 클릭을 처리하는 로직"""
        raid_res = await supabase.table('boss_raids').select('id, bosses(name)').eq('status', 'active').eq('bosses.type', boss_type).maybe_single().execute()
        if not raid_res.data:
            await interaction.response.send_message("❌ 현재 조회할 수 있는 랭킹 정보가 없습니다.", ephemeral=True)
            return
        
        raid_id = raid_res.data['id']
        ranking_view = RankingView(self, raid_id, interaction.user.id)
        await ranking_view.start(interaction)

    async def handle_boss_defeat(self, channel: discord.TextChannel, raid_id: int):
        """보스 처치 시 공지 및 보상 지급 로직"""
        
        # 1. 레이드 상태를 'defeated'로 변경 (중복 처리 방지)
        raid_update_res = await supabase.table('boss_raids').update({
            'status': 'defeated',
            'defeat_time': datetime.now(timezone.utc).isoformat()
        }).eq('id', raid_id).eq('status', 'active').select('*, bosses(*)').single().execute()
        
        if not raid_update_res.data:
            logger.warning(f"Raid ID {raid_id}는 이미 처치되었거나 활성 상태가 아닙니다. 보상 지급을 건너뜁니다.")
            return

        raid_data = raid_update_res.data
        boss_name = raid_data['bosses']['name']
        
        # 2. 보스 채널에 처치 공지 (24시간 후 삭제)
        defeat_embed = discord.Embed(
            title=f"🎉 {boss_name} 처치 성공!",
            description="용감한 모험가들의 활약으로 보스를 물리쳤습니다!\n\n참가자들에게 곧 보상이 지급되며, 최종 랭킹이 공지될 예정입니다...",
            color=0x2ECC71
        )
        await channel.send(embed=defeat_embed, delete_after=86400)
        
        # 3. 보상 지급 로직 호출
        await self.distribute_rewards(channel, raid_id, boss_name)

    async def distribute_rewards(self, channel: discord.TextChannel, raid_id: int, boss_name: str):
        """보상 지급 및 최종 랭킹을 공지합니다."""
        try:
            # 1. 모든 참가자 정보를 피해량 순으로 가져옵니다.
            part_res = await supabase.table('boss_participants').select('user_id, total_damage_dealt, pets(nickname)', count='exact').eq('raid_id', raid_id).order('total_damage_dealt', desc=True).execute()

            if not part_res.data:
                logger.info(f"Raid ID {raid_id}에 참가자가 없어 보상 지급을 건너뜁니다.")
                return

            participants = part_res.data
            total_participants = part_res.count or 0
            
            # 2. 보상 아이템 결정
            # (향후 DB에서 가져오도록 수정 가능)
            base_reward_item = "주간 보스 보물 상자" if "주간" in boss_name else "월간 보스 보물 상자"
            rare_reward_items = ["각성의 코어", "초월의 핵"]
            
            top_50_percent_count = (total_participants + 1) // 2

            # 3. 보상 지급 DB 작업 준비
            db_tasks = []
            reward_summary = {} # 유저별 보상 요약

            for i, participant in enumerate(participants):
                user_id = participant['user_id']
                reward_summary[user_id] = [base_reward_item]
                
                # 기본 보상 지급
                db_tasks.append(update_inventory(user_id, base_reward_item, 1))

                # 상위 50% 랭커 추가 보상 (5% 확률)
                if i < top_50_percent_count:
                    if random.random() < 0.05:
                        rare_reward = random.choice(rare_reward_items)
                        db_tasks.append(update_inventory(user_id, rare_reward, 1))
                        reward_summary[user_id].append(rare_reward)
            
            # 4. DB 작업 실행
            await asyncio.gather(*db_tasks)
            logger.info(f"Raid ID {raid_id}의 보상 지급 DB 작업 {len(db_tasks)}개를 완료했습니다.")
            
            # 5. 최종 랭킹 및 보상 공지
            log_channel_id = get_id(COMBAT_LOG_CHANNEL_KEY)
            if not log_channel_id or not (log_channel := self.bot.get_channel(log_channel_id)):
                log_channel = channel # 로그 채널이 없으면 보스 채널에 공지

            final_embed = discord.Embed(title=f"🏆 {boss_name} 최종 랭킹 및 보상", color=0x5865F2)
            
            rank_list = []
            for i, data in enumerate(participants[:10]): # 상위 10명만 표시
                rank = i + 1
                member = self.bot.get_guild(channel.guild.id).get_member(data['user_id'])
                user_name = member.display_name if member else f"ID:{data['user_id']}"
                damage = data['total_damage_dealt']
                rewards = ", ".join(reward_summary.get(data['user_id'], []))
                
                line = f"`{rank}위.` **{user_name}** - `{damage:,}` DMG\n> 🎁 보상: {rewards}"
                rank_list.append(line)

            final_embed.description = "\n".join(rank_list)
            final_embed.set_footer(text=f"총 {total_participants}명의 참가자에게 보상이 지급되었습니다.")
            
            await log_channel.send(embed=final_embed)

        except Exception as e:
            logger.error(f"보상 지급 중 오류 발생 (Raid ID: {raid_id}): {e}", exc_info=True)
            await channel.send("보상을 지급하는 중 오류가 발생했습니다. 관리자에게 문의해주세요.")

# cogs/games/boss_raid.py 파일 하단, setup 함수 위에 추가

class RankingView(ui.View):
    """
    보스 랭킹을 보여주고 페이지를 넘길 수 있는 View입니다.
    """
    def __init__(self, cog_instance: 'BossRaid', raid_id: int, user_id: int):
        super().__init__(timeout=180)
        self.cog = cog_instance
        self.raid_id = raid_id
        self.user_id = user_id # 이 View를 연 사람의 ID
        self.current_page = 0
        self.users_per_page = 10
        self.total_pages = 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # View를 연 사람만 버튼을 누를 수 있도록 합니다.
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("랭킹을 조회한 본인만 조작할 수 있습니다.", ephemeral=True, delete_after=5)
            return False
        return True

    async def start(self, interaction: discord.Interaction):
        """View를 시작하고 첫 페이지를 전송합니다."""
        embed = await self.build_ranking_embed()
        self.update_buttons()
        await interaction.response.send_message(embed=embed, view=self, ephemeral=True)

    async def update_view(self, interaction: discord.Interaction):
        """버튼 클릭 시 View와 임베드를 업데이트합니다."""
        embed = await self.build_ranking_embed()
        self.update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    def update_buttons(self):
        # '이전'과 '다음' 버튼의 활성화/비활성화 상태를 업데이트합니다.
        prev_button = discord.utils.get(self.children, custom_id="prev_page")
        next_button = discord.utils.get(self.children, custom_id="next_page")
        if prev_button:
            prev_button.disabled = self.current_page == 0
        if next_button:
            next_button.disabled = self.current_page >= self.total_pages - 1

    async def build_ranking_embed(self) -> discord.Embed:
        """현재 페이지에 맞는 랭킹 임베드를 생성합니다."""
        offset = self.current_page * self.users_per_page
        
        # 참가자 수와 해당 페이지의 랭킹 데이터를 동시에 가져옵니다.
        count_res = await supabase.table('boss_participants').select('id', count='exact').eq('raid_id', self.raid_id).execute()
        total_participants = count_res.count or 0
        self.total_pages = max(1, (total_participants + self.users_per_page - 1) // self.users_per_page)

        rank_res = await supabase.table('boss_participants').select('user_id, pet_id, total_damage_dealt, pets(nickname)').eq('raid_id', self.raid_id).order('total_damage_dealt', desc=True).range(offset, offset + self.users_per_page - 1).execute()
        
        embed = discord.Embed(title="🏆 피해량 랭킹", color=0xFFD700)
        
        if not rank_res.data:
            embed.description = "아직 랭킹 정보가 없습니다."
        else:
            rank_list = []
            for i, data in enumerate(rank_res.data):
                rank = offset + i + 1
                member = self.cog.bot.get_guild(self.user_id).get_member(data['user_id']) # user_id는 int여야 함
                user_name = member.display_name if member else f"ID:{data['user_id']}"
                pet_name = data['pets']['nickname'] if data.get('pets') else "알 수 없는 펫"
                damage = data['total_damage_dealt']
                
                line = f"`{rank}위.` **{user_name}** - `{pet_name}`: `{damage:,}`"
                if rank <= math.ceil(total_participants * 0.5):
                    line += " 🌟" # 상위 50% 랭커 표시
                rank_list.append(line)
            embed.description = "\n".join(rank_list)

        embed.set_footer(text=f"페이지 {self.current_page + 1} / {self.total_pages} (🌟: 상위 50% 보상 대상)")
        return embed

    @ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary, custom_id="prev_page")
    async def prev_page(self, interaction: discord.Interaction, button: ui.Button):
        self.current_page -= 1
        await self.update_view(interaction)

    @ui.button(label="▶ 다음", style=discord.ButtonStyle.secondary, custom_id="next_page")
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        self.current_page += 1
        await self.update_view(interaction)

async def setup(bot: commands.Bot):
    await bot.add_cog(BossRaid(bot))
