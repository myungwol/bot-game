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

from utils.database import (
    get_inventory, update_inventory, supabase, get_id,
    save_panel_id, get_panel_id, get_embed_from_db,
    get_item_database, get_user_pet
)
from utils.helpers import format_embed_from_db
from utils.combat_system import process_turn, Combatant

logger = logging.getLogger(__name__)

# ... (파일 상단의 pad_korean_string, load_dungeon_data_from_db, SkillSelectView 클래스는 변경 없음) ...
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
    def __init__(self, main_view: 'DungeonGameView', learned_skills: List[Dict], current_energy: int):
        super().__init__(timeout=60)
        self.main_view = main_view
        self.learned_skills = learned_skills
        self.current_energy = current_energy
        self._build_components()

    def _build_components(self):
        if not self.learned_skills:
            self.add_item(ui.Button(label="배운 스킬이 없습니다!", disabled=True))
            return
        
        options = []
        for s in self.learned_skills:
            skill = s['pet_skills']
            cost = skill.get('cost', 0)
            power = skill.get('power', 0)
            description = skill.get('description', '설명 없음')
            
            is_disabled_by_energy = self.current_energy < cost

            # [핵심 수정] description에 더 많은 정보를 담도록 변경
            # 디스코드 description 최대 길이에 맞춰 설명을 자릅니다.
            truncated_desc = (description[:50] + '...') if len(description) > 50 else description
            
            if is_disabled_by_energy:
                option_description = f"기력이 부족합니다! (현재:{self.current_energy})"
            else:
                option_description = f"위력: {power} | {truncated_desc}"

            options.append(discord.SelectOption(
                label=f"{skill['skill_name']} (코스트: {cost})",
                value=str(skill['id']),
                description=option_description
            ))

        skill_select = ui.Select(placeholder="사용할 스킬을 선택하세요...", options=options)
        skill_select.callback = self.on_skill_select
        self.add_item(skill_select)

    async def on_skill_select(self, interaction: discord.Interaction):
        skill_id = int(interaction.data['values'][0])
        skill_data = next((s['pet_skills'] for s in self.learned_skills if s['pet_skills']['id'] == skill_id), None)
        if skill_data:
            await self.main_view.handle_skill_use(skill_data, interaction)
        self.stop()

class DungeonGameView(ui.View):
    def __init__(self, cog: 'Dungeon', user: discord.Member, pet_data: Dict, dungeon_tier: str, end_time: datetime, session_id: int, current_state: str = "exploring", monster_data: Optional[Dict] = None):
        super().__init__(timeout=None)
        self.cog = cog; self.user = user; self.pet_data_raw = pet_data; self.dungeon_tier = dungeon_tier; self.end_time = end_time
        self.session_id = session_id
        self.final_pet_stats = self._calculate_final_pet_stats()
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
        self.current_monster: Optional[Dict] = monster_data.get('data') if monster_data else None
        self.monster_current_hp: int = monster_data.get('hp', 0) if monster_data else 0
        self.storage_base_url = f"{os.environ.get('SUPABASE_URL')}/storage/v1/object/public/monster_images"
        
        self.pet_current_energy: int = 100
        self.pet_max_energy: int = 100
        
        self.build_components()

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
        embed = await self.build_embed()
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

    async def build_embed(self) -> discord.Embed:
        dungeon_info = self.cog.dungeon_data[self.dungeon_tier]
        embed = discord.Embed(title=f"탐험 중... - {dungeon_info['name']}", color=0x71368A)
        description_content = ""
        
        pet_base_stats = self.final_pet_stats
        pet_stats_text = (f"❤️ **체력**: {self.pet_current_hp} / {pet_base_stats['hp']}\n"
                          f"⚡ **기력**: {self.pet_current_energy} / {self.pet_max_energy}\n"
                          f"⚔️ **공격력**: {pet_base_stats['attack']}\n"
                          f"🛡️ **방어력**: {pet_base_stats['defense']}\n"
                          f"💨 **스피드**: {pet_base_stats['speed']}")
        embed.add_field(name=f"🐾 {self.pet_data_raw['nickname']}", value=pet_stats_text, inline=False)
        
        if self.pet_is_defeated:
            description_content = "☠️ 펫이 쓰러졌습니다! '아이템'을 사용해 '치료제'로 회복시키거나 던전을 나가야 합니다."
        elif self.state == "exploring":
            description_content = "깊은 곳으로 나아가 몬스터를 찾아보자."
        # ▼▼▼ [핵심 추가] 이 부분을 추가합니다. ▼▼▼
        elif self.state == "encounter" and self.current_monster:
            embed.title = f"몬스터 조우! - {self.current_monster['name']}"
            embed.set_image(url=self.current_monster['image_url'])
            
            # 속도 비교 결과에 따른 메시지
            if self.is_pet_turn:
                description_content = f"**{self.pet_data_raw['nickname']}**이(가) 민첩하게 먼저 움직일 수 있습니다!\n어떻게 하시겠습니까?"
            else:
                description_content = f"**{self.current_monster['name']}**이(가) 더 빠릅니다! 전투를 시작하면 선공을 당하게 됩니다!\n어떻게 하시겠습니까?"
            
            monster_base_stats = self.current_monster
            monster_stats_text = (f"❤️ **체력**: {self.monster_current_hp} / {monster_base_stats['hp']}\n"
                                f"⚔️ **공격력**: {monster_base_stats['attack']}\n"
                                f"🛡️ **방어력**: {monster_base_stats['defense']}\n"
                                f"💨 **스피드**: {monster_base_stats['speed']}")
            embed.add_field(name=f"몬스터: {self.current_monster['name']}", value=monster_stats_text, inline=False)
        # ▲▲▲ [핵심 추가] 완료 ▲▲▲
        elif self.state == "in_battle" and self.current_monster:
            turn_indicator = ">>> **💥 당신의 턴입니다! 💥**" if self.is_pet_turn else "⏳ 상대의 턴을 기다리는 중..."
            embed.title = f"전투 중! - {self.current_monster['name']}"; embed.description = turn_indicator
            embed.set_image(url=self.current_monster['image_url'])
            monster_base_stats = self.current_monster
            monster_stats_text = (f"❤️ **체력**: {self.monster_current_hp} / {monster_base_stats['hp']}\n"
                                f"⚔️ **공격력**: {monster_base_stats['attack']}\n"
                                f"🛡️ **방어력**: {monster_base_stats['defense']}\n"
                                f"💨 **스피드**: {monster_base_stats['speed']}")
            embed.add_field(name=f"몬스터: {self.current_monster['name']}", value=monster_stats_text, inline=False)
            if self.battle_log:
                embed.add_field(name="⚔️ 전투 기록", value="\u200b", inline=False)
                for log_entry in self.battle_log[-3:]:
                    if isinstance(log_entry, dict): embed.add_field(name=log_entry['title'], value=log_entry['value'], inline=False)
                    else: embed.add_field(name="\u200b", value=str(log_entry), inline=False)
        elif self.state == "battle_over":
            embed.title = "전투 종료"
            if self.current_monster and self.current_monster.get('image_url'): embed.set_thumbnail(url=self.current_monster['image_url'])
            if self.battle_log:
                embed.add_field(name="⚔️ 전투 결과", value="\u200b", inline=False)
                for log_entry in self.battle_log:
                    if isinstance(log_entry, dict): embed.add_field(name=log_entry['title'], value=log_entry['value'], inline=False)
                    else: embed.add_field(name="\u200b", value=str(log_entry), inline=False)

        if self.rewards:
            rewards_str = "\n".join([f"> {item}: {qty}개" for item, qty in self.rewards.items()])
            embed.add_field(name="--- 현재까지 획득한 보상 ---", value=rewards_str, inline=False)
        if description_content: embed.description = description_content
        closing_time_text = f"\n\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n던전은 {discord.utils.format_dt(self.end_time, 'R')}에 닫힙니다."
        if embed.description: embed.description += closing_time_text
        else: embed.description = closing_time_text.strip()
        return embed
    
    def build_components(self):
        self.clear_items(); base_id = f"dungeon_view:{self.user.id}"
        buttons_map = { "explore": ui.Button(label="탐색하기", style=discord.ButtonStyle.success, emoji="🗺️", custom_id=f"{base_id}:explore"), "use_item": ui.Button(label="아이템", style=discord.ButtonStyle.secondary, emoji="👜", custom_id=f"{base_id}:use_item"), "skill": ui.Button(label="스킬", style=discord.ButtonStyle.primary, emoji="✨", custom_id=f"{base_id}:skill"), "flee": ui.Button(label="도망가기", style=discord.ButtonStyle.danger, emoji="🏃", custom_id=f"{base_id}:flee"), "leave": ui.Button(label="던전 나가기", style=discord.ButtonStyle.grey, emoji="🚪", custom_id=f"{base_id}:leave"), "explore_disabled": ui.Button(label="탐색 불가", style=discord.ButtonStyle.secondary, emoji="☠️", custom_id=f"{base_id}:explore_disabled", disabled=True)}
        if self.pet_is_defeated:
            self.add_item(buttons_map["explore_disabled"])
            self.add_item(buttons_map["use_item"])
        elif self.state in ["exploring", "battle_over"]:
            self.add_item(buttons_map["explore"])
            self.add_item(buttons_map["use_item"])
        # ▼▼▼ [핵심 추가] 이 부분을 추가합니다. ▼▼▼
        elif self.state == "encounter":
            start_battle_button = ui.Button(label="전투 시작", style=discord.ButtonStyle.danger, emoji="⚔️", custom_id=f"{base_id}:start_battle")
            self.add_item(start_battle_button)
            self.add_item(buttons_map["flee"]) # 기존 도망가기 버튼 재사용
        # ▲▲▲ [핵심 추가] 완료 ▲▲▲
        elif self.state == "in_battle":
            buttons_map["skill"].disabled = not self.is_pet_turn
            self.add_item(buttons_map["skill"])
            self.add_item(buttons_map["use_item"])
            self.add_item(buttons_map["flee"])
        self.add_item(buttons_map["leave"])
        for item in self.children:
            if isinstance(item, ui.Button): item.callback = self.dispatch_callback

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("자신의 던전만 조작할 수 있습니다.", ephemeral=True, delete_after=5)
            return False
        return True

    async def dispatch_callback(self, interaction: discord.Interaction):
        try: action = interaction.data['custom_id'].split(':')[-1]
        except (KeyError, IndexError): return
        method_map = { 
            "explore": self.handle_explore, 
            "start_battle": self.handle_start_battle, # <--- 이 줄을 추가
            "skill": self.handle_skill_button, 
            "flee": self.handle_flee, 
            "leave": self.handle_leave, 
            "use_item": self.handle_use_item
        }
        if method := method_map.get(action): await method(interaction)

    async def refresh_ui(self, interaction: Optional[discord.Interaction] = None):
        if interaction and not interaction.response.is_done(): await interaction.response.defer()
        self.build_components()
        embed = await self.build_embed()
        if self.message:
            try: await self.message.edit(embed=embed, view=self)
            except discord.NotFound: self.stop()

    async def _process_battle_turn(self, skill_data: Dict):
        if self.is_pet_turn:
            self.pet_current_energy = min(self.pet_max_energy, self.pet_current_energy + 10)

        
        pet_combatant = Combatant(
            name=self.pet_data_raw['nickname'], stats=self.final_pet_stats,
            current_hp=self.pet_current_hp, max_hp=self.final_pet_stats['hp'], effects=self.pet_effects,
            current_energy=self.pet_current_energy, max_energy=self.pet_max_energy
        )
        monster_combatant = Combatant(
            name=self.current_monster['name'], stats=self.current_monster,
            current_hp=self.monster_current_hp, max_hp=self.current_monster['hp'], effects=self.monster_effects
        )
        
        updated_pet, updated_monster, pet_turn_logs = process_turn(pet_combatant, monster_combatant, skill_data)
        
        self.pet_current_hp = updated_pet['current_hp']
        self.pet_effects = updated_pet['effects']
        self.monster_current_hp = updated_monster['current_hp']
        self.monster_effects = updated_monster['effects']
        self.battle_log.extend(pet_turn_logs)
        
        # ▼▼▼ [핵심 수정] 이 한 줄을 추가합니다. ▼▼▼
        if 'current_energy' in updated_pet:
            self.pet_current_energy = updated_pet['current_energy']
        # ▲▲▲ [핵심 수정] 완료 ▲▲▲
        
        if self.pet_current_hp <= 0: self.pet_is_defeated = True

        if self.monster_current_hp <= 0 and self.pet_is_defeated:
             return await self.handle_battle_draw()
        elif self.monster_current_hp <= 0:
            return await self.handle_battle_win()
        
        # ▼▼▼ [핵심 수정] 속도 비교 조건문을 삭제하고, 몬스터의 턴을 항상 실행하도록 변경합니다. ▼▼▼
        await self.refresh_ui()
        await asyncio.sleep(2)
        await self._execute_monster_turn()
        # ▲▲▲ [핵심 수정] 완료 ▲▲▲

        if self.pet_current_hp <= 0 and self.monster_current_hp <= 0:
            return await self.handle_battle_draw()
        elif self.pet_current_hp <= 0:
            return await self.handle_battle_lose()

        self.is_pet_turn = True
        self.pet_current_energy = min(self.pet_max_energy, self.pet_current_energy + 10)
        await self.refresh_ui()

    async def handle_skill_use(self, skill_data: Dict, skill_interaction: discord.Interaction):
        try:
            if not skill_interaction.response.is_done(): await skill_interaction.response.defer()
        except discord.NotFound: logger.warning(f"handle_skill_use 진입 시 interaction(ID:{skill_interaction.id})을 찾을 수 없습니다."); return
        if self.state != "in_battle" or not self.current_monster or not self.is_pet_turn: return
        try: await skill_interaction.delete_original_response()
        except (discord.NotFound, discord.HTTPException): logger.warning(f"SkillSelectView 메시지 삭제 시도 중 찾지 못함 (User: {self.user.id})")
        
        self.is_pet_turn = False; self.battle_log = []
        await self.refresh_ui()
        asyncio.create_task(self._process_battle_turn(skill_data))
        
    async def _execute_monster_turn(self):
        pet_combatant = Combatant(
            name=self.pet_data_raw['nickname'], stats=self.final_pet_stats,
            current_hp=self.pet_current_hp, max_hp=self.final_pet_stats['hp'], effects=self.pet_effects,
            current_energy=self.pet_current_energy, max_energy=self.pet_max_energy # <--- 추가
        )
        monster_combatant = Combatant(
            name=self.current_monster['name'], stats=self.current_monster,
            current_hp=self.monster_current_hp, max_hp=self.current_monster['hp'], effects=self.monster_effects
        )
        basic_attack_skill = {"skill_name": "공격", "power": 100, "cost": 0} 

        updated_monster, updated_pet, monster_turn_logs = process_turn(monster_combatant, pet_combatant, basic_attack_skill)
        
        self.pet_current_hp = updated_pet['current_hp']; self.pet_effects = updated_pet['effects']
        self.monster_current_hp = updated_monster['current_hp']; self.monster_effects = updated_monster['effects']
        self.battle_log.extend(monster_turn_logs)

        if updated_monster['current_hp'] <= 0: self.monster_current_hp = 0
        if self.pet_current_hp <= 0: self.pet_is_defeated = True
        
        await supabase.table('pets').update({'current_hp': self.pet_current_hp}).eq('id', self.pet_data_raw['id']).execute()
    
    async def handle_explore(self, interaction: discord.Interaction):
        if self.pet_is_defeated: return await interaction.response.send_message("펫이 쓰러져서 탐색할 수 없습니다.", ephemeral=True, delete_after=5)
        
        self.current_monster = self.generate_monster()
        self.monster_current_hp = self.current_monster['hp']
        self.battle_log = [] # 전투 시작 전이므로 로그 초기화
        self.pet_effects.clear(); self.monster_effects.clear()
        self.pet_current_energy = self.pet_max_energy

        # ▼▼▼ [핵심 수정] 상태를 'encounter'로 변경하고, 선공권만 결정합니다. ▼▼▼
        if self.final_pet_stats['speed'] >= self.current_monster.get('speed', 0):
            self.is_pet_turn = True
        else:
            self.is_pet_turn = False

        self.state = "encounter" # 상태를 '조우'로 변경
        await supabase.table('dungeon_sessions').update({
            'state': self.state, 
            'current_monster_json': {'data': self.current_monster, 'hp': self.monster_current_hp}
        }).eq('id', self.session_id).execute()

        await self.refresh_ui(interaction)
        # ▲▲▲ [핵심 수정] 완료 ▲▲▲

    async def handle_skill_button(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        learned_skills = self.pet_data_raw.get('learned_skills', [])

        can_use_any_skill = any(self.pet_current_energy >= s['pet_skills'].get('cost', 0) for s in learned_skills)
        if not can_use_any_skill:
            # ▼▼▼ [핵심 수정] 이 블록 전체를 아래 코드로 교체합니다. ▼▼▼
            msg = await interaction.followup.send("⚠️ 기력이 부족하여 사용할 수 있는 스킬이 없습니다! '발버둥'으로 공격합니다.", ephemeral=True)
            self.cog.bot.loop.create_task(delete_message_after(msg, 5))
            
            # handle_skill_use를 호출하는 대신, 필요한 로직을 직접 실행합니다.
            self.is_pet_turn = False
            self.battle_log = []
            await self.refresh_ui() # UI를 먼저 '상대 턴'으로 바꿉니다.
            
            struggle_skill = {"skill_name": "발버둥", "power": 25, "cost": 0, "is_struggle": True}
            asyncio.create_task(self._process_battle_turn(struggle_skill))
            return
            # ▲▲▲ [핵심 수정] 완료 ▲▲▲

        if not learned_skills: return await interaction.followup.send("❌ 배운 스킬이 없습니다!", ephemeral=True)
        
        skill_selection_view = SkillSelectView(self, learned_skills, self.pet_current_energy)
        await interaction.followup.send("어떤 스킬을 사용하시겠습니까?", view=skill_selection_view, ephemeral=True)

    async def handle_monster_turn(self):
        if self.state != "in_battle" or self.is_pet_turn or self.pet_is_defeated: return
        await asyncio.sleep(1.5)
        await self._execute_monster_turn()
        if self.pet_current_hp <= 0: return await self.handle_battle_lose()
        
        self.is_pet_turn = True
        await self.refresh_ui()

    async def handle_battle_win(self):
        self.state = "battle_over"
        await supabase.table('dungeon_sessions').update({'state': self.state, 'current_monster_json': None}).eq('id', self.session_id).execute()
        self.battle_log.append({"title": f"🎉 **{self.current_monster['name']}**을(를) 물리쳤다!", "value": "> 전투에서 승리했습니다."})
        pet_exp_gain = self.current_monster['xp']
        self.total_pet_xp_gained += pet_exp_gain
        await supabase.rpc('add_xp_to_pet', {'p_user_id': self.user.id, 'p_xp_to_add': pet_exp_gain}).execute()
        self.battle_log.append({"title": "✨ 경험치 획득", "value": f"> 펫이 **{pet_exp_gain} XP**를 획득했다!"})
        for item, (chance, min_qty, max_qty) in self.cog.loot_table.get(self.dungeon_tier, {}).items():
            if random.random() < chance:
                qty = random.randint(min_qty, max_qty)
                self.rewards[item] += qty
                self.battle_log.append({"title": "🎁 전리품 획득", "value": f"> **{item}** {qty}개를 획득했다!"})
        await self.refresh_ui()

    async def handle_battle_lose(self):
        self.state = "battle_over"; self.pet_is_defeated = True
        await supabase.table('dungeon_sessions').update({'state': self.state, 'current_monster_json': None}).eq('id', self.session_id).execute()
        self.battle_log.append({"title": f"☠️ **{self.pet_data_raw['nickname']}**이(가) 쓰러졌다...", "value": "> 전투에서 패배했습니다."})
        self.current_monster = None
        await self.refresh_ui()

    async def handle_battle_draw(self):
        self.state = "battle_over"
        self.pet_is_defeated = True
        await supabase.table('dungeon_sessions').update({'state': self.state, 'current_monster_json': None}).eq('id', self.session_id).execute()
        self.battle_log.append({"title": f"⚔️ 무승부", "value": "> 양쪽 모두 쓰러졌습니다."})
        self.current_monster = None
        await self.refresh_ui()

    async def handle_flee(self, interaction: discord.Interaction):
        self.state = "exploring"; self.current_monster = None
        await supabase.table('dungeon_sessions').update({'state': self.state, 'current_monster_json': None}).eq('id', self.session_id).execute()
        self.battle_log = ["무사히 도망쳤다..."]; await self.refresh_ui(interaction)

    async def handle_leave(self, interaction: discord.Interaction):
        await interaction.response.send_message("던전에서 나가는 중입니다...", ephemeral=True, delete_after=5)
        await self.cog.close_dungeon_session(self.user.id, self.rewards, self.total_pet_xp_gained, interaction.channel)

    async def handle_use_item(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        inventory = await get_inventory(self.user); usable_items = []; item_db = get_item_database()
        for name, qty in inventory.items():
            item_data = item_db.get(name, {}); effect = item_data.get('effect_type')
            if effect == 'pet_revive' and self.pet_is_defeated: usable_items.append(discord.SelectOption(label=f"{name} ({qty}개)", value=name, emoji="💊"))
            elif effect == 'pet_heal' and not self.pet_is_defeated and self.pet_current_hp < self.final_pet_stats['hp']: usable_items.append(discord.SelectOption(label=f"{name} ({qty}개)", value=name, emoji="🧪"))
        if not usable_items: msg = await interaction.followup.send("사용할 수 있는 아이템이 없습니다.", ephemeral=True); self.cog.bot.loop.create_task(msg.delete(delay=5)); return
        select = ui.Select(placeholder="사용할 아이템을 선택하세요...", options=usable_items)
        async def on_item_select(select_interaction: discord.Interaction):
            await select_interaction.response.defer(); item_name = select_interaction.data['values'][0]; item_data = get_item_database().get(item_name, {}); effect = item_data.get('effect_type')
            await update_inventory(self.user.id, item_name, -1); db_update_task = None
            if effect == 'pet_revive':
                self.pet_is_defeated = False; self.pet_current_hp = self.final_pet_stats['hp']; self.state = "exploring"; self.battle_log = [f"💊 '{item_name}'을(를) 사용해 펫이 완전히 회복되었다!"]
                db_update_task = supabase.table('pets').update({'current_hp': self.pet_current_hp}).eq('id', self.pet_data_raw['id']).execute()
            elif effect == 'pet_heal':
                heal_amount = item_data.get('power', 0); self.pet_current_hp = min(self.final_pet_stats['hp'], self.pet_current_hp + heal_amount)
                self.battle_log = [f"🧪 '{item_name}'을(를) 사용해 체력을 {heal_amount} 회복했다!"]; db_update_task = supabase.table('pets').update({'current_hp': self.pet_current_hp}).eq('id', self.pet_data_raw['id']).execute()
                if self.state == "in_battle":
                    self.is_pet_turn = False
                    await self.refresh_ui()
                    asyncio.create_task(self.handle_monster_turn())
                    await select_interaction.delete_original_response()
                    return
            if db_update_task: await db_update_task
            await self.refresh_ui(); await select_interaction.delete_original_response()
        select.callback = on_item_select
        view = ui.View(timeout=60).add_item(select)
        await interaction.followup.send(view=view, ephemeral=True)
        
    def stop(self):
        if self.cog and self.user: self.cog.active_sessions.pop(self.user.id, None)
        super().stop()

    # ▼▼▼ [핵심 추가] 이 메서드 전체를 추가합니다. ▼▼▼
    async def handle_start_battle(self, interaction: discord.Interaction):
        if self.state != "encounter":
            return await interaction.response.defer() # 이미 전투가 시작되었으면 무시

        self.state = "in_battle"
        await supabase.table('dungeon_sessions').update({'state': self.state}).eq('id', self.session_id).execute()

        self.battle_log.append(f"**{self.current_monster['name']}** 와(과)의 전투를 시작했다!")
        
        if self.is_pet_turn:
            self.battle_log.append(f"**{self.pet_data_raw['nickname']}**이(가) 민첩하게 먼저 움직인다!")
            await self.refresh_ui(interaction)
        else:
            self.battle_log.append(f"**{self.current_monster['name']}**이(가) 더 빠르다! 먼저 공격해온다!")
            await self.refresh_ui(interaction)
            # 몬스터가 선공일 경우에만 몬스터 턴을 바로 시작
            asyncio.create_task(self.handle_monster_turn())
    # ▲▲▲ [핵심 추가] 완료 ▲▲▲

class Dungeon(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot; self.active_sessions: Dict[int, DungeonGameView] = {}
        self.dungeon_data: Dict = {}; self.monster_base_data: Dict = {}; self.loot_table: Dict = {}
        self.check_expired_dungeons.start()
        self.active_views_loaded = False

    async def cog_load(self):
        data = await load_dungeon_data_from_db()
        self.dungeon_data = data["dungeons"]; self.monster_base_data = data["monsters"]; self.loot_table = data["loot"]

    def cog_unload(self): self.check_expired_dungeons.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        self.bot.add_view(await DungeonPanelView.create(self))
        if not self.active_views_loaded:
            await self.reload_active_dungeon_views()
            self.active_views_loaded = True

    async def reload_active_dungeon_views(self):
        logger.info("[Dungeon] 활성화된 던전 게임 UI를 다시 로드합니다...")
        try:
            res = await supabase.table('dungeon_sessions').select('*, pets(*, pet_species(*), learned_skills:pet_learned_skills(*, pet_skills(*)))').not_.is_('message_id', 'null').execute()
            if not res.data: logger.info("[Dungeon] 다시 로드할 활성 던전 UI가 없습니다."); return
            reloaded_count = 0
            for session_data in res.data:
                try:
                    user_id, message_id = int(session_data['user_id']), int(session_data['message_id'])
                    pet_data, dungeon_tier = session_data.get('pets'), session_data['dungeon_tier']
                    end_time, session_id = datetime.fromisoformat(session_data['end_time']), session_data['id']
                    current_state = session_data.get('state', 'exploring'); monster_data = session_data.get('current_monster_json')
                    if not pet_data: logger.warning(f"던전 세션(ID:{session_id})에 연결된 펫 정보가 없어 UI를 로드할 수 없습니다."); continue
                    user = self.bot.get_user(user_id)
                    if not user: logger.warning(f"던전 UI 로드 중 유저(ID:{user_id})를 찾을 수 없습니다."); continue
                    view = DungeonGameView(self, user, pet_data, dungeon_tier, end_time, session_id, current_state=current_state, monster_data=monster_data)
                    try:
                        if thread_id := session_data.get('thread_id'):
                            if thread := self.bot.get_channel(int(thread_id)): view.message = await thread.fetch_message(message_id)
                    except (discord.NotFound, discord.Forbidden): logger.warning(f"던전 UI 재로드 중 메시지(ID: {message_id})를 찾을 수 없어 해당 세션을 건너뜁니다."); continue
                    self.bot.add_view(view, message_id=message_id); self.active_sessions[user_id] = view; reloaded_count += 1
                except Exception as e: logger.error(f"던전 세션(ID: {session_data.get('id')}) UI 재로드 중 오류 발생: {e}", exc_info=True)
            logger.info(f"[Dungeon] 총 {reloaded_count}개의 던전 게임 UI를 성공적으로 다시 로드했습니다.")
        except Exception as e: logger.error(f"활성 던전 UI 로드 중 오류 발생: {e}", exc_info=True)

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
        if res and res.data and (thread := self.bot.get_channel(int(res.data['thread_id']))): return await interaction.followup.send(f"❌ 이미 던전에 입장해 있습니다. {thread.mention}", ephemeral=True)
        pet_data = await get_user_pet(user.id)
        if not pet_data: return await interaction.followup.send("❌ 던전에 입장하려면 펫이 필요합니다.", ephemeral=True)
        dungeon_name = self.dungeon_data[tier]['name']; ticket_name = f"{dungeon_name} 입장권"
        if (await get_inventory(user)).get(ticket_name, 0) < 1: return await interaction.followup.send(f"❌ '{ticket_name}'이 부족합니다.", ephemeral=True)
        try: thread = await interaction.channel.create_thread(name=f"🛡️｜{user.display_name}의 {dungeon_name}", type=discord.ChannelType.private_thread, auto_archive_duration=1440)
        except Exception: return await interaction.followup.send("❌ 던전을 여는 데 실패했습니다.", ephemeral=True)
        await update_inventory(user.id, ticket_name, -1); await thread.add_user(user)
        end_time = datetime.now(timezone.utc) + timedelta(hours=24)
        session_res = await supabase.table('dungeon_sessions').upsert({ "user_id": str(user.id), "thread_id": str(thread.id), "end_time": end_time.isoformat(), "pet_id": pet_data['id'], "dungeon_tier": tier, "rewards_json": "{}", "state": "exploring" }, on_conflict="user_id").execute()
        if not (session_res and session_res.data): logger.error(f"던전 세션 생성/업데이트 실패 (User: {user.id})"); return await interaction.followup.send("❌ 던전 입장에 실패했습니다. (DB 오류)", ephemeral=True)
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
            if rewards: await asyncio.gather(*[update_inventory(user.id, item, qty) for item, qty in rewards.items()])
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
                try: old_message = await channel.fetch_message(msg_id); await old_message.delete()
                except (discord.NotFound, discord.Forbidden): pass
        if embed_data := await get_embed_from_db(panel_key):
            dungeon_levels = {f"{tier}_rec_level": f"Lv.{data.get('recommended_level', '?')}" for tier, data in self.dungeon_data.items()}
            embed = format_embed_from_db(embed_data, **dungeon_levels)
            view = await DungeonPanelView.create(self)
            new_message = await channel.send(embed=embed, view=view)
            await save_panel_id(panel_name, new_message.id, channel.id)
            
    async def skill_autocomplete(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        res = await supabase.table('pet_skills').select('skill_name').ilike('skill_name', f'%{current}%').limit(25).execute()
        if not (res and res.data): return []
        return [app_commands.Choice(name=row['skill_name'], value=row['skill_name']) for row in res.data]

    @app_commands.command(name="던전테스트", description="[관리자] 던전 전투 시스템을 테스트합니다.")
    @app_commands.describe(
        action="실행할 작업을 선택하세요.",
        value="[HP/기력 설정] 설정할 숫자 값입니다.",
        effect_type="[효과 부여] 부여할 효과의 유형입니다.",
        duration="[효과 부여] 효과의 지속 턴 수입니다.",
        skill_name="[스킬 부여] 부여할 스킬의 이름입니다.",
        slot="[스킬 부여] 스킬을 부여할 슬롯 번호입니다."
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="[펫] HP 설정", value="pet_hp"),
        app_commands.Choice(name="[펫] 기력 설정", value="pet_energy"),
        app_commands.Choice(name="[몬스터] HP 설정", value="monster_hp"),
        app_commands.Choice(name="[효과] 펫에게 효과 부여", value="add_effect_pet"),
        app_commands.Choice(name="[효과] 몬스터에게 효과 부여", value="add_effect_monster"),
        app_commands.Choice(name="[효과] 모든 효과 제거", value="clear_effects"),
        app_commands.Choice(name="[전투] 턴 강제 종료", value="end_turn"),
        app_commands.Choice(name="[설정] 스킬 부여", value="add_skill"),
        app_commands.Choice(name="[설정] 몬스터 강제 소환", value="spawn_monster"),
    ])
    @app_commands.autocomplete(skill_name=skill_autocomplete)
    async def dungeon_test(self, interaction: discord.Interaction, action: str, value: Optional[int] = None,
                           effect_type: Optional[str] = None, duration: Optional[app_commands.Range[int, 1, 99]] = None,
                           skill_name: Optional[str] = None, slot: Optional[app_commands.Range[int, 1, 4]] = None):
        if not await self.bot.is_owner(interaction.user): return await interaction.response.send_message("❌ 봇 소유자만 사용할 수 있는 명령어입니다.", ephemeral=True)
        if interaction.user.id not in self.active_sessions: return await interaction.response.send_message("❌ 먼저 던전에 입장해주세요.", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        view = self.active_sessions[interaction.user.id]

        if action == "pet_hp":
            if value is None: return await interaction.followup.send("HP 값을 입력해주세요.")
            view.pet_current_hp = max(0, min(value, view.final_pet_stats['hp']))
            await view.refresh_ui()
            await interaction.followup.send(f"펫의 HP를 {view.pet_current_hp}로 설정했습니다.")
        elif action == "pet_energy":
            if value is None: return await interaction.followup.send("기력 값을 입력해주세요.")
            view.pet_current_energy = max(0, min(value, view.pet_max_energy))
            await view.refresh_ui()
            await interaction.followup.send(f"펫의 기력을 {view.pet_current_energy}로 설정했습니다.")
        elif action == "monster_hp":
            if not view.current_monster: return await interaction.followup.send("전투 중인 몬스터가 없습니다.")
            if value is None: return await interaction.followup.send("HP 값을 입력해주세요.")
            view.monster_current_hp = max(0, min(value, view.current_monster['hp']))
            await view.refresh_ui()
            await interaction.followup.send(f"몬스터의 HP를 {view.monster_current_hp}로 설정했습니다.")
        elif action in ["add_effect_pet", "add_effect_monster"]:
            if not effect_type or not duration: return await interaction.followup.send("효과 유형과 지속 턴을 입력해주세요.")
            target_effects = view.pet_effects if action == "add_effect_pet" else view.monster_effects
            target_name = "펫" if action == "add_effect_pet" else "몬스터"
            target_effects.append({"type": effect_type.upper(), "duration": duration, "value": 0.2})
            await view.refresh_ui()
            await interaction.followup.send(f"{target_name}에게 {effect_type.upper()} 효과를 {duration}턴 동안 부여했습니다.")
        elif action == "clear_effects":
            view.pet_effects.clear()
            view.monster_effects.clear()
            await view.refresh_ui()
            await interaction.followup.send("모든 효과를 제거했습니다.")
        elif action == "end_turn":
            if not view.state == "in_battle": return await interaction.followup.send("전투 중에만 사용할 수 있습니다.")
            if view.is_pet_turn:
                view.is_pet_turn = False
                await view.refresh_ui()
                asyncio.create_task(view.handle_monster_turn())
                await interaction.followup.send("펫의 턴을 강제로 종료하고 몬스터 턴을 시작합니다.")
            else:
                view.is_pet_turn = True
                view.pet_current_energy = min(view.pet_max_energy, view.pet_current_energy + 10)
                await view.refresh_ui()
                await interaction.followup.send("몬스터의 턴을 강제로 종료하고 펫의 턴으로 넘깁니다.")
        elif action == "add_skill":
            if not skill_name or not slot: return await interaction.followup.send("스킬 이름과 슬롯을 입력해주세요.")
            res = await supabase.table('pet_skills').select('*').eq('skill_name', skill_name).maybe_single().execute()
            if not (res and res.data): return await interaction.followup.send(f"'{skill_name}' 스킬을 찾을 수 없습니다.")
            skill_data, pet_id = res.data, view.pet_data_raw['id']
            await supabase.table('pet_learned_skills').upsert({'pet_id': pet_id, 'skill_id': skill_data['id'], 'slot_number': slot}, on_conflict='pet_id, slot_number').execute()
            view.pet_data_raw['learned_skills'] = [s for s in view.pet_data_raw.get('learned_skills', []) if s['slot_number'] != slot]
            view.pet_data_raw['learned_skills'].append({'slot_number': slot, 'pet_skills': skill_data})
            await interaction.followup.send(f"펫에게 '{skill_name}' 스킬을 {slot}번 슬롯에 부여했습니다.")
        elif action == "spawn_monster":
            # 이전에 제공된 코드에는 element와 level 파라미터가 있었으나, 최신 app_commands 구조에서는 누락되었습니다.
            # 필요하다면 함수 시그니처에 다시 추가해야 합니다. 지금은 이 기능을 비활성화합니다.
            await interaction.followup.send("몬스터 소환 기능은 현재 비활성화되어 있습니다. 파라미터 재정의가 필요합니다.")
        else:
            await interaction.followup.send("알 수 없는 작업입니다.")

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
