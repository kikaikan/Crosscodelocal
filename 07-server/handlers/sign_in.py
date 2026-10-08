"""Atomic, idempotent calendar-day sign-in from local CfgSignReward data.

Only original type1 calendars are implemented. No annual catalog is silently
copied, and no payment, makeup sign-in, continuous/event enrollment or official
account state is used. Sources: GEnum.lua:840, SignInMgr.lua:26, SignInInfo.lua
:61/:128/:142, ClientProto.lua:20 and cfgCfgSignReward{,Item}.lua.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import time

from server_core import register, Reply
from database import StorageError
from seed_generator import selected_record
from handlers.initialization import config_table, feature_open


MAX_VALUE = 2147483647


def integer(fields: dict, name: str, default=0) -> int:
    value = fields.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError("Sign-in field must be an integer: " + name)
    return value


def day_at(timestamp: int) -> datetime:
    # Config g_ActivityDiffDayTime=3; Beijing timezone never uses DST.
    return datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8))) - timedelta(hours=3)


@lru_cache(maxsize=16)
def reward_catalog(identifier: int) -> tuple[dict, dict]:
    return selected_record("cfgCfgSignRewardItem.lua", identifier)


def rewards_info(saved: dict, month: int) -> dict:
    # JSON storage has string keys; Lua client CheckIndexIsDone indexes by number.
    result = {"index": month, "indexs": {int(day): deepcopy(value)
              for day, value in saved.get("indexs", {}).items()},
              "muCheckinCost": int(saved.get("muCheckinCost", 0))}
    for key in ("firstSingTime", "lastSingTime"):
        if key in saved:
            result[key] = int(saved[key])
    return result


def rejected(identifier: int, month: int, day: int = 0, saved: dict | None = None) -> list[Reply]:
    result = {"isOk": False, "id": identifier, "index": month, "subIndex": day, "is_mucheckin": False}
    if saved:
        result["rewardsInfos"] = rewards_info(saved, month)
    return [Reply("ClientProto:AddSignRet", result)]


def configured_day(identifier: int, requested_month: int, timestamp: int):
    """Return only the actual current configured day; callers cannot choose a day."""
    calendar = day_at(timestamp)
    cfg = config_table("cfgCfgSignReward.lua").get(identifier)
    if cfg is None or cfg.get("type") != 1:
        return None
    begin, end = int(cfg.get("nBegTime", 0)), int(cfg.get("nEndTime", 0))
    if begin and timestamp < begin or end and timestamp >= end:
        return None
    if requested_month not in (0, calendar.month):
        return None
    month = next((row for row in cfg.get("infos", []) if row.get("index") == calendar.month), None)
    if month is None:
        return None
    rewards, source = reward_catalog(int(month["activityRewardId"]))
    row = next((row for row in rewards.get("infos", []) if row.get("index") == calendar.day), None)
    if row is None:
        return None
    amounts = {}
    for value in row.get("rewards", []):
        if not isinstance(value, list) or len(value) != 3:
            raise StorageError("Unsupported sign-in reward specification")
        item, amount, kind = value
        if any(isinstance(v, bool) or not isinstance(v, int) for v in value):
            raise StorageError("Invalid sign-in reward numbers")
        if kind != 2 or item <= 0 or not 1 <= amount <= MAX_VALUE:
            raise StorageError("Only positive configured item sign-in rewards are supported")
        amounts[item] = amounts.get(item, 0) + amount
        if amounts[item] > MAX_VALUE:
            raise StorageError("Sign-in reward integer overflow")
    if not amounts:
        raise StorageError("Configured sign-in day has no award")
    return calendar, amounts, source


def grant_items(tx, amounts: dict[int, int]) -> list[Reply]:
    """Grant deterministic type2 items; special experience pool is its own state.

    PlayerClient.GetCoin(10003) calls RoleMgr.GetStoreExp; CardUpdate refreshes
    that value. All other current daily catalogs contain ordinary bag items or
    GOLD (10001), whose PlayerTxn.add_item already synchronizes player wealth.
    This helper deliberately does not import a gacha-card-specific reward path.
    """
    updates = []
    pool_changed, wealth_changed = False, False
    for cfgid, amount in amounts.items():
        if cfgid == 10003:
            total = int(tx.state.get("store_exp", 0)) + amount
            if not 0 <= total <= MAX_VALUE:
                raise StorageError("Experience pool overflow")
            tx.state["store_exp"] = total
            pool_changed = True
        else:
            total = tx.add_item(cfgid, amount)
            wealth_changed = wealth_changed or cfgid in (10001, 10002)
        updates.append({"id": cfgid, "num": total, "add": amount, "ix": 0,
                        "time": 0, "expiry": 0, "get_infos": {}})
    replies = [Reply("PlayerProto:ItemUpdate", {"data": updates})]
    if pool_changed:
        replies.append(Reply("PlayerProto:CardUpdate", {"cards": [], "store_exp": tx.state["store_exp"]}))
    if wealth_changed:
        replies.append(Reply("LoginProto:PlrUpdate", {"infos": deepcopy(tx.state["player"])}))
    return replies


@register("ClientProto:AddSign")
async def add_sign(ctx, fields):
    ctx.require_login()
    identifier, requested_month = integer(fields, "id"), integer(fields, "index")
    if identifier < 0 or not 0 <= requested_month <= 12:
        return rejected(identifier, requested_month)
    replies = []
    # Award and receipt commit in one SQLite transaction. No await while locked.
    with ctx.store.transaction(ctx.uid) as tx:
        timestamp = int(tx.state.get("offline_clock", time.time()))
        resolved = configured_day(identifier, requested_month, timestamp)
        calendar = day_at(timestamp)
        month, day = calendar.month, calendar.day
        saved = tx.state.get("signs", {}).get(f"{identifier}_{month}", {})
        # SignInMgr.SignInIsOpen uses ActivityListView's configured stage gate.
        if resolved is None or not feature_open(tx.state, "ActivityListView"):
            return rejected(identifier, requested_month or month, day, saved)
        key = f"{identifier}_{month}"
        marks = saved.get("indexs", {})
        mark = marks.get(str(day), marks.get(day))
        repeat = mark is not None and mark is not False  # Lua regards numeric 0 as true.
        if not repeat:
            _, amounts, source = resolved
            replies.extend(grant_items(tx, amounts))
            saved = tx.state.setdefault("signs", {}).setdefault(key, {"indexs": {}})
            saved["indexs"][str(day)] = True
            saved.setdefault("firstSingTime", timestamp)
            saved["lastSingTime"] = timestamp
            tx.state.setdefault("sign_claims", {})[f"{identifier}:{calendar.date().isoformat()}"] = {
                "time": timestamp, "items": [{"id": item, "num": amount, "type": 2}
                                             for item, amount in amounts.items()],
                "source": source,
            }
        replies.append(Reply("ClientProto:AddSignRet", {"isOk": True, "id": identifier,
                     "index": month, "subIndex": day, "rewardsInfos": rewards_info(saved, month),
                     "is_mucheckin": False}))
    return replies
