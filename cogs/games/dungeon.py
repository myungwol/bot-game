# cogs/games/dungeon.py

import discord
from discord.ext import commands, tasks
from discord import ui
import logging
import asyncio
import random
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any
from collections import defaultdict
from discord import app_commands
from typing import Optional # Optional import 했는지 확인

from utils.database import (
    get_inventory, update_inventory, supabase, get_id,
    save_panel_id, get_panel_id, get_embed_from_db,
    get_item_database, get_user_pet # [수정] get_user_pet 추가
)
from utils.helpers import format_embed_from_db

logger = logging.getLogger(__name__)

def pad_korean_string(text: str, total_width: int) -> str:
    current_width = 0
    for char in text:
        if '\uac00' <= char <= '\ud7a3':
            current_width += 2
        else:
            current_width += 1
    
    padding = " " * max(0, total_width - current_width)
    return text + padding

async def load_dungeon_data_from_db() -> Dict[str, Any]:
    try:
        dungeons_res, monsters_res, loot_res = await asyncio.gather(
            supabase.table('dungeons').select('*').order('recommended_level').execute(),
            supabase.table('monster_species').select('*').execute(),
            supabase.table('dungeon_loot').select('*').execute()
        )
        dungeon_data = {d['tier_key']: d for d in dungeons_res.data} if dungeons_res.data else {}
        monster_base_data = {m['element_key']: m for m in monsters_res.data} if monsters_res.data else {}
        loot_table = defaultdict(dict)
        if loot_res.data:
            for item in loot_res.data:
                loot_table[item['dungeon_tier']][item['item_name']] = (item['drop_chance'], item['min_qty'], item['max_qty'])
        logger.info(f"✅ 던전 데이터 로드 완료: 던전({len(dungeon_data)}), 몬스터({len(monster_base_data)}), 보상({len(loot_table)})")
        return {"dungeons": dungeon_data, "monsters": monster_base_data, "loot": dict(loot_table)}
    except Exception as e:
        logger.error(f"❌ 던전 데이터 DB 로드 실패: {e}", exc_info=True)
        return {"dungeons": {}, "monsters": {}, "loot": {}}

class SkillSelectView(ui.View):
    def __init__(self, main_view: 'DungeonGameView', learned_skills: List[Dict]):
        super().__init__(timeout=60)
        self.main_view = main_view  # DungeonGameView 인스턴스를 저장
        self.learned_skills = learned_skills
        self._build_components()

    def _build_components(self):
        if not self.learned_skills:
            self.add_item(ui.Button(label="배운 스킬이 없습니다!", disabled=True))
            return

        options = [
            discord.SelectOption(
                label=s['pet_skills']['skill_name'],
                value=str(s['pet_skills']['id']),
                description=f"위력: {s['pet_skills']['power']} | 속성: {s['pet_skills']['element']}"
            ) for s in self.learned_skills
        ]

        skill_select = ui.Select(placeholder="사용할 스킬을 선택하세요...", options=options)
        skill_select.callback = self.on_skill_select
        self.add_item(skill_select)

    async def on_skill_select(self, interaction: discord.Interaction):
        # 1. 스킬 선택 상호작용에 대한 응답
        await interaction.response.defer()

        # 2. 선택된 스킬 정보 찾기
        skill_id = int(interaction.data['values'][0])
        skill_data = next((s['pet_skills'] for s in self.learned_skills if s['pet_skills']['id'] == skill_id), None)
        
        # 3. 메인 View의 스킬 사용 로직 호출
        if skill_data:
            await self.main_view.handle_skill_use(skill_data)
        
        # 4. 스킬 선택창(임시 메시지) 삭제
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass # 이미 처리되었거나 다른 이유로 메시지가 없을 수 있음
        
        self.stop()

# 이제 DungeonGameView 클래스가 시작됩니다.
class DungeonGameView(ui.View):
    def __init__(self, cog: 'Dungeon', user: discord.Member, pet_data: Dict, dungeon_tier: str, end_time: datetime, session_id: int, current_state: str = "exploring", monster_data: Optional[Dict] = None):
        super().__init__(timeout=None)
        self.cog = cog; self.user = user; self.pet_data_raw = pet_data; self.dungeon_tier = dungeon_tier; self.end_time = end_time
        self.session_id = session_id
        self.final_pet_stats = self._calculate_final_pet_stats()
        
        # [수정] DB에서 불러온 state와 monster 데이터로 초기화합니다.
        self.state = current_state
        self.message: Optional[discord.Message] = None
        self.battle_log: List[Any] = []
        self.rewards: Dict[str, int] = defaultdict(int)
        self.total_pet_xp_gained: int = 0
        
        self.pet_current_hp: int = self.pet_data_raw.get('current_hp') or self.final_pet_stats['hp']
        self.pet_is_defeated: bool = self.pet_current_hp <= 0
        
        self.is_pet_turn: bool = True
        
        self.pet_effects: List[Dict] = []
        self.monster_effects: List[Dict] = []
        
        # [수정] DB에서 불러온 monster 데이터로 복원합니다.
        self.current_monster: Optional[Dict] = monster_data.get('data') if monster_data else None
        self.monster_current_hp: int = monster_data.get('hp', 0) if monster_data else 0
        
        self.storage_base_url = f"{os.environ.get('SUPABASE_URL')}/storage/v1/object/public/monster_images"
        
        self.build_components()

    # _calculate_final_pet_stats, start, generate_monster 는 변경 없음 (생략)
    def _calculate_final_pet_stats(self) -> Dict[str, int]:
        species_info = self.pet_data_raw.get('pet_species', {})
        level = self.pet_data_raw.get('level', 1)
        stats = {}
        for key in ['hp', 'attack', 'defense', 'speed']:
            base = species_info.get(f'base_{key}', 0) + (level - 1) * species_info.get(f'{key}_growth', 0)
            natural_bonus = self.pet_data_raw.get(f"natural_bonus_{key}", 0)
            allocated = self.pet_data_raw.get(f"allocated_{key}", 0)
            stats[key] = round(base) + natural_bonus + allocated
        return stats

    async def start(self, thread: discord.Thread):
        embed = self.build_embed()
        self.message = await thread.send(embed=embed, view=self)
        try:
            await supabase.table('dungeon_sessions').update({'message_id': self.message.id}).eq('id', self.session_id).execute()
        except Exception as e:
            logger.error(f"던전 세션(ID:{self.session_id})의 message_id 업데이트 실패: {e}")

    def generate_monster(self) -> Dict:
        dungeon_info = self.cog.dungeon_data[self.dungeon_tier]
        element = random.choice(dungeon_info['elements'])
        base_monster = self.cog.monster_base_data[element]
        monster_level = random.randint(dungeon_info.get('min_monster_level', 1), dungeon_info.get('max_monster_level', 5))
        hp_bonus = (monster_level - 1) * 8; other_stat_bonus = (monster_level - 1) * 5
        hp = int(base_monster['base_hp'] * dungeon_info['hp_mult']) + hp_bonus
        attack = int(base_monster['base_attack'] * dungeon_info['atk_mult']) + other_stat_bonus
        defense = int(base_monster['base_defense'] * dungeon_info['def_mult']) + other_stat_bonus
        speed = int(base_monster['base_speed'] * dungeon_info['spd_mult']) + other_stat_bonus
        xp = max(1, int(hp * dungeon_info['xp_mult']) // 20) + (monster_level * 2)
        image_url = f"{self.storage_base_url}/{element}_{dungeon_info['image_suffix']}.png"
        return {"name": f"Lv.{monster_level} {dungeon_info['name'].replace('던전', '')} {base_monster['name']}", "hp": hp, "attack": attack, "defense": defense, "speed": speed, "xp": xp, "element": element, "image_url": image_url}

    # ▼▼▼ [교체] build_embed 메서드 전체를 아래 코드로 교체해주세요 ▼▼▼
    def build_embed(self) -> discord.Embed:
        dungeon_info = self.cog.dungeon_data[self.dungeon_tier]
        embed = discord.Embed(title=f"탐험 중... - {dungeon_info['name']}", color=0x71368A)
        description_content = ""

        # --- 펫 스탯 표시 로직 (수정됨) ---
        pet_base_stats = self.final_pet_stats
        
        # 현재 효과를 적용한 최종 스탯 계산
        pet_final_attack = self._get_stat_with_effects(pet_base_stats['attack'], 'ATK', self.pet_effects)
        pet_final_defense = self._get_stat_with_effects(pet_base_stats['defense'], 'DEF', self.pet_effects)
        pet_final_speed = self._get_stat_with_effects(pet_base_stats['speed'], 'SPD', self.pet_effects)
        
        # 스탯 변동량에 따라 색상과 화살표 추가하는 함수
        def get_stat_change_indicator(base_stat, final_stat):
            if final_stat > base_stat:
                return f"🔺 (+{final_stat - base_stat})"
            elif final_stat < base_stat:
                return f"🔻 ({final_stat - base_stat})"
            return ""

        pet_attack_str = f"{pet_final_attack} {get_stat_change_indicator(pet_base_stats['attack'], pet_final_attack)}"
        pet_defense_str = f"{pet_final_defense} {get_stat_change_indicator(pet_base_stats['defense'], pet_final_defense)}"
        pet_speed_str = f"{pet_final_speed} {get_stat_change_indicator(pet_base_stats['speed'], pet_final_speed)}"

        pet_stats_text = (f"❤️ **체력**: {self.pet_current_hp} / {pet_base_stats['hp']}\n"
                          f"⚔️ **공격력**: {pet_attack_str}\n"
                          f"🛡️ **방어력**: {pet_defense_str}\n"
                          f"💨 **스피드**: {pet_speed_str}")
        
        embed.add_field(name=f"🐾 {self.pet_data_raw['nickname']}", value=pet_stats_text, inline=False)
        
        if self.pet_is_defeated:
            description_content = "☠️ 펫이 쓰러졌습니다! '아이템'을 사용해 '치료제'로 회복시키거나 던전을 나가야 합니다."
        elif self.state == "exploring":
            description_content = "깊은 곳으로 나아가 몬스터를 찾아보자."
        elif self.state == "in_battle" and self.current_monster:
            turn_indicator = ">>> **💥 당신의 턴입니다! 💥**" if self.is_pet_turn else "⏳ 상대의 턴을 기다리는 중..."
            embed.title = f"전투 중! - {self.current_monster['name']}"
            embed.description = turn_indicator
            embed.set_image(url=self.current_monster['image_url'])

            # --- 몬스터 스탯 표시 로직 (수정됨) ---
            monster_base_stats = self.current_monster
            monster_final_attack = self._get_stat_with_effects(monster_base_stats['attack'], 'ATK', self.monster_effects)
            monster_final_defense = self._get_stat_with_effects(monster_base_stats['defense'], 'DEF', self.monster_effects)
            monster_final_speed = self._get_stat_with_effects(monster_base_stats['speed'], 'SPD', self.monster_effects)
            
            monster_attack_str = f"{monster_final_attack} {get_stat_change_indicator(monster_base_stats['attack'], monster_final_attack)}"
            monster_defense_str = f"{monster_final_defense} {get_stat_change_indicator(monster_base_stats['defense'], monster_final_defense)}"
            monster_speed_str = f"{monster_final_speed} {get_stat_change_indicator(monster_base_stats['speed'], monster_final_speed)}"
            
            monster_stats_text = (f"❤️ **체력**: {self.monster_current_hp} / {monster_base_stats['hp']}\n"
                                f"⚔️ **공격력**: {monster_attack_str}\n"
                                f"🛡️ **방어력**: {monster_defense_str}\n"
                                f"💨 **스피드**: {monster_speed_str}")
                                
            embed.add_field(name=f"몬스터: {self.current_monster['name']}", value=monster_stats_text, inline=False)
            
            if self.battle_log:
                embed.add_field(name="⚔️ 전투 기록", value="\u200b", inline=False)
                for log_entry in self.battle_log[-3:]:
                    if isinstance(log_entry, dict):
                        embed.add_field(name=log_entry['title'], value=log_entry['value'], inline=False)
                    else:
                        embed.add_field(name="\u200b", value=log_entry, inline=False)
        elif self.state == "battle_over":
            embed.title = "전투 종료"
            if self.current_monster and self.current_monster.get('image_url'):
                embed.set_thumbnail(url=self.current_monster['image_url'])
            if self.battle_log:
                embed.add_field(name="⚔️ 전투 결과", value="\u200b", inline=False)
                for log_entry in self.battle_log:
                    if isinstance(log_entry, dict):
                        embed.add_field(name=log_entry['title'], value=log_entry['value'], inline=False)
                    else:
                        embed.add_field(name="\u200b", value=log_entry, inline=False)
        if self.rewards:
            rewards_str = "\n".join([f"> {item}: {qty}개" for item, qty in self.rewards.items()])
            embed.add_field(name="--- 현재까지 획득한 보상 ---", value=rewards_str, inline=False)
        
        if description_content:
            embed.description = description_content

        closing_time_text = f"\n\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n던전은 {discord.utils.format_dt(self.end_time, 'R')}에 닫힙니다."
        if embed.description:
            embed.description += closing_time_text
        else:
            embed.description = closing_time_text.strip()
            
        return embed
    
    def build_components(self):
        self.clear_items()
        base_id = f"dungeon_view:{self.user.id}"
        
        buttons_map = {
            "explore": ui.Button(label="탐색하기", style=discord.ButtonStyle.success, emoji="🗺️", custom_id=f"{base_id}:explore"),
            "use_item": ui.Button(label="아이템", style=discord.ButtonStyle.secondary, emoji="👜", custom_id=f"{base_id}:use_item"),
            # ▼▼▼ [수정] '공격' 버튼을 '스킬' 버튼으로 변경 ▼▼▼
            "skill": ui.Button(label="스킬", style=discord.ButtonStyle.primary, emoji="✨", custom_id=f"{base_id}:skill"),
            "flee": ui.Button(label="도망가기", style=discord.ButtonStyle.danger, emoji="🏃", custom_id=f"{base_id}:flee"),
            "leave": ui.Button(label="던전 나가기", style=discord.ButtonStyle.grey, emoji="🚪", custom_id=f"{base_id}:leave"),
            "explore_disabled": ui.Button(label="탐색 불가", style=discord.ButtonStyle.secondary, emoji="☠️", custom_id=f"{base_id}:explore_disabled", disabled=True)
        }
        
        if self.pet_is_defeated:
            self.add_item(buttons_map["explore_disabled"])
            self.add_item(buttons_map["use_item"])
        elif self.state in ["exploring", "battle_over"]:
            self.add_item(buttons_map["explore"])
            self.add_item(buttons_map["use_item"])
        elif self.state == "in_battle":
            # ▼▼▼ [수정] '공격' 대신 '스킬' 버튼을 추가하고, 펫의 턴일 때만 활성화 ▼▼▼
            buttons_map["skill"].disabled = not self.is_pet_turn
            self.add_item(buttons_map["skill"])
            self.add_item(buttons_map["use_item"])
            self.add_item(buttons_map["flee"])
        
        self.add_item(buttons_map["leave"])
        
        for item in self.children:
            if isinstance(item, ui.Button):
                item.callback = self.dispatch_callback

    # ... interaction_check 메서드는 변경 없음 ...
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("자신의 던전만 조작할 수 있습니다.", ephemeral=True, delete_after=5)
            return False
        return True

    async def dispatch_callback(self, interaction: discord.Interaction):
        try:
            action = interaction.data['custom_id'].split(':')[-1]
        except (KeyError, IndexError):
            return
        method_map = {
            "explore": self.handle_explore, 
            "skill": self.handle_skill_button, # 'attack' -> 'skill'
            "flee": self.handle_flee, 
            "leave": self.handle_leave, 
            "use_item": self.handle_use_item
        }
        if method := method_map.get(action): 
            await method(interaction)

    # ... refresh_ui 메서드는 변경 없음 ...
    async def refresh_ui(self, interaction: Optional[discord.Interaction] = None):
        if interaction and not interaction.response.is_done(): await interaction.response.defer()
        self.build_components(); embed = self.build_embed()
        if self.message:
            try: await self.message.edit(embed=embed, view=self)
            except discord.NotFound: self.stop()

    async def _execute_pet_turn(self, used_skill: Dict):
        skill_power = used_skill.get('power', 0)
        
        # 1. 비공격 기술 처리 (버프, 디버프, 힐 등)
        if skill_power == 0:
            self._apply_skill_effect(used_skill, self.pet_effects, self.monster_effects, self.pet_data_raw['nickname'], self.current_monster['name'], self.final_pet_stats['hp'])
        # 2. 공격 기술 처리
        else:
            # 2a. 효과를 적용한 최종 스탯 계산
            final_attack = self._get_stat_with_effects(self.final_pet_stats['attack'], 'ATK', self.pet_effects)
            final_defense = self._get_stat_with_effects(self.current_monster.get('defense', 0), 'DEF', self.monster_effects)
            
            # 2b. 데미지 계산 및 적용
            damage = max(1, round(final_attack * (skill_power / 100)) - final_defense)
            self.monster_current_hp = max(0, self.monster_current_hp - damage)
            
            self.battle_log.append({
                "title": f"▶️ **{self.pet_data_raw['nickname']}**의 **{used_skill['skill_name']}**!",
                "value": f"> **{self.current_monster['name']}**에게 **{damage}**의 데미지!"
            })
            
            # 2c. 공격 후 발생하는 부가 효과 처리 (흡혈, 반동 데미지 등)
            if used_skill.get('effect_type'):
                self._apply_skill_effect(used_skill, self.pet_effects, self.monster_effects, self.pet_data_raw['nickname'], self.current_monster['name'], damage_dealt=damage)
            
            # 2d. 반동 데미지 처리
            if used_skill.get('effect_type') == 'RECOIL':
                recoil_damage = max(1, round(damage * used_skill.get('effect_value', 0)))
                self.pet_current_hp = max(0, self.pet_current_hp - recoil_damage)
                self.battle_log.append(f"💥 **{self.pet_data_raw['nickname']}**은(는) 반동으로 **{recoil_damage}**의 데미지를 입었다!")

        # 3. 펫의 턴 종료 시 지속 효과 처리
        self._process_turn_end_effects(self.pet_effects, self.pet_data_raw['nickname'], is_pet=True)
        if self.pet_current_hp <= 0: # 지속 데미지로 쓰러질 경우
            self.pet_is_defeated = True
        
    # ▼▼▼ [최종 수정] 아래 _apply_skill_effect 메서드 전체를 교체해주세요 ▼▼▼
    def _apply_skill_effect(self, skill_data: Dict, caster_effects: List[Dict], target_effects: List[Dict], caster_name: str, target_name: str, caster_max_hp: int = 0, damage_dealt: int = 0):
        """스킬의 특수 효과를 적용하고 배틀 로그를 추가합니다."""
        effect_type = skill_data.get('effect_type')
        if not effect_type:
            return

        value = skill_data.get('effect_value', 0)
        duration = skill_data.get('effect_duration', 0)
        
        log_value = ""
        
        # [수정] DEBUFF 조건을 BUFF 조건보다 먼저 확인하도록 순서를 변경하고, 로직을 명확히 합니다.
        # 디버프 (상대에게 적용)
        if 'DEBUFF' in effect_type:
            target_effects.append({'type': effect_type, 'value': value, 'duration': duration})
            stat_name = {"ATK": "공격력", "DEF": "방어력", "SPD": "스피드", "ACC": "명중률"}.get(effect_type.split('_')[0], "능력")
            log_value = f"> **{target_name}**의 **{stat_name}**이(가) 하락했다!"
        
        # 버프 (자신에게 적용)
        elif 'BUFF' in effect_type:
            caster_effects.append({'type': effect_type, 'value': value, 'duration': duration})
            stat_name = {"ATK": "공격력", "DEF": "방어력", "SPD": "스피드", "EVA": "회피율"}.get(effect_type.split('_')[0], "능력")
            log_value = f"> **{caster_name}**의 **{stat_name}**이(가) 상승했다!"
        
        # HP 회복 (자신에게 적용)
        elif effect_type == 'HEAL_PERCENT':
            heal_amount = round(caster_max_hp * value)
            self.pet_current_hp = min(self.final_pet_stats['hp'], self.pet_current_hp + heal_amount)
            log_value = f"> **{caster_name}**이(가) 체력을 **{heal_amount}** 회복했다!"
            
        # 흡혈 (자신에게 적용)
        elif effect_type in ['DRAIN', 'LEECH']:
            # ▼▼▼ [수정] 아래 value를 직접 0.5로 설정합니다. ▼▼▼
            # drain_amount = round(damage_dealt * value) 
            drain_amount = round(damage_dealt * 0.5) # '영혼 흡수'는 무조건 50% 회복
            # ▲▲▲ [수정] 완료 ▲▲▲
            self.pet_current_hp = min(self.final_pet_stats['hp'], self.pet_current_hp + drain_amount)
            log_value = f"> **{target_name}**에게서 체력을 **{drain_amount}** 흡수했다!"
        
        # 지속 데미지 (상대에게 적용)
        elif effect_type == 'BURN':
            target_effects.append({'type': effect_type, 'value': value, 'duration': duration})
            log_value = f"> **{target_name}**은(는) 화상을 입었다!"

        if log_value:
            self.battle_log.append({"title": f"✨ 스킬 효과: {skill_data['skill_name']}", "value": log_value})

def _get_stat_with_effects(self, base_stat: int, stat_key: str, effects: List[Dict]) -> int:
    """버프/디버프 효과가 적용된 최종 스탯을 계산합니다."""
    multiplier = 1.0
    for effect in effects:
        # [수정] stat_key('ATK')가 effect['type']('ATK_DEBUFF') 안에 포함되는지 확인합니다.
        if stat_key in effect['type']:
            if 'BUFF' in effect['type']:
                multiplier += effect['value']
            elif 'DEBUFF' in effect['type']:
                multiplier -= effect['value']
    return max(1, round(base_stat * multiplier))

    # ▼▼▼ [수정] _process_turn_end_effects 메서드를 아래 내용으로 교체합니다. ▼▼▼
    def _process_turn_end_effects(self, effects: List[Dict], target_name: str, is_pet: bool):
        """턴 종료 시 지속 효과(데미지, 지속시간 감소)를 처리합니다."""
        
        # [수정] 효과 타입(영어)을 한글 이름으로 변환하기 위한 딕셔너리
        effect_name_map = {
            'BURN': '화상',
            'ATK_BUFF': '공격력 증가',
            'DEF_BUFF': '방어력 증가',
            'SPD_BUFF': '스피드 증가',
            'EVA_BUFF': '회피율 증가',
            'ATK_DEBUFF': '공격력 감소',
            'DEF_DEBUFF': '방어력 감소',
            'SPD_DEBUFF': '스피드 감소',
            'ACC_DEBUFF': '명중률 감소'
        }

        effects_to_remove = []
        for effect in effects:
            # 지속 데미지 효과
            if effect['type'] == 'BURN':
                dot_damage = max(1, round(effect['value']))
                if is_pet:
                    self.pet_current_hp = max(0, self.pet_current_hp - dot_damage)
                else:
                    self.monster_current_hp = max(0, self.monster_current_hp - dot_damage)
                self.battle_log.append(f"🔥 **{target_name}**은(는) 화상 데미지로 **{dot_damage}**의 피해를 입었다!")

            # 지속시간 감소 및 만료 처리
            effect['duration'] -= 1
            if effect['duration'] <= 0:
                effects_to_remove.append(effect)
                # [수정] effect_name_map을 사용하여 한글 이름으로 출력
                effect_name = effect_name_map.get(effect['type'], effect['type']) # 맵에 없는 경우 원본 키 출력
                self.battle_log.append(f"💨 **{target_name}**에게 걸려있던 **{effect_name}** 효과가 사라졌다.")
        
        for expired_effect in effects_to_remove:
            effects.remove(expired_effect)
    
    async def _execute_monster_turn(self):
        # 1. 효과를 적용한 최종 스탯 계산
        final_attack = self._get_stat_with_effects(self.current_monster.get('attack', 1), 'ATK', self.monster_effects)
        final_defense = self._get_stat_with_effects(self.final_pet_stats['defense'], 'DEF', self.pet_effects)

        # 2. 데미지 계산 및 적용
        damage = max(1, final_attack - final_defense)
        self.pet_current_hp = max(0, self.pet_current_hp - damage)
        
        await supabase.table('pets').update({'current_hp': self.pet_current_hp}).eq('id', self.pet_data_raw['id']).execute()
        
        self.battle_log.append({
            "title": f"◀️ **{self.current_monster['name']}**의 공격!",
            "value": f"> **{self.pet_data_raw['nickname']}**에게 **{damage}**의 데미지!"
        })

        # 3. 몬스터의 턴 종료 시 지속 효과 처리
        self._process_turn_end_effects(self.monster_effects, self.current_monster['name'], is_pet=False)
        
    async def handle_explore(self, interaction: discord.Interaction):
        if self.pet_is_defeated: return await interaction.response.send_message("펫이 쓰러져서 탐색할 수 없습니다.", ephemeral=True, delete_after=5)
        
        self.current_monster = self.generate_monster()
        self.monster_current_hp = self.current_monster['hp']
        self.battle_log = [f"**{self.current_monster['name']}** 이(가) 나타났다!"]
        
        self.pet_effects.clear()
        self.monster_effects.clear()
        
        if self.final_pet_stats['speed'] >= self.current_monster.get('speed', 0):
            self.is_pet_turn = True
            self.battle_log.append(f"**{self.pet_data_raw['nickname']}**이(가) 민첩하게 먼저 움직인다!")
        else:
            self.is_pet_turn = False
            self.battle_log.append(f"**{self.current_monster['name']}**이(가) 더 빠르다! 먼저 공격할 것이다.")
            
        self.state = "in_battle"
        
        # [추가] 상태 변경 시 DB에 현재 상태와 몬스터 정보를 저장합니다.
        await supabase.table('dungeon_sessions').update({
            'state': self.state,
            'current_monster_json': {'data': self.current_monster, 'hp': self.monster_current_hp}
        }).eq('id', self.session_id).execute()
        
        if not self.is_pet_turn:
            await self.handle_monster_turn(interaction) 
        else:
            await self.refresh_ui(interaction)

# handle_battle_win 함수를 찾아 DB 업데이트 로직을 추가합니다.
    async def handle_battle_win(self):
        self.state = "battle_over"
        # [추가] 상태 변경 시 DB 업데이트
        await supabase.table('dungeon_sessions').update({'state': self.state, 'current_monster_json': None}).eq('id', self.session_id).execute()
        
        self.battle_log.append({
            "title": f"🎉 **{self.current_monster['name']}**을(를) 물리쳤다!",
            "value": "> 전투에서 승리했습니다."
        })
        # ... (이하 기존 코드 동일) ...
        pet_exp_gain = self.current_monster['xp']
        self.total_pet_xp_gained += pet_exp_gain
        await supabase.rpc('add_xp_to_pet', {'p_user_id': self.user.id, 'p_xp_to_add': pet_exp_gain}).execute()
        self.battle_log.append({
            "title": "✨ 경험치 획득",
            "value": f"> 펫이 **{pet_exp_gain} XP**를 획득했다!"
        })
        for item, (chance, min_qty, max_qty) in self.cog.loot_table.get(self.dungeon_tier, {}).items():
            if random.random() < chance:
                qty = random.randint(min_qty, max_qty)
                self.rewards[item] += qty
                self.battle_log.append({
                    "title": "🎁 전리품 획득",
                    "value": f"> **{item}** {qty}개를 획득했다!"
                })
        await self.refresh_ui()

# handle_battle_lose 함수를 찾아 DB 업데이트 로직을 추가합니다.
    async def handle_battle_lose(self):
        self.state = "battle_over"
        self.pet_is_defeated = True
        
        # [추가] 상태 변경 시 DB 업데이트
        await supabase.table('dungeon_sessions').update({'state': self.state, 'current_monster_json': None}).eq('id', self.session_id).execute()
        
        self.battle_log.append({
            "title": f"☠️ **{self.pet_data_raw['nickname']}**이(가) 쓰러졌다...",
            "value": "> 전투에서 패배했습니다."
        })
        self.current_monster = None
        await self.refresh_ui()

# handle_flee 함수를 찾아 DB 업데이트 로직을 추가합니다.
    async def handle_flee(self, interaction: discord.Interaction):
        self.state = "exploring"
        self.current_monster = None
        
        # [추가] 상태 변경 시 DB 업데이트
        await supabase.table('dungeon_sessions').update({'state': self.state, 'current_monster_json': None}).eq('id', self.session_id).execute()
        
        self.battle_log = ["무사히 도망쳤다..."]
        await self.refresh_ui(interaction)

# reload_active_dungeon_views 함수를 찾아 state와 monster 정보를 불러오도록 수정합니다.
    async def reload_active_dungeon_views(self):
        logger.info("[Dungeon] 활성화된 던전 게임 UI를 다시 로드합니다...")
        try:
            # [수정] state와 current_monster_json 컬럼도 함께 불러옵니다.
            res = await supabase.table('dungeon_sessions').select('*, pets(*, pet_species(*), learned_skills:pet_learned_skills(*, pet_skills(*)))').not_.is_('message_id', 'null').execute()
            
            if not res.data:
                logger.info("[Dungeon] 다시 로드할 활성 던전 UI가 없습니다.")
                return

            reloaded_count = 0
            for session_data in res.data:
                try:
                    user_id, message_id = int(session_data['user_id']), int(session_data['message_id'])
                    pet_data, dungeon_tier = session_data.get('pets'), session_data['dungeon_tier']
                    end_time, session_id = datetime.fromisoformat(session_data['end_time']), session_data['id']
                    
                    # [추가] state와 monster 정보를 세션 데이터에서 가져옵니다.
                    current_state = session_data.get('state', 'exploring')
                    monster_data = session_data.get('current_monster_json')

                    if not pet_data:
                        logger.warning(f"던전 세션(ID:{session_id})에 연결된 펫 정보가 없어 UI를 로드할 수 없습니다.")
                        continue
                    
                    user = self.bot.get_user(user_id)
                    if not user:
                        logger.warning(f"던전 UI 로드 중 유저(ID:{user_id})를 찾을 수 없습니다.")
                        continue
                    
                    # [수정] DungeonGameView 생성자에 state와 monster 데이터를 전달합니다.
                    view = DungeonGameView(self, user, pet_data, dungeon_tier, end_time, session_id, current_state=current_state, monster_data=monster_data)
                    
                    try:
                        if thread_id := session_data.get('thread_id'):
                            if thread := self.bot.get_channel(int(thread_id)):
                                view.message = await thread.fetch_message(message_id)
                    except (discord.NotFound, discord.Forbidden):
                        logger.warning(f"던전 UI 재로드 중 메시지(ID: {message_id})를 찾을 수 없어 해당 세션을 건너뜁니다.")
                        continue
                    
                    self.bot.add_view(view, message_id=message_id)
                    self.active_sessions[user_id] = view
                    reloaded_count += 1
                except Exception as e:
                    logger.error(f"던전 세션(ID: {session_data.get('id')}) UI 재로드 중 오류 발생: {e}", exc_info=True)

            logger.info(f"[Dungeon] 총 {reloaded_count}개의 던전 게임 UI를 성공적으로 다시 로드했습니다.")
        except Exception as e:
            logger.error(f"활성 던전 UI 로드 중 오류 발생: {e}", exc_info=True)

    @tasks.loop(minutes=5)
    async def check_expired_dungeons(self):
        now = datetime.now(timezone.utc)
        res = await supabase.table('dungeon_sessions').select('*').lte('end_time', now.isoformat()).execute()
        if res and res.data:
            for session in res.data:
                user_id = int(session['user_id'])
                if user_id not in self.active_sessions:
                    logger.warning(f"DB에서 방치된 던전 세션(유저: {user_id})을 발견하여 종료합니다.")
                    await self.close_dungeon_session(user_id, json.loads(session.get('rewards_json', '{}')))
    
    @check_expired_dungeons.before_loop
    async def before_check_expired_dungeons(self): await self.bot.wait_until_ready()
    
    async def handle_enter_dungeon(self, interaction: discord.Interaction, tier: str):
        user = interaction.user
        res = await supabase.table('dungeon_sessions').select('thread_id').eq('user_id', str(user.id)).maybe_single().execute()
        if res and res.data and (thread := self.bot.get_channel(int(res.data['thread_id']))):
            return await interaction.followup.send(f"❌ 이미 던전에 입장해 있습니다. {thread.mention}", ephemeral=True)
        
        pet_data = await get_user_pet(user.id) # [수정] 이 함수는 이제 스킬 정보도 함께 가져옵니다.
        if not pet_data: return await interaction.followup.send("❌ 던전에 입장하려면 펫이 필요합니다.", ephemeral=True)
        
        dungeon_name = self.dungeon_data[tier]['name']; ticket_name = f"{dungeon_name} 입장권"
        if (await get_inventory(user)).get(ticket_name, 0) < 1: return await interaction.followup.send(f"❌ '{ticket_name}'이 부족합니다.", ephemeral=True)
        
        try:
            thread = await interaction.channel.create_thread(name=f"🛡️｜{user.display_name}의 {dungeon_name}", type=discord.ChannelType.private_thread, auto_archive_duration=1440)
        except Exception: return await interaction.followup.send("❌ 던전을 여는 데 실패했습니다.", ephemeral=True)
        
        await update_inventory(user.id, ticket_name, -1); await thread.add_user(user)
        end_time = datetime.now(timezone.utc) + timedelta(hours=24)
        
        session_res = await supabase.table('dungeon_sessions').upsert({
            "user_id": str(user.id), "thread_id": str(thread.id), "end_time": end_time.isoformat(), 
            "pet_id": pet_data['id'], "dungeon_tier": tier, "rewards_json": "{}"
        }, on_conflict="user_id").execute()
        
        if not (session_res and session_res.data):
            logger.error(f"던전 세션 생성/업데이트 실패 (User: {user.id})")
            return await interaction.followup.send("❌ 던전 입장에 실패했습니다. (DB 오류)", ephemeral=True)
            
        session_id = session_res.data[0]['id']
        view = DungeonGameView(self, user, pet_data, tier, end_time, session_id)
        
        self.active_sessions[user.id] = view
        await interaction.followup.send(f"던전에 입장했습니다! {thread.mention}", ephemeral=True)
        await view.start(thread)

    async def close_dungeon_session(self, user_id: int, rewards: Dict, total_xp: int = 0, thread: Optional[discord.TextChannel] = None):
        if user_id in self.active_sessions:
            view = self.active_sessions.pop(user_id, None)
            if view and not view.is_finished():
                if total_xp == 0: total_xp = view.total_pet_xp_gained
                view.stop()
        session_res = await supabase.table('dungeon_sessions').select('*').eq('user_id', str(user_id)).maybe_single().execute()
        if not (session_res and session_res.data): return
        session_data = session_res.data
        await supabase.table('dungeon_sessions').delete().eq('user_id', str(user_id)).execute()
        user = self.bot.get_user(user_id)
        panel_channel = self.bot.get_channel(get_id("dungeon_panel_channel_id")) if get_id("dungeon_panel_channel_id") else None
        
        if user and (rewards or total_xp > 0):
            if rewards:
                await asyncio.gather(*[update_inventory(user.id, item, qty) for item, qty in rewards.items()])
            rewards_text = "\n".join([f"> {item}: {qty}개" for item, qty in rewards.items()]) or "> 획득한 아이템이 없습니다."
            embed_data = await get_embed_from_db("log_dungeon_result")
            log_embed = format_embed_from_db(embed_data, user_mention=user.mention, dungeon_name=self.dungeon_data[session_data['dungeon_tier']]['name'], rewards_list=rewards_text, pet_xp_gained=f"{total_xp:,}")
            if user.display_avatar: log_embed.set_thumbnail(url=user.display_avatar.url)
            if panel_channel: await panel_channel.send(embed=log_embed)
        
        try:
            if not thread: thread = self.bot.get_channel(int(session_data['thread_id'])) or await self.bot.fetch_channel(int(session_data['thread_id']))
            await thread.send("**던전이 닫혔습니다. 이 채널은 5초 후에 삭제됩니다.**", delete_after=5)
            await asyncio.sleep(5); await thread.delete()
        except (discord.NotFound, discord.Forbidden): pass
        
        if panel_channel: await self.regenerate_panel(panel_channel)
    
    async def regenerate_panel(self, channel: discord.TextChannel, panel_key: str = "panel_dungeon"):
        panel_name = panel_key.replace("panel_", "")
        if panel_info := get_panel_id(panel_name):
            if msg_id := panel_info.get('message_id'):
                try:
                    old_message = await channel.fetch_message(msg_id)
                    await old_message.delete()
                except (discord.NotFound, discord.Forbidden): pass
        if embed_data := await get_embed_from_db(panel_key):
            dungeon_levels = {f"{tier}_rec_level": f"Lv.{data.get('recommended_level', '?')}" for tier, data in self.dungeon_data.items()}
            embed = format_embed_from_db(embed_data, **dungeon_levels)
            view = await DungeonPanelView.create(self)
            new_message = await channel.send(embed=embed, view=view)
            await save_panel_id(panel_name, new_message.id, channel.id)
            
    async def skill_autocomplete(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        res = await supabase.table('pet_skills').select('skill_name').ilike('skill_name', f'%{current}%').limit(25).execute()
        if not (res and res.data):
            return []
        return [app_commands.Choice(name=row['skill_name'], value=row['skill_name']) for row in res.data]

    @app_commands.command(name="던전테스트", description="[관리자] 던전 전투 시스템을 테스트합니다.")
    @app_commands.describe(
        action="실행할 작업을 선택하세요.",
        skill_name="[스킬부여] 부여할 스킬의 이름입니다.",
        slot="[스킬부여] 스킬을 부여할 슬롯 번호입니다.",
        element="[몬스터소환] 소환할 몬스터의 속성입니다.",
        level="[몬스터소환] 소환할 몬스터의 레벨입니다."
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="스킬 부여", value="스킬부여"),
        app_commands.Choice(name="몬스터 소환", value="몬스터소환"),
        app_commands.Choice(name="현재 효과 확인", value="효과확인"),
    ])
    @app_commands.autocomplete(skill_name=skill_autocomplete)
    async def dungeon_test(self, interaction: discord.Interaction, action: str, 
                         skill_name: Optional[str] = None, 
                         slot: Optional[app_commands.Range[int, 1, 4]] = None, 
                         element: Optional[str] = None, 
                         level: Optional[int] = None):
        """던전 전투 시스템을 테스트하기 위한 관리자 명령어입니다."""
        if not await self.bot.is_owner(interaction.user):
            return await interaction.response.send_message("❌ 봇 소유자만 사용할 수 있는 명령어입니다.", ephemeral=True)

        if interaction.user.id not in self.active_sessions:
            return await interaction.response.send_message("❌ 먼저 던전에 입장해주세요.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        view = self.active_sessions[interaction.user.id]

        if action == "스킬부여":
            if not skill_name or not slot:
                return await interaction.followup.send("❌ '스킬 부여'를 선택한 경우, `skill_name`과 `slot` 옵션을 모두 입력해야 합니다.")

            res = await supabase.table('pet_skills').select('*').eq('skill_name', skill_name).maybe_single().execute()
            if not (res and res.data):
                return await interaction.followup.send(f"❌ '{skill_name}' 스킬을 찾을 수 없습니다.")
            
            skill_data = res.data
            pet_id = view.pet_data_raw['id']

            await supabase.table('pet_learned_skills').upsert({
                'pet_id': pet_id, 'skill_id': skill_data['id'], 'slot_number': slot
            }, on_conflict='pet_id, slot_number').execute()

            view.pet_data_raw['learned_skills'] = [s for s in view.pet_data_raw.get('learned_skills', []) if s['slot_number'] != slot]
            view.pet_data_raw['learned_skills'].append({'slot_number': slot, 'pet_skills': skill_data})
            
            await interaction.followup.send(f"✅ 펫에게 **{skill_name}** 스킬을 {slot}번 슬롯에 임시로 부여했습니다.")

        elif action == "몬스터소환":
            if not element or not level:
                return await interaction.followup.send("❌ '몬스터 소환'을 선택한 경우, `element`와 `level` 옵션을 모두 입력해야 합니다.")

            if element not in self.monster_base_data:
                return await interaction.followup.send(f"❌ 유효하지 않은 속성입니다. ({', '.join(self.monster_base_data.keys())})")

            # ... (몬스터 생성 로직은 동일) ...
            base_monster = self.monster_base_data[element]
            dungeon_info = self.dungeon_data[view.dungeon_tier]
            hp_bonus = (level - 1) * 8; other_stat_bonus = (level - 1) * 5
            hp = int(base_monster['base_hp'] * dungeon_info['hp_mult']) + hp_bonus
            attack = int(base_monster['base_attack'] * dungeon_info['atk_mult']) + other_stat_bonus
            defense = int(base_monster['base_defense'] * dungeon_info['def_mult']) + other_stat_bonus
            speed = int(base_monster['base_speed'] * dungeon_info['spd_mult']) + other_stat_bonus
            xp = max(1, int(hp * dungeon_info['xp_mult']) // 20) + (level * 2)
            image_url = f"{view.storage_base_url}/{element}_{dungeon_info['image_suffix']}.png"
            
            view.current_monster = {"name": f"Lv.{level} 테스트 {base_monster['name']}", "hp": hp, "attack": attack, "defense": defense, "speed": speed, "xp": xp, "element": element, "image_url": image_url}
            view.monster_current_hp = hp
            view.state = "in_battle"
            view.is_pet_turn = True
            view.battle_log = [f"테스트 몬스터 **{view.current_monster['name']}** 이(가) 나타났다!"]
            await view.refresh_ui()
            await interaction.followup.send(f"✅ **{view.current_monster['name']}**을(를) 강제로 소환했습니다.")

        elif action == "효과확인":
            if view.state != "in_battle":
                return await interaction.followup.send("❌ 전투 중에만 사용할 수 없습니다.")
            
            embed = discord.Embed(title="🕵️ 현재 효과 상태 (디버그)", color=0xFFD700)
            pet_effects_str = "\n".join([f"`{e}`" for e in view.pet_effects]) or "없음"
            monster_effects_str = "\n".join([f"`{e}`" for e in view.monster_effects]) or "없음"
            embed.add_field(name="펫 효과", value=pet_effects_str, inline=False)
            embed.add_field(name="몬스터 효과", value=monster_effects_str, inline=False)
            await interaction.followup.send(embed=embed)

        else:
            await interaction.followup.send("❌ 알 수 없는 작업입니다.")

class DungeonPanelView(ui.View):
    def __init__(self, cog_instance: 'Dungeon'):
        super().__init__(timeout=None)
        self.cog = cog_instance

    @classmethod
    async def create(cls, cog_instance: 'Dungeon'):
        view = cls(cog_instance)
        await view._add_buttons()
        return view

    async def _add_buttons(self):
        while not self.cog.dungeon_data: await asyncio.sleep(0.1)
        for tier, data in self.cog.dungeon_data.items():
            button = ui.Button(label=data['name'], style=discord.ButtonStyle.secondary, custom_id=f"enter_dungeon_{tier}")
            button.callback = self.dispatch_callback
            self.add_item(button)

    async def dispatch_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        tier = interaction.data['custom_id'].split('_')[-1]
        await self.cog.handle_enter_dungeon(interaction, tier)

async def setup(bot: commands.Bot):
    await bot.add_cog(Dungeon(bot))
