# utils/combat_system.py

import random
from typing import Dict, List, Tuple, TypedDict, Optional

# 전투 참여자의 정보를 표준화하기 위한 데이터 구조
class Combatant(TypedDict):
    name: str
    stats: Dict[str, int]  # 최종 계산된 스탯 (공격력, 방어력, 스피드 등)
    current_hp: int
    max_hp: int
    effects: List[Dict]

# 전투 로그의 형식을 표준화하기 위한 데이터 구조
class CombatLog(TypedDict):
    title: str
    value: str

def _get_stat_with_effects(base_stat: int, stat_key: str, effects: List[Dict]) -> int:
    """버프/디버프 효과가 적용된 최종 스탯을 계산합니다."""
    multiplier = 1.0
    for effect in effects:
        if effect.get('type') == f"{stat_key}_BUFF":
            multiplier += effect.get('value', 0)
        elif effect.get('type') == f"{stat_key}_DEBUFF":
            multiplier -= effect.get('value', 0)
    return max(1, round(base_stat * multiplier))

def _apply_skill_effect(
    skill: Dict, 
    caster: Combatant, 
    target: Combatant, 
    damage_dealt: int
) -> Tuple[Combatant, Combatant, Optional[CombatLog]]:
    """스킬의 부가 효과를 적용하고 로그를 반환합니다."""
    effect_type = skill.get('effect_type')
    if not effect_type:
        return caster, target, None

    value = skill.get('effect_value', 0)
    duration = skill.get('effect_duration', 0)
    log_value = ""
    log_title = f"✨ 스킬 효과: {skill['skill_name']}"

    if 'DEBUFF' in effect_type:
        target['effects'].append({'type': effect_type, 'value': value, 'duration': duration + 1})
        stat_name = {"ATK": "공격력", "DEF": "방어력", "SPD": "스피드", "ACC": "명중률"}.get(effect_type.split('_')[0], "능력")
        log_value = f"> **{target['name']}**의 **{stat_name}**이(가) 하락했다!"
    elif 'BUFF' in effect_type:
        caster['effects'].append({'type': effect_type, 'value': value, 'duration': duration + 1})
        stat_name = {"ATK": "공격력", "DEF": "방어력", "SPD": "스피드", "EVA": "회피율"}.get(effect_type.split('_')[0], "능력")
        log_value = f"> **{caster['name']}**의 **{stat_name}**이(가) 상승했다!"
    elif effect_type == 'HEAL_PERCENT':
        heal_amount = round(caster['max_hp'] * value)
        caster['current_hp'] = min(caster['max_hp'], caster['current_hp'] + heal_amount)
        log_value = f"> **{caster['name']}**이(가) 체력을 **{heal_amount}** 회복했다!"
    elif effect_type in ['DRAIN', 'LEECH']:
        drain_amount = round(damage_dealt * value)
        caster['current_hp'] = min(caster['max_hp'], caster['current_hp'] + drain_amount)
        log_value = f"> **{target['name']}**에게서 체력을 **{drain_amount}** 흡수했다!"
    elif effect_type == 'BURN':
        target['effects'].append({'type': effect_type, 'value': value, 'duration': duration + 1})
        log_value = f"> **{target['name']}**은(는) 화상을 입었다!"
    elif effect_type in ['PARALYZE', 'PARALYZE_ON_HIT']:
        target['effects'].append({'type': 'PARALYZE', 'duration': duration + 1})
        log_value = f"> **{target['name']}**은(는) 마비되었다!"
    elif effect_type == 'SLEEP':
        target['effects'].append({'type': 'SLEEP', 'duration': duration + 1})
        log_value = f"> **{target['name']}**은(는) 잠이 들었다!"

    if log_value:
        return caster, target, {"title": log_title, "value": log_value}
    return caster, target, None

def _process_turn_end_effects(combatant: Combatant) -> Tuple[Combatant, List[str]]:
    """턴 종료 시 지속 데미지, 효과 지속시간 감소 등을 처리합니다."""
    logs = []
    effects_to_remove = []
    effect_name_map = {'BURN': '화상', 'ATK_BUFF': '공격력 증가', 'DEF_BUFF': '방어력 증가', 'SPD_BUFF': '스피드 증가', 'EVA_BUFF': '회피율 증가', 'ATK_DEBUFF': '공격력 감소', 'DEF_DEBUFF': '방어력 감소', 'SPD_DEBUFF': '스피드 감소', 'ACC_DEBUFF': '명중률 감소', 'PARALYZE': '마비', 'SLEEP': '수면'}

    for effect in combatant['effects']:
        if effect.get('type') == 'BURN':
            dot_damage = max(1, round(effect.get('value', 0)))
            combatant['current_hp'] = max(0, combatant['current_hp'] - dot_damage)
            logs.append(f"🔥 **{combatant['name']}**은(는) 화상 데미지로 **{dot_damage}**의 피해를 입었다!")
        
        effect['duration'] -= 1
        if effect.get('duration', 0) <= 0:
            effects_to_remove.append(effect)
            effect_name = effect_name_map.get(effect.get('type', '효과'), effect.get('type'))
            logs.append(f"💨 **{combatant['name']}**에게 걸려있던 **{effect_name}** 효과가 사라졌다.")
    
    for expired_effect in effects_to_remove:
        combatant['effects'].remove(expired_effect)
        
    return combatant, logs

def process_turn(caster: Combatant, target: Combatant, skill: Dict) -> Tuple[Combatant, Combatant, List[CombatLog | str]]:
    """
    한 턴의 전투를 처리하고, 변경된 상태와 전투 로그를 반환합니다.
    """
    battle_logs: List[CombatLog | str] = []

    # 1. 턴 시작 시 상태 이상 확인 (수면, 마비 등)
    for effect in caster['effects']:
        if effect.get('type') == 'SLEEP':
            battle_logs.append(f"💤 **{caster['name']}**은(는) 깊은 잠에 빠져있다...")
            caster, end_of_turn_logs = _process_turn_end_effects(caster)
            battle_logs.extend(end_of_turn_logs)
            return caster, target, battle_logs
        if effect.get('type') == 'PARALYZE' and random.random() < 0.25:
            battle_logs.append(f"⚡ **{caster['name']}**은(는) 몸이 마비되어 움직일 수 없다!")
            caster, end_of_turn_logs = _process_turn_end_effects(caster)
            battle_logs.extend(end_of_turn_logs)
            return caster, target, battle_logs

    # 2. 스킬 처리 (데미지 및 효과)
    skill_power = skill.get('power', 0)
    damage_dealt = 0

    if skill_power == 0:  # 비공격 스킬
        caster, target, effect_log = _apply_skill_effect(skill, caster, target, 0)
        if effect_log:
            battle_logs.append(effect_log)
    else:  # 공격 스킬
        final_attack = _get_stat_with_effects(caster['stats']['attack'], 'ATK', caster['effects'])
        final_defense = _get_stat_with_effects(target['stats']['defense'], 'DEF', target['effects'])
        
        damage_dealt = max(1, round(final_attack * (skill_power / 100)) - final_defense)
        target['current_hp'] = max(0, target['current_hp'] - damage_dealt)
        
        battle_logs.append({
            "title": f"▶️ **{caster['name']}**의 **{skill['skill_name']}**!",
            "value": f"> **{target['name']}**에게 **{damage_dealt}**의 데미지!"
        })

        # 스킬의 부가 효과 적용
        if skill.get('effect_type'):
            caster, target, effect_log = _apply_skill_effect(skill, caster, target, damage_dealt)
            if effect_log:
                battle_logs.append(effect_log)

        # 반동 데미지 처리
        if skill.get('effect_type') == 'RECOIL':
            recoil_damage = max(1, round(damage_dealt * skill.get('effect_value', 0)))
            caster['current_hp'] = max(0, caster['current_hp'] - recoil_damage)
            battle_logs.append(f"💥 **{caster['name']}**은(는) 반동으로 **{recoil_damage}**의 데미지를 입었다!")

    # 3. 턴 종료 시 효과 처리 (caster)
    caster, end_of_turn_logs = _process_turn_end_effects(caster)
    battle_logs.extend(end_of_turn_logs)
    
    return caster, target, battle_logs
