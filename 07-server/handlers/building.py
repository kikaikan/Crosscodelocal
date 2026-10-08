"""BuildingProto (基地) handlers: a real facility list, construction, upgrades and staffing.

Why this module exists
----------------------
'MatrixView.lua:25' applies the 'matrix_scene_enter' loading weight and only releases it
from 'OpenCurView' ('MatrixView.lua:74-79'). 'OpenCurView' is reached from 'UpdateAll'
('MatrixView.lua:57-69') only when 'MatrixMgr:GetBuildingDatas()' is non-empty, so an
empty 'BuildingProto:BuildsListRet' leaves the client on the loading mask forever while
heartbeats keep working. 'BuildingProto.lua:19-25' also dispatches
'Matrix_Building_Update' only when 'is_finish' is true, and 'MatrixScene.lua:155-156'
looks 'runTypeCfgId' up in 'CfgBGobalPower' directly ('0' is truthy in Lua, so
'0 or 1' stays '0').

Every constant below is read from the client tables and is cross-checked against those
tables by '07-server/tests/test_building.py' instead of being trusted blindly:

  cfgCfgBuidingBase.lua      id/key/type/upCfg/initOpen of each facility
  cfgCfgBControlTowerLvl.lua, cfgCfgBPowerHouseLvl.lua, cfgCfgBProductLvl.lua,
  cfgCfgBTradeLvl.lua, cfgCfgBCompoundLvl.lua, cfgCfgBRemouldLvl.lua
                             powerVal (positive supplies power, negative consumes),
                             maxHp and the ten level keys per facility
  cfgCfgBGobalPower.lua      the 1..4 power states

Deliberate local choices (stated, not disguised as official data)
----------------------------------------------------------------
* Construction and upgrades do not charge currency. The local save has no economy, and
  the new-account seed holds exactly 1000 gold (07-server/data/new_account_seed.json),
  the same price cfgCfgBuidingBase.lua lists for one facility; charging would make
  tutorial group 120 (build the power house, then upgrade the command tower) impossible.
  The client still gates both buttons on the displayed cost
  (MatrixCreateInfo.lua:39-58, MatrixUp.lua:83-157).
* MatrixCreateInfo.lua:74 sends 'pos={0,0}'. A requested cell that is outside the open
  grid or occupied is replaced by the first free cell, and the assigned cell is echoed in
  'BuildCreateRet.pos' so the placement is never faked.
* Production, orders and expeditions are not simulated: 'tFlush' stays 0, so
  'MatrixMgr:GetResetIds()' stays empty and 'GetBuildUpdate' only re-sends stored rows.
"""
from __future__ import annotations

import time

from database import StorageError
from server_core import Reply, register

# tx.state key owned by this module.  It is keyed by facility id (JSON object keys are
# strings) and each row carries 'cfgid'/'level' aliases because tasks.py:200-204 reads
# local building rows with exactly those names.
STATE_KEY = 'buildings'

# cfgglobal_setting.lua:314 g_BuildScale='30,16'; cfgCfgBOpenArea.lua open area 1 is
# startPos={1,1}, scale={22,16}.
GRID_COLUMNS = 30
GRID_ROWS = 16
OPEN_AREA = (22, 16)

# cfgCfgBuidingBase.lua rows, indexed by cfgId.  Type ids follow MatrixCommon.lua:20-30.
BUILDING_CFGS = {
    1001: {'type': 1, 'name': '行星指挥部'},
    1002: {'type': 2, 'name': '能源发电站'},
    1003: {'type': 3, 'name': '挖掘矿场'},
    1004: {'type': 4, 'name': '原料交易所'},
    1006: {'type': 6, 'name': '合成工厂'},
    1009: {'type': 9, 'name': '研发中心'},
}

# cfgCfgBuidingBase.lua rows for the shared dormitory entrance (2001, type 10) and the
# consulting room (2002, type 11).  Neither has a cost row, and MatrixCreate.lua:94-96 lists
# every isShow row of CfgBuidingBase, so tapping 建造 on the dorm plot reaches
# BuildingProto:BuildCreate(2001/2002).  Refusing it left the player on a dead button, so both
# are buildable here as level-1 facilities.  They stay out of BUILDING_CFGS because that map
# is cross-checked against the industrial CfgB*Lvl tables by tests/test_building.py.
#
# Local policies, stated rather than disguised as client data:
#   * 2002's power is the only CfgPhyRoomLvl row's powerVal=0; 2001 has no upCfg at all, so
#     its power contribution is stated as 0 - the dormitory is a scene entrance, not a plant.
#   * Neither client table defines maxHp, so both use ENTRY_MAX_HP.
#   * Both have exactly one level, so Upgrade answers with the client's own 最高等级 path.
ENTRY_CFGS = {
    2001: {'type': 10, 'name': '宿舍'},
    2002: {'type': 11, 'name': '心理辅导室'},
}
ENTRY_MAX_HP = 1000
ALL_CFGS = dict(BUILDING_CFGS)
ALL_CFGS.update(ENTRY_CFGS)

# powerVal per level (1..10) from the CfgB*Lvl tables listed in the module docstring.
_POWER_VAL = {
    1001: (540, 560, 580, 600, 620, 640, 660, 680, 700, 720),
    1002: (140, 180, 220, 260, 300, 340, 380, 420, 460, 500),
    1003: (-260, -280, -300, -320, -340, -360, -380, -400, -420, -440),
    1004: (-100, -110, -120, -130, -140, -150, -160, -170, -180, -190),
    1006: (-180, -190, -200, -210, -220, -230, -240, -250, -260, -270),
    1009: (-90, -100, -110, -120, -130, -140, -150, -160, -170, -180),
    2001: (0,),
    2002: (0,),
}
_MAX_HP = {
    1001: (1000, 1200, 1500, 2000, 2500, 2800, 3000, 3200, 3500, 4000),
    1002: (500, 501, 502, 503, 504, 505, 506, 507, 508, 509),
    1003: (800, 950, 1100, 1250, 1300, 1550, 1700, 1850, 2000, 2150),
    1004: (600, 750, 900, 1050, 1200, 1350, 1500, 1650, 1800, 1950),
    1006: (1000, 1150, 1300, 1450, 1600, 1750, 1900, 2050, 2200, 2350),
    1009: (1000, 1150, 1300, 1450, 1600, 1750, 1900, 2050, 2200, 2350),
    2001: (ENTRY_MAX_HP,),
    2002: (ENTRY_MAX_HP,),
}

# The observed new account shape: the facilities whose cfgCfgBuidingBase.lua row has
# initOpen=true and a level table.  1002/1004/1009 (initOpen=false) are what the base
# tutorial builds.  2001/2002 are the dorm/consulting entry handled by DormProto and have
# no cost row, so they are never part of the building list here.
INITIAL_BUILDINGS = ((1001, (15, 8)), (1003, (11, 8)), (1006, (19, 8)))

# cfgCfgBControlTowerLvl.lua:6-17 buildNumLimit gives every facility one slot at command
# tower level 1 and builSumLimit gives the base eight slots.
CFG_LIMIT = 1
TOTAL_LIMIT = 8

# cfgCfgBGobalPower.lua states, addressed by MatrixScene.lua:155-156.
RUN_TYPE_CFG_IDS = (1, 2, 3, 4)


def level_count(cfgid):
    return len(_POWER_VAL[cfgid])


def _max_hp(cfgid, level):
    return _MAX_HP[cfgid][level - 1]


def _power_val(cfgid, level):
    return _POWER_VAL[cfgid][level - 1]


def _require_int(value, message):
    # bool is an int subclass; a boolean is never a valid protocol integer here.
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError(message)
    return int(value)


def _new_row(building_id, cfgid, level, pos):
    return {
        'id': int(building_id),
        # Lower-case aliases: tasks.py:200-204 reads local building rows by cfgid/level.
        'cfgid': int(cfgid),
        'level': int(level),
        'hp': _max_hp(cfgid, level),
        'pos': [int(pos[0]), int(pos[1])],
        'running': False,
        'tUp': 0,
        'tBuild': 0,
        'tCur': 0.0,
        'tFlush': 0.0,
        'tStop': 0,
        'tPreGifs': 0,
        'tNexGifs': 0,
        'tPreGiftsEx': 0,
        'tNexGiftsEx': 0,
        'roleIds': [],
        'gifts': {},
        'giftsEx': {},
        'perHpPower': 100,
        'perRolePower': 100,
        'perBenefit': 0,
        'perHpBenefit': 0,
        'perRoleTiredBenefit': 0,
        'perRoleAbilityBenefit': 0,
        'roleAbilitys': {},
        'rolePower': 0,
        'productAdd': {},
        'presetRoles': [],
        'curPresetId': 0,
        'ctime': int(time.time()),
    }


def initial_state():
    """The official new-account base shape, built idempotently (never re-rolled)."""
    rows = {}
    for building_id, (cfgid, pos) in enumerate(INITIAL_BUILDINGS, start=1):
        rows[str(building_id)] = _new_row(building_id, cfgid, 1, pos)
    return rows


def valid_state(rows):
    return isinstance(rows, dict) and bool(rows) and all(
        isinstance(row, dict) and 'id' in row and 'cfgid' in row and 'level' in row
        and 'pos' in row for row in rows.values())


def state(tx):
    """Lazily create tx.state['buildings'] for saves written before this module."""
    rows = tx.state.get(STATE_KEY)
    if not valid_state(rows):
        rows = initial_state()
        tx.state[STATE_KEY] = rows
    return rows


def read_state(store, uid):
    """Read the rows without taking a write lock unless lazy init is required."""
    rows = store.get_player(uid).get(STATE_KEY)
    if valid_state(rows):
        return rows
    with store.transaction(uid) as tx:
        rows = state(tx)
    return rows


def row_fields(row):
    """Map one stored row to the sBuildInfo field names the codec and Lua read."""
    cfgid, level = int(row['cfgid']), int(row['level'])
    return {
        'id': int(row['id']),
        'cfgId': cfgid,
        'hp': int(row['hp']),
        'lv': level,
        'tUp': int(row.get('tUp', 0)),
        'tBuild': int(row.get('tBuild', 0)),
        'tCur': float(row.get('tCur', 0.0)),
        'pos': [int(v) for v in row['pos']],
        'gifts': dict(row.get('gifts') or {}),
        'tPreGifs': int(row.get('tPreGifs', 0)),
        'tNexGifs': int(row.get('tNexGifs', 0)),
        'giftsEx': dict(row.get('giftsEx') or {}),
        'tPreGiftsEx': int(row.get('tPreGiftsEx', 0)),
        'tNexGiftsEx': int(row.get('tNexGiftsEx', 0)),
        'roleIds': [int(v) for v in row.get('roleIds') or []],
        'running': bool(row.get('running', False)),
        'perHpPower': int(row.get('perHpPower', 100)),
        'perRolePower': int(row.get('perRolePower', 100)),
        'perBenefit': int(row.get('perBenefit', 0)),
        'perHpBenefit': int(row.get('perHpBenefit', 0)),
        'perRoleTiredBenefit': int(row.get('perRoleTiredBenefit', 0)),
        'perRoleAbilityBenefit': int(row.get('perRoleAbilityBenefit', 0)),
        'tStop': int(row.get('tStop', 0)),
        'roleAbilitys': dict(row.get('roleAbilitys') or {}),
        'rolePower': int(row.get('rolePower', 0)),
        'tFlush': float(row.get('tFlush', 0.0)),
        'productAdd': dict(row.get('productAdd') or {}),
        'presetRoles': list(row.get('presetRoles') or []),
        'curPresetId': int(row.get('curPresetId', 0)),
    }


def _rows(rows):
    return [row_fields(rows[key]) for key in sorted(rows, key=lambda k: int(rows[k]['id']))]


def _power_totals(rows):
    supply = demand = 0
    for row in rows.values():
        value = _power_val(int(row['cfgid']), int(row['level']))
        if value >= 0:
            supply += value
        else:
            demand += -value
    return supply, demand


def run_type_cfg_id(supply, demand):
    """Pick the CfgBGobalPower id whose min/max band holds the surplus percentage.

    cfgCfgBGobalPower.lua bands: 1=-10000..-1000, 2=-999..0, 3=1..19, 4=20..1000.  The
    observed initial base (tower + mine + compound) is 540 supplied against 440 consumed,
    an 18 percent surplus, i.e. the 正常负载 band; adding the power house (+140) reaches 35.
    """
    if supply <= 0:
        return 1 if demand > 0 else 3
    surplus = (supply - demand) * 100 // supply
    if surplus <= -1000:
        return 1
    if surplus <= 0:
        return 2
    if surplus <= 19:
        return 3
    return 4


def _parse_pos(value):
    if not isinstance(value, list) or len(value) < 2:
        return None
    for item in value[:2]:
        if isinstance(item, bool) or not isinstance(item, int):
            return None
    return (int(value[0]), int(value[1]))


def _cell_is_free(rows, cell):
    return all(tuple(int(v) for v in row['pos']) != cell for row in rows.values())


def _cell_is_usable(rows, cell):
    # MatrixCreateInfo.lua:74 sends pos={0,0}; anything outside the open area has to be
    # replaced by a real free cell instead of being stored as a fake placement.
    x, y = cell
    if not (1 <= x <= OPEN_AREA[0] and 1 <= y <= OPEN_AREA[1]):
        return False
    return _cell_is_free(rows, cell)


def _free_cell(rows):
    for y in range(1, OPEN_AREA[1] + 1):
        for x in range(1, OPEN_AREA[0] + 1):
            if _cell_is_free(rows, (x, y)):
                return (x, y)
    return None


def _next_id(rows):
    return max([0] + [int(row['id']) for row in rows.values()]) + 1


@register('BuildingProto:BuildsList')
async def builds_list(ctx, fields):
    """BuildingProto.lua:19-25 - non-empty rows plus is_finish release the loading weight."""
    uid = ctx.require_login()
    rows = read_state(ctx.store, uid)
    return [Reply('BuildingProto:BuildsListRet', {'builds': _rows(rows), 'is_finish': True})]


@register('BuildingProto:BuildsBaseInfo')
async def builds_base_info(ctx, fields):
    """MatrixMgr:SetMatrixInfo; MatrixScene.lua:155-156 needs a valid runTypeCfgId."""
    uid = ctx.require_login()
    rows = read_state(ctx.store, uid)
    supply, demand = _power_totals(rows)
    counts = {}
    roles = set()
    for row in rows.values():
        key = str(int(row['cfgid']))
        counts[key] = counts.get(key, 0) + 1
        roles.update(int(v) for v in row.get('roleIds') or [])
    return [Reply('BuildingProto:BuildsBaseInfoRet', {
        'roleCnt': len(roles),
        'buildCnts': counts,
        # MatrixMgr:GetPower reads power.realCost/power.add; MatrixData:GetBenefit reads
        # power.perBenefit and the perRoleAbilityBenefit sibling field.
        'power': {'realCost': demand, 'add': supply, 'perBenefit': 0},
        'perRolePower': 0,
        'perRoleAbilityBenefit': 0,
        'warningLv': 1,
        'runTypeCfgId': run_type_cfg_id(supply, demand),
        # MatrixMgr.lua:1088-1093 defaults to 1 when the field is absent, so a fresh
        # account keeps the first preset team usable (MatrixRolePresetItem.lua:45 reads
        # it as a truthy Lua number: an explicit 0 would lock even team 1).
        'extraPresetTeamNum': 1,
    })]


@register('BuildingProto:BuildCreate')
async def build_create(ctx, fields):
    """MatrixCreateInfo.lua:74 - BuildCreateRet{ok} plus the AddNotice data push."""
    uid = ctx.require_login()
    cfgid = _require_int(fields.get('cfgId'), '缺少建筑配置编号。')
    if cfgid not in ALL_CFGS:
        raise StorageError('本地服务没有可建造的设施配置：%d。' % cfgid)
    requested = _parse_pos(fields.get('pos'))
    with ctx.store.transaction(uid) as tx:
        rows = state(tx)
        if any(int(row['cfgid']) == cfgid for row in rows.values()):
            raise StorageError('%s 已经建造过了。' % ALL_CFGS[cfgid]['name'])
        if len(rows) >= TOTAL_LIMIT:
            raise StorageError('基地设施数量已达上限。')
        pos = requested if requested and _cell_is_usable(rows, requested) else _free_cell(rows)
        if pos is None:
            raise StorageError('基地内没有空余位置。')
        row = _new_row(_next_id(rows), cfgid, 1, pos)
        rows[str(row['id'])] = row
        pushed = [row_fields(row)]
    return [Reply('BuildingProto:AddNotice', {'builds': pushed, 'is_finish': True}),
            Reply('BuildingProto:BuildCreateRet', {'cfgId': cfgid, 'pos': [pos[0], pos[1]], 'ok': True})]


@register('BuildingProto:Upgrade')
async def upgrade(ctx, fields):
    """MatrixUp.lua:169 - UpgradeRet{ok} plus the AddNotice row carrying the new level."""
    uid = ctx.require_login()
    building_id = _require_int(fields.get('id'), '升级请求缺少建筑编号。')
    with ctx.store.transaction(uid) as tx:
        rows = state(tx)
        row = rows.get(str(building_id))
        if row is None:
            raise StorageError('本地存档没有这座设施：%d。' % building_id)
        cfgid, level = int(row['cfgid']), int(row['level'])
        if level >= level_count(cfgid):
            raise StorageError('%s 已达到最高等级。' % ALL_CFGS[cfgid]['name'])
        row['level'] = level + 1
        row['hp'] = _max_hp(cfgid, level + 1)
        row['tUp'] = 0
        row['tBuild'] = 0
        pushed = [row_fields(row)]
    return [Reply('BuildingProto:AddNotice', {'builds': pushed, 'is_finish': True}),
            Reply('BuildingProto:UpgradeRet', {'id': building_id, 'ok': True})]


@register('BuildingProto:BuildSetRole')
async def build_set_role(ctx, fields):
    """MatrixSetRole.lua:69 / MatrixRolePresetItem.lua:186.

    'infos' mixes facility ids with dorm room ids (MatrixSetRole's 'data' is a MatrixData
    or a DormRoomData; RoomType.building decides which).  Only the facility rows are stored
    here - dorm rooms belong to DormProto - and ids that are not facilities are skipped
    without claiming their assignment happened.
    """
    uid = ctx.require_login()
    infos = fields.get('infos')
    if not isinstance(infos, list) or not infos:
        raise StorageError('入驻请求缺少建筑信息。')
    applied = []
    with ctx.store.transaction(uid) as tx:
        rows = state(tx)
        for info in infos:
            if not isinstance(info, dict):
                raise StorageError('入驻信息格式不正确。')
            building_id = _require_int(info.get('id'), '入驻信息缺少建筑编号。')
            role_ids = info.get('roleIds')
            if role_ids is None:
                role_ids = []
            if not isinstance(role_ids, list):
                raise StorageError('驻员编号必须是数组。')
            role_ids = [_require_int(value, '驻员编号必须是整数。') for value in role_ids]
            team_id = info.get('teamId')
            if team_id is not None:
                team_id = _require_int(team_id, '队伍编号必须是整数。')
            row = rows.get(str(building_id))
            if row is None:
                continue
            row['roleIds'] = role_ids
            if team_id is not None:
                row['curPresetId'] = team_id
            applied.append({'id': building_id, 'roleIds': list(role_ids),
                            'teamId': int(row['curPresetId'])})
        pushed = [row_fields(rows[str(item['id'])]) for item in applied]
    replies = []
    if pushed:
        # MatrixMgr:AddNotice updates the existing row before the Dorm_SetRoleList refresh
        # reads it in BuildSetRoleRet.
        replies.append(Reply('BuildingProto:AddNotice', {'builds': pushed, 'is_finish': True}))
    replies.append(Reply('BuildingProto:BuildSetRoleRet', {'infos': applied}))
    return replies


@register('BuildingProto:GetBuildUpdate')
async def get_build_update(ctx, fields):
    """MenuView.lua:326 calls GetBuildUpdate; the schema has no *Ret.

    The only client-side waiter is BuildingProto.GetBuildUpdateCB, consumed by the
    'BuildingProto:UpdateNotices' handler (BuildingProto.lua:298-314), so the honest answer
    for this request is that message.  Nothing is simulated: the stored rows are re-sent.
    """
    uid = ctx.require_login()
    ids = fields.get('ids')
    # Validate before touching the store: a rejected request must not lazily create state.
    if ids is None:
        wanted = None
    elif isinstance(ids, list):
        wanted = [_require_int(value, '建筑编号必须是整数。') for value in ids]
    else:
        raise StorageError('建筑编号列表格式不正确。')
    rows = read_state(ctx.store, uid)
    if wanted is None:
        wanted = [int(row['id']) for row in rows.values()]
    selected = [rows[str(value)] for value in wanted if str(value) in rows]
    return [Reply('BuildingProto:UpdateNotices',
                  {'builds': [row_fields(row) for row in selected], 'is_finish': True})]
