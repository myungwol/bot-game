# utils/combat_system.py

import random
from typing import Dict, List, Tuple, TypedDict, Optional

# ... (Combatant, CombatLog 클래스는 변경 없음) ...
class Combatant(TypedDict):
    name: str
    stats: Dict[str, int]
    current_hp: int
    max_hp: int
    effects: List[Dict]

class CombatLog(TypedDict):
    title: str
    value: str

def _get_stat_with_effects(base_stat: int, stat_key: str, effects: List[Dict]) -> int:
    """버프/디버프 효과가 적용된 최종 스탯을 계산합니다."""
    multiplier = 1.0
    for effect in effects:
        # [수정] 명중(ACC)과 회피(EVA)는 스탯이 아닌 확률 보정치이므로, 이 함수에서 제외하고
        # process_turn에서 직접 처리하도록 합니다.
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
    effect_type = skill.get('effect_type')
    if not effect_type:
        return caster, target, None

    value = skill.get('effect_value', 0)
    duration = skill.get('effect_duration', 0)
    log_value = ""
    log_title = f"✨ 스킬 효과: {skill['skill_name']}"

    existing_effect = next((e for e in target['effects'] if e.get('type') == effect_type), None)
    
    if effect_type == 'DESTINY_BOND':
        caster['effects'].append({'type': 'DESTINY_BOND', 'duration': duration + 1})
    elif existing_effect:
        existing_effect['duration'] = duration + 1
    else:
        if 'DEBUFF' in effect_type or effect_type in ['BURN', 'PARALYZE', 'SLEEP', 'PARALYZE_ON_HIT']:
            target['effects'].append({'type': effect_type.replace('_ON_HIT', ''), 'value': value, 'duration': duration + 1})
        elif 'BUFF' in effect_type:
            caster['effects'].append({'type': effect_type, 'value': value, 'duration': duration + 1})

    if 'DEBUFF' in effect_type:
        stat_name = {"ATK": "공격력", "DEF": "방어력", "SPD": "스피드", "ACC": "명중률"}.get(effect_type.split('_')[0], "능력")
        log_value = f"> **{target['name']}**의 **{stat_name}**이(가) 하락했다!"
    elif 'BUFF' in effect_type:
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
        log_value = f"> **{target['name']}**은(는) 화상을 입었다!"
    elif effect_type in ['PARALYZE', 'PARALYZE_ON_HIT']:
        log_value = f"> **{target['name']}**은(는) 마비되었다!"
    elif effect_type == 'SLEEP':
        log_value = f"> **{target['name']}**은(는) 잠이 들었다!"
    elif effect_type == 'DESTINY_BOND':
        log_value = f"> **{caster['name']}**은(는) 상대를 길동무로 삼았다!"

    if log_value:
        return caster, target, {"title": log_title, "value": log_value}
    return caster, target, None

def _process_turn_end_effects(combatant: Combatant) -> Tuple[Combatant, List[str]]:
    # ... (이 함수는 변경 없이 그대로 유지) ...
    logs = []
    effects_to_remove = []
    effect_name_map = {'BURN': '화상', 'ATK_BUFF': '공격력 증가', 'DEF_BUFF': '방어력 증가', 'SPD_BUFF': '스피드 증가', 'EVA_BUFF': '회피율 증가', 'ATK_DEBUFF': '공격력 감소', 'DEF_DEBUFF': '방어력 감소', 'SPD_DEBUFF': '스피드 감소', 'ACC_DEBUFF': '명중률 감소', 'PARALYZE': '마비', 'SLEEP': '수면', 'DESTINY_BOND': '길동무'}

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
        if expired_effect in combatant['effects']:
            combatant['effects'].remove(expired_effect)
            
    return combatant, logs


def process_turn(caster: Combatant, target: Combatant, skill: Dict) -> Tuple[Combatant, Combatant, List[CombatLog | str]]:
    battle_logs: List[CombatLog | str] = []

    for effect in list(caster['effects']):
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

    # ▼▼▼ [핵심 수정] 명중률 계산 및 판정 로직 추가 ▼▼▼
    # 1. 명중/회피 보정치 계산
    accuracy_modifier = 1.0
    for effect in caster['effects']:
        if effect.get('type') == 'ACC_DEBUFF':
            accuracy_modifier -= effect.get('value', 0)
    for effect in target['effects']:
        if effect.get('type') == 'EVA_BUFF':
            accuracy_modifier -= effect.get('value', 0)

    # 2. 최종 명중률 계산
    # effect_chance가 NULL이거나 1이면 기본 명중률 100%
    base_accuracy = skill.get('effect_chance') if skill.get('effect_chance') is not None else 1.0
    final_accuracy = base_accuracy * accuracy_modifier

    # 3. 명중 판정
    # 위력이 0인 스킬(버프, 디버프 등)은 항상 명중하도록 처리
    if skill.get('power', 0) > 0 and random.random() > final_accuracy:
        battle_logs.append(f"💨 **{caster['name']}**의 **{skill['skill_name']}**! ...하지만 공격은 빗나갔다!")
        caster, end_of_turn_logs = _process_turn_end_effects(caster)
        battle_logs.extend(end_of_turn_logs)
        return caster, target, battle_logs
    # ▲▲▲ [수정] 완료 ▲▲▲

    skill_power = skill.get('power', 0)
    damage_dealt = 0

    if skill_power == 0:
        caster, target, effect_log = _apply_skill_effect(skill, caster, target, 0)
        if effect_log: battle_logs.append(effect_log)
    else:
        final_attack = _get_stat_with_effects(caster['stats']['attack'], 'ATK', caster['effects'])
        final_defense = _get_stat_with_effects(target['stats']['defense'], 'DEF', target['effects'])
        
        base_damage = max(1, final_attack - final_defense)
        damage_dealt = round(base_damage * (1 + (skill_power / 100)))
        target['current_hp'] = max(0, target['current_hp'] - damage_dealt)
        
        battle_logs.append({
            "title": f"▶️ **{caster['name']}**의 **{skill['skill_name']}**!",
            "value": f"> **{target['name']}**에게 **{damage_dealt}**의 데미지!"
        })

        sleep_effect = next((e for e in target['effects'] if e.get('type') == 'SLEEP'), None)
        if sleep_effect:
            target['effects'].remove(sleep_effect)
            battle_logs.append(f"❗ **{target['name']}**은(는) 공격을 받고 잠에서 깨어났다!")

        if skill.get('effect_type'):
            caster, target, effect_log = _apply_skill_effect(skill, caster, target, damage_dealt)
            if effect_log: battle_logs.append(effect_log)

        if skill.get('effect_type') == 'RECOIL':
            recoil_damage = max(1, round(damage_dealt * skill.get('effect_value', 0)))
            caster['current_hp'] = max(0, caster['current_hp'] - recoil_damage)
            battle_logs.append(f"💥 **{caster['name']}**은(는) 반동으로 **{recoil_damage}**의 데미지를 입었다!")

    if target['current_hp'] <= 0:
        destiny_bond_effect = next((e for e in target['effects'] if e.get('type') == 'DESTINY_BOND'), None)
        if destiny_bond_effect:
            caster['current_hp'] = 0
            battle_logs.append(f"🔗 **{target['name']}**의 길동무 효과가 발동하여 **{caster['name']}**도 함께 쓰러졌다!")
            target['effects'].remove(destiny_bond_effect)

    caster, end_of_turn_logs = _process_turn_end_effects(caster)
    battle_logs.extend(end_of_turn_logs)
    
    return caster, target, battle_logs
