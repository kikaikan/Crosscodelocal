"""Source-driven stationary-monster maps; local turn policy is documented separately.

Original map geometry, groups, formations, combat and contents are retained.
Advanced terrain, moving enemies and mechanisms are rejected before entry.
"""
from copy import deepcopy
import math
import sys
from database import StorageError
from server_core import register, Reply
from handlers import gacha, progression

CATALOG = progression.load('battle-tactical-maps')
REWARDS = progression.load('battle-tactical-rewards')
SETTINGS = progression.load('progression-settings')

def initial_pushes(state):
    """Restore current map identity, never invent cleared stages or encounters."""
    data = state.get('active_duplicate')
    if not data:
        return [Reply('FightProto:CurrDuplicate', {'data': [], 'arrUsedTeam': []})]
    team_ids = sorted({int(row['nTeamID']) for row in data['units'] if row['type'] == 1})
    return [Reply('FightProto:CurrDuplicate', {'data': [
        {'index': 1, 'nDuplicateID': data['stage_id'], 'data': team_ids}], 'arrUsedTeam': team_ids})]

def battle_module():
    return sys.modules['handlers.battle']

def entries(value):
    return list(enumerate(value, 1)) if isinstance(value, list) else [(int(key), row) for key, row in value.items()]

def geometry(identifier):
    graph = {}
    for key, layer in entries(CATALOG[str(identifier)]['map']['sub_maps']):
        layer_id = int(layer.get('id', key))
        cells = layer.get('datas', {})
        for row in range(1, layer['h'] + 1):
            for col in range(1, layer['w'] + 1):
                number = layer_id * 10000 + row * 100 + col
                cell = cells.get(str(number), {})
                if cell.get('type') == 1:
                    continue
                if not 0 < number <= 32767:
                    raise StorageError('Map coordinate exceeds recovered short schema')
                graph[number] = {'height': cell.get('height', 0), 'edges': []}
        for number, cell in [(key, value) for key, value in graph.items() if key // 10000 == layer_id]:
            walls = cells.get(str(number), {}).get('walls', {})
            blocked = {index for index, value in entries(walls)} if walls else set()
            for direction, delta in enumerate([100, 1, -100, -1], 1):
                target = number + delta
                if direction not in blocked and target in graph and target // 10000 == layer_id:
                    cell['edges'].append(target)
    return graph

def active(state, index=1):
    if index != 1 or not state.get('active_duplicate'):
        raise StorageError('No matching local tactical duplicate')
    return state['active_duplicate']

def unit(data, oid, kind=None):
    result = next((row for row in data['units'] if row['oid'] == oid and (kind is None or row['type'] == kind)), None)
    if result is None:
        raise StorageError('Unknown tactical object')
    return result

def wire(row):
    return {key: deepcopy(value) for key, value in row.items() if not key.startswith('_')}

def payload(data):
    return {'index': 1, 'nDuplicateID': data['stage_id'], 'nWave': data['wave'],
            'arrChar': [wire(row) for row in data['units']], 'nStep': data['moves'],
            'nBox': data['boxes'], 'nKillCount': data['kills'], 'bIsNewWave': False, 'bIsFresh': False}

def position(state, dungeon, row, unavailable):
    points = [row['born_pos']] if 'born_pos' in row else dungeon['groups'].get(str(row.get('born_group')), [])
    points = [point for point in points if point not in unavailable]
    if not points:
        raise StorageError('No available configured spawn position')
    return points[gacha.random_below(gacha.gacha_state(state), len(points))]

def add_props(state, data):
    dungeon = CATALOG[str(data['stage_id'])]['dungeon']
    created = []
    for index, cfg in enumerate(dungeon.get('props', []), 1):
        if index in data['processed_props'] or cfg.get('wave', 1) > data['wave']:
            continue
        data['processed_props'].append(index)
        if gacha.random_below(gacha.gacha_state(state), 100) >= int(cfg.get('rate', 100)):
            continue
        point = position(state, dungeon, cfg, {row['pos'] for row in data['units'] if row['state'] != 2})
        row = {'oid': data['next_oid'], 'type': 4, 'pos': point, 'state': cfg.get('state', 1),
               'nPropID': cfg.get('nPropID', index), '_prop': index}
        data['next_oid'] += 1
        data['units'].append(row)
        created.append(Reply('FightProto:UpdateChar', wire(row)))
    return created

@register('FightProtocol:EnterDuplicate')
async def enter(ctx, fields):
    ctx.require_login()
    identifier = gacha.integer(fields, 'nDuplicateID')
    if fields.get('index', 1) != 1:
        raise StorageError('This map uses the ordinary duplicate index')
    with ctx.store.transaction(ctx.uid) as tx:
        stage = progression.gate(tx.state, identifier, tactical=True)
        cfg = CATALOG.get(str(identifier))
        if not cfg or cfg['unreconstructed']:
            raise StorageError('Unreconstructed tactical map: ' + ', '.join(cfg['unreconstructed'] if cfg else ['missing local script']))
        if tx.state.get('active_duplicate') or tx.state.get('active_battle'):
            raise StorageError('Finish or quit the existing duplicate first')
        if tx.currency('hot') < abs(int(stage.get('enterLimitHot', 0))):
            raise StorageError('Insufficient energy to enter this map')
        teams = fields.get('list')
        if not teams:
            selected = fields.get('data') or [tx.state['teams'][0]['index']]
            teams = [{'nTeamIndex': value} for value in selected]
        if not 1 <= len(teams) <= int(stage.get('teamNum', 1)):
            raise StorageError('Invalid number of tactical teams')
        if fields.get('data') and list(fields['data']) != [row['nTeamIndex'] for row in teams]:
            raise StorageError('Selected team IDs do not match their formations')
        data = {'stage_id': identifier, 'wave': 1, 'moves': 0, 'kills': 0, 'normal_kills': 0,
                'elite_kills': 0, 'deaths': 0, 'boxes': 0, 'units': [], 'processed_props': [],
                'used_teams': [], 'started_at': gacha.now(tx.state), 'next_oid': 1}
        dungeon, combat = cfg['dungeon'], battle_module()
        graph, used_cards, used_positions, used_teams = geometry(identifier), set(), set(), set()
        for requested in teams:
            if not requested.get('team'):
                saved = next((row for row in tx.state['teams'] if row['index'] == requested['nTeamIndex']), None)
                requested = dict(requested, team=deepcopy(saved['data']) if saved else [])
            team_id, cids, fighters = combat.team(tx.state, {'list': [requested]}, stage)
            if team_id in used_teams or used_cards.intersection(cids):
                raise StorageError('Tactical teams must contain distinct owned fighters')
            used_teams.add(team_id)
            used_cards.update(cids)
            point = position(tx.state, dungeon, {'born_group': dungeon['born_group']}, used_positions)
            if point not in graph:
                raise StorageError('Configured player spawn is not a traversable grid')
            used_positions.add(point)
            leader = next((row['data'] for row in fighters['data'] if row['data']['isLeader']), fighters['data'][0]['data'])
            leader_cfg = combat.NPCS.get(str(leader.get('npcid'))) if leader.get('npcid') else combat.CONFIGS[str(leader['id'])]
            card_rows = [{'index': index, 'id': row['data']['id'], 'cid': row['data']['cuid'],
                         'hp': math.floor(row['data']['hp']), 'sp': int(row['data'].get('sp', 0)),
                         'row': row['row'], 'col': row['col'], 'isLeader': row['data']['isLeader'],
                         **({'npcid': row['data']['npcid']} if row['data'].get('npcid') else {})}
                        for index, row in enumerate(fighters['data'], 1)]
            data['units'].append({'oid': data['next_oid'], 'type': 1, 'pos': point, 'state': 1,
                'nTeamID': team_id, 'model': leader['model'] if 'model' in leader else leader_cfg['model'],
                'nStep': int(SETTINGS['g_TeamStep']['value']) + leader_cfg.get('nStep', 0),
                'nJump': int(SETTINGS['g_TeamJump']['value']) + leader_cfg.get('nJump', 0),
                'team': card_rows, '_pve': fighters, '_cids': cids})
            data['next_oid'] += 1
        for monster in dungeon.get('monsters', []):
            source = combat.MONSTERS[str(monster['id'])]
            point = position(tx.state, dungeon, monster, used_positions)
            if point not in graph:
                raise StorageError('Configured enemy spawn is not a traversable grid')
            used_positions.add(point)
            data['units'].append({'oid': data['next_oid'], 'type': 3, 'pos': point, 'state': 1,
                'nMonsterGroupID': monster['id'], 'nStep': source.get('nStep', 0), 'nJump': source.get('nJump', 0)})
            data['next_oid'] += 1
        if not any(combat.MONSTERS[str(row['nMonsterGroupID'])]['type'] == 3 for row in data['units'] if row['type'] == 3):
            raise StorageError('Map boss completion condition has no configured boss')
        add_props(tx.state, data)
        tx.add_currency('hot', -abs(int(stage.get('enterCostHot', 0))))
        tx.state['active_duplicate'] = data
        replies = [Reply('LoginProto:PlrUpdate', {'infos': deepcopy(tx.state['player'])}),
            Reply('FightProto:EntryDupResult', {'isOk': True}), Reply('FightProto:DuplicateData', payload(data))]
    return replies

@register('FightProtocol:ReqDuplicateData')
async def restore(ctx, fields):
    ctx.require_login()
    data = active(ctx.store.get_player(ctx.uid), fields.get('index', 1))
    if fields.get('nDuplicateID') != data['stage_id']:
        raise StorageError('Requested map does not match the local save')
    return [Reply('FightProto:DuplicateData', payload(data))]

def use_prop(tx, data, actor, prop):
    cfg = CATALOG[str(data['stage_id'])]['dungeon']['props'][prop['_prop'] - 1]
    kind, parameter = cfg['type'], cfg.get('param', [0])[0]
    deltas, equips, replies = {}, [], []
    if kind in [1, 2]:
        for card, fighter in zip(actor['team'], actor['_pve']['data']):
            maximum = fighter['data']['maxhp']
            amount = parameter if kind == 1 else maximum * parameter
            if card['hp'] > 0:
                card['hp'] = min(math.floor(maximum), card['hp'] + math.floor(amount))
            fighter['data']['hp'] = card['hp']
        details = {}
    elif kind in [5, 26, 28]:
        claim = 'chest:' + str(data['stage_id']) + ':' + str(cfg.get('nPropID', prop['_prop']))
        claimed = progression.progress(tx.state)['claimed_rewards']
        if kind == 26 and claim in claimed:
            rewards = []
        else:
            rewards = progression.grant(tx, progression.reward_graph(tx.state, parameter, tables=REWARDS), deltas, equips)
            if kind == 26:
                claimed.append(claim)
        data['boxes'] += 1
        details = {'reward': rewards, 'nBox': data['boxes']}
        replies.extend(gacha.updates(tx, [], deltas))
        replies.extend([Reply('PlayerProto:CardUpdate', {'cards': [], 'store_exp': int(tx.state.get('store_exp', 0))}),
                        Reply('LoginProto:PlrUpdate', {'infos': deepcopy(tx.state['player'])})])
        tasks = sys.modules.get('handlers.tasks')
        if rewards and tasks is not None:
            replies.extend(tasks.advance_tasks(tx.state, 'state_changed', 0))
        if equips:
            replies.append(Reply('EquipProto:EquipAdd', {'equips': equips, 'cur_size': len(tx.state['equips']),
                'max_size': int(tx.state['max_equip_size']), 'is_finish': True}))
    else:
        raise StorageError('Unreconstructed tactical prop')
    prop['state'] = 2
    replies.extend([Reply('FightProto:UseProp', {'oid': actor['oid'], 'nPropOid': prop['oid'], 'type': kind, 'tParam': details}),
                    Reply('FightProto:UpdateChar', wire(prop)), Reply('FightProto:UpdateChar', wire(actor))])
    return replies

@register('FightProtocol:MoveTo')
async def move(ctx, fields):
    ctx.require_login()
    with ctx.store.transaction(ctx.uid) as tx:
        data = active(tx.state, fields.get('index', 1))
        if tx.state.get('active_battle'):
            raise StorageError('A tactical encounter is still pending')
        actor = unit(data, gacha.integer(fields, 'oid'), 1)
        if actor['state'] != 1:
            raise StorageError('This tactical team cannot move')
        path = fields.get('path')
        if not isinstance(path, list) or not 1 <= len(path) <= 256 or any(isinstance(value, bool) or not isinstance(value, int) for value in path):
            raise StorageError('Invalid tactical path')
        if path[0] == actor['pos']:
            path = path[1:]
        if not path or len(path) > actor['nStep'] or len(set(path)) != len(path):
            raise StorageError('Tactical path exceeds movement allowance')
        graph, current = geometry(data['stage_id']), actor['pos']
        for index, target in enumerate(path):
            if target not in graph[current]['edges'] or abs(graph[current]['height'] - graph[target]['height']) > actor['nJump']:
                raise StorageError('Tactical path crosses a wall, absent grid or excessive height')
            occupied = [row for row in data['units'] if row['oid'] != actor['oid'] and row['pos'] == target and row['state'] != 2]
            if any(row['type'] == 1 and index == len(path) - 1 or row['type'] == 3 and index != len(path) - 1 for row in occupied):
                raise StorageError('Tactical path crosses another encounter or ends on an allied team')
            current = target
        actor['pos'] = current
        data['moves'] += 1
        if data['moves'] > 255:
            raise StorageError('Map move counter exceeds recovered byte schema')
        enemies = [row for row in data['units'] if row['type'] == 3 and row['pos'] == current and row['state'] != 2]
        if enemies:
            actor['state'] = enemies[0]['state'] = 3
        replies = [Reply('FightProto:AskMoveTo', {'oid': actor['oid'], 'pos': current, 'state': actor['state']})]
        if enemies:
            replies.append(Reply('FightProto:Encounter', wire(enemies[0])))
        else:
            for prop in [row for row in data['units'] if row['type'] == 4 and row['pos'] == current and row['state'] != 2]:
                replies.extend(use_prop(tx, data, actor, prop))
        replies.append(Reply('FightProto:IsCanMove', {'nIsCanMove': not enemies, 'nStep': data['moves']}))
    return replies

@register('FightProtocol:EnterFight')
async def encounter(ctx, fields):
    ctx.require_login()
    with ctx.store.transaction(ctx.uid) as tx:
        data = active(tx.state, fields.get('index', 1))
        if tx.state.get('active_battle') or fields.get('isMultiReward'):
            raise StorageError('Encounter already pending or unsupported multi-reward')
        actor = unit(data, gacha.integer(fields, 'myOID'), 1)
        enemy = unit(data, gacha.integer(fields, 'monsterOID'), 3)
        if actor['state'] != 3 or enemy['state'] != 3 or actor['pos'] != enemy['pos']:
            raise StorageError('These tactical objects have not encountered each other')
        pve = deepcopy(actor['_pve'])
        changes = fields.get('posData') or []
        if len({row.get('cid') for row in changes}) != len(changes):
            raise StorageError('Repeated tactical formation adjustment')
        fighters = {row['data']['cuid']: row for row in pve['data']}
        for change in changes:
            fighter = fighters.get(change.get('cid'))
            if not fighter or change.get('fuid'):
                raise StorageError('Unowned tactical formation adjustment')
            fighter['row'] = fighter['data']['row'] = change['row']
            fighter['col'] = fighter['data']['col'] = change['col']
        cells = set()
        for fighter in pve['data']:
            cfg = battle_module().NPCS[str(fighter['data']['npcid'])] if fighter['data'].get('npcid') else battle_module().CONFIGS[str(fighter['data']['id'])]
            coverage = battle_module().footprint(cfg, fighter['row'], fighter['col'])
            if cells.intersection(coverage) or any(not 1 <= r <= 3 or not 1 <= c <= 3 for r, c in coverage):
                raise StorageError('Invalid tactical combat formation')
            cells.update(coverage)
        actor['_pve'] = pve
        for card in actor['team']:
            fighter = fighters[card['cid']]
            card['row'], card['col'] = fighter['row'], fighter['col']
        if actor['nTeamID'] not in data['used_teams']:
            data['used_teams'].append(actor['nTeamID'])
        tx.state['active_battle'] = {'stage_id': data['stage_id'], 'group_id': enemy['nMonsterGroupID'],
            'cids': actor['_cids'], 'report_ids': list(fighters), 'team_id': actor['nTeamID'],
            'myOID': actor['oid'], 'monsterOID': enemy['oid'], 'started_at': gacha.now(tx.state), 'tactical': True}
        replies = [Reply('FightProto:SingleFight', {'groupID': enemy['nMonsterGroupID'], 'nDuplicateID': data['stage_id'],
            'myOID': actor['oid'], 'monsterOID': enemy['oid'], 'data': pve, 'exData': {'dupId': data['stage_id']},
            'nTeamIndex': actor['nTeamID']})]
    return replies

def settle_encounter(tx, session, won, grade, stats, hp):
    data = active(tx.state)
    actor, enemy = unit(data, session['myOID'], 1), unit(data, session['monsterOID'], 3)
    if won:
        for card, fighter in zip(actor['team'], actor['_pve']['data']):
            item = hp.get(str(card['cid']), hp.get(card['cid']))
            card['hp'], card['sp'] = item['hp'], int(item.get('sp', 0))
            fighter['data']['hp'], fighter['data']['sp'] = card['hp'], card['sp']
        actor['state'], enemy['state'] = 1, 2
        category = battle_module().MONSTERS[str(enemy['nMonsterGroupID'])]['type']
        data['kills'] += 1
        data['normal_kills'] += int(category == 1)
        # These TaoFa maps contain only ordinary(1) and boss(3), yet require
        # KillElite>=1. Treat their high-energy boss as elite for that condition.
        # This source-consistency inference is not an observed official result.
        data['elite_kills'] += int(category >= 2)
        data['deaths'] = len({card['cid'] for row in data['units'] if row['type'] == 1 for card in row['team'] if card['hp'] == 0})
        data['wave'] += 1
    else:
        actor['state'], enemy['state'] = 2, 1
        for card, fighter in zip(actor['team'], actor['_pve']['data']):
            card['hp'] = fighter['data']['hp'] = 0
        data['deaths'] += len(actor['team'])
    tx.state.pop('active_battle')
    live_bosses = [row for row in data['units'] if row['type'] == 3 and row['state'] != 2 and
                   battle_module().MONSTERS[str(row['nMonsterGroupID'])]['type'] == 3]
    live_teams = [row for row in data['units'] if row['type'] == 1 and row['state'] != 2]
    finished = won and not live_bosses or not live_teams
    replies = [Reply('FightProto:UpdateChar', wire(actor)), Reply('FightProto:UpdateChar', wire(enemy))]
    if finished:
        details = dict(stats, map_normal_kills=data['normal_kills'], map_elite_kills=data['elite_kills'],
            map_kills=data['kills'], map_moves=data['moves'], map_boxes=data['boxes'], map_deaths=data['deaths'],
            map_teams=len(data['used_teams']))
        replies.extend(progression.settle(tx, session, won and not live_bosses, grade, details))
        tx.state.pop('active_duplicate')
    else:
        if won:
            replies.extend(add_props(tx.state, data))
        replies.extend([Reply('FightProto:FightOver', {'bIsWin': won, 'id': data['stage_id'], 'star': 0,
            'nGrade': grade, 'nDupGrade': [0, 0, 0], 'reward': [], 'rewardId': 0, 'exp': 0,
            'gold': 0, 'nPlayerExp': 0, 'cards': [], 'cardsExp': [], 'sectionMultiReward': [],
            'fisrtPassReward': [], 'fisrt3StarReward': [], 'passivBufReward': []}),
            Reply('FightProto:IsCanMove', {'nIsCanMove': True, 'nStep': data['moves']})])
    return replies
