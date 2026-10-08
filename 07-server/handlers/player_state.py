"""Persistent player settings, local profile and owned-card team editing.

Wire shapes come from GameMsg.lua/PlayerProto.lua. Validation limits below are
local policies; they are not assertions about uncaptured official server rules.
"""
from copy import deepcopy
from datetime import date
import json
import math
import sys
import time

from database import StorageError
from server_core import Reply, register
from seed_generator import commander_template

# Populate both data-only templates at startup, before the UI's 1.5-second name callback.
commander_template(1)
commander_template(2)

AI_STRATEGY_MIN = 0
AI_STRATEGY_MAX = 4
AI_STRATEGY_MAX_NODES = 512
AI_STRATEGY_MAX_DEPTH = 8


def player_reply(state):
    return Reply('LoginProto:PlrUpdate', {'infos':deepcopy(state['player'])})


def task_event(state, event):
    # Explicit module dependency: the task engine is an optional module, never another
    # domain's registered protocol name.
    tasks = sys.modules.get('handlers.tasks')
    if tasks is None:
        return []
    return tasks.advance_tasks(state, event)


@register('FightProtocol:RogueTSetWindow')
async def rogue_window(ctx, fields):
    """Persist only the local red-dot acknowledgement (RogueTMgr.lua:347)."""
    ctx.require_login()
    kind, value=fields.get('ty'),fields.get('value')
    if isinstance(kind,bool) or kind not in (1,2):
        raise StorageError('Unsupported RogueT notification kind')
    if isinstance(value,bool) or not isinstance(value,int) or not 0<=value<=4294967295:
        raise StorageError('Invalid RogueT notification value')
    with ctx.store.transaction(ctx.uid) as tx:
        saved=tx.state.setdefault('ui_preferences',{}).setdefault('rogue_t_window',{})
        saved['win'+str(kind)]=value
    return [Reply('FightProto:RogueTSetWindowRet',{'ty':kind,'value':value})]


def text_value(value, maximum=32):
    if not isinstance(value,str) or not 1<=len(value.strip())<=maximum:
        raise StorageError('Invalid local profile text')
    value=value.strip()
    if any(ord(c)<32 or ord(c)==127 for c in value):
        raise StorageError('Profile contains control characters')
    return value


def bounded_integer(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise StorageError('Invalid ' + label)
    return value


def strategy_index(value):
    return bounded_integer(value, 'AI strategy index', AI_STRATEGY_MIN, AI_STRATEGY_MAX)


def strategy_data(value):
    """Validate a bounded Lua-table value before it reaches JSON persistence."""
    if not isinstance(value, (dict, list)):
        raise StorageError('AI strategy data must be a Lua table')
    budget = [AI_STRATEGY_MAX_NODES]

    def visit(item, depth):
        budget[0] -= 1
        if budget[0] < 0 or depth > AI_STRATEGY_MAX_DEPTH:
            raise StorageError('AI strategy data is too large or deeply nested')
        if isinstance(item, list):
            if len(item) > 128:
                raise StorageError('AI strategy array is too large')
            return [visit(child, depth + 1) for child in item]
        if isinstance(item, dict):
            if len(item) > 128:
                raise StorageError('AI strategy table is too large')
            result = {}
            for key, child in item.items():
                if isinstance(key, bool) or not isinstance(key, (str, int)):
                    raise StorageError('AI strategy table key must be a string or integer')
                if isinstance(key, str):
                    if not key or len(key) > 64 or any(ord(char) < 32 or ord(char) == 127 for char in key):
                        raise StorageError('Invalid AI strategy table key')
                    # JSON persistence cannot distinguish integer 1 from string "1".
                    numeric_key = key.lstrip('-').isdigit() and key == str(int(key))
                    if numeric_key:
                        raise StorageError('Numeric AI strategy keys must be integers')
                elif not -2147483648 <= key <= 2147483647:
                    raise StorageError('AI strategy table key is out of range')
                if key in result:
                    raise StorageError('Duplicate AI strategy table key')
                result[key] = visit(child, depth + 1)
            return result
        if isinstance(item, bool):
            return item
        if isinstance(item, int):
            if not -9007199254740991 <= item <= 9007199254740991:
                raise StorageError('AI strategy integer is out of range')
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise StorageError('AI strategy number must be finite')
            return item
        if isinstance(item, str):
            if len(item) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in item):
                raise StorageError('Invalid AI strategy string')
            return item
        raise StorageError('Unsupported AI strategy value')

    return visit(value, 0)


def stored_strategy_data(value):
    """Restore integer Lua keys stringified by SQLite's JSON document."""
    if isinstance(value, list):
        return [stored_strategy_data(item) for item in value]
    if isinstance(value, dict):
        restored = {}
        for key, item in value.items():
            restored_key = int(key) if isinstance(key, str) and key.lstrip('-').isdigit() and key == str(int(key)) else key
            if restored_key in restored:
                raise StorageError('Stored AI strategy contains colliding keys')
            restored[restored_key] = stored_strategy_data(item)
        return restored
    return value


def find_team_member(state, team_index, card_index):
    team = next((row for row in state['teams'] if row.get('index') == team_index), None)
    if not team:
        raise StorageError('Unknown AI strategy team')
    members = team.get('data')
    if not isinstance(members, list):
        raise StorageError('Invalid stored team data')
    member = next((row for row in members if isinstance(row, dict) and row.get('index') == card_index), None)
    if not member or member.get('bIsNpc'):
        raise StorageError('Unknown AI strategy team member')
    if member.get('cid') not in {card.get('cid') for card in state['cards']}:
        raise StorageError('AI strategy team member is not owned')
    return team, member


def save_strategy(state, cid, index, data):
    strategies = state.setdefault('ai_strategies', {})
    if not isinstance(strategies, dict):
        raise StorageError('Invalid stored AI strategies')
    fighter = strategies.setdefault(str(cid), {})
    if not isinstance(fighter, dict):
        raise StorageError('Invalid stored fighter AI strategies')
    fighter[str(index)] = deepcopy(data)


@register('PlayerProto:GetAIStrategy')
async def get_ai_strategy(ctx, fields):
    ctx.require_login()
    cids = fields.get('cid')
    if not isinstance(cids, list) or len(cids) > 150:
        raise StorageError('Invalid AI strategy fighter list')
    state = ctx.store.get_player(ctx.uid)
    owned = {card.get('cid') for card in state['cards']}
    requested = []
    for cid in cids:
        cid = bounded_integer(cid, 'AI strategy fighter', 1, 4294967295)
        if cid not in owned or cid in requested:
            raise StorageError('AI strategy fighter must be owned and unique')
        requested.append(cid)
    strategies = state.get('ai_strategies', {})
    if not isinstance(strategies, dict):
        raise StorageError('Invalid stored AI strategies')
    data = []
    for cid in requested:
        saved = strategies.get(str(cid), {})
        if not isinstance(saved, dict):
            raise StorageError('Invalid stored fighter AI strategies')
        rendered = {}
        for key, value in saved.items():
            index = strategy_index(int(key)) if isinstance(key, str) and key.isdigit() else strategy_index(key)
            rendered[index] = strategy_data(stored_strategy_data(value))
        data.append({'cid': cid, 'tStrategyData': rendered})
    return [Reply('PlayerProto:GetAIStrategyRet', {'data': data})]


@register('PlayerProto:SetAIStrategy')
async def set_ai_strategy(ctx, fields):
    ctx.require_login()
    updates = fields.get('data')
    if not isinstance(updates, list) or not updates or len(updates) > 150:
        raise StorageError('Invalid AI strategy update list')
    with ctx.store.transaction(ctx.uid) as tx:
        validated = []
        seen = set()
        for update in updates:
            if not isinstance(update, dict):
                raise StorageError('Invalid AI strategy update')
            team_index = bounded_integer(update.get('nTeamIndex'), 'AI strategy team', 1, 255)
            card_index = bounded_integer(update.get('nCardIndex'), 'AI strategy card slot', 1, 6)
            index = strategy_index(update.get('nStrategyIndex'))
            apply = update.get('bApply', False)
            if not isinstance(apply, bool):
                raise StorageError('AI strategy apply flag must be a boolean')
            _, member = find_team_member(tx.state, team_index, card_index)
            data = strategy_data(update.get('tStrategyData'))
            identity = (member['cid'], index)
            if identity in seen:
                raise StorageError('Duplicate AI strategy update')
            seen.add(identity)
            validated.append((member, index, data, apply))
        for member, index, data, apply in validated:
            save_strategy(tx.state, member['cid'], index, data)
            if apply:
                member['nStrategyIndex'] = index
    return [Reply('PlayerProto:SetAIStrategyRes', {'ret': True})]


@register('PlayerProto:SwitchAIStrategy')
async def switch_ai_strategy(ctx, fields):
    ctx.require_login()
    team_index = bounded_integer(fields.get('nTeamIndex'), 'AI strategy team', 1, 255)
    card_index = bounded_integer(fields.get('nCardIndex'), 'AI strategy card slot', 1, 6)
    index = strategy_index(fields.get('nStrategyIndex'))
    with ctx.store.transaction(ctx.uid) as tx:
        _, member = find_team_member(tx.state, team_index, card_index)
        member['nStrategyIndex'] = index
    return [Reply('PlayerProto:SwitchAIStrategyRes', {'ret': True})]


@register('PlayerProto:Setting')
async def setting(ctx, fields):
    ctx.require_login()
    value=fields.get('equip_state')
    if not isinstance(value,bool):
        raise StorageError('equip_state must be a boolean')
    with ctx.store.transaction(ctx.uid) as tx:
        tx.state.setdefault('settings',{})['equip_state']=value
    return [Reply('PlayerProto:SettingRet',{'res':True})]


@register('PlayerProto:ClickBoard')
async def click_board(ctx, fields):
    ctx.require_login()
    with ctx.store.transaction(ctx.uid) as tx:
        data=tx.state.setdefault('local_events',{})
        data['board_clicks']=int(data.get('board_clicks',0))+1
        data['last_board_click']=int(time.time())
        replies=task_event(tx.state, 'board_click')
    return replies


@register('PlayerProto:SetClientData')
async def set_client_data(ctx, fields):
    ctx.require_login()
    key=text_value(fields.get('key'),128)
    if key.startswith('crosscore_ps_access'):
        raise StorageError('Content access policy is server-owned')
    kind=fields.get('type')
    if kind not in (1,2,3,4):
        # GameMsg.lua:6510 declares key/data/type all optional, and PlayerProto.lua:529-543 is
        # the client's own classifier: number->1, string->2, table->3, nil->4.  A frame that
        # arrives with no usable type carries no value this server could reconstruct, so the
        # request is rejected; the identity of the caller goes to the event log through
        # error_policy.describe() instead of into the player-facing tip.
        error=StorageError('Unsupported client-data type')
        error.diagnostic={'key':key,'type':kind,'data_present':fields.get('data') is not None}
        raise error
    data=fields.get('data','')
    if not isinstance(data,str) or len(data.encode('utf-8'))>20000:
        raise StorageError('Client-data record too large')
    if kind==1 and not math.isfinite(float(data)):
        raise StorageError('Client-data numeric value must be finite')
    if kind==3:
        parsed=json.loads(data)
        if not isinstance(parsed,(dict,list)):
            raise StorageError('Client-data table must be an object or array')
    with ctx.store.transaction(ctx.uid) as tx:
        replies=[]
        if key=='guide_data_key':
            if kind!=3:
                raise StorageError('Tutorial progress must be a table')
            # Explicit module dependency: the tutorial rules live in handlers.progression,
            # not in whichever module happens to register the battle entry protocol.
            rules=sys.modules.get('handlers.progression')
            if rules is None:
                raise StorageError('Tutorial progress requires the local progression module')
            rules.validate_guide_progress(tx.state, parsed)
            replies=task_event(tx.state, 'state_changed')
        if kind==4:
            tx.state['client_data'].pop(key,None)
        else:
            tx.state['client_data'][key]={'data':data,'type':kind}
    return replies  # No SetClientDataRet exists; trusted progress can update tasks.


@register('PlayerProto:GetClientData')
async def get_client_data(ctx, fields):
    ctx.require_login()
    key=text_value(fields.get('key'),128)
    record=ctx.store.get_player(ctx.uid)['client_data'].get(key)
    if record is None:
        record={'type':3,'data':'{}'} if key=='plot_data' else {'type':4}
    if not isinstance(record,dict) or record.get('type') not in (1,2,3,4):
        raise StorageError('Invalid stored client-data record')
    return [Reply('PlayerProto:GetClientDataRet',{'key':key,**deepcopy(record)})]


def validate_team(state, source):
    if not isinstance(source,dict):
        raise StorageError('Team must be a record')
    old=next((v for v in state['teams'] if v['index']==source.get('index')),None)
    if old is None:
        raise StorageError('Team preset is not unlocked')
    members=source.get('data',[])
    if not isinstance(members,list) or len(members)>6:
        raise StorageError('A local team contains at most six owned fighters')
    owned={c['cid'] for c in state['cards']}
    used,slots=set(),set()
    validated=[]
    for member in members:
        if not isinstance(member, dict):
            raise StorageError('Team fighter must be a record')
        cid=member.get('cid')
        index=member.get('index')
        if cid not in owned or cid in used or member.get('bIsNpc',False):
            raise StorageError('Team fighter must be owned and unique')
        if not isinstance(index,int) or not 1<=index<=6 or index in slots:
            raise StorageError('Invalid or duplicate team slot')
        row,col=member.get('row'),member.get('col')
        if row not in (1,2,3) or col not in (1,2,3):
            raise StorageError('Invalid local formation coordinates')
        selected_strategy=strategy_index(member.get('nStrategyIndex', AI_STRATEGY_MIN))
        used.add(cid); slots.add(index)
        validated.append({'cid':cid,'index':index,'row':row,'col':col,
                          'nStrategyIndex':selected_strategy, 'bIsNpc':False})
    leader=source.get('leader',0)
    if (used and leader not in used) or (not used and leader!=0):
        raise StorageError('Leader must belong to the team')
    # Card stats and skill-group level are stored authority, not copied from the request.
    result=deepcopy(old)
    result.update(data=validated,leader=leader,name=text_value(source.get('name',old['name'])))
    for name in ['bIsReserveSP','nReserveNP']:
        if name in source:
            result[name]=source[name]
    if result.get('nReserveNP',0) not in range(0,11):
        raise StorageError('Invalid local reserve NP')
    return result


def save_teams(state, infos):
    if not isinstance(infos,list) or not infos or len(infos)>len(state['teams']):
        raise StorageError('Invalid preset update count')
    if any(not isinstance(v,dict) for v in infos):
        raise StorageError('Invalid preset record')
    if len({v.get('index') for v in infos})!=len(infos):
        raise StorageError('Duplicate preset update')
    updated=[validate_team(state,v) for v in infos]
    for team in updated:
        state['teams']=[deepcopy(team) if v['index']==team['index'] else v for v in state['teams']]
    return updated


@register('PlayerProto:SetTeamData')
async def set_team(ctx, fields):
    ctx.require_login()
    with ctx.store.transaction(ctx.uid) as tx:
        teams=save_teams(tx.state,[fields.get('info')])
    return [Reply('PlayerProto:SetTeamResult',{'info':teams[0]})]


@register('PlayerProto:MultSetTeamData')
async def mult_set_team(ctx, fields):
    ctx.require_login()
    with ctx.store.transaction(ctx.uid) as tx:
        teams=save_teams(tx.state,fields.get('infos'))
    return [Reply('PlayerProto:MultSetTeamResult',{'infos':teams})]


@register('PlayerProto:CardLock')
async def card_lock(ctx, fields):
    ctx.require_login()
    ops=fields.get('ops')
    if not isinstance(ops,list) or not ops or len(ops)>150:
        raise StorageError('Invalid card lock operations')
    with ctx.store.transaction(ctx.uid) as tx:
        cards={c['cid']:c for c in tx.state['cards']}
        for op in ops:
            if op.get('cid') not in cards or op.get('lock') not in (0,1):
                raise StorageError('Unknown fighter or invalid lock')
            cards[op['cid']]['lock']=op['lock']
    return [Reply('PlayerProto:CardLockRet',{'ops':deepcopy(ops)})]


@register('PlayerProto:Sign')
async def signature(ctx, fields):
    ctx.require_login()
    value=fields.get('sign','')
    if value:
        value=text_value(value,128)
    with ctx.store.transaction(ctx.uid) as tx:
        tx.state['player']['sign']=value
    return [Reply('PlayerProto:SignRet',{'sign':value})]


@register('PlayerProto:PlrNameCheckUse')
async def name_check(ctx, fields):
    ctx.require_login()
    name=text_value(fields.get('name'))
    rows=ctx.store.connection.execute('SELECT uid,state_json FROM accounts').fetchall()
    used=any(row['uid']!=ctx.uid and json.loads(row['state_json'])['player']['name']==name for row in rows)
    return [Reply('PlayerProto:PlrNameCheckUseRet',{'isUse':used})]


@register('PlayerProto:SetPlrName')
async def set_player_name(ctx, fields):
    ctx.require_login()
    name=text_value(fields.get('name'))
    sex=fields.get('index')
    if sex not in (1,2):
        raise StorageError('Invalid commander selection')
    birthday=[int(fields.get('month',0)),int(fields.get('day',0))]
    date(2000,*birthday)
    template=commander_template(sex)
    with ctx.store.transaction(ctx.uid) as tx:
        state=tx.state
        if state['login'].get('can_modify_name')!=2:
            raise StorageError('First-time character setup is already complete')
        commander=next(c for c in state['cards'] if c['cfgid'] in (71010,71020))
        changed=deepcopy(template)
        changed.update(cid=commander['cid'],ctime=commander['ctime'])
        commander.update(changed)
        state['player']['name']=name
        state['login'].update(can_modify_name=0,sel_card_ix=sex,birth=birthday,use_vid=int(fields.get('use_vid',1)))
        state['login'].update(icon_id=template['skin'],panel_id=template['skin'],role_panel_id=template['cfgid'])
        icons={key:state['login'][key] for key in ('icon_id','panel_id','role_panel_id')}
        result=[Reply('PlayerProto:CardUpdate',{'cards':[deepcopy(commander)], 'store_exp':int(state.get('store_exp',0))}),
                player_reply(state),Reply('PlayerProto:SetPlrNameRet',icons)]
    return result


@register('PlayerProto:PlrPaneInfo')
async def pane_info(ctx, fields):
    ctx.require_login()
    state=ctx.store.get_player(ctx.uid)
    login,progress=state['login'],state['progress']
    return [Reply('PlayerProto:PlrPaneInfoRet',{'info':{
        'icon_id':login['icon_id'],'icon_frame':login['icon_frame'],
        'sel_card_ix':login['sel_card_ix'],'icon_title':login['icon_title'],
        'role_num':len(state['cards']),'max_dup':max(progress.get('cleared_stages',[]) or [0]),
        'c_time':state['player']['create_time'],'max_tower':0,'max_rank_level':0,'build_control_lv':0}})]
