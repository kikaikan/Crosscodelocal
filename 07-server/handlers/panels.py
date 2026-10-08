"""Random board protocols: PlayerProto 3630-3643.

Registration names, reply names and reply fields follow the recovered client
contract: GameMsg.lua:4988-5057 (sNewPanel at :4953) and its callbacks in
PlayerProto.lua:1523-1802 / CRoleDisplayMgr.lua:165-285. Every rejection is a
StorageError, so server_core answers SystemProto:Tips and keeps the session
(error_policy.classify -> business_rejected -> continue); no handler closes a
connection and no handler reports a board it did not store.

CRoleDisplayMgr.lua:17 starts random_idx at 7 and g_RandomKanbanQuantity=500
(cfgglobal_setting.lua:146) caps each random page.
"""
import unicodedata
from copy import deepcopy

from database import StorageError
from server_core import Reply, register
import panel_service
import reply_chunks

SET_RANDOM_PANEL = 'PlayerProto:SetRandomPanel'
GET_RANDOM_PANEL_DETAIL = 'PlayerProto:GetRandomPanelDetail'
SET_PANEL_RANDOM_TYPE = 'PlayerProto:SetPanelRandomType'
REMOVE_RANDOM_PANEL = 'PlayerProto:RemoveRandomPanel'
ADD_RANDOM_SKINS_ALL = 'PlayerProto:AddRandomSkinsAll'
SET_RANDOM_PANEL_NAME = 'PlayerProto:SetRandomPanelName'
RANDOM_PANEL_CLEAN = 'PlayerProto:RandomPanelClean'


def _checked(ctx, replies):
    """Encode inside the transaction: an unencodable frame must not commit."""
    for reply in replies:
        ctx.server.codec.encode_frame(reply.name, reply.fields)
    return replies


def _page_room(section, random_type):
    return panel_service.RANDOM_LIMIT - len(panel_service.boards(section, random_type))


def _clean_name(value):
    """PlayerProto:SetRandomPanelName value: 1..32 visible characters.

    CRoleDisplayS.lua:148-151 filters the edit box through StringUtil:FilterChar
    and refuses control characters; the server keeps the same bound instead of
    storing a name the client cannot render.
    """
    if not isinstance(value, str):
        raise StorageError('随机看板名称必须是文本。')
    name = ''.join(char for char in value if unicodedata.category(char)[0] != 'C')
    name = name.strip()
    if not name or len(name) > 32:
        raise StorageError('随机看板名称需要 1 到 32 个字符，且不能包含控制字符。')
    return name


@register(SET_RANDOM_PANEL)
async def set_random_panel(ctx, fields):
    """New or updated single/double random board; idx comes from random_idx."""
    uid = ctx.require_login()
    request = fields.get('random_panel')
    if not isinstance(request, dict):
        raise StorageError('随机看板数据格式不正确。')
    row = deepcopy(request)
    with ctx.store.transaction(uid) as tx:
        section = panel_service.stored(tx.state)
        if (str(row.get('idx')) not in section['random_panels'] and row.get('bg', 1) == 1
                and not panel_service.owned_background(tx.state, 1)):
            # A board created in the client starts at bg=1 (CRoleDisplayData.lua:16),
            # which need not be the account background; a brand-new board adopts
            # the account background instead of being refused for it. Any other
            # background still has to be owned.
            row['bg'] = int(tx.state.get('login', {}).get('background_id', 1))
        checked = panel_service.validate_panel(tx.state, row, random=True)
        index = str(checked['idx'])
        if index not in section['random_panels'] and _page_room(section, checked['ty'] - 1) <= 0:
            raise StorageError('随机看板数量已达到上限 %d，请先删除后再添加。' % panel_service.RANDOM_LIMIT)
        section['random_panels'][index] = checked
        # CRoleDisplaySItem.lua:64 opens the editor with GetRandomIdx(), so the
        # next free index must never be handed out twice.
        section['random_idx'] = max(int(section['random_idx']), int(checked['idx']) + 1)
        panel_service.persist(tx.state, section)
        return _checked(ctx, [Reply(SET_RANDOM_PANEL + 'Ret',
                                    {'random_panel': deepcopy(checked),
                                     'random_idx': section['random_idx']})])


@register(GET_RANDOM_PANEL_DETAIL)
async def get_random_panel_detail(ctx, fields):
    """Read one board for CRoleDisplaySItemConta.lua:67/83."""
    ctx.require_login()
    state = ctx.store.get_player(ctx.uid)
    index = panel_service.random_index(fields.get('idx'))
    row = panel_service.stored(state)['random_panels'].get(str(index))
    if row is None:
        raise StorageError('随机看板 %d 不存在，可能已被删除。' % index)
    return [Reply(GET_RANDOM_PANEL_DETAIL + 'Ret',
                  {'idx': index, 'random_panel': deepcopy(row)})]


@register(SET_PANEL_RANDOM_TYPE)
async def set_panel_random_type(ctx, fields):
    """Select the single/double/all random page (GEnum.lua:2275)."""
    uid = ctx.require_login()
    random_type = panel_service.strict_random_type(fields.get('random_type'))
    with ctx.store.transaction(uid) as tx:
        section = panel_service.stored(tx.state)
        section['random_type'] = random_type
        using = section['random_panels'].get(str(section['using']))
        if section['random'] == 1 and not panel_service.in_type(using, random_type):
            panel_service.retreat(section)
        panel_service.persist(tx.state, section)
        return _checked(ctx, [Reply(SET_PANEL_RANDOM_TYPE + 'Ret', {'random_type': random_type})])


@register(REMOVE_RANDOM_PANEL)
async def remove_random_panel(ctx, fields):
    """Delete one board; the client collects ids for tracking (CRoleDisplayMgr.lua:277)."""
    uid = ctx.require_login()
    index = panel_service.random_index(fields.get('idx'))
    with ctx.store.transaction(uid) as tx:
        section = panel_service.stored(tx.state)
        removed = section['random_panels'].pop(str(index), None)
        if removed is not None:
            if section['random'] == 1 and not panel_service.in_type(
                    section['random_panels'].get(str(section['using'])), section['random_type']):
                panel_service.retreat(section)
            panel_service.persist(tx.state, section)
        return _checked(ctx, [Reply(REMOVE_RANDOM_PANEL + 'Ret', {'idx': index})])


@register(ADD_RANDOM_SKINS_ALL)
async def add_random_skins_all(ctx, fields):
    """One-key add of every selected skin to one random page (CRoleDisplayS.lua:253)."""
    uid = ctx.require_login()
    random_type = panel_service.strict_random_type(fields.get('random_type'))
    if random_type == 3:
        raise StorageError('一键添加需要指定单人页或双人页随机看板。')
    ids = fields.get('ids')
    if not isinstance(ids, list) or len(ids) > panel_service.RANDOM_LIMIT:
        raise StorageError('一键添加的看板列表无效。')
    with ctx.store.transaction(uid) as tx:
        section = panel_service.stored(tx.state)
        room = _page_room(section, random_type)
        if room <= 0:
            raise StorageError('随机看板数量已达到上限 %d，请先删除后再添加。' % panel_service.RANDOM_LIMIT)
        known = panel_service.type_ids(section, random_type)
        created = []
        for raw in ids:
            if len(created) >= room:
                break
            identifier = panel_service.integer(raw, '看板图片编号无效。', 1)
            if identifier in known:
                continue  # the same image already has a board on this page
            row = panel_service.new_random_row(tx.state, random_type + 1, identifier,
                                               section['random_idx'])
            try:
                row = panel_service.validate_panel(tx.state, row, random=True)
            except StorageError:
                # CRoleDisplayS.lua:275-292 offers every owned skin/illustration
                # for either page, so an id the store cannot accept (unowned, or
                # an out-of-range value) is left out instead of failing the whole
                # batch; the reply still reports only the boards really stored.
                continue
            section['random_panels'][str(row['idx'])] = row
            section['random_idx'] = int(row['idx']) + 1
            created.append(row)
            known.add(identifier)
        if created:
            panel_service.persist(tx.state, section)
        frames = reply_chunks.pack_frames(ctx.server.codec, ADD_RANDOM_SKINS_ALL + 'Ret',
                                          reply_chunks.PACK_PANEL, 'panels', created,
                                          {'random_type': random_type,
                                           'random_idx': section['random_idx']},
                                          finish_key='finish')
        return _checked(ctx, [Reply(ADD_RANDOM_SKINS_ALL + 'Ret', frame) for frame in frames])


@register(SET_RANDOM_PANEL_NAME)
async def set_random_panel_name(ctx, fields):
    """Rename one random page; every page name is returned (CRoleDisplayMgr.lua:849)."""
    uid = ctx.require_login()
    random_type = panel_service.strict_random_type(fields.get('random_type'))
    name = _clean_name(fields.get('name'))
    with ctx.store.transaction(uid) as tx:
        section = panel_service.stored(tx.state)
        section['name_list'] = [row for row in section['name_list'] if row['second'] != random_type]
        section['name_list'].append({'first': name, 'second': random_type})
        section['name_list'].sort(key=lambda row: row['second'])
        panel_service.persist(tx.state, section)
        return _checked(ctx, [Reply(SET_RANDOM_PANEL_NAME + 'Ret',
                                    {'name_list': deepcopy(section['name_list'])})])


@register(RANDOM_PANEL_CLEAN)
async def random_panel_clean(ctx, fields):
    """Clear one random page except the board the client asked to keep."""
    uid = ctx.require_login()
    random_type = panel_service.strict_random_type(fields.get('random_type'))
    if random_type == 3:
        # CRoleDisplayMgr.lua:868-884 mirrors the delete locally with ty =
        # random_type + 1, so ALL cannot be cleared without diverging; the live
        # UI only ever sends the single/double page (CRoleDisplayS.lua:301).
        raise StorageError('清空随机看板需要指定单人页或双人页。')
    keep = panel_service.integer(fields.get('idx'), '要保留的看板下标无效。', 0)
    with ctx.store.transaction(uid) as tx:
        section = panel_service.stored(tx.state)
        removed = [index for index in panel_service.boards(section, random_type) if index != keep]
        for index in removed:
            section['random_panels'].pop(str(index), None)
        if removed:
            if section['random'] == 1 and not panel_service.in_type(
                    section['random_panels'].get(str(section['using'])), section['random_type']):
                panel_service.retreat(section)
            panel_service.persist(tx.state, section)
        return _checked(ctx, [Reply(RANDOM_PANEL_CLEAN + 'Ret',
                                    {'random_type': random_type, 'idx': keep})])
