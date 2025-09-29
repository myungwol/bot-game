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
        # 다음 단계에서 구현
        pass

    async def update_all_boss_panels(self, boss_type_to_update: Optional[str] = None):
        types_to_process = [boss_type_to_update] if boss_type_to_update else ['weekly', 'monthly']
        for boss_type in types_to_process:
            await self.regenerate_panel(boss_type=boss_type)
            await asyncio.sleep(1) # API 제한 방지를 위한 짧은 딜레이

    async def regenerate_panel(self, boss_type: str, channel: Optional[discord.TextChannel] = None):
        logger.info(f"[{boss_type.upper()}] 패널 재생성 시작...")
        
        channel_key = WEEKLY_BOSS_CHANNEL_KEY if boss_type == 'weekly' else MONTHLY_BOSS_CHANNEL_KEY
        msg_key = WEEKLY_BOSS_PANEL_MSG_KEY if boss_type == 'weekly' else MONTHLY_BOSS_PANEL_MSG_KEY
        
        if not channel:
            channel_id = get_id(channel_key)
            if not channel_id or not (channel := self.bot.get_channel(channel_id)):
                logger.warning(f"[{boss_type.upper()}] 보스 채널이 설정되지 않았거나 찾을 수 없습니다.")
                return

        raid_res = await supabase.table('boss_raids').select('*, bosses(*)').eq('status', 'active').eq('bosses.type', boss_type).maybe_single().execute()
        
        is_combat_locked = self.combat_lock.locked()
        is_defeated = not (raid_res.data and raid_res.data['status'] == 'active')

        view = BossPanelView(self, boss_type, is_combat_locked, is_defeated)
        
        if raid_res.data:
            embed = self.build_boss_panel_embed(raid_res.data)
        else:
            embed = discord.Embed(
                title=f"👑 다음 {boss_type} 보스를 기다리는 중...",
                description="새로운 보스가 곧 나타납니다!\n리셋 시간: " + ("매주 월요일 00시" if boss_type == 'weekly' else "매월 1일 00시"),
                color=0x34495E
            )

        message_id = get_id(msg_key)
        try:
            if message_id:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=embed, view=view)
            else:
                await channel.purge(limit=100)
                new_message = await channel.send(embed=embed, view=view)
                await supabase.table('channel_configs').upsert({'channel_key': msg_key, 'channel_id': str(new_message.id)}).execute()
                await new_message.pin()
        except discord.NotFound:
            await channel.purge(limit=100)
            new_message = await channel.send(embed=embed, view=view)
            await supabase.table('channel_configs').upsert({'channel_key': msg_key, 'channel_id': str(new_message.id)}).execute()
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
        user = interaction.user
        
        if self.combat_lock.locked():
            await interaction.response.send_message("❌ 다른 유저가 전투 중입니다. 잠시 후 다시 시도해주세요.", ephemeral=True, delete_after=5)
            return

        # 1. 도전 조건 확인
        raid_res = await supabase.table('boss_raids').select('id').eq('status', 'active').eq('bosses.type', boss_type).single().execute()
        if not raid_res.data:
            await interaction.response.send_message("❌ 현재 도전할 수 있는 보스가 없습니다.", ephemeral=True); return
        
        raid_id = raid_res.data['id']
        
        pet = await get_user_pet(user.id)
        if not pet:
            await interaction.response.send_message("❌ 전투에 참여할 펫이 없습니다.", ephemeral=True); return
        
        # 2. 도전 횟수 확인
        start_time_utc = get_week_start_utc() if boss_type == 'weekly' else get_month_start_utc()
        
        part_res = await supabase.table('boss_participants').select('last_fought_at').eq('raid_id', raid_id).eq('user_id', user.id).maybe_single().execute()
        
        if part_res.data and part_res.data['last_fought_at']:
            last_fought_dt = datetime.fromisoformat(part_res.data['last_fought_at'].replace('Z', '+00:00'))
            if last_fought_dt >= start_time_utc:
                 await interaction.response.send_message(f"❌ 이번 {('주' if boss_type == 'weekly' else '달')}에는 이미 보스에게 도전했습니다.", ephemeral=True)
                 return
        
        # 3. 전투 시작
        async with self.combat_lock:
            await interaction.response.send_message("✅ 전투를 준비합니다... 잠시만 기다려주세요.", ephemeral=True, delete_after=3)
            await self.update_all_boss_panels() # 도전하기 버튼을 비활성화하기 위해 패널 업데이트

            combat_task = asyncio.create_task(self.run_combat_simulation(interaction, user, pet, raid_id, boss_type))
            self.active_combats[boss_type] = combat_task
            await combat_task
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
        await channel.send("🎉 **보스를 처치했습니다!** 잠시 후 보상이 지급됩니다.")
        # 다음 단계에서 구현

    async def handle_ranking(self, interaction: discord.Interaction, boss_type: str):
        await interaction.response.send_message(f"[{boss_type}] 랭킹 보기 기능은 현재 개발 중입니다.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BossRaid(bot))
