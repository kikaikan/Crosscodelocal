"""Byte-budget chunking for full-snapshot replies.

The internal frame limit is 32767 B including the 4 B header
(02-tools/scripts/protocol_codec.py:524-531). server_core.py pre-encodes every
reply of one request before writing any of them, so a single oversized reply
closes the whole connection with close_reason='frame_encode_failed'. Chunking by
measured bytes keeps every frame under the limit without guessing row counts, and
stays correct when card fields grow (skills, equips, sub_talent, mix_data...).

Completion markers were verified against the device Lua consumers:
  PlayerProto:CardAdd        finish     RoleMgr.lua:507-527
  PlayerProto:AddCardRole    (none)     CRoleMgr.lua:40-61
  PlayerProto:UpdateCardRole is_finish  CRoleMgr.lua:81-90
  PlayerProto:ItemBag        ix, is_finish  BagMgr.lua:5-22
"""
from __future__ import annotations
from database import StorageError

CARD_ADD = 'PlayerProto:CardAdd'
ADD_CARD_ROLE = 'PlayerProto:AddCardRole'
UPDATE_CARD_ROLE = 'PlayerProto:UpdateCardRole'
ITEM_BAG = 'PlayerProto:ItemBag'
# PlayerProto:GetRandomPanelRet carries {idx: sNewPanel}; the map encoder accepts
# fewer than 127 entries per frame, so both the byte budget and that count limit
# apply.
RANDOM_PANELS = 'PlayerProto:GetRandomPanelRet'
PACK_PANEL = 'sNewPanel'


def pack_frames(codec, name, row_struct, row_field, rows, fixed=None, *,
                finish_key=None, sequence_key=None):
    """Pack rows into field dicts whose encoded frame stays within the limit.

    A frame is base + sum(row costs), where base is the frame encoded with an
    empty row list, so the 2 B list count is already accounted for. At least one
    field dict is returned, so an all-empty snapshot still reaches the client.
    finish_key is False on every frame but the last; sequence_key numbers frames
    from 1. A single row that cannot fit raises StorageError: it must surface as
    a business tip, never as a silent truncation or a closed connection.
    """
    rows = list(rows)
    fixed = dict(fixed or {})
    limit = codec.config.max_frame_size
    base = len(codec.encode_frame(name, dict(fixed, **{row_field: []})))
    if base > limit:
        raise StorageError(name + ' 的固定字段超过内部帧上限')
    chunks, current, used = [], [], 0
    for row in rows:
        cost = len(codec.encode_struct(row_struct, row))
        if base + cost > limit:
            raise StorageError(name + ' 的单个数据项超过内部帧上限，无法分片发送')
        if current and used + cost > limit - base:
            chunks.append(current)
            current, used = [], 0
        current.append(row)
        used += cost
    chunks.append(current)
    frames = []
    for index, chunk in enumerate(chunks):
        fields = dict(fixed, **{row_field: chunk})
        if finish_key:
            fields[finish_key] = index == len(chunks) - 1
        if sequence_key:
            fields[sequence_key] = index + 1
        frames.append(fields)
    return frames


def _replies(name, frames):
    # Imported lazily: server_core imports this module from its handlers.
    from server_core import Reply
    return [Reply(name, fields) for fields in frames]


def card_add(codec, cards, cur_size, max_size):
    """PlayerProto:CardAdd frames; finish marks the last one (RoleMgr.lua:517)."""
    frames = pack_frames(codec, CARD_ADD, 'sCardsData', 'cards', cards,
                         {'cur_size': int(cur_size), 'max_size': int(max_size)},
                         finish_key='finish')
    return _replies(CARD_ADD, frames)


def add_card_role(codec, roles):
    """PlayerProto:AddCardRole frames; the message has no completion field."""
    frames = pack_frames(codec, ADD_CARD_ROLE, 'sCardRole', 'roles', roles)
    return _replies(ADD_CARD_ROLE, frames)


def update_card_role(codec, roles):
    """PlayerProto:UpdateCardRole frames; is_finish marks the last one."""
    frames = pack_frames(codec, UPDATE_CARD_ROLE, 'sCardRole', 'roles', roles,
                         finish_key='is_finish')
    return _replies(UPDATE_CARD_ROLE, frames)


def item_bag(codec, items):
    """PlayerProto:ItemBag frames; is_finish marks the last one.

    BagMgr.lua:7-10 rebuilds the local bag only when ix==1. A single frame keeps
    the existing ix=0 push unchanged; a split snapshot starts at 1 with the
    following frames incrementing, so stale entries cannot survive the rebuild.
    """
    frames = pack_frames(codec, ITEM_BAG, 'ItemData', 'item', items,
                         finish_key='is_finish')
    for index, fields in enumerate(frames):
        fields['ix'] = 0 if len(frames) == 1 else index + 1
    return _replies(ITEM_BAG, frames)


def random_panels(codec, panels, random_idx, name_list):
    """PlayerProto:GetRandomPanelRet frames; finish marks the last one.

    pack_frames cannot be reused here: GameMsg declares random_panels as
    map|sNewPanel|idx and its base frame encodes an empty list, which the map
    encoder refuses. The same base+cost accounting is applied, plus the codec's
    hard limit of fewer than 127 map entries per frame. CRoleDisplayMgr.lua:174-177
    only accepts random_idx/name_list when finish is true, so every frame carries
    them and an empty board set still sends one frame.
    """
    limit = codec.config.max_frame_size
    fixed = {'random_idx': int(random_idx), 'name_list': list(name_list)}
    base = len(codec.encode_frame(RANDOM_PANELS, dict(fixed, random_panels={}, finish=False)))
    if base > limit:
        raise StorageError(RANDOM_PANELS + ' 的固定字段超过内部帧上限')
    chunks, current, used = [], {}, 0
    for key, row in panels.items():
        cost = len(codec.encode_struct(PACK_PANEL, row))
        if base + cost > limit:
            raise StorageError(RANDOM_PANELS + ' 的单个看板超过内部帧上限，无法分片发送')
        if current and (used + cost > limit - base or len(current) >= 126):
            chunks.append(current)
            current, used = {}, 0
        current[str(row.get('idx', key))] = row
        used += cost
    chunks.append(current)
    frames = []
    for index, chunk in enumerate(chunks):
        frames.append(dict(fixed, random_panels=chunk, finish=index == len(chunks) - 1))
    return _replies(RANDOM_PANELS, frames)
