"""Configured local tasks; Lua catalogs are parsed as data and never executed.

Source contracts: MissionInfo.lua (finish counters/state/is_get), MissionMgr.lua
(claim-all and cumulative star awards), GEnum.lua task types. Only trusted
server operations may call advance_tasks; client-supplied completion is ignored.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import re

from database import StorageError
from server_core import Reply, register
from seed_generator import LUA_DIR, normalize_long_strings, python_data
from config_codec import parse_lua_table
from handlers.initialization import cleared_stages, local_time, reset_times


CATALOGS = {1: "cfgCfgTaskMain.lua", 2: "cfgCfgTaskSub.lua",
            3: "cfgCfgTaskDaily.lua", 4: "cfgCfgTaskWeekly.lua",
            16: "cfgCfgGuideFinish.lua", 17: "cfgCfgGuideTask.lua", 37: "cfgCfgNewPlayerSevenDayTask.lua"}
FILES = set(CATALOGS.values()) | {"cfgCfgTaskFinishVal.lua", "cfgCfgTaskDailyStarReward.lua",
                                  "cfgCfgTaskWeeklyStarReward.lua"}
# Long strings are normalized by seed_generator.normalize_long_strings so every
# Lua reader shares one implementation; re-exported here for existing importers.
RECORD_KEY = re.compile(r"\[([0-9]+)\]\s*=\s*")


def table_end(source: str, start: int) -> int:
    depth, quote, i = 0, None, start
    while i < len(source):
        ch = source[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("Unclosed static record")


@lru_cache(maxsize=12)
def catalog(filename: str) -> dict:
    if filename not in FILES:
        raise ValueError("Unsupported task catalog")
    source = normalize_long_strings((LUA_DIR / filename).read_text("utf-8-sig"))
    if len(source) > 2_000_000:
        raise ValueError("Oversized static task catalog")
    assignment = re.search(r"_G\[[^\]]+\]\s*=\s*\{", source)
    if assignment is None:
        raise ValueError("Missing static configuration table")
    i, result, index = assignment.end(), {}, 1
    while i < len(source):
        while source[i].isspace() or source[i] == ",":
            i += 1
        if source[i] == "}":
            return result
        match = RECORD_KEY.match(source, i)
        if match:
            key, i = int(match[1]), i + len(match[0])
        else:
            key, index = index, index + 1
        if source[i] != "{" or key in result:
            raise ValueError("Malformed or duplicate static task record")
        end = table_end(source, i)
        row = python_data(parse_lua_table(source[i:end]))
        if not isinstance(row, dict):
            raise ValueError("Task record must be a keyed data table")
        result[key], i = row, end
    raise ValueError("Unclosed static task catalog")


# Condition function IDs are source configuration IDs, not eTaskEventType IDs.
EVENTS = {10027: "board_click", 10151: "buy_hot", 20003: "card_upgrade",
          20005: "card_break", 22001: "skill_upgrade", 22026: "talent_upgrade",
          30001: "stage_clear", 30002: "stage_clear", 35131: "shop_exchange",
          40003: "equip_upgrade", 40022: "equip_remould", 45131: "build_order",
          45141: "build_collect", 50001: "card_create", 61001: "arena_win",
          61022: "arena_score"}
MAX_VALUE = 2_147_483_647
TZ = timezone(timedelta(hours=8))


def integer(value, label: str, minimum=0, maximum=MAX_VALUE) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise StorageError("Invalid task " + label)
    return value


def period_keys(state: dict) -> dict[str, str]:
    day = datetime.fromtimestamp(local_time(state), TZ) - timedelta(hours=3)
    monday = day.date() - timedelta(days=day.weekday())
    return {"daily": day.date().isoformat(), "weekly": monday.isoformat()}


def guide_day(state: dict) -> int:
    current = datetime.fromtimestamp(local_time(state), TZ) - timedelta(hours=3)
    created = datetime.fromtimestamp(int(state["player"].get("create_time", local_time(state))), TZ) - timedelta(hours=3)
    # Explicit local enrollment policy: account day unlocks consecutive stages.
    return min(7, max(1, (current.date() - created.date()).days + 1))


def task_config(task: dict) -> dict:
    return catalog(CATALOGS[task["type"]])[task["cfgid"]]


def stats(state: dict, kind: int) -> dict:
    scope = "daily" if kind == 3 else "weekly" if kind == 4 else "lifetime"
    return state["task_state"]["stats"][scope]


def counter(data: dict, event: str, identifiers=None) -> int:
    row = data.get(event, {})
    if not identifiers:
        return int(row.get("total", 0))
    return sum(int(row.get("by_id", {}).get(str(key), 0)) for key in identifiers)


def condition_value(state: dict, task: dict, condition: dict) -> int:
    kind, target = condition["nType"], int(condition["nVal1"])
    a2, a3, a5 = condition.get("aVal2", []), condition.get("aVal3", []), condition.get("aVal5", [])
    cards = state.get("cards", [])
    if kind == 10001:
        return int(state["player"]["level"])
    if kind == 10022:
        # Authenticated GetTasksData is the persisted login-day observation.
        return int(stats(state, task["type"]).get("login", {}).get("total", 0))
    if kind == 10025:
        required = int(a2[0]) if a2 else target
        return int(any(len({row.get("cid") for row in team.get("data", []) if row.get("cid")}) >= required
                       for team in state.get("teams", [])))
    if kind == 20001:
        return sum((not a3 or int(card.get("level", 1)) >= a3[0]) and
                   (not a5 or int(card.get("break_level", 1)) >= a5[0]) for card in cards)
    if kind == 20014:
        owned = {card["cfgid"] for card in cards}
        return sum(identifier in owned for identifier in a2)
    if kind == 30005:
        return sum(identifier in cleared_stages(state) for identifier in a2)
    if kind == 40002:
        return sum(int(row.get("level", 1)) >= int(a2[0]) for row in state.get("equips", [])) if a2 else 0
    if kind == 40026:
        # Equipment modules can persist the effective set skill levels they
        # actually activated. Raw random affixes are not set activation proof.
        return max([0] + [int(v) for v in state.get("equip_set_skill_levels", [])])
    if kind in (45001, 45024):
        buildings = state.get("buildings", [])
        if isinstance(buildings, dict):
            buildings = list(buildings.values())
        selected = [row for row in buildings if row.get("cfgid", row.get("id")) in a2]
        return max([0] + [int(row.get("level", 0)) for row in selected]) if kind == 45001 else len({row.get("cfgid", row.get("id")) for row in selected})
    if kind == 45019:
        return int(state.get("dorm_comfort", 0))
    if kind == 60001:
        # A completed stage requires real Finish states, independent of claims.
        return sum(row["state"] == 3 and row["type"] in a2 and
                   (not a3 or task_config(row).get("nStage") in a3)
                   for row in state.get("tasks", []))
    if kind in EVENTS:
        event = EVENTS[kind]
        if kind == 40003 and target >= 1000:
            event = "equip_exp_spent"
        ids = a2 if kind in (30002, 45131, 45141, 50001) else None
        value = counter(stats(state, task["type"]), event, ids)
        if kind == 50001 and a3:
            # aVal2 inclusion is evidenced by explicitly named pool conditions;
            # aVal3 exclusion is inferred from100133 excluding limited pool1003.
            value -= counter(stats(state, task["type"]), event, a3)
        return max(0, value)
    return 0  # Unsupported source functions never silently complete.


def render(task: dict) -> dict:
    return {key: deepcopy(task[key]) for key in ("id", "cfgid", "type", "is_get", "rewards", "state", "finish_ids")}


def refresh(state: dict) -> list[dict]:
    conditions, changed = catalog("cfgCfgTaskFinishVal.lua"), []
    # Guide stages depend on normal guide-task Finish, hence run them last.
    for task in sorted(state["tasks"], key=lambda row: row["type"] == 16):
        if task["is_get"] == 2:
            continue
        ids = task_config(task).get("aFinishIds", [])
        values, done = [], bool(ids)
        for identifier in ids:
            condition = conditions[identifier]
            value = min(integer(int(condition["nVal1"]), "condition target", 1),
                        max(0, condition_value(state, task, condition)))
            values.append({"id": identifier, "num": value, "type": 2})
            done = done and value >= condition["nVal1"]
        new = {"state": 3 if done else 2, "finish_ids": values}
        if any(task[key] != value for key, value in new.items()):
            task.update(new)
            changed.append(render(task))
    return changed


def ensure_tasks(state: dict) -> list[Reply]:
    """Assign config roots, unlock claimed chains, and replace expired periods.

    The assignments and reset boundaries are explicit local policies; official
    server internals are unavailable. Conditions and rewards remain source data.
    """
    data = state.setdefault("task_state", {})
    data.setdefault("stats", {"lifetime": {}, "daily": {}, "weekly": {}})
    for scope in ("lifetime", "daily", "weekly"):
        data["stats"].setdefault(scope, {})
    data.setdefault("next_id", 1)
    data.setdefault("star_claims", {"3": [], "4": []})
    rows = state.setdefault("tasks", [])
    deleted, periods = [], period_keys(state)
    for scope, kind, star in (("daily", 3, "dailyStar"), ("weekly", 4, "weeklyStar")):
        if data.get(scope + "_period") != periods[scope]:
            deleted.extend({"id": row["id"], "cfgid": row["cfgid"], "type": kind} for row in rows if row["type"] == kind)
            rows[:] = [row for row in rows if row["type"] != kind]
            data["stats"][scope], data[star], data["star_claims"][str(kind)] = {}, 0, []
            data[scope + "_period"] = periods[scope]
    added, present = [], {(row["type"], row["cfgid"]) for row in rows}
    claimed = {(row["type"], row["cfgid"]) for row in rows if row["is_get"] == 2}
    passed, level, day = cleared_stages(state), int(state["player"]["level"]), guide_day(state)
    for kind, filename in CATALOGS.items():
        for cfgid, cfg in sorted(catalog(filename).items()):
            if (kind, cfgid) in present or cfg.get("nOpenLevel", 1) > level:
                continue
            if cfg.get("nPreTaskId") and (kind, cfg["nPreTaskId"]) not in claimed:
                continue
            if cfg.get("nOppssId") and cfg["nOppssId"] not in passed:
                continue
            if kind == 17 and cfg["nStage"] > day or kind == 16 and cfgid > day:
                continue
            task = {"id": integer(data["next_id"], "instance id", 1), "cfgid": cfgid, "type": kind,
                    "is_get": 1, "state": 2, "finish_ids": [], "rewards": []}
            data["next_id"] += 1
            rows.append(task)
            added.append(task)
    changed = refresh(state)
    replies = [Reply("TaskProto:TaskDelete", {"tasks": deleted})] if deleted else []
    if added:
        replies.append(Reply("TaskProto:TaskAdd", {"tasks": [render(row) for row in added], "is_finish": True}))
    added_ids = {row["id"] for row in added}
    changed = [row for row in changed if row["id"] not in added_ids]
    if changed:
        replies.append(Reply("TaskProto:TaskFlush", {"tasks": changed}))
    return replies


def advance_tasks(state: dict, event: str, amount=1, identifier=None, metadata=None) -> list[Reply]:
    """Call inside an existing account transaction after a validated operation.

    `stage_clear` receives the stage ID, `card_create` the configured pool ID,
    and build events their building category.
    `state_changed` refreshes current level/cards/team/progress without a counter.
    The caller appends returned pushes to its own response, and commits once.
    """
    known = set(EVENTS.values()) | {"login", "state_changed", "equip_exp_spent"}
    if event not in known:
        raise StorageError("Unknown trusted task event")
    amount = integer(amount, "event amount")
    if identifier is not None:
        identifier = integer(identifier, "event identifier")
    replies = ensure_tasks(state)
    if event != "state_changed" and amount:
        for scope in ("lifetime", "daily", "weekly"):
            row = state["task_state"]["stats"][scope].setdefault(event, {"total": 0, "by_id": {}})
            row["total"] = integer(row["total"] + amount, "event counter")
            if identifier is not None:
                key = str(identifier)
                row["by_id"][key] = integer(row["by_id"].get(key, 0) + amount, "event subcounter")
    changed = refresh(state)
    if changed:
        replies.append(Reply("TaskProto:TaskFlush", {"tasks": changed}))
    return replies


@register("TaskProto:GetTasksData")
async def get_tasks(ctx, fields):
    uid = ctx.require_login()
    with ctx.store.transaction(uid) as tx:
        reset_pushes = [reply for reply in ensure_tasks(tx.state) if reply.name == "TaskProto:TaskDelete"]
        daily = tx.state["task_state"]["stats"]["daily"]
        if not daily.get("login", {}).get("total"):
            advance_tasks(tx.state, "login")
        # Full snapshot is chunked below the UInt16 frame limit.
        rows = [render(row) for row in tx.state["tasks"]]
        replies = [Reply("TaskProto:TaskAdd", {"tasks": rows[i:i + 80], "is_finish": i + 80 >= len(rows)})
                   for i in range(0, len(rows), 80)]
        if not rows:
            replies = [Reply("TaskProto:TaskAdd", {"tasks": [], "is_finish": True})]
    return reset_pushes + replies + [Reply("TaskProto:GetTasksDataRet", {})]


@register("TaskProto:GetResetTaskInfo")
async def get_reset(ctx, fields):
    uid = ctx.require_login()
    with ctx.store.transaction(uid) as tx:
        replies = ensure_tasks(tx.state)
        resets, data = reset_times(local_time(tx.state)), tx.state["task_state"]
        replies.append(Reply("TaskProto:GetResetTaskInfoRet", {"dailyResetTime": resets["d_time"],
            "weeklyResetTime": resets["w_time"], "dailyStar": data["dailyStar"],
            "weeklyStar": data["weeklyStar"], "anvsStarInfo": []}))
    return replies


@register("TaskProto:GetSevenTasksDay")
async def get_days(ctx, fields):
    uid = ctx.require_login()
    kind = integer(fields.get("type", 0), "type", 0, 255)
    state = ctx.store.get_player(uid)
    day = guide_day(state) if kind in (16, 17) else 0
    return [Reply("TaskProto:GetSevenTasksDayRet", {"type": kind, "c_day": day})]


def reward_rows(cfg):
    from handlers.progression import explicit_rewards
    return explicit_rewards(cfg.get("jAwardId", []))


def grant_rewards(tx, rows):
    """Reuse deterministic item/equipment awards and configured card templates."""
    from handlers import gacha
    from handlers.progression import grant
    deltas, equips, cards, rendered = {}, [], [], []
    normal = []
    for row in rows:
        if row["type"] == 3:
            if str(row["id"]) not in gacha.CARDS:
                raise StorageError("Task card reward template is unavailable locally")
            for _ in range(integer(row["num"], "card reward count", 1, 100)):
                if not any(card["cfgid"] == row["id"] for card in tx.state["cards"]) and len(tx.state["cards"]) >= tx.state["max_card_size"]:
                    raise StorageError("Card bag is full")
                result, card, is_new, role = gacha.award(tx, row["id"], {"nType": 1})
                cards.append((result, card, is_new, role))
                rendered.append({"id": row["id"], "num": 1, "type": 3, "c_id": card["cid"]})
        else:
            normal.append(row)
    rendered.extend(grant(tx, normal, deltas, equips))
    pool_delta = deltas.pop(10003, 0)
    player_delta = deltas.pop(10004, 0)
    replies = gacha.updates(tx, cards, deltas) if cards or deltas else []
    if pool_delta:
        replies.append(Reply("PlayerProto:CardUpdate", {"cards": [], "store_exp": tx.state["store_exp"]}))
    if player_delta or any(row["id"] in (10001, 10002, 10998) for row in normal):
        replies.append(Reply("LoginProto:PlrUpdate", {"infos": deepcopy(tx.state["player"])}))
    if equips:
        replies.append(Reply("EquipProto:EquipAdd", {"equips": equips, "cur_size": len(tx.state["equips"]),
                                                "max_size": tx.state["max_equip_size"], "is_finish": True}))
    return rendered, replies


def claim(tx, selected: list[dict]) -> list[Reply]:
    data, receipts, rows = tx.state["task_state"], tx.state.setdefault("task_claims", {}), []
    taken = []
    for task in selected:
        if task["is_get"] == 2:
            continue
        if task["state"] != 3:
            raise StorageError("Task condition has not been completed")
        cfg = task_config(task)
        rows.extend(reward_rows(cfg))
        task["is_get"] = 2
        taken.append(task)
        receipts[str(task["id"])] = {"type": task["type"], "cfgid": task["cfgid"],
                "time": local_time(tx.state), "rewards": deepcopy(cfg.get("jAwardId", []))}
        if task["type"] in (3, 4):
            key = "dailyStar" if task["type"] == 3 else "weeklyStar"
            data[key] = integer(data[key] + int(cfg.get("nStar", 0)), "stars")
    for kind, filename, key in ((3, "cfgCfgTaskDailyStarReward.lua", "dailyStar"),
                                 (4, "cfgCfgTaskWeeklyStarReward.lua", "weeklyStar")):
        log = data["star_claims"][str(kind)]
        for identifier, cfg in sorted(catalog(filename).items()):
            if cfg["star"] <= data[key] and identifier not in log:
                rows.extend(reward_rows(cfg))
                log.append(identifier)
    rendered, replies = grant_rewards(tx, rows)
    # Receipts and actual awards are committed together; retries contain no gets.
    for task in taken:
        cfg = task_config(task)
        task["rewards"] = [{"id": row[0], "num": row[1], "type": row[2]} for row in cfg.get("jAwardId", [])]
    replies.extend(ensure_tasks(tx.state))
    infos = [{key: task[key] for key in ("id", "is_get", "cfgid", "type")} for task in selected]
    replies.append(Reply("TaskProto:GetRewardRet", {"infos": infos, "dailyStar": data["dailyStar"],
            "weeklyStar": data["weeklyStar"], "gets": rendered, "anvsStarInfo": []}))
    return replies


@register("TaskProto:GetReward")
async def get_reward(ctx, fields):
    uid = ctx.require_login()
    ids = fields.get("ids", [])
    if not isinstance(ids, list) or len(ids) > 1000:
        raise StorageError("Invalid task claim list")
    ids = {integer(value, "instance id", 1) for value in ids}
    if fields.get("id"):
        ids.add(integer(fields["id"], "instance id", 1))
    if not ids:
        raise StorageError("Task claim requires an instance id")
    with ctx.store.transaction(uid) as tx:
        prefix = ensure_tasks(tx.state)
        selected = [row for row in tx.state["tasks"] if row["id"] in ids]
        if len(selected) != len(ids):
            raise StorageError("Unknown or expired task instance")
        replies = prefix + claim(tx, selected)
        if len(selected) == 1:
            replies[-1].fields["info"] = deepcopy(replies[-1].fields["infos"][0])
    return replies


@register("TaskProto:GetRewardByType")
async def get_by_type(ctx, fields):
    return await claim_types(ctx, [{"type": fields.get("type", 0), "nGroup": fields.get("nGroup", 0)}])


@register("TaskProto:GetRewardByTypes")
async def get_by_types(ctx, fields):
    types = fields.get("taskType", [])
    if not isinstance(types, list) or not 1 <= len(types) <= 64:
        raise StorageError("Invalid task type list")
    return await claim_types(ctx, types)


async def claim_types(ctx, requested):
    uid = ctx.require_login()
    selectors = []
    for item in requested:
        kind = integer(item.get("type", 0), "type", 1, 255)
        group = integer(item.get("nGroup", 0), "group")
        if kind not in CATALOGS:
            raise StorageError("This task activity domain is not reconstructed")
        selectors.append((kind, group))
    with ctx.store.transaction(uid) as tx:
        prefix = ensure_tasks(tx.state)
        selected = [row for row in tx.state["tasks"] if row["state"] == 3 and row["is_get"] != 2
                    and any(row["type"] == kind and (not group or task_config(row).get("nGroup", 0) == group)
                            for kind, group in selectors)]
        replies = prefix + claim(tx, selected)
        replies[-1].name = "TaskProto:GetRewardByTypeRet"
    return replies
