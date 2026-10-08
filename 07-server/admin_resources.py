"""Local resource editing; caller owns the SQLite transaction.

The game-side helpers (item_allowed, resource_key strict) keep their narrow
whitelist. The local control path passes allow_any=True and may write any row of
admin-items.json into a bag balance, with risk warnings instead of refusals.
No level/experience shortcut and no paid-balance coercion.
Pushes render committed current values, allowing an outbox to coalesce changes.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import time

from config_codec import app_path
from database import StorageError
from server_core import Reply

DATA = app_path('data')
ITEMS = json.loads((DATA / 'admin-items.json').read_text('utf-8'))
SOURCE = json.loads((DATA / 'admin-source.json').read_text('utf-8'))
HOT_LEVELS = {str(row['id']): row for row in SOURCE['hot']} if isinstance(SOURCE['hot'], list) else SOURCE['hot']
INT_MAX = 2147483647
RESOURCES = {
    'gold': (10001, '星币', 'currency'),
    'diamond': (10002, '粲晶', 'currency'),
    'hot': (10035, '燃料', 'stamina'),
    'army_coin': (10010, '演习勋章', 'currency'),
    'ability_num': (10020, '战术点数', 'currency'),
    'BIND_DIAMOND': (10040, '微晶', 'currency'),
    'tp': (None, '世界首领 TP', 'stamina'),
    'store_exp': (10003, '技术点', 'experience_pool'),
}
ITEM_KEYS = {value[0]: key for key, value in RESOURCES.items() if value[0]}
# Source ITEM_TYPE (GEnum.lua:48-83). These are ordinary stackable objects,
# with use effects handled by the game's existing consumable handlers.
STACK_TYPES = {2, 4, 7, 8, 10, 11, 14, 15, 17, 22, 23, 28, 31, 32}
# Source ITEM_ID values requiring their own domain/SDK/paid state. Unlisted
# type1 currencies can be a bag balance (e.g. dorm furniture currency10013).
DOMAIN_ITEMS = {10004, 10030, 10041, 10043, 10044, 10045, 10046, 12005, 10998, 10999}
# Current cfgCfgItemExchange rules 1004/1005 award these ticket stacks, while
# the extracted cfgItemInfo union has no rows for them. Keep the exception
# narrow and source-backed; absent item metadata means the normal INT_MAX cap.
EXCHANGE_ONLY_ITEMS = {11008, 11009}
# Risk classes that no longer block an administrator write but must be reported.
# The game-side handlers keep their own item_allowed() gate, so only the local
# control path (allow_any=True) accepts these rows.
ITEM_RISKS = {
    'domain': '该物品由独立域（账号/付费/活动状态）管理，写进背包后可能不显示或不生效',
    'auto_use': '该物品会自动使用，写入后可能被立即消耗',
    'expiry': '该物品带有效期，背包条目没有到期时间，可能无法正常使用或被清理',
    'type': '该物品类型不属于普通可堆叠道具，客户端可能不显示或无法操作',
}

def integer(value, minimum=0, maximum=INT_MAX):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise StorageError('Resource integer outside supported signed range')
    return value

def item_allowed(cfgid):
    if isinstance(cfgid, bool) or not isinstance(cfgid, int):
        return False
    if cfgid in EXCHANGE_ONLY_ITEMS:
        return True
    row = ITEMS.get(str(cfgid))
    if not row or cfgid in DOMAIN_ITEMS:
        return False
    if cfgid in ITEM_KEYS:
        return True
    return (row.get('type') in STACK_TYPES or row.get('type') == 1) and not (
        row.get('auto_use') or row.get('nExpiry') or row.get('sExpiry') or row.get('expiry'))

def item_present(cfgid):
    """Any item the local table describes, plus the narrow exchange-only pair.

    This is the administrator-write gate only. item_allowed() above stays the
    narrow game-handler gate for rewards, mail and exchange, so widening the
    control page cannot change what the game itself may grant.
    """
    if isinstance(cfgid, bool) or not isinstance(cfgid, int):
        return False
    return cfgid in EXCHANGE_ONLY_ITEMS or str(cfgid) in ITEMS

def item_risks(cfgid):
    """Warning codes for an administrator write; empty for ordinary stackables."""
    row = ITEMS.get(str(cfgid))
    if not row or cfgid in ITEM_KEYS:
        return ()
    codes = []
    if cfgid in DOMAIN_ITEMS:
        codes.append('domain')
    if row.get('auto_use'):
        codes.append('auto_use')
    if row.get('nExpiry') or row.get('sExpiry') or row.get('expiry'):
        codes.append('expiry')
    if row.get('type') not in STACK_TYPES and row.get('type') != 1:
        codes.append('type')
    return tuple(codes)

def item_warnings(cfgid):
    return [{'code': code, 'message': ITEM_RISKS[code]} for code in item_risks(cfgid)]

def resource_key(key, strict=True):
    """Resolve a key. strict keeps the game-handler gate; strict=False is the
    administrator gate that accepts every item in admin-items.json."""
    if not isinstance(key, str):
        raise StorageError('Resource key must be text')
    if key in RESOURCES:
        return key, RESOURCES[key][0]
    match = re.fullmatch(r'item:([1-9][0-9]{0,9})', key)
    if not match:
        raise StorageError('Unsupported resource key')
    cfgid = integer(int(match[1]), 1)
    if strict:
        if not item_allowed(cfgid):
            raise StorageError('Item is not a supported stackable resource')
    elif not item_present(cfgid):
        raise StorageError('Item is not in the local item table')
    return ITEM_KEYS.get(cfgid, key), cfgid

def maximum(state, key, cfgid):
    if key == 'hot':
        row = HOT_LEVELS.get(str(state['player']['level']))
        if not row:
            raise StorageError('Missing configured stamina level')
        return min(32767, integer(row['max']), integer(ITEMS['10035'].get('upperLimit', INT_MAX)))
    if key == 'tp':
        return min(32767, integer(SOURCE['tp_max']))
    if key in RESOURCES:
        return INT_MAX
    return min(INT_MAX, integer(ITEMS.get(str(cfgid), {}).get('upperLimit', INT_MAX), 1))

def balance(state, key, cfgid=None):
    if key in ('ability_num', 'BIND_DIAMOND'):
        value = state['login'].get(key, 0)
    elif key == 'store_exp':
        value = state.get('store_exp', 0)
    elif key in RESOURCES:
        value = state['player'].get(key, 0)
    else:
        value = state['inventory'].get(str(cfgid), 0)
    return integer(value)

def catalog():
    from gift_service import gift_allowed, MAX_GRANT_QUANTITY
    resources = []
    for key, (cfgid, label, kind) in RESOURCES.items():
        row = {'key': key, 'label': label, 'kind': kind, 'min': 0, 'max': INT_MAX}
        if cfgid:
            row['cfgid'] = cfgid
        if key == 'hot':
            row.update(max=min(32767, max(value['max'] for value in HOT_LEVELS.values())), max_by_player_level=True)
        elif key == 'tp':
            row['max'] = SOURCE['tp_max']
        resources.append(row)
    # supported now means "the administrator may write it into a bag balance",
    # so every row of admin-items.json is selectable. mail_supported keeps the
    # game-handler gate, so mail attachment eligibility is unchanged.
    items = [{'cfgid': int(key), 'name': row.get('name', key), 'type': row['type'],
              'supported': item_present(int(key)),
              'risks': list(item_risks(int(key))),
              'mail_supported': item_allowed(int(key)) or gift_allowed(int(key)),
              'mail_max': MAX_GRANT_QUANTITY if gift_allowed(int(key)) else INT_MAX if int(key) in ITEM_KEYS else min(INT_MAX, int(row.get('upperLimit', INT_MAX))),
              'max': INT_MAX if int(key) in ITEM_KEYS else min(INT_MAX, int(row.get('upperLimit', INT_MAX)))}
             for key, row in sorted(ITEMS.items(), key=lambda pair: int(pair[0]))]
    return {'resources': resources, 'items': items,
            'supported_item_types': sorted(STACK_TYPES | {1}),
            'risk_labels': ITEM_RISKS,
            'unsupported': ['player exp/level', 'paid currency', 'card-to-bag conversion',
                            'equipment instance stats', 'skins/cosmetics rendering',
                            'auto-use effects', 'expiry countdown']}

def apply_resource(tx, key, mode, amount, allow_any=False):
    """allow_any is the local control path: any admin-items.json row plus the
    exchange-only pair. Resource keys behave identically either way."""
    canonical, cfgid = resource_key(key, strict=not allow_any)
    if mode not in ('set', 'add'):
        raise StorageError('Resource mode must be set or add')
    amount = integer(amount, -INT_MAX if mode == 'add' else 0)
    before = balance(tx.state, canonical, cfgid)
    after = amount if mode == 'set' else before + amount
    integer(after, 0, maximum(tx.state, canonical, cfgid))
    delta = after - before
    if canonical == 'store_exp':
        tx.state['store_exp'] = after
        # These rewards are canonical pool/stamina, never ordinary bag rows.
        tx.state['inventory'].pop('10003', None)
    elif canonical == 'tp':
        tx.state['player']['tp'] = after
        tx.state['player']['tpBeginTime'] = int(tx.state.get('offline_clock', time.time()))
    elif canonical in RESOURCES:
        tx.add_currency(canonical, delta)
        if canonical == 'hot':
            tx.state['inventory'].pop('10035', None)
            row = HOT_LEVELS[str(tx.state['player']['level'])]
            adds = row['adds1'] if after >= row['adds'][2] else row['adds']
            tx.state['login']['t_hot'] = (0 if after >= row['max'] else
                int(tx.state.get('offline_clock', time.time())) + int(adds[1]))
    else:
        tx.add_item(cfgid, delta)
    result = {'key': canonical, 'requested_key': key, 'cfgid': cfgid,
              'before': before, 'after': after, 'delta': delta,
              'max': maximum(tx.state, canonical, cfgid)}
    if canonical.startswith('item:'):
        result['warnings'] = item_warnings(cfgid)
    return result

def _item_row(cfgid, num):
    return {'id': cfgid, 'add': 0, 'num': integer(num), 'time': 0,
            'ix': 0, 'expiry': 0, 'get_infos': {}}

def resource_pushes(state, changed, allow_any=False):
    """changed accepts current-state outbox keys, or apply_resource results."""
    if isinstance(changed, dict):
        changed = [changed]
    keys, items = set(), {}
    for entry in changed:
        key = entry['key'] if isinstance(entry, dict) else entry
        if key == 'inventory':
            for cfgid, count in state['inventory'].items():
                items[int(cfgid)] = _item_row(int(cfgid), count)
            keys.update(('gold', 'diamond', 'army_coin', 'ability_num', 'BIND_DIAMOND'))
        else:
            canonical, cfgid = resource_key(key, strict=not allow_any)
            keys.add(canonical)
            if cfgid and canonical not in ('hot', 'store_exp'):
                items[cfgid] = _item_row(cfgid, balance(state, canonical, cfgid))
    replies = []
    # Bounded chunks remain below the recovered int16 frame size even for an
    # inventory-wide repeated-card compensation refresh.
    values = [items[key] for key in sorted(items)]
    for start in range(0, len(values), 128):
        replies.append(Reply('PlayerProto:ItemUpdate', {'data': values[start:start + 128]}))
    fields = {key: state['player'][key] for key in keys if key in ('gold', 'diamond', 'hot', 'army_coin', 'tp')}
    if 'tp' in keys:
        fields['tpBeginTime'] = state['player']['tpBeginTime']
    if fields:
        body = {'infos': fields}
        if 'hot' in keys:
            body['t_hot'] = int(state['login'].get('t_hot', 0))
        replies.append(Reply('LoginProto:PlrUpdate', body))
    if 'store_exp' in keys:
        replies.append(Reply('PlayerProto:CardUpdate', {'cards': [], 'store_exp': balance(state, 'store_exp')}))
    return replies

def award_items(tx, rows):
    """Mail's already validated type2 positive attachments, in caller transaction."""
    rendered, changed = [], []
    for row in rows:
        if not isinstance(row, dict) or row.get('type', 2) != 2:
            raise StorageError('Expected type2 item reward')
        cfgid, amount = integer(row.get('id'), 1), integer(row.get('num'), 1)
        changed.append(apply_resource(tx, 'item:' + str(cfgid), 'add', amount))
        rendered.append({'id': cfgid, 'num': amount, 'type': 2})
    return rendered, resource_pushes(tx.state, changed)
