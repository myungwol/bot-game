# utils/combat_system.py

import random
from typing import Dict, List, Tuple, TypedDict, Optional

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
    effect_type = skill.get('effect_type')
    if not effect_type:
        return caster, target, None

    value = skill.get('effect_value', 0)
    duration = skill.get('effect_duration', 0)
    chance = skill.get('effect_chance', 1.0)
    log_value = ""
    log_title = f"✨ 스킬 효과: {skill['skill_name']}"

    if random.random() <= chance:
        if effect_type == 'TRAP_DOT':
            duration = random.randint(2, 4)
        
        if effect_type == 'SELF_SLEEP':
            caster['effects'].append({'type': 'SLEEP', 'duration': duration + 1})
            log_value = f"> **{caster['name']}**은(는) 스킬의 반동으로 깊은 잠에 빠졌다!"
        # ▼▼▼ [핵심 수정] RECHARGE, ROOTED_REGEN 효과 처리 ▼▼▼
        elif effect_type == 'RECHARGE':
            caster['effects'].append({'type': 'RECHARGING', 'duration': duration + 1})
            # 이 효과는 즉시 발동되므로 별도 로그는 process_turn에서 처리
        elif effect_type == 'ROOTED_REGEN':
            caster['effects'].append({'type': 'ROOTED_REGEN', 'value': value, 'duration': 999}) # 무한 지속
            caster['effects'].append({'type': 'DEF_DEBUFF', 'value': 0.2, 'duration': 999}) # 방어 20% 감소 페널티
            log_value = f"> **{caster['name']}**이(가) 땅에 뿌리를 내렸다! 매 턴 체력을 회복하지만 방어력이 감소한다."
        # ▲▲▲ [수정] 완료 ▲▲▲
        else:
            existing_effect = next((e for e in target['effects'] if e.get('type') == effect_type), None)
            
            if effect_type == 'DESTINY_BOND':
                caster['effects'].append({'type': 'DESTINY_BOND', 'duration': duration + 1})
            elif existing_effect:
                existing_effect['duration'] = duration + 1
            else:
                if 'DEBUFF' in effect_type or effect_type in ['BURN', 'PARALYZE', 'SLEEP', 'PARALYZE_ON_HIT', 'TRAP_DOT']:
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
            elif effect_type == 'TRAP_DOT':
                log_value = f"> **{target['name']}**은(는) 소용돌이에 휘말렸다! ({duration}턴 지속)"

    if log_value:
        return caster, target, {"title": log_title, "value": log_value}
    return caster, target, None

def _process_turn_end_effects(combatant: Combatant) -> Tuple[Combatant, List[str]]:
    logs = []
    effects_to_remove = []
    effect_name_map = {'BURN': '화상', 'TRAP_DOT': '소용돌이', 'ATK_BUFF': '공격력 증가', 'DEF_BUFF': '방어력 증가', 'SPD_BUFF': '스피드 증가', 'EVA_BUFF': '회피율 증가', 'ATK_DEBUFF': '공격력 감소', 'DEF_DEBUFF': '방어력 감소', 'SPD_DEBUFF': '스피드 감소', 'ACC_DEBUFF': '명중률 감소', 'PARALYZE': '마비', 'SLEEP': '수면', 'DESTINY_BOND': '길동무', 'RECHARGING': '재충전', 'ROOTED_REGEN': '뿌리내리기'}

    for effect in combatant['effects']:
        if effect.get('type') in ['BURN', 'TRAP_DOT']:
            dot_damage = max(1, round(effect.get('value', 0)))
            combatant['current_hp'] = max(0, combatant['current_hp'] - dot_damage)
            damage_type = "화상" if effect.get('type') == 'BURN' else "소용돌이"
            logs.append(f"🔥 **{combatant['name']}**은(는) {damage_type} 데미지로 **{dot_damage}**의 피해를 입었다!")
        # ▼▼▼ [핵심 수정] 뿌리내리기 체력 회복 로직 추가 ▼▼▼
        elif effect.get('type') == 'ROOTED_REGEN':
            heal_amount = max(1, round(effect.get('value', 0)))
            combatant['current_hp'] = min(combatant['max_hp'], combatant['current_hp'] + heal_amount)
            logs.append(f"🌱 **{combatant['name']}**은(는) 뿌리로부터 **{heal_amount}**의 체력을 회복했다!")
        # ▲▲▲ [수정] 완료 ▲▲▲
        
        # 뿌리내리기 같은 영구 효과는 턴이 감소하지 않도록 예외 처리
        if effect.get('duration', 0) < 999:
            effect['duration'] -= 1
            
        if effect.get('duration', 0) <= 0:
            effects_to_remove.append(effect)
            effect_name = effect_name_map.get(effect.get('type', '효과'), effect.get('type'))
            logs.append(f"💨 **{combatant['name']}**에게 걸려있던 **{effect_name}** 효과가 사라졌다.")
    
    for expired_effect in effects_to_remove:
        if expired_effect in combatant['effects']:
            # [수정] 뿌리내리기는 방어력 감소 효과도 함께 제거
            if expired_effect.get('type') == 'ROOTED_REGEN':
                def_debuff = next((e for e in combatant['effects'] if e.get('type') == 'DEF_DEBUFF' and e.get('duration') == 999), None)
                if def_debuff:
                    combatant['effects'].remove(def_debuff)
            combatant['effects'].remove(expired_effect)
            
    return combatant, logs

def process_turn(caster: Combatant, target: Combatant, skill: Dict) -> Tuple[Combatant, Combatant, List[CombatLog | str]]:
    battle_logs: List[CombatLog | str] = []

    # ▼▼▼ [핵심 수정 1] 펫의 턴일 경우에만 코스트를 소모하도록 명시적으로 추가합니다. ▼▼▼
    # caster의 이름에 'Lv.'가 포함되어 있지 않으면 펫으로 간주합니다.
    is_pet_turn = 'Lv.' not in caster['name']
    if is_pet_turn:
        cost = skill.get('cost', 0)
        # 펫 객체는 'current_energy'와 'max_energy' 키를 가지고 있다고 가정합니다.
        # 이 키가 없다면 dungeon.py에서 Combatant 객체를 만들 때 추가해야 합니다.
        if 'current_energy' in caster:
             caster['current_energy'] -= cost
    # ▲▲▲ [핵심 수정 1] 완료 ▲▲▲

    for effect in list(caster['effects']):
        # ▼▼▼ [핵심 수정] RECHARGING(재충전) 상태이상 체크 추가 ▼▼▼
        if effect.get('type') == 'RECHARGING':
            battle_logs.append(f"⚡ **{caster['name']}**은(는) 강력한 기술의 반동으로 움직일 수 없다!")
            caster, end_of_turn_logs = _process_turn_end_effects(caster)
            battle_logs.extend(end_of_turn_logs)
            return caster, target, battle_logs
        # ▲▲▲ [수정] 완료 ▲▲▲
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

    accuracy_modifier = 1.0
    for effect in caster['effects']:
        if effect.get('type') == 'ACC_DEBUFF': accuracy_modifier -= effect.get('value', 0)
    for effect in target['effects']:
        if effect.get('type') == 'EVA_BUFF': accuracy_modifier -= effect.get('value', 0)

    base_accuracy = float(skill.get('effect_chance')) if skill.get('effect_chance') is not None else 1.0
    final_accuracy = base_accuracy * accuracy_modifier

    if skill.get('power', 0) > 0 and random.random() > final_accuracy:
        battle_logs.append(f"💨 **{caster['name']}**의 **{skill['skill_name']}**! ...하지만 공격은 빗나갔다!")
        caster, end_of_turn_logs = _process_turn_end_effects(caster)
        battle_logs.extend(end_of_turn_logs)
        return caster, target, battle_logs

    skill_power = skill.get('power', 0)
    damage_dealt = 0

    if skill.get('effect_type') == 'FIELD_ACC_DEBUFF':
        duration = skill.get('effect_duration', 0); value = skill.get('effect_value', 0)
        caster['effects'].append({'type': 'ACC_DEBUFF', 'value': value, 'duration': duration + 1})
        target['effects'].append({'type': 'ACC_DEBUFF', 'value': value, 'duration': duration + 1})
        battle_logs.append({"title": f"✨ 스킬 효과: {skill['skill_name']}", "value": f"> 필드 전체에 짙은 안개가 깔려 모두의 명중률이 하락했다!"})
    elif skill_power == 0:
        caster, target, effect_log = _apply_skill_effect(skill, caster, target, 0)
        if effect_log: battle_logs.append(effect_log)
    else:
        final_attack = _get_stat_with_effects(caster['stats']['attack'], 'ATK', caster['effects'])
        final_defense = _get_stat_with_effects(target['stats']['defense'], 'DEF', target['effects'])
        
        # [핵심 수정 2] 데미지 공식을 다시 한번 확인하고 적용합니다.
        raw_damage = (final_attack * (1 + (skill_power / 100))) - final_defense
        damage_dealt = max(1, round(raw_damage))
        
        target['current_hp'] = max(0, target['current_hp'] - damage_dealt)
        
        battle_logs.append({"title": f"▶️ **{caster['name']}**의 **{skill['skill_name']}**!", "value": f"> **{target['name']}**에게 **{damage_dealt}**의 데미지!"})

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

    caster, end_of_turn_logs_caster = _process_turn_end_effects(caster)
    battle_logs.extend(end_of_turn_logs_caster)
    
    # 몬스터의 턴이 끝났을 때 타겟(펫)의 턴 종료 효과도 처리해줘야 합니다.
    # 펫의 턴이 끝났을 때는 이 함수를 빠져나간 후 몬스터 턴에서 처리되므로 중복되지 않습니다.
    if not is_pet_turn:
        target, end_of_turn_logs_target = _process_turn_end_effects(target)
        battle_logs.extend(end_of_turn_logs_target)
    
    return caster, target, battle_logs
