"""Launch the original client's SingleFightMgrServer, then settle its report locally.
Server-authoritative command playback, tactical maps and special modes are explicit gaps.
"""
from copy import deepcopy
import math
from database import StorageError
from server_core import Reply, register
from handlers import gacha, progression, player_state
import equipment_stats
import formation_halo

CONFIGS = progression.load('battle-card-config')
LEVELS = progression.load('battle-card-level')
BREAKS = progression.load('battle-card-break')
INTENSIFY = progression.load('battle-card-intensify')
MONSTERS = progression.load('battle-monster-groups')
FORMATIONS = progression.load('battle-monster-formations')
COMMANDERS = progression.load('battle-commander-skills')
NPCS = progression.load('battle-npc-config')

def battle_integer(value, label, minimum=0, maximum=2147483647):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise StorageError('Invalid battle ' + label)
    return value

def configured_choice(state, identifiers):
    if not isinstance(identifiers, list):
        return battle_integer(identifiers, 'scripted fighter ID', 1, 4294967295)
    if not identifiers:
        raise StorageError('Empty scripted fighter choice')
    selected = battle_integer(state['login'].get('sel_card_ix', 1), 'commander selection', 1, 255) - 1
    return battle_integer(identifiers[min(max(selected, 0), len(identifiers) - 1)],
                          'scripted fighter ID', 1, 4294967295)

def footprint(cfg, row, col):
    grids = cfg.get('grids')
    if grids is None:
        offsets = [[0, 0]]
    elif isinstance(grids, int) and not isinstance(grids, bool):
        formation = FORMATIONS.get(str(grids))
        offsets = formation.get('coordinate') if isinstance(formation, dict) else None
        if not isinstance(offsets, list) or not offsets:
            raise StorageError('Missing local fighter footprint configuration')
    elif isinstance(grids, list):
        # Compatibility with already-expanded fixtures: these coordinates are
        # one-based holder cells, while MonsterFormation coordinates are offsets.
        offsets = [[cell[0] - 1, cell[1] - 1] for cell in grids
                   if isinstance(cell, list) and len(cell) == 2]
        if len(offsets) != len(grids) or not offsets:
            raise StorageError('Invalid expanded fighter footprint')
    else:
        raise StorageError('Invalid fighter footprint configuration')
    if any(not isinstance(cell, list) or len(cell) != 2 or
           any(isinstance(value, bool) or not isinstance(value, int) for value in cell)
           for cell in offsets):
        raise StorageError('Invalid fighter footprint coordinates')
    return {(row + cell[0], col + cell[1]) for cell in offsets}

def npc_card(state, npc_id, row, col, index, leader):
    cfg = NPCS.get(str(npc_id))
    if not cfg:
        raise StorageError('Missing local scripted NPC configuration')
    data = deepcopy(cfg)
    data.update(id=npc_id, npcid=npc_id, cuid=npc_id, uid=state['player']['uid'], oid=index,
                row=row, col=col, teamID=1, modelA=cfg['model'], hp=cfg['maxhp'],
                break_level=cfg.get('break_level', 1), intensify_level=cfg.get('intensify_level', 1),
                nStrategyIndex=player_state.AI_STRATEGY_MIN, isLeader=leader, isUseCommon=0,
                eskills=cfg.get('eskills', []), use_sub_talent=[])
    return {'cid': npc_id, 'uid': state['player']['uid'], 'row': row, 'col': col, 'data': data}

def default_forced_positions(state, stage):
    positions, used = [], set()
    for entry in stage['arrForceTeam'][0]:
        identifiers = entry.get('nForceID')
        if not identifiers:
            continue
        cfgid = configured_choice(state, identifiers)
        npc = entry.get('bIsNpc', False)
        if npc:
            cfg = NPCS.get(str(cfgid))
            cid = cfgid
        else:
            card = next((card for card in state['cards'] if card['cfgid'] == cfgid), None)
            if not card:
                raise StorageError('Required owned fighter is unavailable')
            cfg, cid = CONFIGS[str(cfgid)], card['cid']
        if not cfg:
            raise StorageError('Required scripted NPC is unavailable')
        candidates = [entry['nPos']] if entry.get('nPos') else [[r, c] for r in range(1, 4) for c in range(1, 4)]
        target = next((pos for pos in candidates if all(1 <= r <= 3 and 1 <= c <= 3 for r, c in footprint(cfg, *pos)) and not used.intersection(footprint(cfg, *pos))), None)
        if target is None:
            raise StorageError('Scripted fighters do not fit their source formation')
        used.update(footprint(cfg, *target))
        positions.append({'index': entry['index'], 'cid': cid, 'id': cfgid, 'npcid': cfgid if npc else None,
                          'row': target[0], 'col': target[1], 'isLeader': entry['index'] == 1})
    return positions

def fight_card(state, card, row, col, index, leader, selected_strategy):
    cfg = CONFIGS[str(card['cfgid'])]
    level = LEVELS[str(card['level'])]
    brk = BREAKS[str(card.get('break_level', 1))]
    improve = INTENSIFY[str(card.get('intensify_level', 1))]
    data = {key: cfg[key] for key in ['np', 'sp', 'sp_race', 'sp_race2', 'crit', 'crit_rate',
            'hit', 'resist', 'bedamage', 'damage', 'becure', 'cure', 'damagePhysics', 'damageLight'] if key in cfg}
    for key in ['attack', 'maxhp', 'defense']:
        # 不在此处取整：CardCalculator 是在 TakePropertyAdd 之后统一 floor（lua:468-473），
        # 先把小数截掉会让副天赋的百分比乘区偏小 1 点（实测 579 vs 580）。
        data[key] = cfg[key] * level.get(key, 1) * brk.get(key, 1)
    for key in ['speed', 'crit_rate', 'crit', 'hit', 'resist', 'np', 'sp', 'sp_race', 'sp_race2', 'damagePhysics']:
        if key == 'damagePhysics' and not any(key in source for source in [cfg, level, brk, improve]):
            continue  # FightCardBase's missing damage coefficient defaults to 1.
        data[key] = cfg.get(key, 0) + level.get(key, 0) + brk.get(key, 0) + improve.get(key, 0)
    # 已装备副天赋的属性乘区/加值（CardCalculator.lua:410-443 的 jPropertys），与
    # equipment_stats.equipped_stats 共用同一个 apply_property_totals；未装备时它不改任何键。
    equipment_stats.apply_property_totals(data, equipment_stats.talent_properties(card))
    # 与 CardCalculator.lua:468-473 一致：整数列在属性应用后取整（否则浮点会撑爆 int 线字段）。
    for key in equipment_stats.FLOORED:
        if key in data:
            data[key] = math.floor(data[key])
    data.update(id=card['cfgid'], cuid=card['cid'], uid=state['player']['uid'], oid=index,
                row=row, col=col, teamID=1, model=card.get('skin', cfg['model']),
                modelA=card.get('skin_a', cfg['model']), name=cfg['name'], level=card['level'],
                hp=data['maxhp'],
                skills=sorted(set([int(key) for key in card.get('skills', {})] or cfg.get('skills', []))
                              | set(equipment_stats.talent_fight_skills(card))),
                break_level=card.get('break_level', 1), intensify_level=card.get('intensify_level', 1),
                nStrategyIndex=selected_strategy, isLeader=card['cid'] == leader, isUseCommon=0,
                eskills=[], use_sub_talent=equipment_stats.equipped_talents(card))
    return {'cid': card['cfgid'], 'uid': state['player']['uid'], 'row': row, 'col': col, 'data': data}

def validate_forced_submission(state, stage, submitted_team, canonical):
    if not isinstance(submitted_team, list) or len(submitted_team) != len(canonical):
        raise StorageError('Battle team violates the configured scripted roster')
    expected = {row['index']: row for row in canonical}
    seen = set()
    owned = {card['cid']: card for card in state['cards']}
    for row in submitted_team:
        if not isinstance(row, dict):
            raise StorageError('Battle team member must be a record')
        slot = battle_integer(row.get('index'), 'scripted card slot', 1, 6)
        if slot in seen or slot not in expected:
            raise StorageError('Battle team violates the configured scripted roster')
        seen.add(slot)
        target = expected[slot]
        cid = battle_integer(row.get('cid', row.get('id')), 'scripted fighter ID', 1, 4294967295)
        npc_id = row.get('npcid')
        if npc_id is not None:
            npc_id = battle_integer(npc_id, 'scripted NPC ID', 1, 4294967295)
        actual_id = npc_id if target.get('npcid') else owned.get(cid, {}).get('cfgid')
        if (cid != target['cid'] or actual_id != target['id'] or
                row.get('row') != target['row'] or row.get('col') != target['col']):
            raise StorageError('Battle team violates the configured scripted roster')
        if 'nStrategyIndex' in row:
            player_state.strategy_index(row['nStrategyIndex'])
    if seen != set(expected):
        raise StorageError('Battle team violates the configured scripted roster')

def team(state, fields, stage=None):
    submitted = fields.get('list')
    stored = state['teams']
    forced = stage and stage.get('arrForceTeam')
    if forced and (not isinstance(stage['arrForceTeam'], list) or len(stage['arrForceTeam']) != 1 or
                   not isinstance(stage['arrForceTeam'][0], list)):
        raise StorageError('Multiple scripted battle teams are not reconstructed yet')
    submitted_row = None
    if submitted is not None:
        if not isinstance(submitted, list) or len(submitted) != 1 or not isinstance(submitted[0], dict):
            raise StorageError('Multiple tactical teams are not reconstructed yet')
        submitted_row = submitted[0]
    if forced:
        team_id = stage['id'] * 10 + 1
        positions = default_forced_positions(state, stage)
        if submitted_row is not None:
            requested_team = battle_integer(submitted_row.get('nTeamIndex'), 'team index', 1, 4294967295)
            if requested_team != team_id:
                raise StorageError('Scripted team index does not match this stage')
            validate_forced_submission(state, stage, submitted_row.get('team'), positions)
    elif submitted_row is not None:
        team_id = battle_integer(submitted_row.get('nTeamIndex'), 'team index', 1, 4294967295)
        positions = submitted_row.get('team')
        if not isinstance(positions, list):
            raise StorageError('Invalid local battle team')
    else:
        team_id = battle_integer(fields.get('nTeamIndex', stored[0]['index'] if stored else 1),
                                 'team index', 1, 4294967295)
        saved = next((row for row in stored if row['index'] == team_id), None)
        if not saved:
            raise StorageError('No saved battle team')
        else:
            positions = saved['data']
    saved = next((row for row in stored if row['index'] == team_id), None)
    if forced:
        if team_id != stage['id'] * 10 + 1:
            raise StorageError('Scripted team index does not match this stage')
        force_skill = stage.get('forceSkill', [])
        skill_id = force_skill[0] if force_skill else 0
        saved = {'leader': positions[0].get('cid') if positions else None, 'skill_group_id': skill_id}
        by_slot = {battle_integer(pos.get('index', n), 'scripted card slot', 1, 6): pos
                   for n, pos in enumerate(positions, 1)}
        for rule in stage['arrForceTeam'][0]:
            if not rule.get('nForceID'):
                continue
            pos = by_slot.get(rule['index'])
            cfgid = configured_choice(state, rule['nForceID'])
            actual_id = pos.get('npcid') if pos and rule.get('bIsNpc') else next((row['cfgid'] for row in state['cards'] if pos and row['cid'] == pos.get('cid')), None)
            if not pos or actual_id != cfgid or rule.get('nPos') and [pos.get('row'), pos.get('col')] != rule['nPos']:
                raise StorageError('Battle team violates the configured scripted roster')
    if not saved or not positions or len(positions) > 6:
        raise StorageError('Invalid local battle team')
    owner = {card['cid']: card for card in state['cards']}
    cards, ids, cells, npc_ids = [], [], set(), set()
    allowed_npcs = set(stage.get('arrNPC', [])) if stage else set()
    mandatory_npc = configured_choice(state, stage['forceNPC']) if stage and stage.get('forceNPC') else None
    if mandatory_npc:
        allowed_npcs.add(mandatory_npc)
    if forced:
        allowed_npcs.update(configured_choice(state, rule['nForceID']) for rule in stage['arrForceTeam'][0] if rule.get('bIsNpc') and rule.get('nForceID'))
    for index, pos in enumerate(positions, 1):
        if not isinstance(pos, dict):
            raise StorageError('Battle team member must be a record')
        cid = battle_integer(pos.get('cid', pos.get('id')), 'fighter ID', 1, 4294967295)
        npc_id = pos.get('npcid')
        if npc_id is not None:
            npc_id = battle_integer(npc_id, 'NPC ID', 1, 4294967295)
        if pos.get('fuid', 0):
            raise StorageError('Remote friend support has no offline owned-card source')
        if npc_id:
            if npc_id not in allowed_npcs or npc_id in npc_ids or cid != npc_id or str(npc_id) not in NPCS:
                raise StorageError('Unconfigured or repeated scripted NPC')
            cfg = NPCS[str(npc_id)]
        elif cid not in owner or cid in ids:
            raise StorageError('Battle team contains an unowned or repeated card')
        else:
            cfg = CONFIGS[str(owner[cid]['cfgid'])]
        row = battle_integer(pos.get('row', 1), 'formation row', 1, 3)
        col = battle_integer(pos.get('col', index), 'formation column', 1, 3)
        coverage = footprint(cfg, row, col)
        if any(not 1 <= r <= 3 or not 1 <= c <= 3 for r, c in coverage) or cells.intersection(coverage):
            raise StorageError('Invalid battle formation position')
        if npc_id:
            cards.append(npc_card(state, npc_id, row, col, index, bool(pos.get('isLeader', index == 1))))
            npc_ids.add(npc_id)
        else:
            card = owner[cid]
            # spec §7.1：未装备（use 全 0）无属性影响；已装备的属性已按 CardCalculator.lua:410-443
            # 精确移植（equipment_stats.talent_properties/talent_fight_skills，见 fight_card），
            # 因此副天赋不再阻断出战 —— 拒绝面收敛为仍未移植的芯片/武器。
            if card.get('equips') or card.get('equip_ids'):
                raise StorageError('Equipped stat or talent calculation is not reconstructed yet')
            selected_strategy = player_state.strategy_index(pos.get('nStrategyIndex', player_state.AI_STRATEGY_MIN))
            cards.append(fight_card(state, card, row, col, index,
                                    saved.get('leader', ids[0] if ids else cid), selected_strategy))
            ids.append(cid)
        cells.update(coverage)
    if mandatory_npc and mandatory_npc not in npc_ids:
        raise StorageError('Stage requires its configured support NPC')
    skill_cfg = COMMANDERS.get(str(saved.get('skill_group_id', 0)), {})
    if submitted_row and submitted_row.get('nSkillGroup', saved.get('skill_group_id', 0)) != saved.get('skill_group_id', 0):
        raise StorageError('Requested commander skill group does not match the saved team')
    data = {'data': cards}
    if skill_cfg.get('aSkillIds'):
        data['tCommanderSkill'] = skill_cfg['aSkillIds']
    elif saved.get('skill_group_id'):
        raise StorageError('Missing configured commander skill group')
    return team_id, ids, data

def entry_payload(stage, team_id, data):
    data = deepcopy(data)
    data['data'] = formation_halo.apply(data['data'], {**CONFIGS, **NPCS}, LEVELS, BREAKS, footprint)
    return {'groupID': stage['nGroupID'], 'nDuplicateID': stage['id'],
            'myOID': 1, 'monsterOID': 2, 'data': deepcopy(data),
            'exData': {'dupId': stage['id']}, 'nTeamIndex': team_id}

def entry_identity(payload):
    return [(row['data']['cuid'], row['data'].get('npcid'), row['data']['id'], row['row'], row['col'],
             row['data'].get('nStrategyIndex', player_state.AI_STRATEGY_MIN))
            for row in payload['data']['data']]

def entry_replies(state, payload):
    return [Reply('LoginProto:PlrUpdate', {'infos': deepcopy(state['player'])}),
            Reply('FightProto:EntryDupResult', {'isOk': True}),
            Reply('FightProto:SingleFight', deepcopy(payload))]

def log_rejection(ctx, kind, reason, stage_id=None):
    # The dispatcher intentionally never logs raw reports/authentication.
    # These reasons are static validation messages, with only a configured ID.
    if getattr(ctx.server, 'log_path', None) is not None:
        ctx.server.event('battle_' + kind + '_rejected', stage_id=stage_id, reason=reason)

def stored_battle(state):
    """Validated view of the persisted encounter; a legacy or damaged save rejects cleanly.

    Older local sessions stored fewer keys. Reading them directly raised KeyError,
    which the dispatcher used to treat as a handler defect and close the connection.
    """
    existing = state.get('active_battle')
    if existing is None:
        return None
    if not isinstance(existing, dict):
        raise StorageError('Invalid stored battle session')
    for key in ('started_at', 'stage_id', 'team_id'):
        if isinstance(existing.get(key), bool) or not isinstance(existing.get(key), int):
            raise StorageError('Invalid stored battle session')
    for key in ('cids', 'report_ids'):
        if key in existing and not isinstance(existing[key], list):
            raise StorageError('Invalid stored battle session')
    if not isinstance(existing.get('cids'), list):
        raise StorageError('Invalid stored battle session')
    return existing

async def start(ctx, fields):
    try:
        return await start_checked(ctx, fields)
    except StorageError as error:
        log_rejection(ctx, 'entry', str(error), fields.get('nDuplicateID'))
        raise

async def start_checked(ctx, fields):
    ctx.require_login()
    stage_id = gacha.integer(fields, 'nDuplicateID')
    with ctx.store.transaction(ctx.uid) as tx:
        stage = progression.gate(tx.state, stage_id)
        if not stage.get('nGroupID') or str(stage['nGroupID']) not in MONSTERS:
            raise StorageError('Tactical map or story-only stage is not reconstructed yet')
        if fields.get('isMultiReward') or fields.get('selectBuffs'):
            raise StorageError('Multi-reward and support buffs are not reconstructed yet')
        if tx.state.get('active_duplicate'):
            raise StorageError('Quit the active tactical duplicate before direct battle')
        existing = stored_battle(tx.state)
        if existing is not None and gacha.now(tx.state) - existing['started_at'] < 3600:
            team_id, cids, data = team(tx.state, fields, stage)
            report_ids = [row['data'].get('npcid', row['data']['cuid']) for row in data['data']]
            if (existing.get('tactical') or existing['stage_id'] != stage_id or
                    existing['group_id'] != stage['nGroupID'] or existing['team_id'] != team_id or
                    existing['cids'] != cids or existing.get('report_ids', existing['cids']) != report_ids):
                raise StorageError('Retry does not match the unfinished local encounter')
            proposed = entry_payload(stage, team_id, data)
            saved = existing.get('entry_reply')
            if saved is not None and entry_identity(saved) != entry_identity(proposed):
                raise StorageError('Retry changes the unfinished encounter formation')
            if saved is None:
                # Pre-snapshot sessions only saved source group/team/participants.
                # Reconstruct that same party on its legitimate re-entry request;
                # preserve the original session clock and already charged fuel.
                existing['entry_reply'] = saved = proposed
            return entry_replies(tx.state, saved)
        limit = abs(int(stage.get('enterLimitHot', 0)))
        if tx.currency('hot') < limit:
            raise StorageError('Insufficient energy to enter this stage')
        team_id, cids, data = team(tx.state, fields, stage)
        tx.add_currency('hot', -abs(int(stage.get('enterCostHot', 0))))
        payload = entry_payload(stage, team_id, data)
        tx.state['active_battle'] = {'stage_id': stage_id, 'group_id': stage['nGroupID'],
            'cids': cids, 'report_ids': [row['data'].get('npcid', row['data']['cuid']) for row in data['data']],
            'team_id': team_id, 'myOID': 1, 'monsterOID': 2, 'started_at': gacha.now(tx.state),
            'entry_reply': payload}
        replies = entry_replies(tx.state, payload)
    return replies

register('FightProtocol:EnterFightDuplicate')(start)

@register('FightProtocol:SwitchAIStrategy')
async def switch_ai_strategy(ctx, fields):
    ctx.require_login()
    battle_integer(fields.get('index'), 'AI scene index', 0, 255)
    oid = battle_integer(fields.get('oid'), 'AI party OID', 0, 255)
    updates = fields.get('data')
    if not isinstance(updates, list) or not updates or len(updates) > 6:
        raise StorageError('Invalid battle AI strategy update list')
    with ctx.store.transaction(ctx.uid) as tx:
        active = tx.state.get('active_battle')
        if not isinstance(active, dict) or oid != active.get('myOID'):
            raise StorageError('No matching active local party for AI strategy update')
        participants = set(active.get('cids', []))
        entry = active.get('entry_reply')
        if not isinstance(entry, dict) or not isinstance(entry.get('data'), dict) or not isinstance(entry['data'].get('data'), list):
            raise StorageError('Active battle has no mutable entry snapshot')
        seen = set()
        validated = []
        for update in updates:
            if not isinstance(update, dict):
                raise StorageError('Invalid battle AI strategy update')
            cid = battle_integer(update.get('cuid'), 'AI fighter ID', 1, 4294967295)
            if update.get('fuid') not in (None, 0):
                raise StorageError('Remote support AI strategies are not persisted locally')
            if cid not in participants or cid in seen:
                raise StorageError('Battle AI strategy fighter is not an owned participant')
            index = player_state.strategy_index(update.get('nStrategyIndex'))
            data = player_state.strategy_data(update.get('tStrategyData'))
            rows = [row for row in entry['data']['data'] if isinstance(row, dict) and
                    isinstance(row.get('data'), dict) and row['data'].get('cuid') == cid and
                    not row['data'].get('npcid')]
            if len(rows) != 1:
                raise StorageError('Battle AI strategy fighter snapshot is missing or ambiguous')
            seen.add(cid)
            validated.append((cid, index, data, rows[0]))
        stored_team = next((team for team in tx.state['teams'] if team.get('index') == active.get('team_id')), None)
        for cid, index, data, row in validated:
            player_state.save_strategy(tx.state, cid, index, data)
            row['data']['nStrategyIndex'] = index
            if stored_team:
                member = next((member for member in stored_team.get('data', [])
                               if isinstance(member, dict) and member.get('cid') == cid), None)
                if member:
                    member['nStrategyIndex'] = index
    return [Reply('PlayerProto:SwitchAIStrategyRes', {'ret': True})]

@register('FightProtocol:StartMainLineFight')
async def legacy_start(ctx, fields):
    # The client only calls this legacy developer path with a hard-coded test roster.
    # Its CardPoint identifiers are not documented as owned-card IDs; do not ignore them.
    if fields.get('data'):
        raise StorageError('Legacy test-roster CardPoint mapping is not reconstructed')
    stage = progression.STAGES.get(str(fields.get('nDuplicateID')))
    if fields.get('groupID') and (not stage or fields['groupID'] != stage.get('nGroupID')):
        raise StorageError('Legacy group does not match the configured stage')
    return await start(ctx, fields)

@register('FightProtocol:OnFightOver')
async def report(ctx, fields):
    try:
        return await report_checked(ctx, fields)
    except StorageError as error:
        log_rejection(ctx, 'report', str(error))
        raise

def report_integer(value, label):
    # Lua table.Encode serializes a number's value, not a Python int type.
    # Integral .0 values are exact integers; fractions and booleans are not.
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            isinstance(value, float) and not math.isfinite(value) or int(value) != value):
        raise StorageError('Non-integral battle report number: ' + label)
    return int(value)

def hp_table(value):
    # SingleFightMgr.lua:82-105 uses numeric cid keys. A table whose keys are
    # 1..N is a Lua array, so the safe data parser correctly returns a list.
    # Sparse/NPC/string-keyed maps keep their keys; never reinterpret indices
    # as the current party's order (e.g. [1] cannot stand for CID91101001).
    if isinstance(value, list):
        return {index: row for index, row in enumerate(value, 1)}
    if isinstance(value, dict):
        return value
    raise StorageError('Participant HP must be a Lua table')

def log_hp_rejection(ctx, active, cid, row, condition):
    """Log only bounded participant diagnostics, never the raw client report."""
    if getattr(ctx.server, 'log_path', None) is None:
        return
    def number(key):
        value = row.get(key) if isinstance(row, dict) else None
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value)):
            return None
        return value
    ctx.server.event('battle_hp_rejected', stage_id=active['stage_id'], cid=cid,
                     hp=number('hp'), maxhp=number('maxhp'), condition=condition,
                     connection_id=getattr(ctx, 'connection_id', None))

async def report_checked(ctx, fields):
    ctx.require_login()
    with ctx.store.transaction(ctx.uid) as tx:
        active = stored_battle(tx.state)
        if active is None or gacha.now(tx.state) - active['started_at'] > 3600:
            raise StorageError('No active local battle session for this report')
        if fields.get('myOID') != active.get('myOID') or fields.get('monsterOID') != active.get('monsterOID'):
            raise StorageError('Battle report does not match the active local encounter')
        winner = gacha.integer(fields, 'winer')
        if winner not in [1, 2]:
            raise StorageError('Invalid local battle winner')
        won = winner == 1
        grade = fields.get('nGrade', [])
        if len(grade) != 3 or any(value not in [0, 1] for value in grade) or grade[0] != int(won):
            raise StorageError('Invalid battle grade array')
        stats = fields.get('exdata') or {}
        if not isinstance(stats, dict):
            raise StorageError('Missing local battle statistics')
        stats = dict(stats)
        for key in ['turnNum', 'deathCnt', 'cardCnt', 'nMinHpPercent']:
            stats[key] = report_integer(stats.get(key), key)
        report_ids = active.get('report_ids') or active['cids']
        if (not 0 <= stats['deathCnt'] <= stats['cardCnt'] or stats['cardCnt'] != len(report_ids) or
                not 0 <= stats['nMinHpPercent'] <= 100 or not 0 <= stats['turnNum'] <= 10000):
            raise StorageError('Inconsistent battle statistics')
        hp = hp_table(fields.get('data') or {})
        if won:
            if not isinstance(hp, dict) or any(str(cid) not in hp and cid not in hp for cid in report_ids):
                raise StorageError('Winning report lacks participant HP')
            for cid in report_ids:
                row = hp.get(str(cid), hp.get(cid))
                if not isinstance(row, dict):
                    log_hp_rejection(ctx, active, cid, row, 'participant_not_record')
                    raise StorageError('Invalid reported participant HP')
                row = dict(row)
                row['hp'] = report_integer(row.get('hp'), 'hp')
                # SingleFightMgr.lua reports raw v.hp but base v.maxhp. Buffs
                # can raise HP above that base (BuffBase:AddMaxHpPercent), and
                # overkill leaves negative HP (FightCardBase:AddHpNoShield).
                # Combat is simulated locally; this field is not its live cap.
                if (isinstance(row.get('maxhp'), bool) or not isinstance(row.get('maxhp'), (int, float)) or
                        not math.isfinite(row['maxhp']) or row['maxhp'] <= 0):
                    log_hp_rejection(ctx, active, cid, row, 'hp_or_maxhp_out_of_range')
                    raise StorageError('Invalid reported participant HP')
                # Tactical maps persist HP in unsigned CardPoint fields and
                # count deaths using zero, so normalize source overkill here.
                row['hp'] = max(0, row['hp'])
                hp[str(cid) if str(cid) in hp else cid] = row
            if not any(hp.get(str(cid), hp.get(cid))['hp'] > 0 for cid in report_ids):
                raise StorageError('Winning report has no surviving participant')
            if grade[1] != int(stats['deathCnt'] == 0):
                raise StorageError('Battle grade disagrees with death count')
        elif grade != [0, 0, 0]:
            raise StorageError('Lost encounter cannot have a positive combat grade')
        if active.get('tactical'):
            replies = battle_tactical.settle_encounter(tx, active, won, grade, stats, hp)
        else:
            replies = progression.settle(tx, active, won, grade, stats)
    return replies

@register('FightProtocol:QuitDuplicate')
async def quit_duplicate(ctx, fields):
    ctx.require_login()
    with ctx.store.transaction(ctx.uid) as tx:
        active = stored_battle(tx.state)
        tactical = tx.state.get('active_duplicate')
        if tactical:
            if fields.get('nDuplicateID') != tactical['stage_id']:
                raise StorageError('No matching active tactical duplicate')
            tx.state.pop('active_duplicate')
            tx.state.pop('active_battle', None)
            return [Reply('FightProto:AskQuitDuplicate', {'ret': True, 'fisrt3StarReward': [], 'reward': []})]
        stage = progression.STAGES.get(str(fields.get('nDuplicateID')))
        if not active and stage and stage.get('sub_type') == 1:
            return progression.complete_story(tx, stage['id'])
        if not active or fields.get('nDuplicateID') != active['stage_id']:
            raise StorageError('No matching active duplicate')
        tx.state.pop('active_battle')
    return [Reply('FightProto:AskQuitDuplicate', {'ret': True, 'fisrt3StarReward': [], 'reward': []})]

@register('FightProtocol:LeaveFight')
async def leave_fight(ctx, fields):
    ctx.require_login()
    # Completion has already settled; this acknowledges leaving by clearing only the session.
    with ctx.store.transaction(ctx.uid) as tx:
        if tx.state.get('active_battle', {}).get('tactical'):
            raise StorageError('Report or quit the pending tactical encounter first')
        tx.state.pop('active_battle', None)
    return []

from handlers import battle_tactical
