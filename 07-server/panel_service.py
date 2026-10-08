"""Board presets: the original six slots plus the random-board family.

Storage under state['panels']:

    panels         {str(idx): sNewPanel}  original six slots, idx 1..6, ty=1
    setting        rotation setting sent by CRoleDisplayMain
    random         0 = six-slot board, 1 = random board in use
    using          selected slot 1..6, or the random-board idx (>= 7) when random=1
    update_time    local_time stamp of the last change
    random_type    1 single, 2 double, 3 all (GEnum.lua:2275 eRandomPanelType)
    random_panels  {str(idx): sNewPanel}  random boards, idx >= 7, ty 2/3
    random_idx     next free random idx; starts at 7 and only grows
    name_list      [{'first': name, 'second': random_type}]

random_type defaults to 1, not 0: CRoleDisplayMgr.lua:89-91 falls back with
'panelRet.random_type or SINGLE', and Lua treats 0 as truthy, so a stored 0
would make GetRandomPanels(0) return an empty list. random_idx starts at 7
(CRoleDisplayMgr.lua:17) and the per-type cap is g_RandomKanbanQuantity=500
(cfgglobal_setting.lua:146).
"""
from copy import deepcopy
import math
from database import StorageError

RANDOM_SLOT_MIN = 7            # CRoleDisplayMgr.lua:17
RANDOM_LIMIT = 500             # cfgglobal_setting.lua:146
RANDOM_TYPES = (1, 2, 3)       # eRandomPanelType SINGLE/DOUBLE/ALL
MAX_UINT = 4294967295


def integer(value, message, low=0, high=MAX_UINT):
    """Strict uint32 for wire fields; bool is rejected because bool is an int."""
    if type(value) is not int or not low <= value <= high:
        raise StorageError(message)
    return value


def available(state, identifier):
    from handlers.shop import catalog
    if identifier < 10000:
        cfg = catalog('cfgCfgArchiveMultiPicture.lua').get(identifier)
        return bool(cfg and int(state['inventory'].get(str(cfg.get('itemId')), 0)) > 0)
    from skins_service import forms, validate_model, SkinRejected
    for card in state['cards']:
        try:
            primary, alternate = forms(state, card)
            validate_model(state, card, identifier, 1, primary + alternate)
            return True
        except SkinRejected:
            pass
    return False


def owned_background(state, background):
    """The account's current background, or a menu background the save owns."""
    from handlers.shop import catalog
    cfg = catalog('cfgCfgMenuBg.lua').get(background)
    if not cfg:
        return False
    if background == int(state.get('login', {}).get('background_id', 1)):
        return True
    return int(state['inventory'].get(str(cfg.get('item_id')), 0)) > 0


def random_type_value(value):
    """Read-side normalization; an unknown stored type reads as SINGLE."""
    return value if type(value) is int and value in RANDOM_TYPES else 1


def strict_random_type(value, message='随机看板类型无效，必须是 1、2 或 3。'):
    return integer(value, message, 1, 3)


def random_index(value, message='随机看板下标无效，必须是 7 或更大的整数。'):
    return integer(value, message, RANDOM_SLOT_MIN)


def in_type(row, random_type):
    """True when a stored board belongs to the page random_type selects."""
    if random_type == 3:
        return isinstance(row, dict)
    return isinstance(row, dict) and row.get('ty') == random_type + 1


def boards(section, random_type):
    """Sorted indices of the boards on one random page (ALL selects every page)."""
    rows = section.get('random_panels', {})
    if random_type == 3:
        return sorted(int(key) for key, row in rows.items() if isinstance(row, dict))
    return sorted(int(key) for key, row in rows.items()
                  if isinstance(row, dict) and row.get('ty') == random_type + 1)


def type_ids(section, random_type):
    found = set()
    for row in section.get('random_panels', {}).values():
        if not in_type(row, random_type):
            continue
        for identifier in row.get('ids') or []:
            if type(identifier) is int and identifier:
                found.add(identifier)
    return found


def retreat(section):
    """Point 'using' at the first board of the current page, else at slot 1."""
    candidates = boards(section, random_type_value(section.get('random_type')))
    if candidates:
        section['random'] = 1
        section['using'] = candidates[0]
    else:
        section['random'] = 0
        section['using'] = 1


def _rows(value):
    if not isinstance(value, dict):
        return {}
    return {str(key): deepcopy(row) for key, row in value.items() if isinstance(row, dict)}


def _names(value):
    if not isinstance(value, list):
        return []
    return [{'first': row['first'], 'second': row['second']} for row in value
            if isinstance(row, dict) and isinstance(row.get('first'), str)
            and type(row.get('second')) is int and row['second'] in RANDOM_TYPES]


def normalized(section):
    """Tolerant copy of a panels section; legacy saves read as usable defaults."""
    section = section if isinstance(section, dict) else {}
    stored_random = section.get('random')
    stored_using = section.get('using')
    stored_idx = section.get('random_idx')
    return {
        'panels': _rows(section.get('panels')),
        'setting': section.get('setting') if type(section.get('setting')) is int and 0 <= section['setting'] <= MAX_UINT else 0,
        'random': 1 if type(stored_random) is int and stored_random == 1 else 0,
        'using': stored_using if type(stored_using) is int and stored_using >= 1 else 1,
        'update_time': section.get('update_time') if type(section.get('update_time')) is int and section['update_time'] >= 0 else 0,
        'random_type': random_type_value(section.get('random_type')),
        'random_panels': _rows(section.get('random_panels')),
        'random_idx': stored_idx if type(stored_idx) is int and stored_idx >= RANDOM_SLOT_MIN else RANDOM_SLOT_MIN,
        'name_list': _names(section.get('name_list')),
    }


def stored(state):
    return normalized(state.get('panels'))


def persist(state, section):
    """Write a normalized section back and stamp its update time."""
    from handlers.initialization import local_time
    section['update_time'] = local_time(state)
    state['panels'] = section


def validate_panel(state, row, *, random=False):
    """Validate one sNewPanel; returns the normalized row.

    ty=1 keeps the original six-slot layout (idx 1..6, idx 6 holds two images).
    ty=2/3 are the random single/double pages: idx >= 7, exactly one image for a
    single board, two pads for a double board. Every image must be really owned
    (panel_service.available), which follows the whole role_id family that
    skins_service.forms exposes, while an unowned family skin still fails.
    """
    if not isinstance(row, dict):
        raise StorageError('看板数据格式不正确。')
    row = deepcopy(row)
    kind = integer(row.get('ty', 1), '看板类型无效。', 1, 3)
    if random and kind == 1:
        raise StorageError('随机看板必须是单人页或双人页看板。')
    if not random and kind != 1:
        raise StorageError('六格看板槽位只支持原有看板类型。')
    if kind == 1:
        index = integer(row.get('idx'), '看板槽位下标无效。', 1, 6)
        slots = 2 if index == 6 else 1
    else:
        index = random_index(row.get('idx'))
        slots = 1 if kind == 2 else 2
    ids = row.get('ids')
    if not isinstance(ids, list):
        raise StorageError('看板图片列表格式不正确。')
    if kind == 3 and len(ids) == 1:
        # CRoleDisplaySItemConta.lua:64 creates a ty=3 board whose ids only hold
        # one entry (CRoleDisplayData.lua:35-40 makes IsTwoRole() false for ty=3)
        # and CRoleDisplay.lua:56 fills one slot before the first save. The list
        # view reads ids[2] for every stored double board
        # (CRoleDisplaySItemConta.lua:23-26), so the second pad is stored as 0.
        ids = [ids[0], 0]
    if len(ids) != slots:
        raise StorageError('看板槽位布局不正确。')
    for identifier in ids:
        integer(identifier, '看板图片编号无效。')
        if identifier and not available(state, identifier):
            raise StorageError('看板图片不存在或尚未拥有。')
    if kind in (2, 3) and not any(ids):
        raise StorageError('随机看板至少要有一张图片。')
    if kind == 1 and index == 6:
        # Only the original six-slot double pad is character-only: the client
        # hides the illustration tabs exactly when IsTwoRole() is true, which is
        # CRoleDisplayData.lua:35-40 index==6 of ty=1. A ty=3 random double page
        # keeps all three tabs (CRoleSelectView.lua:228-236), so an illustration
        # is a layout the client really offers there and must not be refused.
        if any(0 < identifier < 10000 for identifier in ids):
            raise StorageError('插图或特写不能放进六格看板的双人槽位。')
    background = integer(row.get('bg', 1), '看板背景编号无效。', 1)
    if not owned_background(state, background):
        raise StorageError('看板背景尚未拥有。')
    row.update(idx=index, ids=list(ids), bg=background, ty=kind)
    for slot in (1, 2):
        detail = row.get('detail' + str(slot), {})
        if not isinstance(detail, dict):
            raise StorageError('看板摆放参数格式不正确。')
        detail = {'x': 0, 'y': 0, 'scale': 1, 'live2d': False, 'top': slot == 1, **detail}
        for key in ('x', 'y', 'scale'):
            value = detail[key]
            if type(value) not in (int, float) or not math.isfinite(value) or (key == 'scale' and value <= 0):
                raise StorageError('看板摆放参数无效。')
        if type(detail['top']) is not bool or type(detail['live2d']) is not bool:
            raise StorageError('看板显示开关无效。')
        row['detail' + str(slot)] = detail
    visible = [position for position, value in enumerate(ids, 1) if value]
    if visible and sum(row['detail' + str(position)]['top'] for position in visible) != 1:
        for position in (1, 2):
            row['detail' + str(position)]['top'] = position == visible[0]
    return row


def supports_animation(identifier):
    """Same source l2dName capability as CRoleDisplayData:HadL2D.

    Ownership is still checked by validate_panel; this never invents an asset.
    """
    if identifier < 10000:
        from handlers.shop import catalog
        cfg = catalog('cfgCfgArchiveMultiPicture.lua').get(identifier)
        return bool(cfg and cfg.get('l2dName'))
    from skins_service import catalog
    return bool(catalog()['base_characters'].get(str(identifier), {}).get('has_l2d'))


def new_random_row(state, ty, identifier, index):
    """sNewPanel for one image on a random page (one-key add / bulk import)."""
    return {'idx': index, 'ids': [identifier],
            'detail1': {'x': 0, 'y': 0, 'scale': 1, 'live2d': supports_animation(identifier), 'top': True},
            'detail2': {'x': 0, 'y': 0, 'scale': 1, 'live2d': False, 'top': False},
            'bg': int(state.get('login', {}).get('background_id', 1)), 'ty': ty}


def save(state, fields, current):
    """PlayerProto:SetNewPanel / SetNewPanelUsing persistence.

    random selects the board family: 0 uses one of the six slots, 1 uses one of
    the random boards of the current random_type. Both are validated here and
    every rejection is a Chinese StorageError, never a silent write.
    """
    panels = fields.get('panels', {})
    if not isinstance(panels, dict) or len(panels) > 6:
        raise StorageError('看板槽位数据无效。')
    result = normalized(current)
    for key, row in panels.items():
        checked = validate_panel(state, row)
        if str(key) != str(checked['idx']):
            raise StorageError('看板槽位下标与键不一致。')
        result['panels'][str(checked['idx'])] = checked
    result['setting'] = integer(fields.get('setting', result['setting']), '看板轮换设置无效。', 0)
    if 'random_type' in fields:
        result['random_type'] = strict_random_type(fields['random_type'])
    else:
        result['random_type'] = random_type_value(result.get('random_type'))
    random_on = integer(fields.get('random', result['random']), '随机看板开关无效。', 0, 1)
    if random_on:
        using = random_index(fields.get('using', result['using']))
    else:
        using = integer(fields.get('using', result['using']), '看板槽位下标无效。', 1, 6)
    result['random'], result['using'] = random_on, using
    if random_on:
        chosen = result['random_panels'].get(str(using))
        if not in_type(chosen, result['random_type']):
            raise StorageError('当前使用的随机看板不存在或不属于当前类型。')
    else:
        chosen = result['panels'].get(str(using))
        if not chosen or not any(chosen['ids']):
            raise StorageError('所选看板槽位为空。')
        validate_panel(state, chosen)
    persist(state, result)
    return deepcopy(result)
