# cogs/games/boss_raid.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
import random
import math
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

from utils.database import (
    supabase, get_user_pet, get_config, get_id,
    update_wallet, update_inventory, save_id_to_db
)
from utils.helpers import format_embed_from_db, create_bar

logger = logging.getLogger(__name__)

WEEKLY_BOSS_CHANNEL_KEY = "weekly_boss_channel_id"
MONTHLY_BOSS_CHANNEL_KEY = "monthly_boss_channel_id"
WEEKLY_BOSS_PANEL_MSG_KEY = "weekly_boss_panel_msg_id"
MONTHLY_BOSS_PANEL_MSG_KEY = "monthly_boss_panel_msg_id"
COMBAT_LOG_CHANNEL_KEY = "boss_log_channel_id"

KST = timezone(timedelta(hours=9))

def get_week_start_utc() -> datetime:
    now_kst = datetime.now(KST)
    start_of_week_kst = now_kst - timedelta(days=now_kst.weekday())
    return start_of_week_kst.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

def get_month_start_utc() -> datetime:
    now_kst = datetime.now(KST)
    return now_kst.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


class BossPanelView(ui.View):
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
            label=challenge_label, style=discord.ButtonStyle.success,
            custom_id=f"boss_challenge:{self.boss_type}", disabled=(is_combat_locked or is_defeated)
        )
        challenge_button.callback = self.on_challenge_click
        self.add_item(challenge_button)

        ranking_button = ui.Button(
            label="🏆 현재 랭킹", style=discord.ButtonStyle.secondary,
            custom_id=f"boss_ranking:{self.boss_type}", disabled=is_defeated
        )
        ranking_button.callback = self.on_ranking_click
        self.add_item(ranking_button)

    async def on_challenge_click(self, interaction: discord.Interaction):
        await self.cog.handle_challenge(interaction, self.boss_type)

    async def on_ranking_click(self, interaction: discord.Interaction):
        await self.cog.handle_ranking(interaction, self.boss_type)

class BossCombatView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)


class BossRaid(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_combats: Dict[str, asyncio.Task] = {}
        self.combat_lock = asyncio.Lock()
        self.panel_updater_loop.start()
        self.boss_reset_loop.start()

    def cog_unload(self):
        self.panel_updater_loop.cancel()
        self.boss_reset_loop.cancel()
        for task in self.active_combats.values():
            task.cancel()

    @tasks.loop(minutes=2)
    async def panel_updater_loop(self):
        await self.update_all_boss_panels()

    @tasks.loop(hours=1)
    async def boss_reset_loop(self):
        now_kst = datetime.now(KST)

        if now_kst.weekday() == 0 and now_kst.hour == 0:
            active_weekly_raid_res = await supabase.table('boss_raids').select('id, bosses!inner(type)').eq('status', 'active').eq('bosses.type', 'weekly').maybe_single().execute()
            if not (active_weekly_raid_res and active_weekly_raid_res.data):
                logger.info("[BossRaid] 새로운 주간 보스를 생성합니다.")
                await self.create_new_raid('weekly', force=True)

        if now_kst.day == 1 and now_kst.hour == 0:
            active_monthly_raid_res = await supabase.table('boss_raids').select('id, bosses!inner(type)').eq('status', 'active').eq('bosses.type', 'monthly').maybe_single().execute()
            if not (active_monthly_raid_res and active_monthly_raid_res.data):
                logger.info("[BossRaid] 새로운 월간 보스를 생성합니다.")
                await self.create_new_raid('monthly', force=True)
    
    @boss_reset_loop.before_loop
    async def before_boss_reset_loop(self):
        await self.bot.wait_until_ready()

    async def create_new_raid(self, boss_type: str, force: bool = False):
        try:
            if force:
                logger.info(f"[{boss_type.upper()}] 기존 레이드를 강제 종료/만료시킵니다.")
                raids_to_expire_res = await supabase.table('boss_raids').select('id, bosses!inner(type)').eq('bosses.type', boss_type).eq('status', 'active').execute()
                if raids_to_expire_res and raids_to_expire_res.data:
                    raid_ids_to_expire = [raid['id'] for raid in raids_to_expire_res.data]
                    if raid_ids_to_expire:
                        await supabase.table('boss_raids').update({'status': 'expired'}).in_('id', raid_ids_to_expire).execute()

            boss_template_res = await supabase.table('bosses').select('*').eq('type', boss_type).limit(1).single().execute()
            if not boss_template_res.data: return
            boss_template = boss_template_res.data
            
            await supabase.table('boss_raids').insert({'boss_id': boss_template['id'], 'current_hp': boss_template['max_hp'], 'status': 'active'}).execute()
            
            channel_key = WEEKLY_BOSS_CHANNEL_KEY if boss_type == 'weekly' else MONTHLY_BOSS_CHANNEL_KEY
            channel_id = get_id(channel_key)
            if channel_id and (channel := self.bot.get_channel(channel_id)):
                embed = discord.Embed(title=f"‼️ 새로운 {boss_template['name']}이(가) 나타났습니다!", description="마을의 평화를 위해 힘을 합쳐 보스를 물리치세요!", color=0xF1C40F)
                if boss_template.get('image_url'): embed.set_thumbnail(url=boss_template['image_url'])
                await channel.send(embed=embed, delete_after=86400)

            await self.regenerate_panel(boss_type)
        except Exception as e:
            logger.error(f"[{boss_type.upper()}] 신규 레이드 생성 중 오류 발생: {e}", exc_info=True)
            
    async def update_all_boss_panels(self, boss_type_to_update: Optional[str] = None):
        types_to_process = [boss_type_to_update] if boss_type_to_update else ['weekly', 'monthly']
        for boss_type in types_to_process:
            await self.regenerate_panel(boss_type=boss_type)
            await asyncio.sleep(1)

    async def regenerate_panel(self, boss_type: str, channel: Optional[discord.TextChannel] = None):
        channel_key = WEEKLY_BOSS_CHANNEL_KEY if boss_type == 'weekly' else MONTHLY_BOSS_CHANNEL_KEY
        msg_key = WEEKLY_BOSS_PANEL_MSG_KEY if boss_type == 'weekly' else MONTHLY_BOSS_PANEL_MSG_KEY
        
        if not channel:
            channel_id = get_id(channel_key)
            if not channel_id or not (channel := self.bot.get_channel(channel_id)): return

        raid_res = await supabase.table('boss_raids').select('*, bosses!inner(*)').eq('status', 'active').eq('bosses.type', boss_type).maybe_single().execute()
        raid_data = raid_res.data if raid_res and hasattr(raid_res, 'data') else None
        
        is_combat_locked = self.combat_lock.locked()
        is_defeated = not (raid_data and raid_data.get('status') == 'active')

        view = BossPanelView(self, boss_type, is_combat_locked, is_defeated)
        
        if raid_data:
            embed = self.build_boss_panel_embed(raid_data)
        else:
            embed = discord.Embed(
                title=f"👑 다음 {('주간' if boss_type == 'weekly' else '월간')} 보스를 기다리는 중...",
                description="새로운 보스가 곧 나타납니다!\n리셋 시간: " + ("매주 월요일 00시" if boss_type == 'weekly' else "매월 1일 00시"),
                color=0x34495E
            )
            for item in view.children: item.disabled = True

        message_id = get_id(msg_key)
        try:
            if message_id:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=embed, view=view)
            else:
                await channel.purge(limit=100)
                new_message = await channel.send(embed=embed, view=view)
                await save_id_to_db(msg_key, new_message.id)
                await new_message.pin()
        except discord.NotFound:
            await channel.purge(limit=100)
            new_message = await channel.send(embed=embed, view=view)
            await save_id_to_db(msg_key, new_message.id)
            await new_message.pin()
        except (discord.Forbidden, discord.HTTPException) as e:
            logger.error(f"[{boss_type.upper()}] 패널 메시지를 수정/생성/고정하는 데 실패했습니다: {e}")
            
    def build_boss_panel_embed(self, raid_data: Dict[str, Any]) -> discord.Embed:
        boss_info = raid_data.get('bosses')
        if not boss_info:
            logger.error(f"레이드 데이터(ID: {raid_data.get('id')})에 연결된 보스 정보(bosses)가 없습니다.")
            return discord.Embed(title="데이터 오류", description="활성 레이드에 연결된 보스 정보를 찾을 수 없습니다.", color=discord.Color.red())
        
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

    async def handle_challenge(self, interaction: discord.Interaction, boss_type: str):
        user = interaction.user
        if self.combat_lock.locked():
            await interaction.response.send_message("❌ 다른 유저가 전투 중입니다. 잠시 후 다시 시도해주세요.", ephemeral=True, delete_after=5)
            return

        raid_res = await supabase.table('boss_raids').select('id, bosses!inner(type)').eq('status', 'active').eq('bosses.type', boss_type).maybe_single().execute()
        if not (raid_res and raid_res.data):
            await interaction.response.send_message("❌ 현재 도전할 수 있는 보스가 없습니다.", ephemeral=True)
            return
        
        raid_id = raid_res.data['id']
        pet = await get_user_pet(user.id)
        if not pet:
            await interaction.response.send_message("❌ 전투에 참여할 펫이 없습니다.", ephemeral=True)
            return
        
        start_time_utc = get_week_start_utc() if boss_type == 'weekly' else get_month_start_utc()
        part_res = await supabase.table('boss_participants').select('last_fought_at').eq('raid_id', raid_id).eq('user_id', user.id).maybe_single().execute()
        if part_res and part_res.data and part_res.data.get('last_fought_at'):
            last_fought_dt = datetime.fromisoformat(part_res.data['last_fought_at'].replace('Z', '+00:00'))
            if last_fought_dt >= start_time_utc:
                 await interaction.response.send_message(f"❌ 이번 {('주' if boss_type == 'weekly' else '달')}에는 이미 보스에게 도전했습니다.", ephemeral=True)
                 return
        
        async with self.combat_lock:
            await interaction.response.send_message("✅ 전투를 준비합니다... 잠시만 기다려주세요.", ephemeral=True, delete_after=3)
            await self.update_all_boss_panels()
            combat_task = asyncio.create_task(self.run_combat_simulation(interaction, user, pet, raid_id, boss_type))
            self.active_combats[boss_type] = combat_task
            try:
                await combat_task
            finally:
                self.active_combats.pop(boss_type, None)
        await self.update_all_boss_panels()

    async def run_combat_simulation(self, interaction: discord.Interaction, user: discord.Member, pet: Dict, raid_id: int, boss_type: str):
        """실시간 턴제 전투를 시뮬레이션하고 UI를 업데이트합니다."""
        combat_message = None
        try:
            raid_res = await supabase.table('boss_raids').select('*, bosses(*)').eq('id', raid_id).single().execute()
            raid_data = raid_res.data
            boss = raid_data['bosses']
            pet_hp, pet_attack, pet_defense, pet_speed = pet.get('current_hp', 100), pet.get('current_attack', 10), pet.get('current_defense', 10), pet.get('current_speed', 10)
            boss_hp, boss_attack, boss_defense = raid_data['current_hp'], boss['attack'], boss['defense']
            boss_speed = int(boss_attack * 0.5)
            combat_logs = [f"**{user.display_name}**님이 **{pet['nickname']}**와(과) 함께 전투를 시작합니다!"]
            total_damage_dealt = 0
            view = BossCombatView()
            embed = self.build_combat_embed(user, pet, boss, pet_hp, boss_hp, combat_logs)
            combat_message = await interaction.channel.send(embed=embed, view=view)
            turn_count = 0
            while pet_hp > 0 and boss_hp > 0 and turn_count < 50:
                turn_count += 1
                await asyncio.sleep(2.5)
                pet_first = pet_speed > boss_speed
                if pet_first:
                    if pet_hp > 0:
                        damage_reduction = boss_defense / (boss_defense + 200)
                        base_damage = pet_attack * random.uniform(0.9, 1.1)
                        pet_damage = max(1, int(base_damage * (1 - damage_reduction)))
                        boss_hp -= pet_damage
                        total_damage_dealt += pet_damage
                        combat_logs.append(f"🔥 **{pet['nickname']}**이(가) `{pet_damage}`의 피해를 입혔습니다!")
                        await combat_message.edit(embed=self.build_combat_embed(user, pet, boss, pet_hp, boss_hp, combat_logs))
                        if boss_hp <= 0: break
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
                else:
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
                    if pet_hp > 0:
                        damage_reduction = boss_defense / (boss_defense + 200)
                        base_damage = pet_attack * random.uniform(0.9, 1.1)
                        pet_damage = max(1, int(base_damage * (1 - damage_reduction)))
                        boss_hp -= pet_damage
                        total_damage_dealt += pet_damage
                        combat_logs.append(f"🔥 **{pet['nickname']}**이(가) `{pet_damage}`의 피해를 입혔습니다!")
                        await combat_message.edit(embed=self.build_combat_embed(user, pet, boss, pet_hp, boss_hp, combat_logs))
                        if boss_hp <= 0: break
            combat_logs.append("---")
            if boss_hp <= 0:
                combat_logs.append(f"🎉 **{boss['name']}**을(를) 쓰러뜨렸습니다!")
            else:
                combat_logs.append(f"☠️ **{pet['nickname']}**이(가) 쓰러졌습니다.")
            combat_logs.append(f"✅ 전투 종료! 총 `{total_damage_dealt:,}`의 피해를 입혔습니다.")
            await combat_message.edit(embed=self.build_combat_embed(user, pet, boss, pet_hp, boss_hp, combat_logs))

            final_boss_hp = max(0, raid_data['current_hp'] - total_damage_dealt)
            new_log_entry = f"`[{datetime.now(KST).strftime('%H:%M')}]` ⚔️ **{user.display_name}** 님이 `{total_damage_dealt:,}`의 피해를 입혔습니다. (남은 HP: `{final_boss_hp:,}`)"
            recent_logs = raid_data.get('recent_logs', [])
            recent_logs.insert(0, new_log_entry)
            await supabase.table('boss_raids').update({'current_hp': final_boss_hp, 'recent_logs': recent_logs[:10]}).eq('id', raid_id).execute()

            part_res = await supabase.table('boss_participants').select('total_damage_dealt').eq('raid_id', raid_id).eq('user_id', user.id).maybe_single().execute()
            
            existing_damage = 0
            if part_res and part_res.data:
                existing_damage = part_res.data.get('total_damage_dealt', 0)
            
            new_total_damage = existing_damage + total_damage_dealt
            
            await supabase.table('boss_participants').upsert({
                'raid_id': raid_id,
                'user_id': user.id,
                'pet_id': pet['id'],
                'total_damage_dealt': new_total_damage,
                'last_fought_at': datetime.now(timezone.utc).isoformat()
            }).execute()
            
            if final_boss_hp <= 0 and raid_data['status'] == 'active':
                 await self.handle_boss_defeat(interaction.channel, raid_id)

        except Exception as e:
            logger.error(f"보스 전투 시뮬레이션 중 오류: {e}", exc_info=True)
            if combat_message:
                await combat_message.edit(content="전투 중 오류가 발생했습니다.", embed=None, view=None)
        finally:
            if combat_message:
                await asyncio.sleep(10)
                try: await combat_message.delete()
                except discord.NotFound: pass

    def build_combat_embed(self, user: discord.Member, pet: Dict, boss: Dict, pet_hp: int, boss_hp: int, logs: List[str]) -> discord.Embed:
        embed = discord.Embed(title=f"⚔️ {boss['name']}와(과)의 전투", color=0xC27C0E)
        embed.set_author(name=f"{user.display_name}님의 도전", icon_url=user.display_avatar.url if user.display_avatar else None)
        pet_stats_text = (f"❤️ **HP:** `{max(0, pet_hp)} / {pet['current_hp']}`\n" f"⚔️ **공격력:** `{pet['current_attack']}`\n" f"🛡️ **방어력:** `{pet['current_defense']}`\n" f"💨 **스피드:** `{pet['current_speed']}`")
        embed.add_field(name=f"내 펫: {pet['nickname']} (Lv.{pet['level']})", value=pet_stats_text, inline=True)
        boss_speed = int(boss['attack'] * 0.5)
        boss_stats_text = (f"❤️ **HP:** `{max(0, boss_hp):,} / {boss['max_hp']:,}`\n" f"⚔️ **공격력:** `{boss['attack']}`\n" f"🛡️ **방어력:** `{boss['defense']}`\n" f"💨 **스피드:** `{boss_speed}`")
        embed.add_field(name=f"보스: {boss['name']}", value=boss_stats_text, inline=True)
        log_text = "\n".join(f"> {line}" for line in logs[-10:])
        embed.add_field(name="--- 전투 기록 ---", value=log_text, inline=False)
        return embed

    async def handle_boss_defeat(self, channel: discord.TextChannel, raid_id: int):
        raid_update_res = await supabase.table('boss_raids').update({'status': 'defeated', 'defeat_time': datetime.now(timezone.utc).isoformat()}).eq('id', raid_id).eq('status', 'active').select('*, bosses(*)').single().execute()
        if not raid_update_res.data:
            logger.warning(f"Raid ID {raid_id}는 이미 처치되었거나 활성 상태가 아닙니다. 보상 지급을 건너뜁니다.")
            return
        raid_data = raid_update_res.data
        boss_name = raid_data['bosses']['name']
        defeat_embed = discord.Embed(title=f"🎉 {boss_name} 처치 성공!", description="용감한 모험가들의 활약으로 보스를 물리쳤습니다!\n\n참가자들에게 곧 보상이 지급되며, 최종 랭킹이 공지될 예정입니다...", color=0x2ECC71)
        await channel.send(embed=defeat_embed, delete_after=86400)
        await self.distribute_rewards(channel, raid_id, boss_name)

    async def distribute_rewards(self, channel: discord.TextChannel, raid_id: int, boss_name: str):
        try:
            part_res = await supabase.table('boss_participants').select('user_id, total_damage_dealt, pets(nickname)', count='exact').eq('raid_id', raid_id).order('total_damage_dealt', desc=True).execute()
            if not part_res.data:
                logger.info(f"Raid ID {raid_id}에 참가자가 없어 보상 지급을 건너뜁니다.")
                return
            participants = part_res.data
            total_participants = part_res.count or 0
            base_reward_item = "주간 보스 보물 상자" if "주간" in boss_name else "월간 보스 보물 상자"
            rare_reward_items = ["각성의 코어", "초월의 핵"]
            top_50_percent_count = (total_participants + 1) // 2
            db_tasks = []
            reward_summary = {}
            for i, participant in enumerate(participants):
                user_id = participant['user_id']
                reward_summary[user_id] = [base_reward_item]
                db_tasks.append(update_inventory(user_id, base_reward_item, 1))
                if i < top_50_percent_count and random.random() < 0.05:
                    rare_reward = random.choice(rare_reward_items)
                    db_tasks.append(update_inventory(user_id, rare_reward, 1))
                    reward_summary[user_id].append(rare_reward)
            await asyncio.gather(*db_tasks)
            logger.info(f"Raid ID {raid_id}의 보상 지급 DB 작업 {len(db_tasks)}개를 완료했습니다.")
            log_channel_id = get_id(COMBAT_LOG_CHANNEL_KEY)
            log_channel = self.bot.get_channel(log_channel_id) if log_channel_id else channel
            final_embed = discord.Embed(title=f"🏆 {boss_name} 최종 랭킹 및 보상", color=0x5865F2)
            rank_list = []
            for i, data in enumerate(participants[:10]):
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

    async def handle_ranking(self, interaction: discord.Interaction, boss_type: str):
        raid_res = await supabase.table('boss_raids').select('id, bosses!inner(type, name)').eq('status', 'active').eq('bosses.type', boss_type).maybe_single().execute()
        if not (raid_res and raid_res.data):
            await interaction.response.send_message("❌ 현재 조회할 수 있는 랭킹 정보가 없습니다.", ephemeral=True)
            return
        
        raid_id = raid_res.data['id']
        # --- ▼▼▼▼▼ 핵심 수정 시작 ▼▼▼▼▼ ---
        # 원인: RankingView 생성자에 user_id 대신 user 객체를 전달해야 합니다.
        # 해결: interaction.user.id 대신 interaction.user를 전달합니다.
        ranking_view = RankingView(self, raid_id, interaction.user)
        # --- ▲▲▲▲▲ 핵심 수정 종료 ▲▲▲▲▲ ---
        await ranking_view.start(interaction)

class RankingView(ui.View):
    # --- ▼▼▼▼▼ 핵심 수정 시작 ▼▼▼▼▼ ---
    # 원인: 생성자에서 user_id만 받고 user 객체를 저장하지 않았습니다.
    # 해결: user 객체를 직접 받고 self.user와 self.user_id에 모두 할당합니다.
    def __init__(self, cog_instance: 'BossRaid', raid_id: int, user: discord.Member):
        super().__init__(timeout=180)
        self.cog = cog_instance
        self.raid_id = raid_id
        self.user = user
        self.user_id = user.id
        self.current_page = 0
        self.users_per_page = 10
        self.total_pages = 1
    # --- ▲▲▲▲▲ 핵심 수정 종료 ▲▲▲▲▲ ---
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("랭킹을 조회한 본인만 조작할 수 있습니다.", ephemeral=True, delete_after=5)
            return False
        return True
    async def start(self, interaction: discord.Interaction):
        embed = await self.build_ranking_embed()
        self.update_buttons()
        await interaction.response.send_message(embed=embed, view=self, ephemeral=True)
    async def update_view(self, interaction: discord.Interaction):
        embed = await self.build_ranking_embed()
        self.update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)
    def update_buttons(self):
        prev_button = discord.utils.get(self.children, custom_id="prev_page")
        next_button = discord.utils.get(self.children, custom_id="next_page")
        if prev_button: prev_button.disabled = self.current_page == 0
        if next_button: next_button.disabled = self.current_page >= self.total_pages - 1
    
    async def build_ranking_embed(self) -> discord.Embed:
        offset = self.current_page * self.users_per_page
        count_res = await supabase.table('boss_participants').select('id', count='exact').eq('raid_id', self.raid_id).execute()
        total_participants = count_res.count or 0
        self.total_pages = max(1, (total_participants + self.users_per_page - 1) // self.users_per_page)
        rank_res = await supabase.table('boss_participants').select('user_id, pet_id, total_damage_dealt, pets(nickname)').eq('raid_id', self.raid_id).order('total_damage_dealt', desc=True).range(offset, offset + self.users_per_page - 1).execute()
        embed = discord.Embed(title="🏆 피해량 랭킹", color=0xFFD700)
        
        if not rank_res.data:
            embed.description = "아직 랭킹 정보가 없습니다."
        else:
            rank_list = []
            guild = self.user.guild
            
            for i, data in enumerate(rank_res.data):
                rank = offset + i + 1
                user_id_int = data['user_id']
                member = guild.get_member(user_id_int) if guild else None
                user_display = member.mention if member else f"ID:{user_id_int}"
                pet_name = data['pets']['nickname'] if data.get('pets') else "알 수 없는 펫"
                damage = data['total_damage_dealt']
                
                line = f"`{rank}위.` {user_display} - `{pet_name}`: `{damage:,}`"
                if rank <= math.ceil(total_participants * 0.5):
                    line += " 🌟"
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
