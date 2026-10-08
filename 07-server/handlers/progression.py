"""Local main-line gates and reward settlement, with explicit unsupported branches."""
from copy import deepcopy
from decimal import Decimal
import json
import re
import sys
from pathlib import Path
from config_codec import app_path
from database import StorageError
from server_core import Reply, register
from handlers import gacha

DATA = app_path('data')
def load(name):
    return json.loads((DATA / (name + '.json')).read_text(encoding='utf-8'))
STAGES = load('progression-stages')
REWARDS = load('progression-rewards')
PLAYER_LEVELS = load('progression-player-level')
CARD_LEVELS = load('battle-card-level')
CARD_CAPS = load('battle-card-level-cap')
EQUIPS = load('progression-equips')
RAND_SKILLS = load('progression-equip-rand-skills')
RAND_LEVELS = load('progression-equip-rand-levels')
STAR_REWARDS = load('progression-star-rewards')
GUIDES = load('progression-guides')
STORIES = load('progression-story-info')


def task_engine():
    """Explicit optional dependency on the local task engine module.

    Module availability is the module itself, never another domain's registered
    protocol name: the task engine owns TaskProto:GetReward, and the progression
    paths below only want its advance_tasks API.
    """
    return sys.modules.get('handlers.tasks')

# Chinese branch of GuideBehaviour.lua; scene/view input remains a trusted UI event.
GUIDE_CLEAR = {60: 1001, 105: 1002, 110: 1002, 113: 1002, 115: 1005, 120: 1005,
               130: 1114, 1090: 1001, 1140: 1002}
GUIDE_PRIOR = {113: 110, 140: 130}
# Local training scenes 8001/8002/8003 can start entirely inside the frontend;
# their context is a trusted UI event and has no server encounter to inspect.
GUIDE_CONTEXT = {1005: 1001, 1030: 1006, 1040: 1104, 1050: 1109, 1060: 1111, 1065: 1119}

def guide_groups():
    """Known guide groups keyed to their source rows (one row per tutorial step)."""
    groups = {}
    for row in GUIDES.values():
        group = row.get('group', 0)
        if group:
            groups.setdefault(group, []).append(row)
    return groups


def validate_earned_guide(state, groups, group, done):
    """Original per-group requirements for one tutorial the player actually played."""
    first = min(groups[group], key=lambda row: row['id'])
    candidates = [row for rows in groups.values() for row in rows
                  if row.get('line', 0) == first.get('line', 0) and row['group'] not in done]
    if min(candidates, key=lambda row: row['id'])['group'] != group:
        raise StorageError('Earlier tutorial on this guide line is unfinished')
    if state['player']['level'] < int(first.get('lv', 0)):
        raise StorageError('Tutorial commander level requirement is unmet')
    cleared = set(state.get('progress', {}).get('cleared_stages', []))
    if group in GUIDE_CLEAR and GUIDE_CLEAR[group] not in cleared:
        raise StorageError('Tutorial required stage has not been cleared')
    if group in GUIDE_PRIOR and GUIDE_PRIOR[group] not in done:
        raise StorageError('Tutorial prerequisite group is unfinished')
    history = state.get('progress', {}).get('battle_history', [])
    current = state.get('active_battle', {}).get('stage_id') or (history[-1]['id'] if history else None)
    if group in GUIDE_CONTEXT and GUIDE_CONTEXT[group] != current:
        raise StorageError('Tutorial does not match the local encounter context')
    for condition in first.get('condition_check', []):
        kind, target = str(condition[0]).lower(), condition[1]
        valid = (kind == 'checkdungeonpass' and target in cleared or kind == 'isguided' and target in done or
                 kind == 'getcurrdungeonid' and target == current)
        if not valid:
            raise StorageError('Tutorial configured condition is unmet or unsupported')


def validate_guide_progress(state, parsed):
    """Validate one monotonic original UI tutorial report; never infer UI actions.

    GuideMgr.Record (GuideMgr.lua:83) sends the whole {k<group>:1} dictionary after each
    group, so one new group is a completed tutorial and keeps every original per-group
    requirement. GuideMgr:SkipTo/SkipAll (GuideMgr.lua:603/:621) submit every remaining
    group in one update: that is an explicit local UI skip and must stay expressible.
    Both shapes stay monotonic, and skipped groups are recorded apart from earned ones so
    a later gate can tell them apart.

    Scene, touch and custom UI checks remain trusted local frontend events.
    """
    if not isinstance(parsed, dict):
        raise StorageError('Guide completion must be a keyed dictionary')
    groups = guide_groups()
    supplied = set()
    for key, value in parsed.items():
        if not isinstance(key, str) or re.fullmatch(r'k[1-9][0-9]*', key) is None or isinstance(value, bool) or not isinstance(value, int) or value != 1:
            raise StorageError('Invalid original guide completion entry')
        group = int(key[1:])
        if group not in groups:
            raise StorageError('Unknown tutorial group')
        supplied.add(group)
    # Read-only until every check has passed: a rejected report must leave the save
    # unchanged so the client can resend the accumulated dictionary.
    existing = state.get('progress', {})
    done = set(existing.get('completed_guides', []))
    if not done.issubset(supplied):
        raise StorageError('Completed tutorials cannot be removed')
    skipped = set(existing.get('skipped_guides', []))
    if not skipped.issubset(supplied):
        raise StorageError('Skipped tutorials cannot be removed')
    added = sorted(supplied - done)
    if len(added) == 1:
        validate_earned_guide(state, groups, added[0], done)
    else:
        # Several new groups in one report is GuideMgr:SkipTo / SkipAll: the client
        # explicitly skipped the remaining tutorial. Level, cleared-stage, encounter and
        # configured-condition rules describe a tutorial the player actually played, so a
        # batch is recorded as skipped instead of being promoted to earned silently.
        skipped.update(added)
    data = progress(state)
    data['completed_guides'] = sorted(done | set(added))
    if skipped:
        data['skipped_guides'] = sorted(skipped)
    return data['completed_guides']

def progress(state):
    data = state['progress']
    for key in ['cleared_stages', 'claimed_rewards', 'completed_guides', 'mainLine']:
        data.setdefault(key, [])
    return data

def experience_resource(stage, section=None):
    """Source daily experience type, not an ID-range or a special-mode bypass."""
    if stage.get('type') != 104:
        return False
    if section is None:
        from access_policy import sections
        section = sections().get(stage.get('group'))
    return bool(section and section.get('group') == 2 and section.get('type') == 5)


def gate(state, stage_id, tactical=False):
    stage = STAGES.get(str(stage_id))
    if not stage:
        raise StorageError('Unknown locally configured stage')
    data = progress(state)
    from access_policy import enabled, activity_stage, sections
    from handlers.initialization import conditions_open, cleared_stages
    unrestricted = enabled(state, 'activities') and activity_stage(stage)
    passed = cleared_stages(state)
    section = sections().get(stage.get('group'))
    if section and not unrestricted and not conditions_open(state, section.get('conditions', [])):
        raise StorageError('Chapter prerequisite has not been met')
    if section and section.get('group') == 2 and section.get('openTime') and not unrestricted:
        from handlers.initialization import local_time
        from datetime import datetime, timezone, timedelta
        weekday = datetime.fromtimestamp(local_time(state), timezone(timedelta(hours=8))).weekday()
        if section['openTime'][weekday] != 1:
            raise StorageError('Daily stage is closed today')
    unrestricted = unrestricted or stage_id in passed
    if not unrestricted and int(state['player']['level']) < int(stage.get('LockLevel', 1)):
        raise StorageError('Stage requires a higher commander level')
    if not unrestricted and any(int(key) not in passed for key in stage.get('preChapterID', [])):
        raise StorageError('Previous stage has not been cleared')
    if not unrestricted and stage.get('LockMission'):
        from handlers.tasks import guide_day
        if any(kind != 16 or value >= guide_day(state) for kind, value in stage['LockMission']):
            raise StorageError('Stage guide-task phase prerequisite is unmet')
    supported_types = [8] if tactical else [1, 2, 3, 4, 99]
    if not tactical and experience_resource(stage, section):
        supported_types.append(104)
    if stage.get('type') not in supported_types:
        raise StorageError('This special mode has not been reconstructed yet')
    if any(stage.get('star' + str(i), [0])[0] not in (range(1, 10) if tactical else [1, 2, 3]) for i in range(1, 4)):
        raise StorageError('This stage rating condition is not reconstructed yet')
    return stage

def complete_story(tx, stage_id):
    stage = gate(tx.state, stage_id)
    if stage.get('sub_type') != 1 or not stage.get('storyID'):
        raise StorageError('This request is not a configured story-only completion')
    if stage.get('enterCostHot', 0) or stage.get('winCostHot', 0):
        raise StorageError('Story-only energy charging is not reconstructed for this configuration')
    cfg = STORIES.get(str(stage['storyID']))
    saved = tx.state.get('client_data', {}).get('plot_data', {})
    if not cfg or saved.get('type') != 3:
        raise StorageError('Missing original plot completion data')
    try:
        parsed = json.loads(saved['data'])
    except (KeyError, ValueError, TypeError) as error:
        raise StorageError('Invalid saved plot completion data') from error
    line = cfg.get('line', cfg.get('storyType', 1))
    value = parsed.get('line_' + str(line)) if isinstance(parsed, dict) else None
    actual = STORIES.get(str(value))
    if isinstance(value, bool) or not isinstance(value, int) or value < stage['storyID'] or not actual or actual.get('line', actual.get('storyType', 1)) != line:
        raise StorageError('Plot record does not match this story line')
    if tx.state.get('active_battle'):
        raise StorageError('Cannot complete a story while a battle session is active')
    data = progress(tx.state)
    deltas, equips, rewards = {}, [], []
    first_key = 'first:' + str(stage_id)
    if first_key not in data['claimed_rewards']:
        rewards = grant(tx, explicit_rewards(stage.get('fisrtPassReward', [])), deltas, equips)
        data['claimed_rewards'].append(first_key)
    if stage_id not in data['cleared_stages']:
        data['cleared_stages'].append(stage_id)
    row = {'id': stage_id, 'star': 1, 'data': [1, 0, 0], 'isHisPass': 1}
    if not any(item['id'] == stage_id for item in data['mainLine']):
        data['mainLine'].append(row)
    first_clear = stage_id not in data.setdefault('completed_story_stages', [])
    if first_clear:
        data['completed_story_stages'].append(stage_id)
    replies = gacha.updates(tx, [], deltas)
    replies.append(Reply('LoginProto:PlrUpdate', {'infos': deepcopy(tx.state['player'])}))
    replies.append(Reply('PlayerProto:CardUpdate', {'cards': [], 'store_exp': int(tx.state.get('store_exp', 0))}))
    replies.append(Reply('PlayerProto:DuplicateData', {'mainLine': [row], 'is_finish': True}))
    if equips:
        replies.append(Reply('EquipProto:EquipAdd', {'equips': equips, 'cur_size': len(tx.state['equips']),
                'max_size': int(tx.state.get('max_equip_size', 500)), 'is_finish': True}))
    if rewards:
        replies.append(Reply('ClientProto:RewardNotice', {'rewards': rewards, 'is_finish': True}))
    replies.append(Reply('FightProto:AskQuitDuplicate', {'ret': True, 'fisrt3StarReward': [], 'reward': []}))
    tasks = task_engine()
    if first_clear and tasks is not None:
        replies.extend(tasks.advance_tasks(tx.state, 'stage_clear', 1, stage_id, metadata={'story': True}))
    return replies

def stage_grades(stage, won, stats):
    # DungeonUtil.GetStarInfo2 consumes completion flags, not raw death/turn counts.
    result = []
    if not stage.get('nGroupID') and stage.get('sub_type') != 1:
        values = {1: int(won), 2: stats['map_normal_kills'], 3: stats['map_elite_kills'],
                  4: stats['map_kills'], 5: stats['map_moves'], 6: stats['map_boxes'],
                  7: stats['map_normal_kills'], 8: stats['map_deaths'], 9: stats['map_teams']}
        for index in range(1, 4):
            kind, target = stage['star' + str(index)]
            value = values[kind]
            result.append(int(won and (value <= target if kind in [5, 8, 9] else value >= target)))
        return result
    for index in range(1, 4):
        kind, target = stage['star' + str(index)]
        if kind == 1:
            passed = won
        elif kind == 2:
            passed = won and stats['deathCnt'] <= target
        elif kind == 3:
            passed = won and stats['turnNum'] <= target
        else:
            raise StorageError('Unreconstructed stage rating condition')
        result.append(int(passed))
    return result

def pick(state, rows, key='weight'):
    if not rows:
        raise StorageError('Empty local reward distribution')
    data = gacha.gacha_state(state)
    weights = [Decimal(str(row.get(key, row.get('probability', 1) if key == 's_probability' else 1))) for row in rows]
    scale = Decimal(10) ** min(30, max(max(0, -v.as_tuple().exponent) for v in weights))
    counts = [max(0, int(v * scale)) for v in weights]
    number = gacha.random_below(data, sum(counts))
    for row, count in zip(rows, counts):
        if number < count:
            return row
        number -= count
    raise AssertionError('Reward selection escaped range')

def resource_reward_count(state, row):
    """Local policy: uniformly sample inclusive source count/countUplimit bounds."""
    lower = row.get('count', 1)
    upper = row.get('countUplimit', lower)
    if (any(isinstance(v, bool) or not isinstance(v, int) for v in (lower, upper)) or
            not 0 <= lower <= upper <= 2147483647):
        raise StorageError('Invalid configured resource reward count range')
    return lower if upper == lower else lower + gacha.random_below(gacha.gacha_state(state), upper - lower + 1)


def reward_graph(state, reward_id, depth=0, tables=None, quantity_ranges=False):
    if depth > 16:
        raise StorageError('Deep reward nesting')
    table = (tables or REWARDS).get(str(reward_id))
    if not table:
        raise StorageError('Unknown local reward template')
    items = table.get('item', [])
    if table['type'] == 1 and all(row.get('probability') == 100 for row in items):
        chosen = items  # Fixed 100% map chest contents; uncertain independent rolls remain unsupported.
    elif table['type'] == 2:
        chosen = items  # Bundle containing each nested category.
    elif table['type'] == 3:
        chosen = [pick(state, items, 's_probability') for _ in range(int(table.get('dropCnt', 1)))]
    else:
        raise StorageError('Unreconstructed reward template algorithm')
    result = []
    for row in chosen:
        count = resource_reward_count(state, row) if quantity_ranges else int(row.get('count', 1))
        if row['type'] == 1:
            for _ in range(count):
                result.extend(reward_graph(state, row['id'], depth + 1, tables, quantity_ranges))
        else:
            result.append({'id': row['id'], 'num': count, 'type': row['type']})
    return result

def explicit_rewards(rows):
    return [{'id': row[0], 'num': row[1], 'type': row[2] if len(row) > 2 else 2} for row in rows]

def add_equip(tx, cfgid):
    cfg = EQUIPS.get(str(cfgid))
    if not cfg:
        raise StorageError('Unknown local equipment reward')
    bag = tx.state.setdefault('equips', [])
    if len(bag) >= int(tx.state.get('max_equip_size', 200)):
        raise StorageError('Equipment bag is full')
    used = {row['sid'] for row in bag}
    sid = max(int(tx.state.get('next_equip_id', 1)), 1)
    while sid in used:
        sid += 1
    skills = []
    for rule_id in cfg.get('randSkills', []):
        rule = RAND_SKILLS.get(str(rule_id))
        if not rule:
            raise StorageError('Unreconstructed equipment skill pool')
        skill = pick(tx.state, rule['infos'])
        level = pick(tx.state, RAND_LEVELS[str(skill['lvCfgId'])]['infos'])['lv']
        skills.append(int(skill['skillId']) + int(level) - 1)
    equip = {'cfgid': cfgid, 'sid': sid, 'level': 0, 'exp': 0, 'lock': 0,
             'rand_skill_type': 0, 'rand_skill_value': 0, 'card_id': 0,
             'is_new': 1, 'num': 1, 'skills': skills}
    bag.append(equip)
    tx.state['next_equip_id'] = sid + 1
    return deepcopy(equip)

def grant(tx, rows, deltas, equips):
    rendered = []
    for row in rows:
        row = dict(row)
        if row['type'] == 2:
            if row['id'] == 10003:
                total = int(tx.state.get('store_exp', 0)) + row['num']
                if not 0 <= total <= 2147483647:
                    raise StorageError('Experience pool overflow')
                tx.state['store_exp'] = total
            elif row['id'] == 10004:
                add_player_exp(tx, row['num'])
            else:
                tx.add_item(row['id'], row['num'])
                deltas[row['id']] = deltas.get(row['id'], 0) + row['num']
            rendered.append(row)
        elif row['type'] == 4:
            for _ in range(row['num']):
                equip = add_equip(tx, row['id'])
                equips.append(equip)
                rendered.append({'id': row['id'], 'num': 1, 'type': 4, 'c_id': equip['sid'], 'eSkills': equip['skills']})
        else:
            raise StorageError('This stage reward type is not reconstructed yet')
    return rendered

def add_player_exp(tx, amount):
    player = tx.state['player']
    player['exp'] = int(player.get('exp', 0)) + int(amount)
    if not 0 <= player['exp'] <= 2147483647:
        raise StorageError('Commander experience overflow')
    while str(player['level']) in PLAYER_LEVELS:
        threshold = int(PLAYER_LEVELS[str(player['level'])].get('nNextExp', 0))
        if threshold <= 0 or player['exp'] < threshold:
            break
        player['exp'] -= threshold
        player['level'] += 1

def level_cap(break_level):
    """Card level ceiling for one reachable break level.

    cfgCfgCardBreakLimitLv.lua keys rows 1..6 with limitLv. Break level 7 is
    reachable (cfgCardBreak.lua has seven rows and cards_items.card_break stops
    at jump 6) but has no row of its own, so the top row's MaxLv is its ceiling;
    cards_items.upgrade_transaction uses the same fallback.
    """
    if isinstance(break_level, bool) or not isinstance(break_level, int) or not 1 <= break_level <= 7:
        raise StorageError('Invalid owned card break level')
    row = CARD_CAPS.get(str(break_level))
    if row is None:
        row = CARD_CAPS[max(CARD_CAPS, key=int)]
        cap = row.get('MaxLv')
    else:
        cap = row.get('limitLv')
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 90:
        raise StorageError('Missing local card level cap configuration')
    return cap

def stored_integer(value, label):
    """Integral persisted numbers only: no bool, no fraction, no container."""
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            isinstance(value, float) and not math.isfinite(value) or int(value) != value):
        raise StorageError('Invalid stored card number: ' + label)
    return int(value)

def add_experience(tx, active, stage):
    add_player_exp(tx, stage.get('plrExp', 0))
    participants = active.get('cids')
    if not isinstance(participants, list):
        raise StorageError('Active battle has no participant list')
    changed = []
    for card in tx.state['cards']:
        if not isinstance(card, dict) or card.get('cid') not in participants:
            continue
        level = stored_integer(card.get('level', 1), 'level')
        card['exp'] = stored_integer(card.get('exp', 0), 'experience') + stored_integer(stage.get('exp', 0), 'stage experience')
        cap = level_cap(card.get('break_level', 1))
        while level < cap:
            row = CARD_LEVELS.get(str(level))
            if not isinstance(row, dict):
                raise StorageError('Missing local card level configuration')
            threshold = stored_integer(row.get('exp', 0), 'level threshold')
            if threshold <= 0 or card['exp'] < threshold:
                break
            card['exp'] -= threshold
            level += 1
        card['level'] = level
        changed.append(deepcopy(card))
    return changed

def star_total(state, cfg):
    eligible = {row['id'] for row in STAGES.values() if row.get('starIx') == cfg['starIx'] and
                row.get('group') == cfg['group'] and row.get('type') == cfg['type'] and row.get('sub_type') != 1}
    return sum(min(3, max(0, int(row.get('star', 0)))) for row in state.get('progress', {}).get('mainLine', []) if row['id'] in eligible)

def star_infos(state, ids=None):
    claimed = progress(state).setdefault('star_claims', {})
    if ids is None:
        ids = [int(key) for key in claimed]
    if any(str(identifier) not in STAR_REWARDS for identifier in ids):
        raise StorageError('Unknown chapter star reward')
    # Real captured 1034 indexs uses a contiguous array of 1s (claimed prefix).
    return [{'id': identifier, 'indexs': [1] * len(claimed.get(str(identifier), []))} for identifier in ids]

@register('ClientProto:DupSumStarRewardInfo')
async def get_star_rewards(ctx, fields):
    uid = ctx.require_login()
    with ctx.store.transaction(uid) as tx:
        infos = star_infos(tx.state, fields.get('ids') or None)
    return [Reply('ClientProto:DupSumStarRewardInfoRet', {'infos': infos})]

@register('ClientProto:GetDupSumStarReward')
async def claim_star_rewards(ctx, fields):
    uid = ctx.require_login()
    requested = fields.get('infos')
    if not isinstance(requested, list) or not 1 <= len(requested) <= 256:
        raise StorageError('Invalid chapter star reward batch')
    with ctx.store.transaction(uid) as tx:
        claimed = progress(tx.state).setdefault('star_claims', {})
        deltas, equips, rendered, affected = {}, [], [], set()
        # Normal UI sends all reachable unclaimed entries in order. Reject sparse claims.
        pairs = sorted({(gacha.integer(row, 'id'), gacha.integer(row, 'index')) for row in requested})
        for identifier, index in pairs:
            cfg = STAR_REWARDS.get(str(identifier))
            row = next((value for value in cfg.get('arr', []) if value['index'] == index), None) if cfg else None
            if not row:
                raise StorageError('Unknown chapter reward tier')
            got = claimed.setdefault(str(identifier), [])
            affected.add(identifier)
            if index in got:
                continue
            if index != len(got) + 1:
                raise StorageError('Chapter reward claims must follow their configured order')
            if star_total(tx.state, cfg) < row['starNum']:
                raise StorageError('Insufficient chapter stars')
            rendered.extend(grant(tx, explicit_rewards(row.get('rewards', [])), deltas, equips))
            got.append(index)
        replies = gacha.updates(tx, [], deltas)
        if rendered:
            replies.append(Reply('PlayerProto:CardUpdate', {'cards': [], 'store_exp': int(tx.state.get('store_exp', 0))}))
            replies.append(Reply('LoginProto:PlrUpdate', {'infos': deepcopy(tx.state['player'])}))
            if equips:
                replies.append(Reply('EquipProto:EquipAdd', {'equips': equips, 'cur_size': len(tx.state['equips']),
                    'max_size': int(tx.state.get('max_equip_size', 500)), 'is_finish': True}))
            replies.append(Reply('ClientProto:RewardNotice', {'rewards': rendered, 'is_finish': True}))
        replies.append(Reply('ClientProto:DupSumStarRewardInfoRet', {'infos': star_infos(tx.state, sorted(affected))}))
        tasks = task_engine()
        if rendered and tasks is not None:
            replies.extend(tasks.advance_tasks(tx.state, 'state_changed', 0))
    return replies

def settle(tx, active, won, grade, stats):
    stage = STAGES[str(active['stage_id'])]
    data = progress(tx.state)
    dup_grade = stage_grades(stage, won, stats)
    star = sum(dup_grade)
    deltas, equips, ordinary, first, third = {}, [], [], [], []
    changed = []
    if won:
        win_cost = abs(int(stage.get('winCostHot', 0)))
        if tx.currency('hot') < win_cost:
            raise StorageError('Insufficient remaining energy to complete this stage')
        tx.add_currency('hot', -win_cost)
        ordinary = grant(tx, reward_graph(tx.state, stage['reward'],
            quantity_ranges=experience_resource(stage)) if stage.get('reward') else [], deltas, equips)
        first_key, third_key = 'first:' + str(stage['id']), 'three:' + str(stage['id'])
        if first_key not in data['claimed_rewards']:
            first = grant(tx, explicit_rewards(stage.get('fisrtPassReward', [])), deltas, equips)
            data['claimed_rewards'].append(first_key)
        if star == 3 and third_key not in data['claimed_rewards']:
            third = grant(tx, explicit_rewards(stage.get('fisrt3StarReward', [])), deltas, equips)
            data['claimed_rewards'].append(third_key)
        gold = int(stage.get('gold', 0))
        tx.add_currency('gold', gold)
        deltas[10001] = deltas.get(10001, 0) + gold
        changed = add_experience(tx, active, stage)
        if stage['id'] not in data['cleared_stages']:
            data['cleared_stages'].append(stage['id'])
        previous = next((row for row in data['mainLine'] if row['id'] == stage['id']), None)
        best_grade = previous['data'] if previous and previous['star'] > star else list(dup_grade)
        current = {'id': stage['id'], 'star': max(star, previous['star'] if previous else 0),
                   'nGrade': list(grade), 'data': list(best_grade), 'isHisPass': 1}
        if previous:
            previous.update(current)
        else:
            data['mainLine'].append(current)
    data.setdefault('battle_history', []).append({'id': active['stage_id'], 'win': won,
            'star': star, 'time': gacha.now(tx.state), 'cids': active['cids']})
    tx.state.pop('active_battle', None)
    replies = []
    tasks = task_engine()
    if won and tasks is not None:
        replies.extend(tasks.advance_tasks(tx.state, 'stage_clear', 1, stage['id'],
                metadata=dict(stats, grade=list(dup_grade))))
    if changed:
        replies.append(Reply('PlayerProto:CardUpdate', {'cards': changed, 'store_exp': int(tx.state.get('store_exp', 0))}))
    if equips:
        replies.append(Reply('EquipProto:EquipAdd', {'equips': equips, 'cur_size': len(tx.state['equips']),
                'max_size': int(tx.state.get('max_equip_size', 200)), 'is_finish': True}))
    replies.extend(gacha.updates(tx, [], deltas))
    replies.append(Reply('LoginProto:PlrUpdate', {'infos': deepcopy(tx.state['player']), 'add_exp': int(stage.get('plrExp', 0)) if won else 0}))
    if won:
        replies.append(Reply('PlayerProto:DuplicateData', {'mainLine': [current], 'is_finish': True}))
    payload = {'bIsWin': won, 'id': stage['id'], 'star': star,
            'nGrade': grade, 'nDupGrade': dup_grade,
            'reward': ordinary, 'rewardId': int(stage.get('reward', 0)),
            'fisrtPassReward': first, 'fisrt3StarReward': third,
            'exp': int(stage.get('exp', 0)) if won else 0, 'gold': int(stage.get('gold', 0)) if won else 0,
            'nPlayerExp': int(stage.get('plrExp', 0)) if won else 0,
            'cards': [{'id': cid, 'num': int(stage.get('exp', 0)) if won else 0} for cid in active['cids']],
            'cardsExp': [], 'sectionMultiReward': [], 'passivBufReward': []}
    replies.append(Reply('FightProto:FightOver', payload))
    # Direct battle bypasses a tactical-map phase, so completion is one duplicate.
    replies.append(Reply('FightProto:DuplicateOver', {'bIsWin': won, 'id': stage['id'], 'star': star,
        'nGrade': list(dup_grade), 'data': payload['nDupGrade'], 'fisrtPassReward': first, 'fisrt3StarReward': third,
        'rewardId': payload['rewardId'], 'reward': [], 'exp': payload['exp'], 'gold': payload['gold'],
        'nPlayerExp': payload['nPlayerExp'], 'specialReward': []}))
    return replies
