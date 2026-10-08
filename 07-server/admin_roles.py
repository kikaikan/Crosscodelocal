"""Local source-role grants and explicit archival content switches."""
from copy import deepcopy
import json
from pathlib import Path

from config_codec import app_path
from database import StorageError
from server_core import Reply
from handlers import gacha, progression
from admin_resources import integer

DATA = app_path('data')
ROLES = json.loads((DATA / 'admin-role-templates.json').read_text('utf-8'))
LIMITED_RULE = 17171

def archivable(pool):
    return pool.get('nType') != 4 and bool(pool.get('nStart') or pool.get('nEnd'))

def role_allowed(cfgid):
    return not isinstance(cfgid, bool) and isinstance(cfgid, int) and str(cfgid) in ROLES

def catalog():
    limited = {int(cid) for pool in gacha.POOLS.values() if LIMITED_RULE in pool.get('cardRule', [])
               for cid in pool.get('sel_card_ids', [])}
    pools = [{'id': row['id'], 'name': row.get('sName', str(row['id'])),
              'limited': LIMITED_RULE in row.get('cardRule', []), 'archivable': archivable(row),
              'start': int(row.get('nStart', 0)), 'end': int(row.get('nEnd', 0)),
              'role_ids': list(row.get('sel_card_ids', [])), 'conditions': list(row.get('conditions', [])),
              'type': row['nType'], 'supported': True}
             for row in sorted(gacha.POOLS.values(), key=lambda row: row['id'])]
    roles = [{'cfgid': int(key), 'name': row['name'], 'quality': row['quality'],
              'limited': int(key) in limited, 'supported': True,
              'pool_ids': [pool['id'] for pool in gacha.POOLS.values()
                           if int(key) in pool.get('sel_card_ids', [])],
              'source_available_from_gm': bool(row.get('get_from_gm', False))}
             for key, row in sorted(ROLES.items(), key=lambda pair: int(pair[0]))]
    stages = []
    tactical = progression.load('battle-tactical-maps')
    for row in sorted(progression.STAGES.values(), key=lambda row: row['id']):
        source_type = row.get('type')
        supported = source_type in (1, 2, 3, 4, 99) and all(
            row.get('star' + str(i), [0])[0] in (1, 2, 3) for i in (1, 2, 3))
        if row.get('sub_type') == 1:
            supported = supported and bool(row.get('storyID')) and not (row.get('enterCostHot') or row.get('winCostHot'))
        elif source_type == 8:
            map_cfg = tactical.get(str(row['id']))
            supported = bool(map_cfg is not None and not map_cfg.get('unreconstructed'))
        else:
            supported = supported and bool(row.get('nGroupID'))
        stages.append({'id': row['id'], 'name': row.get('name', row.get('sName', str(row['id']))),
                       'type': source_type, 'supported': supported})
    return {'roles': roles, 'pools': pools, 'stages': stages}

def grant_role(tx, cfgid):
    cfgid = integer(cfgid, 1)
    if not role_allowed(cfgid):
        raise StorageError('Role is not an obtainable source card template')
    result, card, new, role = gacha.award(tx, cfgid, {'nType': 1})
    return {'cid': card['cid'], 'cfgid': cfgid, 'duplicate': not new,
            'get_cnt': card['get_cnt'], 'compensation': deepcopy(result['items']),
            'role_id': int(ROLES[str(cfgid)]['role_id'])}

def role_pushes(state, changed):
    ids = {integer(value, 1) for value in changed}
    cards = [deepcopy(card) for card in state['cards'] if card['cid'] in ids]
    if len(cards) != len(ids):
        raise StorageError('Queued role no longer belongs to this account')
    role_ids = {int(ROLES.get(str(card['cfgid']), gacha.CARDS[str(card['cfgid'])])['role_id']) for card in cards}
    roles = [deepcopy(row) for row in state.get('card_roles', []) if row['id'] in role_ids]
    # CardAdd.UpdateData overwrites by cid and also accepts an offline client's
    # previously unseen card; coalescing duplicates cannot leave it missing.
    replies = [Reply('PlayerProto:CardAdd', {'cards': cards[start:start + 16], 'cur_size': len(state['cards']),
               'max_size': int(state.get('max_card_size', 150)), 'finish': start + 16 >= len(cards)})
               for start in range(0, len(cards), 16)]
    if roles:
        replies.append(Reply('PlayerProto:AddCardRole', {'roles': roles}))
    return replies

def set_archive_pools(tx, payload):
    values, enabled = payload.get('pool_ids'), payload.get('enabled')
    if not isinstance(values, list) or not 1 <= len(values) <= len(gacha.POOLS) or not isinstance(enabled, bool):
        raise StorageError('Archive pools need a bounded ID list and boolean enabled')
    ids = [integer(value, 1) for value in values]
    if len(set(ids)) != len(ids):
        raise StorageError('Duplicate pool IDs')
    for pool_id in ids:
        pool = gacha.POOLS.get(str(pool_id))
        if not pool or not archivable(pool):
            raise StorageError('Only saved scheduled activity pools can be archived')
    saved = set(tx.state.get('offline_archive_pools', []))
    saved = saved | set(ids) if enabled else saved - set(ids)
    tx.state['offline_archive_pools'] = sorted(saved)
    return {'pool_ids': ids, 'enabled': enabled, 'offline_archive_pools': sorted(saved)}

def set_content_access(tx, values):
    from access_policy import configure, options
    # Legacy callers may toggle the three exceptions, never ordinary gates.
    if type(values) is bool:
        values = dict.fromkeys(('pools', 'activities', 'illustrations'), values)
    configure(tx.state, values)
    return {'offline_access': options(tx.state), 'offline_unlock_all': False}


def content_pushes(state):
    """Root's InitFinish/outbox can send this before refreshing gate-based UI."""
    from admin_control import access_reply
    return [access_reply(state),
            Reply('PlayerProto:CardFactoryInfoRet', gacha.factory(state))]
