"""AbilityProto (玩家战术能力 + 技能组) handlers over the client's CfgPlrAbility /
CfgPlrSkillGroup tables.

Why this module exists
----------------------
'AbilityProto.lua:44-47' requests GetAbility when PlayerAbilityMgr initializes, and
'AbilityProto.lua:50-53' sends AddAbility(id) from the unlock dialog
('PlayerAbilityMgr.lua:178-181').  The only response schema in that family is
AbilityProto:GetAbilityRet (3311), and 'PlayerAbilityMgr.lua:22-35 SetData' is its only
consumer: it locks every configured ability, unlocks the ids carried in 'abilitys' and
dispatches Update_PlayerAbility.  Answering AddAbility with GetAbilityRet is therefore the
client's own refresh path - there is no AddAbilityRet to send, and fabricating one would
not encode.

Data: 07-server/data/plr-ability.json, generated from the client table
'03-unpack/lua/device-luascripts/cfgCfgPlrAbility.lua' by data/plr-ability-build.py.  Every
number is copied from that table; nothing here invents balance values.

Cost: cfgglobal_setting.lua:156 g_AbilityCoinId=10020, which this server mirrors as
login.ability_num / inventory 10020 (database.py:42-44).  A new account holds 50 points
(data/new_account_seed.json login.ability_num), exactly one root unlock.

The skill group family (this file, added)
-----------------------------------------
Schemas verified in 05-protocol/endpoints.json (real names, never guessed):
  AbilityProto:GetSkillGroup        3302  {}                          (c2s)
  AbilityProto:GetSkillGroupRet     3303  {groups: map|sSkillGroup|id} (s2c, 1 observed sample)
  AbilityProto:SkillGroupUpgrade    3304  {id: uint}                  (c2s)
  AbilityProto:SkillGroupUpgradeRet 3305  {group: struts|sSkillGroup} (s2c)
  AbilityProto:SkillGroupUse        3306  {id: uint, team_id: uint}   (c2s)
  AbilityProto:SkillGroupUseRet     3307  {id: uint, team_id: uint}   (s2c)
and sSkillGroup (3301) = {id: uint, lv: uint, skill_ids: array|uint}.

Client consumption path (what the client actually reads)
  * AbilityProto.lua:7-14   GetSkillGroupRet -> TacticsMgr:SetData(proto.groups).
    TacticsMgr.lua:17-27 calls Init() first (one TacticsData per Cfgs.CfgPlrSkillGroup row,
    all locked) and then SetData only for the map entries: TacticsData:SetData
    (TacticsData.lua:12-17) stores sSkillGroup and calls InitCfg(sSkillGroup.id), which
    resolves Cfgs.CfgPlrSkillGroup:GetByID(id).  So the only field that must exist on every
    map entry is 'id'; the map key itself is not read (the codec rebuilds it from item.id).
  * TacticsData:GetLv (lua:50-52) reads 'lv' (default 1 when absent).
  * TacticsData:GetSkills/GetSkillsIds (lua:55-63, 95-99) read 'skill_ids' when unlocked,
    falling back to cfg.aSkillIds.  AbilityInfoView.lua:83-104 and PlayerAbilityInfo.lua:154-168
    render exactly these three fields via TacticsMgr:GetDataByID(cfg.active_id).
  * AbilityInfoView.lua:196-207 / PlayerAbility.lua:245-253 upgrade path:
    group = TacticsMgr:GetDataByID(cfg.active_id); AbilityProto:SkillGroupUpgrade(group:GetCfgID()).
    AbilityProto.lua:36-41 -> TacticsMgr:UpdateData(proto.group): removes the old entry whose
    GetCfgID()==group.id and re-inserts TacticsData:SetData(group).  Again only id/lv/skill_ids
    are read; a group missing 'id' would be dropped.
  * TeamView.lua:1855-1874 OnSkillChange: AbilityProto:SkillGroupUse(cfgId, TeamMgr.currentIndex,
    callback); cfgId is 0 when the player clears the selection ('cfgId=cfgId or 0').  The
    callback ignores the reply fields entirely and locally calls teamData:SetSkillGroupID(cfgId);
    TeamData.lua:622-653 resolves 0/nil to g_DefaultAbilityId (1003) when that group is unlocked,
    and GetSkillGroupID falls back to the same default.  So the ret is a completion signal; the
    authoritative assignment is whatever this server persisted into the team snapshot.
  * TeamMgr.lua:393/787 build PlayerProto:SetTeamData/duplicate payloads with
    skill_group_id=teamData.skillGroupID; player_state.validate_team keeps the stored value
    ("stored authority, not copied from the request"), so SkillGroupUse - not SetTeamData - is the
    only writer of a team's tactic.  battle.py:241-243 also reads the stored skill_group_id.

Data: 07-server/data/plr-skill-group.json, generated from CfgPlrSkillGroup /
CfgPlrSkillGroupUpgrade / CfgPlrAbility(type==1) / cfgglobal_setting.g_DefaultAbilityId by
data/plr-skill-group-build.py.  Every number is copied verbatim.

State: state['skill_groups'] = {'lv': {str(group_id): int}} stores only levels above the
initial 1; unlock is derived from state['abilities']['abilitys'] plus the config's
ability_id owner, exactly as the client derives it from cfg.active_id.  New saves get the
block lazily; no schema migration.

Local policies（本地策略，无官服样本）; these are this server's explicit choices
  * A group becomes usable the moment its CfgPlrAbility row (type==1) is unlocked by AddAbility.
    No captured SkillGroupGet/unlock exchange shows whether the official server materialises a
    group at AddAbility time, so unlock is derived instead of stored.
  * SkillGroupUpgrade consumes CfgPlrSkillGroupUpgrade.infos[lv].costs[1] (item 10020) in the
    amount costs[2], i.e. the client's own TacticsData:GetCost rule; 1->2 costs 100 and 2->3
    costs 200.  No SkillGroupUpgrade exchange was captured, so the cost formula is a local
    policy read from the client table, not an observed server rule.
  * costs[3] (always 2) is unused by TacticsData:GetCost and is left untouched rather than guessed.
  * SkillGroupUse(0, team) resolves to default_group (1003) when that group is unlocked, mirroring
    TeamData.lua:622-653; otherwise it stores 0.  Any number of teams may share one group (the
    seed puts all three teams on 1003); there is no per-group exclusivity or cooldown.
  * SkillGroupUpgrade also refreshes skill_group_lv on every team currently using the group so the
    persisted team snapshot stays coherent.  The client (TeamData) does not read that field and
    battle.py does not use it, so this is bookkeeping only.

Explicit gaps
  * The recovered device Lua never sends 3302: the only reference is the commented-out
    'AbilityProto:GetSkillGroup()' at TacticsMgr.lua:13.  The handler is registered so the
    request works when the client (or the tutorial harness) makes it, but TacticsMgr stays
    unpopulated until then; adding a login/InitFinish push would touch server_core or
    initialization, outside this module's write scope.
  * AbilityProto:ResetAbility still has no handler: the refund rule has no official sample.
  * battle.py:241-248 still selects a group's *base* aSkillIds (battle-commander-skills.json is
    the lv-1 list), so an upgraded group's lv-N skill_ids are pushed to the client but not yet
    consumed by the offline battle simulation.  Wiring per-level commander skills is out of scope
    for this protocol family.
"""
from __future__ import annotations

import json
from pathlib import Path

from config_codec import app_path
from database import StorageError
from server_core import Reply, register

# Feature gate owner (progression-gates.md: 战术 PlayerAbility = 通关 0-7 工厂 ID 1007).
# Imported the same way handlers/shop.py does it: the gate table lives with the progression
# rules, not here, so the local answer cannot drift from the client's own unlock条件.
from handlers.initialization import feature_open

# tx.state key owned by this module.  Shape: {'abilitys': [int, ...], 'lastResetTime': int}.
# 'abilitys' is kept as a sorted list of ids rather than a list of rows because the wire
# schema sAbility carries the id alone (GameMsg.lua:3803).
STATE_KEY = 'abilities'
ABILITY_COIN = 'ability_num'
CONFIG_PATH = app_path('data', 'plr-ability.json')

# Second tx.state key owned by this module.  Shape: {'lv': {str(group_id): int}} and only
# for levels above the initial 1; see the module docstring for why unlock is derived.
SKILL_GROUP_STATE_KEY = 'skill_groups'
SKILL_GROUP_CONFIG_PATH = app_path('data', 'plr-skill-group.json')

_CONFIG = None
_SKILL_GROUP_CONFIG = None


def configs():
    """Return {id string: cfg row}; a missing table is a hard local-data error."""
    global _CONFIG
    if _CONFIG is None:
        if not CONFIG_PATH.is_file():
            raise StorageError('缺少本地能力配置数据：data/plr-ability.json。')
        _CONFIG = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))['abilities']
    return _CONFIG


def skill_group_config():
    """Return the whole generated tactic file: groups, default_group, sources."""
    global _SKILL_GROUP_CONFIG
    if _SKILL_GROUP_CONFIG is None:
        if not SKILL_GROUP_CONFIG_PATH.is_file():
            raise StorageError('缺少本地战术配置数据：data/plr-skill-group.json。')
        _SKILL_GROUP_CONFIG = json.loads(SKILL_GROUP_CONFIG_PATH.read_text(encoding='utf-8'))
    return _SKILL_GROUP_CONFIG


def skill_groups():
    """Return {id string: cfg row} for Cfgs.CfgPlrSkillGroup."""
    return skill_group_config()['groups']


def default_group():
    return int(skill_group_config()['default_group'])


def _require_int(value, message):
    # bool is an int subclass; a boolean is never a valid protocol integer here.
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError(message)
    return int(value)


def valid_state(value):
    return (isinstance(value, dict) and isinstance(value.get('abilitys'), list)
            and all(isinstance(item, int) and not isinstance(item, bool) for item in value['abilitys'])
            and isinstance(value.get('lastResetTime'), int)
            and not isinstance(value.get('lastResetTime'), bool))


def state(tx):
    """Lazily create the block for saves written before this module existed."""
    stored = tx.state.get(STATE_KEY)
    if not valid_state(stored):
        stored = {'abilitys': [], 'lastResetTime': 0}
        tx.state[STATE_KEY] = stored
    return stored


def read_state(store, uid):
    """Read the block without taking a write lock unless lazy init is required."""
    saved = store.get_player(uid)
    stored = saved.get(STATE_KEY)
    if valid_state(stored):
        return stored, saved
    with store.transaction(uid) as tx:
        state(tx)
    saved = store.get_player(uid)
    return saved[STATE_KEY], saved


def valid_skill_group_state(value):
    if not isinstance(value, dict) or not isinstance(value.get('lv'), dict):
        return False
    for key, level in value['lv'].items():
        if not (isinstance(key, str) and key.isdigit()):
            return False
        if isinstance(level, bool) or not isinstance(level, int) or level < 2:
            return False
    return True


def skill_group_state(tx):
    """Lazily create the tactic block for saves written before this module existed."""
    stored = tx.state.get(SKILL_GROUP_STATE_KEY)
    if not valid_skill_group_state(stored):
        stored = {'lv': {}}
        tx.state[SKILL_GROUP_STATE_KEY] = stored
    return stored


def read_skill_group_state(store, uid):
    """Read the tactic block without taking a write lock unless lazy init is required."""
    saved = store.get_player(uid)
    stored = saved.get(SKILL_GROUP_STATE_KEY)
    if valid_skill_group_state(stored):
        return stored, saved
    with store.transaction(uid) as tx:
        skill_group_state(tx)
    saved = store.get_player(uid)
    return saved[SKILL_GROUP_STATE_KEY], saved


def reply_fields(stored, points):
    return {'num': int(points),
            'abilitys': [{'id': ability_id} for ability_id in stored['abilitys']],
            'lastResetTime': int(stored['lastResetTime'])}


def group_level(stored, group_id):
    """Current level of an unlocked group; the client's own default is 1."""
    level = stored['lv'].get(str(int(group_id)))
    return level if isinstance(level, int) and not isinstance(level, bool) and level >= 1 else 1


def group_wire(cfg, level):
    """sSkillGroup (GameMsg.lua:3768): id + lv + the level-N skill list.

    CfgPlrSkillGroup.aSkillIds lists the level-1 ids and cfgskill.lua carries every level, so a
    level-N group sends base..base+N-1.  This is the rule the official 3303 sample shows for all
    five groups (1001 lv3 -> 1000103/1000203/1000303; 1003 lv2 -> 1020102/1020202/1020302).
    """
    offset = int(level) - 1
    return {'id': int(cfg['id']), 'lv': int(level),
            'skill_ids': [int(value) + offset for value in cfg['skill_ids']]}


def unlocked_abilitys(saved):
    stored = saved.get(STATE_KEY)
    return list(stored['abilitys']) if valid_state(stored) else []


def locked_group(cfg):
    return cfg.get('ability_id') is None


def reply_groups(abilitys, levels):
    """Non-empty only for groups whose owning CfgPlrAbility row is unlocked."""
    owned = set(abilitys)
    result = {}
    for key, cfg in skill_groups().items():
        if locked_group(cfg) or cfg['ability_id'] not in owned:
            continue
        level = levels.get(key)
        level = level if isinstance(level, int) and not isinstance(level, bool) and level >= 1 else 1
        result[int(cfg['id'])] = group_wire(cfg, level)
    return result


@register('AbilityProto:GetAbility')
async def get_ability(ctx, fields):
    """AbilityProto.lua:44-47 -> AbilityProto:GetAbilityRet.

    Read-only: a valid block must not bump the save revision just because the client asked.
    """
    uid = ctx.require_login()
    saved = ctx.store.get_player(uid)
    if not feature_open(saved, 'PlayerAbility'):
        # Same contract as the old initialization.py handler: a locked feature reads as
        # "nothing unlocked" instead of turning a login-time read into an error tip.
        return [Reply('AbilityProto:GetAbilityRet',
                      {'num': 0, 'abilitys': [], 'lastResetTime': 0})]
    stored, saved = read_state(ctx.store, uid)
    return [Reply('AbilityProto:GetAbilityRet',
                  reply_fields(stored, saved['login'].get('ability_num', 0)))]


@register('AbilityProto:AddAbility')
async def add_ability(ctx, fields):
    """PlayerAbilityMgr.lua:178-181 -> the same GetAbilityRet refresh.

    Rejections are business errors: the caller turns them into a SystemProto:Tips and the
    session stays open (07-server/error_policy.py).
    """
    uid = ctx.require_login()
    ability_id = _require_int(fields.get('id'), '缺少能力编号。')
    entry = configs().get(str(ability_id))
    if entry is None:
        raise StorageError('本地服务没有这个能力配置：%d。' % ability_id)
    if not feature_open(ctx.store.get_player(uid), 'PlayerAbility'):
        raise StorageError('战术能力尚未开放。')
    with ctx.store.transaction(uid) as tx:
        stored = state(tx)
        if ability_id in stored['abilitys']:
            raise StorageError('%s 已经解锁过了。' % entry['name'])
        level = int(tx.state['player'].get('level', 1))
        open_lv = int(entry['open_lv'])
        if level < open_lv:
            raise StorageError('%s 需要指挥官等级 %d。' % (entry['name'], open_lv))
        missing = [value for value in entry['prev_id'] if value not in stored['abilitys']]
        if missing:
            names = '、'.join(configs()[str(value)]['name'] for value in missing)
            raise StorageError('%s 需要先解锁：%s。' % (entry['name'], names))
        cost = int(entry['cost_num'])
        if tx.currency(ABILITY_COIN) < cost:
            raise StorageError('战术点数不足，%s 需要 %d 点。' % (entry['name'], cost))
        tx.add_currency(ABILITY_COIN, -cost)
        stored['abilitys'] = sorted(stored['abilitys'] + [ability_id])
        pushed = reply_fields(stored, tx.currency(ABILITY_COIN))
    return [Reply('AbilityProto:GetAbilityRet', pushed)]


@register('AbilityProto:GetSkillGroup')
async def get_skill_group(ctx, fields):
    """AbilityProto.lua:7-10 -> AbilityProto:GetSkillGroupRet (map|sSkillGroup|id).

    TacticsMgr:SetData rebuilds from Cfgs.CfgPlrSkillGroup on every call, so a locked feature
    or a save with no unlocked ability simply reads as an empty map - never an error tip.
    """
    uid = ctx.require_login()
    saved = ctx.store.get_player(uid)
    if not feature_open(saved, 'PlayerAbility'):
        return [Reply('AbilityProto:GetSkillGroupRet', {'groups': {}})]
    stored, saved = read_skill_group_state(ctx.store, uid)
    return [Reply('AbilityProto:GetSkillGroupRet',
                  {'groups': reply_groups(unlocked_abilitys(saved), stored['lv'])})]


@register('AbilityProto:SkillGroupUpgrade')
async def skill_group_upgrade(ctx, fields):
    """AbilityProto.lua:31-34 -> AbilityProto:SkillGroupUpgradeRet {group: sSkillGroup}.

    Validate -> transact -> reply.  Costs come from CfgPlrSkillGroupUpgrade.infos[lv] exactly
    as TacticsData:GetCost reads them; see the module docstring for the local-policy caveat.
    """
    uid = ctx.require_login()
    group_id = _require_int(fields.get('id'), '缺少战术编号。')
    entry = skill_groups().get(str(group_id))
    if entry is None:
        raise StorageError('本地服务没有这个战术配置：%d。' % group_id)
    if not feature_open(ctx.store.get_player(uid), 'PlayerAbility'):
        raise StorageError('战术能力尚未开放。')
    with ctx.store.transaction(uid) as tx:
        owned = state(tx)
        if locked_group(entry) or entry['ability_id'] not in owned['abilitys']:
            # Client side this is TacticsMgr:GetDataByID(active_id) returning nil.
            raise StorageError('%s 还没有解锁，先解锁对应能力。' % entry['name'])
        stored = skill_group_state(tx)
        level = group_level(stored, group_id)
        max_level = int(entry['max_lv'])
        if level >= max_level:
            raise StorageError('%s 已经满级。' % entry['name'])
        upgrade = next((row for row in entry['upgrades'] if row.get('index') == level), None)
        costs = upgrade.get('costs') if isinstance(upgrade, dict) else None
        if not isinstance(costs, list) or len(costs) < 2:
            raise StorageError('本地战术升级配置缺少消耗：%s。' % entry['name'])
        currency, price = int(costs[0]), int(costs[1])
        if price <= 0 or tx.item_count(currency) < price:
            raise StorageError('战术点数不足，%s 升级到 %d 级需要 %d 点。'
                               % (entry['name'], level + 1, price))
        tx.add_item(currency, -price)
        new_level = level + 1
        stored['lv'][str(group_id)] = new_level
        pushed = group_wire(entry, new_level)
        # Local policy: keep the persisted team snapshot's level in step with the group it uses.
        for team in tx.state['teams']:
            if team.get('skill_group_id') == group_id:
                team['skill_group_lv'] = new_level
    return [Reply('AbilityProto:SkillGroupUpgradeRet', {'group': pushed})]


@register('AbilityProto:SkillGroupUse')
async def skill_group_use(ctx, fields):
    """AbilityProto.lua:17-21 -> AbilityProto:SkillGroupUseRet {id, team_id}.

    Persists the chosen tactic onto the team snapshot (state['teams'][index].skill_group_id),
    because player_state.validate_team deliberately refuses to copy client-supplied group ids
    and battle.py:241-243 reads the stored value.  id=0 means "clear", which TeamData.lua:622-653
    resolves back to g_DefaultAbilityId (1003); this server mirrors that locally.
    """
    uid = ctx.require_login()
    group_id = _require_int(fields.get('id'), '缺少战术编号。')
    team_id = _require_int(fields.get('team_id'), '缺少队伍编号。')
    entry = None
    if group_id == 0:
        entry = skill_groups().get(str(default_group()))
    else:
        entry = skill_groups().get(str(group_id))
        if entry is None:
            raise StorageError('未知的战术编号：%d。' % group_id)
    if not feature_open(ctx.store.get_player(uid), 'PlayerAbility'):
        raise StorageError('战术能力尚未开放。')
    with ctx.store.transaction(uid) as tx:
        team = next((row for row in tx.state['teams'] if row.get('index') == team_id), None)
        if team is None:
            raise StorageError('队伍不存在：%d。' % team_id)
        owned = state(tx)
        stored = skill_group_state(tx)
        if group_id != 0 and (locked_group(entry) or entry['ability_id'] not in owned['abilitys']):
            raise StorageError('%s 还没有解锁。' % entry['name'])
        if entry is not None and not locked_group(entry) and entry['ability_id'] in owned['abilitys']:
            assigned = int(entry['id'])
            level = group_level(stored, assigned)
        else:
            # Nothing to fall back to (default group locked too): the team carries no tactic.
            assigned, level = 0, 0
        team['skill_group_id'] = assigned
        team['skill_group_lv'] = level
    return [Reply('AbilityProto:SkillGroupUseRet', {'id': assigned, 'team_id': team_id})]
