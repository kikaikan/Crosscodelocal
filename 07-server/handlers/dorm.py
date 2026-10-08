"""Local dorm rooms: open list, per-room reads and the persisted room layout.

Owns DormProto:GetOpenDorm / GetDorm / Open / ModFurniture and the explicit
local rejections of UseGift / BuyFurniture.

Field values and the room id scheme come from the decoded client config:
  * cfgCfgDorm.lua     floors -> infos[index]; room id = floor_id*100 + index
                       (GCalculatorHelp.lua:1310-1318 GetDormId/GetDormCfgId)
  * cfgCfgDormRoom.lua the legal room levels (only id=1: maxRole=5, scale 16x16)
Consumers: DormProto.lua:12-43/252-260/286-302, DormMgr.lua:857-881/1070-1089,
DormRoom.lua:33-56/120-172.

Wire contract that must not be "improved":
  * DormMgr.lua:860 branches on `if (fid)` and Lua 0 is truthy, so a self
    GetOpenDormRet must omit the optional fid key entirely (never send 0).
  * DormMgr.lua:874 branches on `fid ~= nil`, so a self GetDormRet must also
    omit fid, otherwise the room lands in friendRoomDatas[0].
  * DormRoom.lua:122-153 raises its room mask and only lowers it inside the
    GetDorm callback, so a valid cfg room id always gets a GetDormRet.

A positive fid is a friend read. Local storage holds no cross-account dorm
state, so it answers with the cfgCfgDorm default room set and empty room bodies
(no residents, furniture or screenshot) rather than the caller's own layout.
"""
from __future__ import annotations

import math
import re
from functools import lru_cache

from server_core import Reply, register
from database import StorageError
from seed_generator import LUA_DIR, balanced_table, python_data
from config_codec import parse_lua_table

ROOM_ID_BASE = 100          # GCalculatorHelp.lua:1311 GetDormId
UINT32_MAX = 0xFFFFFFFF
INT32_MIN, INT32_MAX = -0x80000000, 0x7FFFFFFF
FLOAT32_MAX = 3.4028234663852886e38
MAX_FURNITURES = 126        # protocol_codec.py:403 refuses 127+ Lua map entries
MAX_ID_LIST = 64
MAX_IMAGE_NAME = 200
FURNITURE_FIELDS = frozenset({"id", "point", "planeType", "rotateY", "parentID", "childID"})
POINT_FIELDS = frozenset({"x", "y", "z"})


def _read_config(filename):
    """Parse one bounded static Lua table; never evaluate the module."""
    source = (LUA_DIR / filename).read_text("utf-8-sig")
    assignment = re.search(r"_G\[[^\]]+\]\s*=\s*\{", source)
    if assignment is None:
        raise StorageError("本地宿舍配置缺少静态表，未修改任何数据。")
    return python_data(parse_lua_table(balanced_table(source, assignment.end() - 1)))


@lru_cache(maxsize=1)
def _catalog():
    """Return {room_id: room config} plus the legal level list per room."""
    try:
        floors = _read_config("cfgCfgDorm.lua")
        levels = _read_config("cfgCfgDormRoom.lua")
    except StorageError:
        raise
    except (OSError, UnicodeError, TypeError, ValueError) as error:
        raise StorageError("本地宿舍配置无法解析，未修改任何数据。") from error
    if not isinstance(floors, list) or not isinstance(levels, list):
        raise StorageError("本地宿舍配置结构不正确，未修改任何数据。")
    legal_levels = sorted({int(row["id"]) for row in levels
                           if isinstance(row, dict) and isinstance(row.get("id"), int)})
    if not legal_levels:
        raise StorageError("本地宿舍配置没有合法房间等级，未修改任何数据。")
    catalog = {}
    for floor in floors:
        if not isinstance(floor, dict):
            continue
        floor_id = floor.get("id")
        for info in floor.get("infos") or []:
            if not isinstance(floor_id, int) or isinstance(floor_id, bool) or not isinstance(info, dict):
                continue
            index = info.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or floor_id <= 0 or index <= 0:
                continue
            max_lv = info.get("maxLv")
            allowed = [level for level in legal_levels
                       if isinstance(max_lv, int) and not isinstance(max_lv, bool) and level <= max_lv]
            catalog[floor_id * ROOM_ID_BASE + index] = {
                "id": floor_id * ROOM_ID_BASE + index,
                "floor": floor_id,
                "index": index,
                "sName": str(floor.get("sName") or ""),
                "roomName": str(info.get("roomName") or ""),
                "onlyShow": bool(info.get("onlyShow")),
                "defaultTheme": int(info.get("defaultTheme") or 0),
                # cfgCfgDormRoom is the source of legal levels. If CfgDorm.maxLv
                # filters every level out, keep the configured set instead of
                # letting the room disappear: an empty open list would leave
                # DormRoom.lua:122's room mask up forever.
                "levels": allowed or list(legal_levels),
            }
    if not catalog:
        raise StorageError("本地宿舍配置没有可用房间，未修改任何数据。")
    return catalog


def room_config(room_id):
    config = _catalog().get(room_id)
    if config is None:
        raise StorageError("未知的宿舍编号 %s，本地没有该房间配置，未修改任何数据。" % room_id)
    return config


def _plain_int(value):
    """Strict int conversion: JSON keys are strings, booleans never count."""
    if isinstance(value, bool):
        raise ValueError("boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and len(value) <= 20 and value.strip().lstrip("-").isdigit():
        return int(value)
    raise ValueError("not an integer")


def _int_of(value, label):
    try:
        return _plain_int(value)
    except ValueError:
        raise StorageError("%s类型无效，未修改任何数据。" % label) from None


def _uint_of(value, label):
    number = _int_of(value, label)
    if not 0 <= number <= UINT32_MAX:
        raise StorageError("%s超出有效范围，未修改任何数据。" % label)
    return number


def _int32_of(value, label):
    number = _int_of(value, label)
    if not INT32_MIN <= number <= INT32_MAX:
        raise StorageError("%s超出有效范围，未修改任何数据。" % label)
    return number


def _float_of(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StorageError("%s类型无效，未修改任何数据。" % label)
    number = float(value)
    if not math.isfinite(number) or abs(number) > FLOAT32_MAX:
        raise StorageError("%s超出有效范围，未修改任何数据。" % label)
    return number


def _dorms(state, create=False):
    dorms = state.get("dorms")
    if dorms is None:
        if not create:
            return {}
        dorms = {}
        state["dorms"] = dorms
    if not isinstance(dorms, dict):
        raise StorageError("本地宿舍存档格式不正确，未修改任何数据。")
    return dorms


def _rooms(dorms):
    rooms = dorms.get("rooms")
    if rooms is None:
        return {}
    if not isinstance(rooms, dict):
        raise StorageError("本地宿舍房间存档格式不正确，未修改任何数据。")
    return rooms


def _opened(dorms):
    opened = dorms.get("opened")
    if opened is None:
        return {}
    if not isinstance(opened, dict):
        raise StorageError("本地宿舍开启记录格式不正确，未修改任何数据。")
    return opened


def _default_room_ids(catalog):
    """Rooms CfgDorm already treats as usable (onlyShow rooms stay locked)."""
    return {room_id for room_id, room in catalog.items() if not room["onlyShow"]}


def _is_open(dorms, room_id, catalog):
    if room_id in _default_room_ids(catalog):
        return True
    return bool(_opened(dorms).get(str(room_id)))


def _listed_room_ids(catalog, opened):
    """Room ids a GetOpenDormRet may report. Never empty: DormMgr.lua:659 and
    DormRoom.lua:131 both dereference the list, and an empty one would leave the
    room mask up (a cfg with only onlyShow rooms still reports its rooms)."""
    ids = set(_default_room_ids(catalog)) or set(catalog)
    for key, value in opened.items():
        if not value:
            continue
        try:
            room_id = _plain_int(key)
        except ValueError:
            continue
        if room_id in catalog:
            ids.add(room_id)
    return sorted(ids)


def _id_list(value):
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:MAX_ID_LIST]:
        try:
            number = _plain_int(item)
        except ValueError:
            continue
        if 0 <= number <= UINT32_MAX:
            result.append(number)
    return result


def _checked_point(value):
    if not isinstance(value, dict):
        raise StorageError("家具坐标必须是结构表，本次保存已拒绝。")
    if set(value) - POINT_FIELDS:
        raise StorageError("家具坐标包含未知字段，本次保存已拒绝。")
    return {field: _float_of(value[field], "家具坐标 " + field)
            for field in ("x", "y", "z") if field in value}


def _checked_furniture(key, item):
    """Validate one sFurniture row (GameMsg.lua:4318-4321) before persisting it.

    Whatever is stored here is re-encoded by GetOpenDormRet/GetDormRet later, so
    an unencodable entry would kill the connection at encode time instead of
    failing this request.
    """
    if not isinstance(item, dict):
        raise StorageError("家具数据必须是结构表，本次保存已拒绝。")
    unknown = sorted(set(item) - FURNITURE_FIELDS)
    if unknown:
        raise StorageError("家具数据包含未知字段 %s，本次保存已拒绝。" % ",".join(unknown))
    if "childID" in item:
        # sFurniture.childID is int[]; protocol_codec.py:421 cannot encode it.
        raise StorageError("本地协议无法编码家具子节点 childID，本次保存已拒绝。")
    identifier = _uint_of(item.get("id"), "家具编号")
    if identifier <= 0:
        raise StorageError("家具编号无效，本次保存已拒绝。")
    try:
        key_id = _plain_int(key)
    except ValueError:
        raise StorageError("家具表键类型无效，本次保存已拒绝。") from None
    if key_id != identifier:
        raise StorageError("家具表键与家具编号不一致，本次保存已拒绝。")
    entry = {"id": identifier}
    for field in ("planeType", "rotateY", "parentID"):
        if field in item:
            entry[field] = _int32_of(item[field], "家具 " + field)
    if "point" in item:
        entry["point"] = _checked_point(item["point"])
    return entry


def _checked_furnitures(value):
    if not isinstance(value, dict):
        raise StorageError("家具列表必须是键值表，本次保存已拒绝。")
    if len(value) > MAX_FURNITURES:
        raise StorageError("家具数量超过本地上限，本次保存已拒绝。")
    result = {}
    for key, item in value.items():
        entry = _checked_furniture(key, item)
        result[entry["id"]] = entry
    return result


def _sanitize_furnitures(value):
    """Read path: drop entries that cannot be re-encoded instead of closing the socket."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in list(value.items())[:MAX_FURNITURES]:
        try:
            entry = _checked_furniture(key, item)
        except StorageError:
            continue
        result[entry["id"]] = entry
    return result


def _default_layout(config):
    return {"lv": config["levels"][0], "comfort": 0, "img": "",
            "roleIds": [], "furnitures": {}, "petIds": []}


def _layout(dorms, room_id, config):
    """Sanitized stored layout for one room, or the config minimum for a fresh room."""
    layout = _default_layout(config)
    stored = _rooms(dorms).get(str(room_id))
    if stored is None:
        return layout
    if not isinstance(stored, dict):
        raise StorageError("本地宿舍房间存档格式不正确，未修改任何数据。")
    level = stored.get("lv")
    if isinstance(level, int) and not isinstance(level, bool) and level in config["levels"]:
        layout["lv"] = level
    comfort = stored.get("comfort")
    if isinstance(comfort, int) and not isinstance(comfort, bool) and 0 <= comfort <= UINT32_MAX:
        layout["comfort"] = comfort
    image = stored.get("img")
    if isinstance(image, str) and 0 < len(image) <= MAX_IMAGE_NAME:
        layout["img"] = image
    layout["roleIds"] = _id_list(stored.get("roleIds"))
    layout["petIds"] = _id_list(stored.get("petIds"))
    layout["furnitures"] = _sanitize_furnitures(stored.get("furnitures"))
    return layout


def _room_view(dorms, room_id, config):
    layout = _layout(dorms, room_id, config)
    return {"id": room_id, "num": len(layout["roleIds"]), **layout}


def _blank_view(room_id, config):
    """A friend room: config-minimum structure, no local layout, resident or image."""
    layout = _default_layout(config)
    return {"id": room_id, "num": 0, **layout}


def _dorm_a(view):
    """One sDormA row for GetOpenDormRet.infos (GameMsg.lua:4323-4326)."""
    row = {"id": view["id"], "num": view["num"], "roleIds": list(view["roleIds"]),
           "lv": view["lv"], "comfort": view["comfort"]}
    if view["img"]:
        row["img"] = view["img"]
    return row


def _dorm_b(view):
    """One sDormB value for GetDormRet.info (GameMsg.lua:4333-4336)."""
    data = {"roleIds": list(view["roleIds"]), "furnitures": dict(view["furnitures"]),
            "petIds": list(view["petIds"])}
    if view["img"]:
        data["img"] = view["img"]
    return {"id": view["id"], "num": view["num"], "lv": view["lv"],
            "comfort": view["comfort"], "data": data}


def _optional_fid(value):
    """None means 'self'. Absent and zero are the same request (DormProto.lua:12-16)."""
    if value is None:
        return None
    fid = _uint_of(value, "好友编号 fid")
    return fid or None


def _fid_fields(fid):
    return {"fid": fid} if fid else {}


@register("DormProto:GetOpenDorm")
async def get_open_dorm(ctx, fields):
    uid = ctx.require_login()
    fid = _optional_fid(fields.get("fid"))
    catalog = _catalog()
    if fid:
        # No cross-account dorm state exists locally (FriendProto:GetFriendsData
        # is empty), so a friend read answers with the config-default rooms and
        # empty bodies: the caller's own layout is neither reused nor leaked.
        infos = [_dorm_a(_blank_view(room_id, catalog[room_id]))
                 for room_id in _listed_room_ids(catalog, {})]
    else:
        dorms = _dorms(ctx.store.get_player(uid))
        infos = [_dorm_a(_room_view(dorms, room_id, catalog[room_id]))
                 for room_id in _listed_room_ids(catalog, _opened(dorms))]
    # 3515 is a list read, not an action acknowledgement: infos must never be nil
    # (DormMgr.lua:659 ipairs, DormRoom.lua:131 nil dereference).
    return [Reply("DormProto:GetOpenDormRet", {**_fid_fields(fid), "infos": infos})]


@register("DormProto:GetDorm")
async def get_dorm(ctx, fields):
    uid = ctx.require_login()
    fid = _optional_fid(fields.get("fid"))
    room_id = _uint_of(fields.get("id"), "宿舍编号 id")
    config = room_config(room_id)
    if fid:
        view = _blank_view(room_id, config)
    else:
        view = _room_view(_dorms(ctx.store.get_player(uid)), room_id, config)
    # DormProto.lua:37-43 waits for this reply before DormRoom.lua:133-153 lowers
    # the room mask, so every config-valid id answers, opened or not.
    return [Reply("DormProto:GetDormRet", {**_fid_fields(fid), "info": _dorm_b(view)})]


def _open_replies(state, room_id, config):
    # DormProto.lua:251-260 marks Open as a room update; DormRoom.lua:22-25 only
    # listens to Dorm_Update, so the pushed sDormB is what makes the room usable.
    view = _room_view(_dorms(state), room_id, config)
    return [Reply("DormProto:OpenRet", {"id": room_id}),
            Reply("DormProto:Update", {"infos": [_dorm_b(view)]})]


@register("DormProto:Open")
async def open_room(ctx, fields):
    uid = ctx.require_login()
    room_id = _uint_of(fields.get("id"), "宿舍编号 id")
    catalog = _catalog()
    config = room_config(room_id)
    state = ctx.store.get_player(uid)
    if _is_open(_dorms(state), room_id, catalog):
        # Idempotent: opening an already-open room must not rewrite the save.
        return _open_replies(state, room_id, config)
    # cfgCfgDorm defines no costs for any room, so unlocking is the real effect.
    with ctx.store.transaction(uid) as tx:
        dorms = _dorms(tx.state, create=True)
        opened = dorms.get("opened")
        if opened is None:
            opened = {}
            dorms["opened"] = opened
        if not isinstance(opened, dict):
            raise StorageError("本地宿舍开启记录格式不正确，未修改任何数据。")
        opened[str(room_id)] = True
        replies = _open_replies(tx.state, room_id, config)
    return replies


@register("DormProto:ModFurniture")
async def mod_furniture(ctx, fields):
    uid = ctx.require_login()
    room_id = _uint_of(fields.get("id"), "宿舍编号 id")
    config = room_config(room_id)
    furnitures = _checked_furnitures(fields.get("furnitures"))
    img = fields.get("img")
    if img is None:
        img = ""
    if not isinstance(img, str):
        raise StorageError("宿舍截图名称类型无效，本次保存已拒绝。")
    if len(img) > MAX_IMAGE_NAME:
        raise StorageError("宿舍截图名称过长，本次保存已拒绝。")
    with ctx.store.transaction(uid) as tx:
        dorms = _dorms(tx.state, create=True)
        if not _is_open(dorms, room_id, _catalog()):
            raise StorageError("该宿舍尚未开启，家具布局未保存。")
        rooms = dorms.get("rooms")
        if rooms is None:
            rooms = {}
            dorms["rooms"] = rooms
        if not isinstance(rooms, dict):
            raise StorageError("本地宿舍房间存档格式不正确，未修改任何数据。")
        layout = _layout(dorms, room_id, config)
        layout["furnitures"] = furnitures
        layout["img"] = img
        rooms[str(room_id)] = layout
    return [Reply("DormProto:ModFurnitureRet", {"id": room_id})]


@register("DormProto:UseGift")
async def use_gift(ctx, fields):
    ctx.require_login()
    # Real dorm gifting consumes bag items and grants role favour/exp from
    # config this server does not model. DormGift.lua:221-227 fires and forgets,
    # so refusing keeps the bag intact and the session usable.
    raise StorageError("本地服务尚未实现宿舍送礼：礼物未消耗，角色好感度未改变。")


@register("DormProto:BuyFurniture")
async def buy_furniture(ctx, fields):
    ctx.require_login()
    # Price, purchase records and the BuyRecordRet state (initialization.py:149)
    # are a separate module's contract; deducting currency here would spend
    # without a durable record. DormShopConf.lua:148-160 and
    # DormFurniturePayView.lua:168-180 close their view without waiting.
    raise StorageError("本地服务尚未实现家具购买：未扣费、未增加购买记录，家具未到账。")
