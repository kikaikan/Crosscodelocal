"""Offline construction. Wire recovered; random/pity policies are local reconstructions.
No payment or outgoing network code.
"""
from copy import deepcopy
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import secrets
import sys
import time
from config_codec import app_path
from database import StorageError
from access_policy import enabled
from card_roles_service import build_card_skills
from server_core import Reply, register

DATA = app_path('data')
def load(name):
    return json.loads((DATA / ('gacha-' + name + '.json')).read_text(encoding='utf-8'))
POOLS, REWARDS, CARDS = load('pools'), load('rewards'), load('cards')
# The admin/mail role catalog adds actual source card templates only. Draw
# candidates still come exclusively from each original pool's reward leaves.
CARDS.update(json.loads((DATA / 'admin-role-templates.json').read_text('utf-8')))
COMPENSATION, OPEN_RULES = load('compensation'), load('open-rules')
POLICY = load('policy')
PAGE_SIZE = POLICY['page_size']

def integer(fields, key, default=None):
    value = fields.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError('Gacha field must be an integer: ' + key)
    return value

def now(state):
    return int(state.get('offline_clock', time.time()))

def gacha_state(state):
    if 'gacha' not in state:
        state['gacha'] = {'version': 1, 'rng_seed': secrets.token_hex(32), 'rng_counter': 0,
                         'pools': {}, 'selection': {}, 'choice': {}, 'history': [], 'free_cnt': 0}
    data = state['gacha']
    data.setdefault('pity', {})
    day = (now(state) + 5 * 3600) // 86400  # Beijing 03:00 reset.
    if data.get('day') != day:
        data.update(day=day, daily_use_cnt=0)
    return data

def pool_state(data, pool_id):
    return data['pools'].setdefault(str(pool_id), {'count': 0, 'misses': {}, 'tries': 0,
                                               'affirmed': False, 'logs': []})

def check_pool(state, pool_id):
    pool = POOLS.get(str(pool_id))
    if not pool:
        raise StorageError('Unknown local card pool')
    cleared = {int(v) for v in state.get('progress', {}).get('cleared_stages', [])}
    for row in state.get('progress', {}).get('mainLine', []):
        if row.get('star', 0) > 0 or row.get('is_pass', False):
            cleared.add(int(row.get('id', row.get('dupId', row.get('dupID', 0)))))
    for cid in ([] if enabled(state, 'pools') else pool.get('conditions', [])):
        rule = OPEN_RULES[str(cid)]
        if rule['type'] == 1:
            met = int(state['player'].get('level', 0)) >= int(rule['val'])
        elif rule['type'] == 2:
            met = int(rule['val']) in cleared
        else:
            raise StorageError('Unreconstructed gacha unlock condition')
        if not met:
            raise StorageError('Card pool is locked by progression')
    stamp = now(state)
    if not enabled(state, 'pools') and pool_id not in state.get('offline_archive_pools', []):
        if pool['nType'] == 4:
            start = int(state['player'].get('create_time', stamp))
            if start < int(pool.get('nPlrCreateTime', 0)) or stamp >= start + int(pool.get('duration', 0)) * 60:
                raise StorageError('This first-login pool is unavailable or expired')
        elif (int(pool.get('nStart', 0)) and stamp < pool['nStart'] or
              int(pool.get('nEnd', 0)) and stamp >= pool['nEnd']):
            raise StorageError('Card pool is outside its local schedule')
    data = gacha_state(state)
    progress = pool_state(data, pool_id)
    if int(pool.get('nUseCntLimt', 0)) and progress['count'] >= pool['nUseCntLimt']:
        raise StorageError('Card pool construction limit reached')
    return pool, data, progress

def random_below(data, maximum):
    if maximum < 1 or maximum > 1 << 256:
        raise StorageError('Invalid local gacha distribution')
    ceiling = (1 << 256) - ((1 << 256) % maximum)
    while True:
        count = int(data['rng_counter'])
        block = hashlib.sha256(bytes.fromhex(data['rng_seed']) + count.to_bytes(16, 'big')).digest()
        data['rng_counter'] = count + 1
        value = int.from_bytes(block, 'big')
        if value < ceiling:
            return value % maximum

def leaves(reward_id, path=()):
    if len(path) > 16 or reward_id in path:
        raise StorageError('Cyclic or deep card reward graph')
    result = []
    for item in REWARDS[str(reward_id)].get('item', []):
        item_path = path + (reward_id,)
        if item['type'] == 1:
            result.extend(leaves(item['id'], item_path))
        elif item['type'] == 3:
            # Absolute leaf weights; wrapper display probabilities are not multiplied.
            weight = Decimal(str(item.get('s_probability', '1')))
            if weight > 0:
                result.append({'cfgid': item['id'], 'weight': weight, 'path': item_path})
        else:
            raise StorageError('Unreconstructed card reward type')
    return result

def guarantees(reward_id, visited=None):
    visited = set() if visited is None else visited
    if reward_id in visited:
        return []
    visited.add(reward_id)
    result = []
    for item in REWARDS[str(reward_id)].get('item', []):
        if item['type'] != 1:
            continue
        if item.get('mustUseCnt'):
            minimum = min(CARDS[str(row['cfgid'])]['quality'] for row in leaves(item['id']))
            result.append((int(item['mustUseCnt']), minimum, item['id']))
        result.extend(guarantees(item['id'], visited))
    return result

def weighted_pick(data, candidates):
    candidates = [row for row in candidates if row['weight'] > 0]
    if not candidates:
        raise StorageError('Empty local gacha distribution')
    places = min(40, max(max(0, -row['weight'].as_tuple().exponent) for row in candidates))
    scale = Decimal(10) ** places
    weights = [max(1, int(row['weight'] * scale)) for row in candidates]
    value = random_below(data, sum(weights))
    for row, weight in zip(candidates, weights):
        if value < weight:
            return int(row['cfgid'])
        value -= weight
    raise AssertionError('Weighted selection escaped distribution')

def selection(data, pool):
    key = str(pool.get('sel_quality_type', pool['id']))
    index = int(pool.get('def_sel_card_ix', 0))
    cid = pool.get('sel_card_ids', [])[index - 1] if index else 0
    return data['selection'].setdefault(str(pool['id']), {'cid': cid, 'group': key}), key

def pity_group(pool):
    rules = pool.get('cardRule', [])
    for rule, name in [(17110, 'core'), (17123, 'alpha'), (17138, 'rotation'),
                       (17171, 'limited'), (17175, 'choice')]:
        if rule in rules:
            return name
    return 'pool:' + str(pool['id'])

def draw(state, pool, data, progress, ordinal, preview=False, minimum=0):
    with localcontext() as decimal_context:
        decimal_context.prec = 80
        return draw_precise(state, pool, data, progress, ordinal, preview, minimum)

def draw_precise(state, pool, data, progress, ordinal, preview=False, minimum=0):
    mapping = {int(pair[0]): int(pair[1]) for pair in pool['jCardsId']}
    reward_id = mapping.get(-1, mapping[0]) if preview else mapping.get(ordinal, mapping[0])
    candidates = leaves(reward_id)
    group = 'preview:' + str(pool['id']) if preview else pity_group(pool)
    pity = data['pity'].setdefault(group, {'miss_six': 0})
    qualities = {CARDS[str(row['cfgid'])]['quality'] for row in candidates}
    # The one-card first reward bypasses probability and pity rolls.
    if len(candidates) == 1:
        return candidates[0]['cfgid']
    if preview:
        constraints = guarantees(reward_id)
        forced = [quality for threshold, quality, _ in constraints
                  if int(progress['misses'].get(str(quality), 0)) + 1 >= threshold]
        minimum = max([minimum] + forced)
    elif pool['nType'] != 6 and pity['miss_six'] + 1 >= POLICY['normal_max_six']:
        minimum = 6
    rates = {int(key): Decimal(value) for key, value in
             POLICY['choice_rates' if pool['nType'] == 6 else 'normal_rates'].items()}
    if pool['nType'] == 6 and pity['miss_six'] >= POLICY['choice_soft_pity_after']:
        six = min(Decimal(1), rates[6] + Decimal(POLICY['choice_soft_pity_step']) *
                  (pity['miss_six'] - POLICY['choice_soft_pity_after'] + 1))
        for quality in [3, 4, 5]:
            rates[quality] *= (1 - six) / (1 - rates[6])
        rates[6] = six
    adjusted = []
    for quality in qualities:
        if quality < minimum:
            continue
        subset = [dict(row) for row in candidates if CARDS[str(row['cfgid'])]['quality'] == quality]
        mass = sum(row['weight'] for row in subset)
        for row in subset:
            row['weight'] = row['weight'] / mass * rates.get(quality, Decimal(0))
        adjusted.extend(subset)
    candidates = adjusted
    if pool['nType'] == 6:
        choice = data['choice'].get(str(pool['id']))
        if not choice:
            raise StorageError('Select two six-star and three five-star cards first')
        adjusted = []
        for quality in {CARDS[str(row['cfgid'])]['quality'] for row in candidates}:
            subset = [dict(row) for row in candidates if CARDS[str(row['cfgid'])]['quality'] == quality]
            wanted = [cid for cid in choice['cids'] if CARDS[str(cid)]['quality'] == quality]
            mass = sum(row['weight'] for row in subset)
            if wanted:
                for row in subset:
                    row['weight'] *= 1 - Decimal(POLICY['choice_selected_share_local'])
                adjusted.extend({'cfgid': cid, 'weight': mass * Decimal(POLICY['choice_selected_share_local']) / len(wanted)} for cid in wanted)
            adjusted.extend(subset)
        candidates = adjusted
    cid = weighted_pick(data, candidates)
    quality = CARDS[str(cid)]['quality']
    if pool.get('sel_quality_cnt'):
        chosen, group = selection(data, pool)
        misses = int(data['selection'].get('group:' + group, 0))
        if quality == int(pool['sel_quality']) and chosen['cid']:
            if misses + 1 >= int(pool['sel_quality_cnt']):
                cid = int(chosen['cid'])
            data['selection']['group:' + group] = 0 if cid == chosen['cid'] else misses + 1
    pity['miss_six'] = 0 if quality == 6 else pity['miss_six'] + 1
    for target in [5, 6]:
        key = str(target)
        progress['misses'][key] = 0 if quality >= target else int(progress['misses'].get(key, 0)) + 1
    return cid

def costs_for(pool, count, data, free_allowed=True):
    if count == 1 and free_allowed and pool.get('canFreeUse') and data.get('free_cnt', 0) > 0:
        return [], True
    pairs = pool.get('multiCost') if count == pool.get('multiCnt', 10) else pool.get('jCost')
    if not pairs:
        if pool.get('nFirstTryCnt'):
            return [], False
        raise StorageError('Missing local construction cost')
    return [{'id': int(pair[0]), 'num': int(pair[1]), 'type': 2} for pair in pairs], False

def deduct(tx, costs):
    totals = {}
    for row in costs:
        totals[row['id']] = totals.get(row['id'], 0) + row['num']
    if any(tx.item_count(cid) < amount for cid, amount in totals.items()):
        raise StorageError('Insufficient construction tickets')
    for cid, amount in totals.items():
        tx.add_item(cid, -amount)
    return {cid: -amount for cid, amount in totals.items()}

def award(tx, cfgid, pool):
    cfg = CARDS.get(str(cfgid))
    if not cfg:
        raise StorageError('Unknown source role template')
    existing = next((card for card in tx.state['cards'] if card['cfgid'] == cfgid), None)
    new = existing is None
    if new:
        if len(tx.state['cards']) >= int(tx.state.get('max_card_size', 150)):
            raise StorageError('Card storage capacity reached')
        fields = {'name': cfg['name'], 'break_level': 1, 'intensify_level': 1,
                  # sSkillData needs the client SkillMainType; the client filters on it.
                  'skills': build_card_skills(cfg.get('skills', [])),
                  'hp': cfg.get('maxhp', 1), 'cur_hot': 0, 'skin': cfg['model'], 'skin_a': cfg['model'],
                  'skinIsl2d': 0, 'skinIsl2d_a': 0, 'sub_talent': {}, 'open_cards': [], 'open_mechas': []}
        card = tx.add_card(cfgid, fields)
    else:
        if int(existing.get('get_cnt', 1)) >= 2147483647:
            raise StorageError('Card acquisition count exceeds supported integer range')
        existing['get_cnt'] = int(existing.get('get_cnt', 1)) + 1
        card = deepcopy(existing)
    count = card['get_cnt']
    role, role_id = None, int(cfg.get('role_id', cfgid))
    roles = tx.state.setdefault('card_roles', [])
    existing_role = next((row for row in roles if row['id'] == role_id), None)
    if existing_role is None:
        role = {'id': role_id, 'data': {'lv': 100, 'b_lv': 1, 'tf': 0, 'exp': 0, 'tv': 1,
                't_create': now(tx.state), 'build_id': 0, 'clothes': [], 'abilitys': {},
                'audio': {}, 'new': True, 'story_ids': [], 'look_skins': []}}
        roles.append(role)
    elif new and int(existing_role.get('data', {}).get('b_lv', 0)) < 1:
        # Some commander/archive rows exist before their actual card is owned.
        # Push the repaired row so the base board skin unlocks immediately.
        existing_role['data']['b_lv'] = 1
        role = existing_role
    table = COMPENSATION[int(cfg['quality']) - 1]
    rule = next(row for row in table['infos'] if row['minGetCnt'] <= count <= row['maxGetCnt'])
    elem, items = int(rule.get('elemNum', 0)), []
    if pool['nType'] == 6:
        elem = 1 if 2 <= count <= 9 else 0
    if elem and cfg.get('coreItemId'):
        items.append({'id': cfg['coreItemId'], 'num': elem, 'type': 2})
    if pool['nType'] == 6:
        amount = (POLICY['choice_first_compensation'] if count == 1 else
                  POLICY['choice_repeat_compensation'][str(cfg['quality'])][0 if count < 10 else 1])
        items.append({'id': POLICY['choice_item']['id'], 'num': amount, 'type': 2})
    else:
        items.extend({'id': pair[0], 'num': pair[1], 'type': pair[2] if len(pair) > 2 else 2}
                     for pair in rule.get('reward', []))
    for row in items:
        tx.add_item(row['id'], row['num'])
    return {'id': card['cid'], 'num': count, 'items': items}, card, new, role

def factory(state):
    data = gacha_state(state)
    first, selected, counts, dynamic, choices = {}, {}, [], {}, {}
    for pool in POOLS.values():
        progress = pool_state(data, pool['id'])
        counts.append({'id': pool['id'], 'num': progress['count']})
        if pool.get('nFirstTryCnt'):
            first[pool['id']] = {'card_pool_id': pool['id'], 'first_10': progress['affirmed'], 'had_try_cnt': progress['tries']}
        if pool.get('sel_quality_cnt'):
            chosen, group = selection(data, pool)
            selected[pool['id']] = {'id': pool['id'], 'num': chosen['cid'], 'type': int(data['selection'].get('group:' + group, 0))}
            selected[int(group)] = {'id': int(group), 'num': chosen['cid'], 'type': int(data['selection'].get('group:' + group, 0))}
        if enabled(state, 'pools') or pool['id'] in state.get('offline_archive_pools', []):
            # CreateMgr passes this value to CreateData.InitDyOpenPool, which
            # overrides BOTH the original start and end date. No probabilities,
            # costs, progression conditions or historical counts are changed.
            dynamic[pool['id']] = [4102444800]  # 2100-01-01 UTC; uint32-safe.
        elif pool.get('nStart') or pool.get('nEnd'):
            # Explicit closed values clear a previously archived dynamic window
            # on the existing Lua consumer, including a not-yet-started pool.
            stamp = now(state)
            opened = (not pool.get('nStart') or stamp >= pool['nStart']) and (not pool.get('nEnd') or stamp < pool['nEnd'])
            dynamic[pool['id']] = [int(pool.get('nEnd') or 4102444800) if opened else max(0, stamp - 86400)]
        elif pool['nType'] == 4:
            dynamic[pool['id']] = [int(state['player']['create_time']) + pool['duration'] * 60]
        if str(pool['id']) in data['choice']:
            choices[pool['id']] = deepcopy(data['choice'][str(pool['id'])])
    return {'firt_create_infos': first, 'sel_infos': selected, 'daily_use_cnt': data['daily_use_cnt'],
            'sum_pool_cnts': counts, 'create_cnts': {row['id']: row for row in counts},
            'dy_open_pool': dynamic, 'free_cnt': int(data['free_cnt']), 'buildings': [],
            'waitings': [], 'finishs': [], 'choice_infos': choices}

def updates(tx, awards, deltas):
    new_cards, changed, roles = [], [], []
    for result, card, new, role in awards:
        (new_cards if new else changed).append(card)
        if role:
            roles.append(role)
        for row in result['items']:
            deltas[row['id']] = deltas.get(row['id'], 0) + row['num']
    replies = []
    if new_cards:
        replies.append(Reply('PlayerProto:CardAdd', {'cards': new_cards, 'cur_size': len(tx.state['cards']),
                             'max_size': int(tx.state.get('max_card_size', 150)), 'finish': True}))
    if changed:
        replies.append(Reply('PlayerProto:CardUpdate', {'cards': changed, 'store_exp': int(tx.state.get('store_exp', 0))}))
    if roles:
        replies.append(Reply('PlayerProto:AddCardRole', {'roles': roles}))
    if deltas:
        replies.append(Reply('PlayerProto:ItemUpdate', {'data': [{'id': cid, 'add': delta,
                'num': tx.item_count(cid), 'time': 0, 'ix': 0, 'expiry': 0, 'get_infos': {}}
                for cid, delta in deltas.items()]}))
    return replies

def task_updates(state, count, pool_id):
    tasks = sys.modules.get('handlers.tasks')
    return tasks.advance_tasks(state, 'card_create', count, pool_id) if tasks is not None else []

def record(state, pool_id, cfgids):
    gacha_state(state)['history'].append({'t': now(state), 'cfgIds': list(cfgids), 'pool_id': pool_id})

@register('PlayerProto:CardFactoryInfo')
async def card_factory(ctx, fields):
    ctx.require_login()
    with ctx.store.transaction(ctx.uid) as tx:
        reply = factory(tx.state)
    return [Reply('PlayerProto:CardFactoryInfoRet', reply)]

@register('PlayerProto:CardCreate')
async def card_create(ctx, fields):
    ctx.require_login()
    pool_id, count = integer(fields, 'card_pool_id'), integer(fields, 'cnt')
    with ctx.store.transaction(ctx.uid) as tx:
        pool, data, progress = check_pool(tx.state, pool_id)
        if count not in {1, int(pool.get('multiCnt', 10))}:
            raise StorageError('Only configured single or multi construction is supported')
        if pool.get('nFirstTryCnt') and not progress['affirmed']:
            raise StorageError('Use the starter preview and confirm flow')
        if pool.get('nUseCntLimt') and progress['count'] + count > pool['nUseCntLimt']:
            raise StorageError('Construction would exceed this pool limit')
        costs, free = costs_for(pool, count, data)
        deltas = deduct(tx, costs)
        cfgids = []
        for i in range(count):
            minimum = 5 if count > 1 and i == count - 1 and not any(CARDS[str(cid)]['quality'] >= 5 for cid in cfgids) else 0
            cfgids.append(draw(tx.state, pool, data, progress, progress['count'] + i + 1, minimum=minimum))
        awards = [award(tx, cid, pool) for cid in cfgids]
        progress['count'] += count
        data['daily_use_cnt'] += count
        if data['daily_use_cnt'] > 65535:
            raise StorageError('Daily count exceeds the recovered wire field')
        if free:
            data['free_cnt'] -= 1
        record(tx.state, pool_id, cfgids)
        replies = updates(tx, awards, deltas)
        replies.extend(task_updates(tx.state, count, pool_id))
        replies.append(Reply('PlayerProto:CardFactoryInfoRet', factory(tx.state)))
        replies.append(Reply('PlayerProto:CardCreateFinishRet', {'infos': [row[0] for row in awards],
                'cnt': count, 'create_cnt': progress['count'], 'quality_up': [],
                'card_pool_id': pool_id, 'daily_use_cnt': data['daily_use_cnt'], 'costs': costs}))
    return replies

def first_logs(state, requested=None):
    data, result = gacha_state(state), {}
    for key, progress in data['pools'].items():
        pool_id = int(key)
        if not POOLS[key].get('nFirstTryCnt') or requested is not None and requested != pool_id:
            continue
        row = {'card_pool_id': pool_id, 'logs': deepcopy(progress['logs'])}
        if progress.get('last_op'):
            row['last_op'] = deepcopy(progress['last_op'])
        result[pool_id] = row
    return result

@register('PlayerProto:FirstCardCreate')
async def first_create(ctx, fields):
    ctx.require_login()
    pool_id = integer(fields, 'card_pool_id')
    with ctx.store.transaction(ctx.uid) as tx:
        pool, data, progress = check_pool(tx.state, pool_id)
        if not pool.get('nFirstTryCnt') or progress['affirmed'] or progress['tries'] >= pool['nFirstTryCnt']:
            raise StorageError('Starter previews exhausted or already confirmed')
        cfgids = [draw(tx.state, pool, data, progress, i + 1, preview=True) for i in range(pool.get('multiCnt', 10))]
        costs, _ = costs_for(pool, pool.get('multiCnt', 10), data, free_allowed=False)
        progress['tries'] += 1
        progress['last_op'] = {'rewards': [{'id': cid, 'num': 1, 'type': 3} for cid in cfgids], 'costs': costs, 'quality_up': []}
        replies = [Reply('PlayerProto:FirstCardCreateRet', {'card_pool_id': pool_id,
                    'create_cnt': progress['tries'], 'hadGetLog': progress['last_op']['rewards'], 'last_op': deepcopy(progress['last_op'])}),
                   Reply('PlayerProto:CardFactoryInfoRet', factory(tx.state)),
                   Reply('PlayerProto:FirstCardCreateLogsRet', {'logs': first_logs(tx.state, pool_id)})]
    return replies

@register('PlayerProto:FirstCardCreateLogs')
async def get_first_logs(ctx, fields):
    ctx.require_login()
    requested = integer(fields, 'card_pool_id') if 'card_pool_id' in fields else None
    with ctx.store.transaction(ctx.uid) as tx:
        if requested is not None and str(requested) not in POOLS:
            raise StorageError('Unknown card pool')
        reply = first_logs(tx.state, requested)
    return [Reply('PlayerProto:FirstCardCreateLogsRet', {'logs': reply})]

@register('PlayerProto:FirstCardCreateAddLog')
async def save_first_log(ctx, fields):
    ctx.require_login()
    pool_id = integer(fields, 'card_pool_id')
    with ctx.store.transaction(ctx.uid) as tx:
        pool, _, progress = check_pool(tx.state, pool_id)
        if progress['affirmed'] or not progress.get('last_op'):
            raise StorageError('No unconfirmed starter preview to save')
        progress['logs'].append(progress.pop('last_op'))
        progress['logs'] = progress['logs'][-int(pool.get('nFirstLogCnt', 1)):]
        reply = first_logs(tx.state, pool_id)
    return [Reply('PlayerProto:FirstCardCreateLogsRet', {'logs': reply})]

@register('PlayerProto:FirstCardCreateAffirm')
async def affirm_first(ctx, fields):
    ctx.require_login()
    pool_id, ix = integer(fields, 'card_pool_id'), integer(fields, 'ix', 0)
    with ctx.store.transaction(ctx.uid) as tx:
        pool, data, progress = check_pool(tx.state, pool_id)
        if progress['affirmed']:
            raise StorageError('Starter rewards were already claimed')
        candidate = progress.get('last_op') if ix == 0 else (progress['logs'][ix - 1] if 0 < ix <= len(progress['logs']) else None)
        if not candidate:
            raise StorageError('Invalid starter reward candidate')
        costs = deepcopy(candidate['costs'])
        deltas = deduct(tx, costs)
        cfgids = [row['id'] for row in candidate['rewards']]
        awards = [award(tx, cid, pool) for cid in cfgids]
        progress.update(affirmed=True, count=progress['count'] + len(cfgids), logs=[])
        progress.pop('last_op', None)
        data['daily_use_cnt'] += len(cfgids)
        record(tx.state, pool_id, cfgids)
        replies = updates(tx, awards, deltas)
        replies.extend(task_updates(tx.state, len(cfgids), pool_id))
        replies.extend([Reply('PlayerProto:CardFactoryInfoRet', factory(tx.state)),
            Reply('PlayerProto:FirstCardCreateLogsRet', {'logs': first_logs(tx.state, pool_id)}),
            Reply('PlayerProto:FirstCardCreateAffirmRet', {'card_pool_id': pool_id, 'ix': ix,
                'daily_use_cnt': data['daily_use_cnt'], 'create_cnt': progress['count'], 'costs': costs})])
    return replies

@register('PlayerProto:SetCardPoolSelCard')
async def select_target(ctx, fields):
    ctx.require_login()
    pool_id, cid = integer(fields, 'card_pool_id'), integer(fields, 'cid')
    with ctx.store.transaction(ctx.uid) as tx:
        pool, data, _ = check_pool(tx.state, pool_id)
        if not pool.get('sel_quality_cnt') or cid != 0 and cid not in pool.get('sel_card_ids', []):
            raise StorageError('Invalid selected guarantee card')
        chosen, group = selection(data, pool)
        if cid != chosen['cid']:
            chosen['cid'] = cid
            data['selection']['group:' + group] = 0
    return [Reply('PlayerProto:SetCardPoolSelCardRet', {'card_pool_id': pool_id, 'cid': cid})]

@register('PlayerProto:SetSelfChoiceCardPoolCard')
async def choose_cards(ctx, fields):
    ctx.require_login()
    pool_id, cids = integer(fields, 'id'), fields.get('cids')
    if not isinstance(cids, list) or any(isinstance(cid, bool) or not isinstance(cid, int) for cid in cids):
        raise StorageError('Invalid self-choice card array')
    with ctx.store.transaction(ctx.uid) as tx:
        pool, data, _ = check_pool(tx.state, pool_id)
        if (pool['nType'] != 6 or len(cids) != 5 or len(set(cids)) != 5 or
                any(cid not in pool['sel_card_ids'] for cid in cids) or
                [CARDS[str(cid)]['quality'] for cid in cids] != [6, 6, 5, 5, 5]):
            raise StorageError('Select two distinct six-star and three distinct five-star cards')
        previous = data['choice'].get(str(pool_id), {})
        reply = {'id': pool_id, 'cids': list(cids), 'firstCids': previous.get('firstCids', list(cids))}
        data['choice'][str(pool_id)] = deepcopy(reply)
    return [Reply('PlayerProto:SetSelfChoiceCardPoolCardRet', reply)]

@register('PlayerProto:GetCreateCardLogs')
async def history(ctx, fields):
    ctx.require_login()
    pool_id, skip = integer(fields, 'card_pool_id'), integer(fields, 'skip', 0)
    if str(pool_id) not in POOLS or not 0 <= skip <= 65535:
        raise StorageError('Invalid construction history page')
    state = ctx.store.get_player(ctx.uid)
    rows = [row for row in state.get('gacha', {}).get('history', []) if row['pool_id'] == pool_id][::-1]
    offset = skip * PAGE_SIZE
    return [Reply('PlayerProto:GetCreateCardLogsRet', {'card_pool_id': pool_id,
                'logs': rows[offset:offset + PAGE_SIZE], 'is_end': offset + PAGE_SIZE >= len(rows)})]
