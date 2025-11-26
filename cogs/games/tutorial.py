# cogs/games/tutorial.py

import discord
from discord.ext import commands
from discord import ui
import logging
import asyncio
from typing import Optional, Dict, List
from datetime import datetime, timezone, timedelta

from utils.database import (
    supabase, get_wallet, update_wallet, get_inventory, update_inventory,
    get_user_gear, get_user_pet, get_farm_data, get_config,
    save_panel_id, get_panel_id, get_embed_from_db, get_id,
    log_activity, get_user_abilities, get_all_user_stats, get_cooldown
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

# 튜토리얼 단계 정의
TUTORIAL_STEPS = {
    1: {"title": "출석체크 하기", "desc": "일일 게시판에서 '출석 체크' 버튼을 눌러보세요.", "reward_txt": "1,000 코인", "reward": {"coin": 1000}},
    2: {"title": "소지품 확인", "desc": "프로필 패널에서 '소지품 보기'를 눌러 내 정보를 확인하세요.", "reward_txt": "500 코인", "reward": {"coin": 500}},
    3: {"title": "주사위 게임 도전", "desc": "주사위 게임을 1회 진행해보세요. (승패 무관)", "reward_txt": "500 코인", "reward": {"coin": 500}},
    4: {"title": "슬롯머신 도전", "desc": "슬롯머신을 1회 돌려보세요.", "reward_txt": "500 코인", "reward": {"coin": 500}},
    5: {"title": "일일 퀘스트 완료", "desc": "일일 게시판에서 '보상 받기'를 통해 일일 퀘스트 보상을 1회 수령하세요.", "reward_txt": "1,000 코인 + 100 XP", "reward": {"coin": 1000, "xp": 100}},
    6: {"title": "레벨 확인", "desc": "레벨 확인 패널에서 '상태 확인' 버튼을 눌러보세요.", "reward_txt": "100 코인", "reward": {"coin": 100}},
    7: {"title": "낚시 준비", "desc": "상점에서 '나무 낚싯대'를 구매하고, 프로필-장비 탭에서 장착하세요.", "reward_txt": "일반 낚시 미끼 10개", "reward": {"item": {"일반 낚시 미끼": 10}}},
    8: {"title": "첫 낚시와 판매", "desc": "강이나 바다에서 물고기를 잡고, 상점-판매함에서 물고기를 판매하세요.", "reward_txt": "1,000 코인", "reward": {"coin": 1000}},
    9: {"title": "농사 준비", "desc": "상점에서 '나무 괭이', '나무 물뿌리개', '호박 씨앗'을 각각 1개 이상 구매하세요.", "reward_txt": "구매 비용 환급 (1,000 코인)", "reward": {"coin": 1000}},
    10: {"title": "농부의 시작", "desc": "농장을 만들고, 밭을 갈아 씨앗을 심은 뒤 물을 주세요.\n(이미 농장이 있다면 바로 완료됩니다)", "reward_txt": "🎃 호박 1개 (나중에 요리에 쓰입니다!) + 광산 입장권", "reward": {"item": {"호박": 1, "광산 입장권": 1}}},
    11: {"title": "광산 탐험", "desc": "곡괭이와 입장권을 가지고 광산에 입장하여 채굴을 시도하세요.", "reward_txt": "🥚 랜덤 펫 알 1개", "reward": {"item": {"랜덤 펫 알": 1}}},
    12: {"title": "장비 업그레이드", "desc": "대장간에서 아무 도구나 한 단계 업그레이드 하세요.\n(업그레이드를 **시작**하면 완료됩니다)", "reward_txt": "5,000 코인", "reward": {"coin": 5000}},
    13: {"title": "펫 부화", "desc": "인큐베이터에 알을 등록하여 부화를 시작하세요.", "reward_txt": "최고급 사료 1개", "reward": {"item": {"최고급 사료": 1}}},
    14: {"title": "주간 퀘스트 도전", "desc": "일일 게시판-주간 탭에서 주간 퀘스트 보상을 수령하세요.", "reward_txt": "10,000 코인", "reward": {"coin": 10000}},
    15: {"title": "펫 탐사", "desc": "펫을 탐사 지역으로 1회 보내보세요.", "reward_txt": "2,000 코인", "reward": {"coin": 2000}},
    16: {"title": "요리사 데뷔", "desc": "상점에서 '가마솥'을 구매하고 나만의 부엌을 만드세요.", "reward_txt": "설탕 2개 (요리 재료)", "reward": {"item": {"설탕": 2}}},
    17: {"title": "호박죽 요리", "desc": "상점에서 '설탕'을 구매하거나 보상으로 받은 재료를 사용해 **호박죽**을 만드세요.\n(레시피: 호박 + 설탕)", "reward_txt": "✨ 5,000 XP", "reward": {"xp": 5000}},
    18: {"title": "전직의 길", "desc": "레벨 50을 달성하고 1차 전직을 완료하세요.", "reward_txt": "칭호 [베테랑 주민] + 50,000 코인", "reward": {"coin": 50000, "role": "role_resident_veteran"}}
}

class TutorialView(ui.View):
    def __init__(self, cog: 'TutorialSystem', user: discord.Member, step_data: Dict):
        super().__init__(timeout=None)
        self.cog = cog
        self.user = user
        self.step = step_data.get('current_step', 1)
        self.is_completed = step_data.get('is_completed', False)

    @ui.button(label="진행 상황 확인 & 보상 받기", style=discord.ButtonStyle.success, emoji="✅", custom_id="check_tutorial_progress")
    async def check_progress(self, interaction: discord.Interaction, button: ui.Button):
        # 본인 확인
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("❌ 본인의 튜토리얼만 확인할 수 있습니다.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        
        if self.is_completed:
            return await interaction.followup.send("🎉 이미 모든 튜토리얼을 완료하셨습니다!", ephemeral=True)

        # 조건 검사
        passed = await self.cog.check_step_condition(self.user, self.step)
        
        if passed:
            # 보상 지급 및 단계 상승
            await self.cog.complete_step(interaction, self.user, self.step)
        else:
            # 실패 메시지
            current_info = TUTORIAL_STEPS.get(self.step, {})
            await interaction.followup.send(f"❌ 아직 조건을 달성하지 못했습니다.\n**목표:** {current_info.get('desc')}", ephemeral=True)

class TutorialSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.currency_icon = "🪙"

    async def cog_load(self):
        self.currency_icon = get_config("GAME_CONFIG", {}).get("CURRENCY_ICON", "🪙")

    async def get_user_tutorial(self, user_id: int) -> Dict:
        """
        유저의 튜토리얼 진행 정보를 DB에서 가져옵니다.
        DB 오류 발생 시 안전하게 기본값을 반환합니다.
        """
        try:
            res = await supabase.table('user_tutorials').select('*').eq('user_id', str(user_id)).maybe_single().execute()
            
            # [수정] res가 None이거나 res.data가 None인 경우를 모두 체크
            if res and res.data:
                return res.data
            
            # 데이터가 없으면 생성 시도
            init_res = await supabase.table('user_tutorials').insert({'user_id': str(user_id), 'current_step': 1}).select().execute()
            
            if init_res and init_res.data:
                return init_res.data[0]
            
            # 생성 실패 시 (매우 드문 케이스) 기본 딕셔너리 반환
            logger.warning(f"튜토리얼 데이터 생성 실패 (User: {user_id}). 임시 데이터를 사용합니다.")
            return {'user_id': str(user_id), 'current_step': 1, 'is_completed': False}

        except Exception as e:
            logger.error(f"튜토리얼 정보 조회 중 DB 오류 발생 (User: {user_id}): {e}", exc_info=True)
            # DB 연결 실패 시 봇이 멈추지 않도록 기본값 반환
            return {'user_id': str(user_id), 'current_step': 1, 'is_completed': False}

    async def check_step_condition(self, user: discord.Member, step: int) -> bool:
        uid = user.id
        try:
            if step == 1: # 출석체크
                stats = await get_all_user_stats(uid)
                return stats.get('daily', {}).get('check_in_count', 0) > 0
            
            elif step == 2: # 소지품 확인 (버튼 누르는 행위 자체로 인정)
                return True
            
            elif step == 3: # 주사위 게임
                res = await supabase.table('user_activities').select('count', count='exact').eq('user_id', str(uid)).eq('activity_type', 'dice_game_play').execute()
                # [수정] res가 None인 경우 대비
                return (res.count or 0) > 0 if res else False
            
            elif step == 4: # 슬롯머신
                res = await supabase.table('user_activities').select('count', count='exact').eq('user_id', str(uid)).eq('activity_type', 'slot_machine_play').execute()
                return (res.count or 0) > 0 if res else False
            
            elif step == 5: # 일일 퀘스트 완료 (보상 수령 여부 확인)
                today_str = datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d')
                cooldown_key = f"quest_claimed_daily_all_{today_str}"
                return await get_cooldown(uid, cooldown_key) > 0
            
            elif step == 6: # 레벨 확인 (버튼 누르는 행위 자체로 인정)
                return True
            
            elif step == 7: # 낚싯대 구매 및 장착
                gear = await get_user_gear(user)
                return gear.get('rod') and gear.get('rod') != "맨손"
            
            elif step == 8: # 낚시 후 판매
                # 낚시 1회 이상 AND 판매 1회 이상
                act_fish = await supabase.table('user_activities').select('count', count='exact').eq('user_id', str(uid)).eq('activity_type', 'fishing_catch').execute()
                act_sell = await supabase.table('user_activities').select('count', count='exact').eq('user_id', str(uid)).eq('activity_type', 'sell_fish').execute()
                fish_count = (act_fish.count or 0) if act_fish else 0
                sell_count = (act_sell.count or 0) if act_sell else 0
                return fish_count > 0 and sell_count > 0
            
            elif step == 9: # 괭이, 물뿌리개, 호박 씨앗 구매 (인벤토리 확인)
                inv = await get_inventory(user)
                has_hoe = any('괭이' in name for name in inv.keys()) 
                gear = await get_user_gear(user)
                has_hoe_equipped = '괭이' in gear.get('hoe', '')
                
                has_can = any('물뿌리개' in name for name in inv.keys())
                has_can_equipped = '물뿌리개' in gear.get('watering_can', '')
                
                has_seed = inv.get('호박 씨앗', 0) > 0
                
                return (has_hoe or has_hoe_equipped) and (has_can or has_can_equipped) and has_seed
            
            elif step == 10: # 농장 생성 및 파종, 물주기
                farm = await get_farm_data(uid)
                if not farm: return False
                plots = farm.get('farm_plots', [])
                for plot in plots:
                    if plot['state'] == 'planted':
                        return True
                return False
            
            elif step == 11: # 광산 입장
                res = await supabase.table('user_activities').select('count', count='exact').eq('user_id', str(uid)).eq('activity_type', 'mining').execute()
                return (res.count or 0) > 0 if res else False
            
            elif step == 12: # 대장간 업그레이드
                res = await supabase.table('blacksmith_upgrades').select('count', count='exact').eq('user_id', str(uid)).execute()
                count = (res.count or 0) if res else 0
                if count > 0: return True
                
                # 혹은 장비 중 기본 장비가 아닌 것이 있는지 확인
                gear = await get_user_gear(user)
                for g in gear.values():
                    if any(x in g for x in ['구리', '철', '금', '다이아']):
                        return True
                return False
            
            elif step == 13: # 펫 부화 (등록)
                pet = await get_user_pet(uid)
                res = await supabase.table('pets').select('count', count='exact').eq('user_id', str(uid)).execute()
                return (res.count or 0) > 0 if res else False
            
            elif step == 14: # 주간 퀘스트 (보상 수령 여부)
                now = datetime.now(timezone(timedelta(hours=9)))
                start_of_week = now - timedelta(days=now.weekday())
                week_str = start_of_week.strftime('%Y-%m-%d')
                cooldown_key = f"quest_claimed_weekly_all_{week_str}"
                return await get_cooldown(uid, cooldown_key) > 0
            
            elif step == 15: # 펫 탐사
                res = await supabase.table('pet_explorations').select('count', count='exact').eq('user_id', str(uid)).execute()
                return (res.count or 0) > 0 if res else False
            
            elif step == 16: # 부엌 생성
                res = await supabase.table('user_settings').select('kitchen_thread_id').eq('user_id', str(uid)).maybe_single().execute()
                return res.data and res.data.get('kitchen_thread_id') is not None if res else False
            
            elif step == 17: # 호박죽 요리 (인벤토리 확인)
                inv = await get_inventory(user)
                return inv.get('호박죽', 0) > 0
            
            elif step == 18: # 레벨 50 및 전직
                res = await supabase.table('user_jobs').select('job_id').eq('user_id', str(uid)).execute()
                has_job = (res.data and len(res.data) > 0) if res else False
                
                lvl_res = await supabase.table('user_levels').select('level').eq('user_id', str(uid)).single().execute()
                level = lvl_res.data['level'] if lvl_res and lvl_res.data else 1
                
                return level >= 50 and has_job

        except Exception as e:
            logger.error(f"튜토리얼 조건 검사 중 오류 (Step {step}, User {uid}): {e}", exc_info=True)
            # 에러 발생 시 False를 반환하여 진행을 막고, 유저가 다시 시도하게 함
            return False
        
        return False

    async def complete_step(self, interaction: discord.Interaction, user: discord.Member, step: int):
        info = TUTORIAL_STEPS.get(step)
        reward = info.get('reward', {})
        
        # 보상 지급
        if coin := reward.get('coin'):
            await update_wallet(user, coin)
        if xp := reward.get('xp'):
            # [수정] 안전하게 Cog 호출
            if pet_cog := self.bot.get_cog("PetSystem"):
                # 유저 XP 지급용
                await supabase.rpc('add_xp', {'p_user_id': str(user.id), 'p_xp_to_add': xp}).execute()
        if items := reward.get('item'):
            for name, qty in items.items():
                await update_inventory(user.id, name, qty)
        if role_key := reward.get('role'):
            if role_id := get_id(role_key):
                role = user.guild.get_role(role_id)
                if role: 
                    try: await user.add_roles(role)
                    except: pass

        # DB 업데이트
        next_step = step + 1
        is_finished = next_step > len(TUTORIAL_STEPS)
        
        try:
            await supabase.table('user_tutorials').update({
                'current_step': next_step,
                'is_completed': is_finished,
                'last_updated': datetime.now(timezone.utc).isoformat()
            }).eq('user_id', str(user.id)).execute()
        except Exception as e:
            logger.error(f"튜토리얼 단계 업데이트 실패: {e}")
            await interaction.followup.send("❌ 진행 상황 저장 중 오류가 발생했습니다.", ephemeral=True)
            return

        # 메시지 전송
        embed = discord.Embed(title=f"🎉 튜토리얼 {step}단계 완료!", description=f"보상으로 **{info['reward_txt']}**을(를) 받았습니다.", color=0x2ECC71)
        if is_finished:
            embed.description += "\n\n🏆 **모든 튜토리얼을 마쳤습니다! 진정한 서버의 일원이 되신 것을 환영합니다.**"
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
        # 패널 갱신
        await self.regenerate_panel(interaction.channel)

    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_tutorial", **kwargs):
        # 기존 메시지 삭제 로직 (다른 패널과 동일)
        panel_name = panel_key.replace("panel_", "")
        if panel_info := get_panel_id(panel_name):
            if old_ch := self.bot.get_channel(panel_info.get('channel_id')):
                try:
                    msg = await old_ch.fetch_message(panel_info['message_id'])
                    await msg.delete()
                except: pass
        
        # 새 패널 생성
        embed = discord.Embed(
            title="📘 서버 정착 가이드 (튜토리얼)",
            description="서버의 다양한 기능을 차근차근 배워보세요!\n아래 버튼을 눌러 나의 진행 상황을 확인하고 보상을 받을 수 있습니다.",
            color=0x3498DB
        )
        embed.set_footer(text="총 18단계로 구성되어 있습니다.")
        
        view = ui.View(timeout=None)
        check_button = ui.Button(label="내 튜토리얼 보기", style=discord.ButtonStyle.primary, emoji="🧭", custom_id="open_tutorial_status")
        
        async def open_status(interaction: discord.Interaction):
            # 본인 데이터만 조회
            data = await self.get_user_tutorial(interaction.user.id)
            step = data['current_step']
            is_completed = data['is_completed']
            
            if is_completed:
                embed = discord.Embed(title="🏆 튜토리얼 완료", description="모든 과정을 마치셨습니다. 즐거운 서버 생활 되세요!", color=0xFFD700)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            step_info = TUTORIAL_STEPS.get(step, {})
            embed = discord.Embed(title=f"🧭 튜토리얼 {step}/{len(TUTORIAL_STEPS)}단계", color=0x00BFFF)
            embed.add_field(name=f"📌 목표: {step_info.get('title')}", value=step_info.get('desc'), inline=False)
            embed.add_field(name=f"🎁 보상", value=step_info.get('reward_txt'), inline=False)
            
            status_view = TutorialView(self, interaction.user, data)
            await interaction.response.send_message(embed=embed, view=status_view, ephemeral=True)

        check_button.callback = open_status
        view.add_item(check_button)
        
        try:
            msg = await channel.send(embed=embed, view=view)
            await save_panel_id(panel_name, msg.id, channel.id)
        except Exception as e:
            logger.error(f"튜토리얼 패널 생성 실패: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(TutorialSystem(bot))
