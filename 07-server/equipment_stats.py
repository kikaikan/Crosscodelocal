"""CardCalculator/EquipCalculator formulas for source-backed local chips and sub-talents.

Chinese client rules: all configured chip properties and skill levels apply. Active
secondary-talent (副天赋) modifiers are ported from CardCalculator.lua:410-443: each
equipped talent adds its nFightSkillId to the skill list and its jPropertys, mapped
through cfgCfgCardPropertyEnum.sFieldName, to the same property totals as chips.
Weapon and halo modifiers remain explicit rejections.
No client-supplied property values enter these calculations.
"""
from copy import deepcopy
from functools import lru_cache
import json
import math
from pathlib import Path

from config_codec import app_path
from database import StorageError
from equip_service import config_record, card_equipments

# 02-tools/scripts/sub-talent-build.py 生成；这里只读 skills 段（jPropertys/nFightSkillId）。
# Loading it directly keeps this module free of any handlers.* import (no import cycle).
SUB_TALENT_DATA = app_path('data', 'sub-talent.json')

MULTIPLIED = ('attack', 'maxhp', 'defense')
ADDED = ('speed', 'crit_rate', 'crit', 'hit', 'resist', 'hot', 'np', 'sp', 'sp_race', 'sp_race2')
DAMAGE = ('bedamage', 'damage', 'becure', 'cure', 'damagePhysics', 'damageLight')
FIXED = ('attack_fixed', 'maxhp_fixed', 'defense_fixed')
REWARD = ('card_exp_add', 'plr_exp_add', 'gold_add')
FLOORED = ('attack', 'maxhp', 'defense', 'speed', 'hot', 'np', 'sp')


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise StorageError('Invalid configured/persisted property: ' + label)
    return value


def add(container, field, value):
    container[field] = number(container.get(field, 0), field) + number(value, field)


def property_name(identifier):
    # Client CardCalculator.lua:434 indexes CfgCardPropertyEnum[proType] without a nil guard,
    # so an unknown type is a hard failure there too; config_record raises StorageError here.
    return config_record('cfgCfgCardPropertyEnum.lua', identifier)['sFieldName']


@lru_cache(maxsize=1)
def sub_talent_skills() -> dict:
    """The sub-talent skill table (id -> projected fields) from the generated catalog."""
    try:
        payload = json.loads(SUB_TALENT_DATA.read_text(encoding='utf-8'))
        skills = payload['skills']
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise StorageError('Missing local sub-talent catalog for property calculation') from error
    if not isinstance(skills, dict) or not skills:
        raise StorageError('Empty local sub-talent catalog')
    return skills


def equipped_talents(card) -> list:
    """Active sub-talent ids in wire order; mirrors CardCalculator.lua:413-414 (tId > 0).

    Un-equipped slots (use all 0) contribute nothing, so a materialized-but-idle card stays
    fully supported by the ordinary base/equip path.
    """
    data = card.get('sub_talent')
    if not isinstance(data, dict):
        return []
    use = data.get('use')
    if use is None:
        return []      # 客户端 CardCalculator.lua:413 是 useSubTalents or {}
    if not isinstance(use, list):
        raise StorageError('Invalid persisted sub_talent use list')
    result = []
    for value in use:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise StorageError('Invalid persisted sub_talent use entry')
        if value:
            result.append(value)
    return result


def talent_properties(card) -> dict:
    """CardCalculator.lua:427-438's subTablentProperty table (property name -> summed value).

    An id absent from CfgSubTalentSkill is skipped exactly like the client's LogWarning
    branch (CardCalculator.lua:439-441). A malformed jPropertys entry is refused instead of
    guessed: the client indexes proInfo[1]/proInfo[2] and would compare nil in Lua.
    """
    skills = None
    totals = {}
    for identifier in equipped_talents(card):
        skills = sub_talent_skills() if skills is None else skills
        record = skills.get(str(identifier))
        if not isinstance(record, dict):
            continue
        entries = record.get('jPropertys')
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise StorageError('Invalid configured sub-talent property list')
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != 2:
                raise StorageError('Unsupported configured sub-talent property entry')
            add(totals, property_name(entry[0]), number(entry[1], 'sub-talent property'))
    return totals


def talent_fight_skills(card) -> list:
    """CardCalculator.lua:417-424: nFightSkillId joins ret.skills when its cfg has lv/main_type.

    The client logs a warning and skips both a missing skill record and one without
    lv/main_type, so this mirrors that branch instead of inventing metadata.
    """
    result = []
    for identifier in equipped_talents(card):
        record = sub_talent_skills().get(str(identifier))
        if not isinstance(record, dict) or record.get('nFightSkillId') is None:
            continue
        fight_skill = optional_record('cfgskill.lua', record['nFightSkillId'])
        if not isinstance(fight_skill, dict) or 'lv' not in fight_skill or 'main_type' not in fight_skill:
            continue
        result.append(int(record['nFightSkillId']))
    return result


def optional_record(filename, identifier):
    """config_record that answers None where the client's table lookup would be nil."""
    try:
        return config_record(filename, identifier)
    except StorageError:
        return None


def validate_card_modifiers(card):
    mix = card.get('mix_data')
    if mix is not None and not isinstance(mix, dict):
        raise StorageError('Invalid persisted card mix_data')
    mix = mix or {}
    if mix.get('weaponLv', 0) or card.get('weapon_lv', 0):
        raise StorageError('Weapon property calculation is not yet reconstructed')
    equipped_talents(card)      # 已装备副天赋已按 CardCalculator 移植；形状非法时在此抛错
    if card.get('haloInfo'):
        raise StorageError('Halo property calculation is not yet reconstructed')


def card_base_stats(card):
    """CalLvlPropertys without premature integer rounding."""
    validate_card_modifiers(card)
    cfg = config_record('cfgCardData.lua', card['cfgid'])
    lvl = config_record('cfgCardLevel.lua', card['level'])
    brk = config_record('cfgCardBreak.lua', card.get('break_level', 1))
    improve = config_record('cfgCardIntensify.lua', card.get('intensify_level', 1))
    fields = MULTIPLIED + ADDED + DAMAGE + ('quality', 'fight_cost', 'career', 'nStep', 'nJump', 'nMoveType')
    result = {key: deepcopy(cfg[key]) for key in fields if key in cfg}
    for key in MULTIPLIED:
        result[key] = number(cfg[key], key) * number(lvl.get(key, 1), key) * number(brk.get(key, 1), key)
    for key in ADDED + ('damagePhysics',):
        # Add(nil) preserves absence; absent damagePhysics later means 1.
        for source in (lvl, brk, improve):
            if key in source:
                add(result, key, source[key])
    result.update({key: 0 for key in REWARD})
    return result


def equip_properties(equips, is_japan=False):
    """Exact base growth, skill grouping/cap, fight and LifeBuffer results."""
    result = {'baseVal': {}, 'propertySkills': [], 'fightSkills': [],
              'passivBufIds': [], 'eskills': [], 'upSkills': {}}
    totals = {}
    for equip in equips:
        cfg = config_record('cfgCfgEquip.lua', equip['cfgid'])
        if equip.get('rand_skill_type', 0) or equip.get('rand_skill_value', 0):
            raise StorageError('Legacy chip random-stat fields are not reconstructed')
        if cfg.get('nType', 1) != 1:
            raise StorageError('Material chip cannot contribute equipped properties')
        level = number(equip.get('level', 0), 'chip level')
        for ix in (1, 2):
            if not is_japan or (ix == 1 and level >= cfg.get('base1Condition', 0)):
                identifier = cfg.get('nBase' + str(ix))
                if identifier is not None:
                    val = cfg['fBaseVal' + str(ix)] + level * cfg['fBaseAdd' + str(ix)]
                    add(result['baseVal'], property_name(identifier), val)
        skills = equip.get('skills', [])
        if not isinstance(skills, list):
            raise StorageError('Chip skills must be an original ID list')
        for ix, skill_id in enumerate(skills):
            conditions = cfg.get('randSkillsCondition', [])
            if is_japan and level < (conditions[ix] if ix < len(conditions) else 0):
                continue
            if isinstance(skill_id, bool) or not isinstance(skill_id, int) or skill_id < 1:
                raise StorageError('Invalid chip skill ID')
            skill_level = skill_id % 100
            if not skill_level:
                raise StorageError('Invalid zero-level chip skill')
            base_id = skill_id - skill_level + 1
            totals[base_id] = totals.get(base_id, 0) + skill_level
    for base_id, total in sorted(totals.items()):
        base = config_record('cfgCfgEquipSkill.lua', base_id)
        skill_id = base_id + min(total, base['maxLv']) - 1
        skill = config_record('cfgCfgEquipSkill.lua', skill_id)
        result['eskills'].append(skill_id)
        kind = skill['nType']
        if kind == 1:
            result['propertySkills'].append(skill_id)
            result['upSkills'][skill_id] = 1
        elif kind == 2:
            if skill.get('nGetSkillId'):
                result['fightSkills'].append(skill_id)
                result['upSkills'][skill_id] = 1
        elif kind == 3:
            if skill.get('nGetSkillId'):
                buff = config_record('cfgCfgLifeBuffer.lua', skill['nGetSkillId'])
                if 'jValiTime' in buff or 'jOpenDups' in buff:
                    result['passivBufIds'].append(buff['id'])
                    result['upSkills'][skill_id] = 1
                elif buff.get('jVal'):
                    add(result['baseVal'], property_name(buff['nType']), buff['jVal'][0])
                    result['upSkills'][skill_id] = 1
        else:
            raise StorageError('Unknown configured chip effect category')
        if 'nGetBaseType' in skill and 'fGetBaseVal' in skill:
            add(result['baseVal'], property_name(skill['nGetBaseType']), skill['fGetBaseVal'])
    return result


def apply_property_totals(result, properties):
    """TakePropertyAdd's cal1..cal4 (CardCalculator.lua:280-320) for one merged table.

    cal1 multiplies attack/maxhp/defense by (1+value), cal2 adds, cal3 replaces the damage
    coefficients with (1+value), cal4 adds the *_fixed entries onto their base property.
    Property names outside these four lists are ignored exactly like the client (e.g. the
    'suck' entry of property type 40 never reaches CardCalculator's totals).
    """
    for key in MULTIPLIED:
        if properties.get(key, 0):
            result[key] = number(result.get(key, 1), key) * (1 + properties[key])
    for key in ADDED:
        if properties.get(key, 0):
            add(result, key, properties[key])
    for key in DAMAGE:
        value = result.get(key, 0) + properties.get(key, 0)
        if value:
            result[key] = 1 + value
    for key, target in zip(FIXED, MULTIPLIED):
        if properties.get(key, 0):
            add(result, target, properties[key])


def equipped_stats(state, card, base_stats=None):
    """CalSumPropery, preserving original battle skills and conditional buffs."""
    validate_card_modifiers(card)
    result = card_base_stats(card) if base_stats is None else deepcopy(base_stats)
    props = equip_properties(card_equipments(state, card))
    base = deepcopy(props['baseVal'])
    # CardCalculator.lua:463 passes equipsProperty and subTablentProperty to one
    # TakePropertyAdd, which sums them per property name before applying cal1..cal4;
    # merging the totals here is exactly that sum. REWARD stays chip-only.
    for key, value in talent_properties(card).items():
        add(base, key, value)
    for key in REWARD:
        if key in props['baseVal']:
            add(result, key, props['baseVal'][key])
    skill_map = card.get('skills') or {}
    if not isinstance(skill_map, dict):
        raise StorageError('Card skills must be an original ID map')
    result['skills'] = []
    for key in skill_map:
        skill = config_record('cfgskill.lua', int(key))
        if 'lv' not in skill or 'main_type' not in skill:
            raise StorageError('Owned skill lacks calculation metadata')
        result['skills'].append(skill['id'])
        if skill['main_type'] == 2:
            quality = config_record('cfgCfgMainTalentSkillUpgrade.lua', result['quality'])
            infos = quality['infos']
            if not 1 <= skill['lv'] <= len(infos):
                raise StorageError('Missing configured main-talent level')
            extra_sp = infos[skill['lv'] - 1].get('nAddSp')
            if extra_sp is not None:
                add(result, 'sp', extra_sp)
    for skill_id in props['fightSkills']:
        skill = config_record('cfgCfgEquipSkill.lua', skill_id)
        fight_skill = config_record('cfgskill.lua', skill['nGetSkillId'])
        if 'lv' not in fight_skill or 'main_type' not in fight_skill:
            raise StorageError('Equipment battle skill lacks calculation metadata')
        result['skills'].append(fight_skill['id'])
    result['skills'].extend(talent_fight_skills(card))   # CardCalculator.lua:417-424
    apply_property_totals(result, base)
    for key in FLOORED:
        if key in result:
            result[key] = math.floor(number(result[key], key))
    if result.get('maxhp', 0) <= 0:
        raise StorageError('Computed card HP is not positive')
    result['skills'].sort()
    result['eskills'] = props['eskills']
    result['passivBufIds'] = props['passivBufIds']
    return result


def refresh_card_hp(state, card):
    result = equipped_stats(state, card)
    card['hp'] = result['maxhp']
    return result
