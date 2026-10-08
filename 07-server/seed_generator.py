"""Generate a fresh local account seed from decoded client configuration.

Never reads captured account field values. Captured traffic is used only to
identify message/structure names; source values come from Lua config tables.
The bounded parser handles data, never executes Lua, loadstring, or eval.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sys

_SOURCE_ROOT = Path(__file__).resolve().parents[1]
# A frozen executable must read the user's unpacked client Lua from beside the
# executable, not from the temporary directory the archive unpacks into. The
# source layout is kept as the fallback so a checkout next to the exe still works.
ROOT = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
        else _SOURCE_ROOT)
sys.path.insert(0, str(ROOT / "02-tools" / "scripts"))
from config_codec import app_path, parse_lua_table
LUA_DIR = app_path("03-unpack", "lua", "device-luascripts")
from card_roles_service import build_card_skills


def python_data(value):
    if not isinstance(value, dict) or "$lua_table" not in value:
        return value
    entries = value["$lua_table"]
    if entries and [entry["key"] for entry in entries] == list(range(1, len(entries) + 1)):
        return [python_data(entry["value"]) for entry in entries]
    return {entry["key"]: python_data(entry["value"]) for entry in entries}


LONG_OPEN = re.compile(r"\[(=*)\[")


def normalize_long_strings(source: str) -> str:
    """Replace Lua long-quoted *data* strings with ordinary quoted literals.

    Lexical scanning keeps delimiters inside ordinary strings untouched. This
    adds data-string syntax only; expressions/functions/comments stay rejected
    by the finite parser. No module imports or Lua evaluation are performed.
    """
    out, i = [], 0
    while i < len(source):
        if source[i] in "\"'":
            start, quote = i, source[i]
            i += 1
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                elif source[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
            else:
                raise ValueError("Unclosed static string")
            out.append(source[start:i])
        elif source[i] == "[" and (match := LONG_OPEN.match(source, i)):
            closing = "]" + match[1] + "]"
            start = i + len(match[0])
            end = source.find(closing, start)
            if end < 0:
                raise ValueError("Unclosed static long string")
            value = source[start:end]
            if value.startswith("\r\n"):
                value = value[2:]
            elif value.startswith("\n"):
                value = value[1:]
            out.append(json.dumps(value, ensure_ascii=False))
            i = end + len(closing)
        else:
            out.append(source[i])
            i += 1
    return "".join(out)


def balanced_table(text: str, start: int) -> str:
    """Extract one record table, normalizing supported Lua long strings.

    Long-bracket data strings ([[...]] and [=[...]=]) are converted to ordinary
    quoted literals so the finite parser can read them; braces, quotes and
    brackets inside them do not affect nesting. Arbitrary neighboring module
    statements and large catalog tables are not parsed or executed.
    """
    if text[start:start + 1] != "{":
        raise ValueError("Record does not start with a table")
    depth = 0
    quote = None
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "[" and (match := LONG_OPEN.match(text, index)):
            closing = "]" + match[1] + "]"
            end = text.find(closing, index + len(match[0]))
            if end < 0:
                raise ValueError("Unterminated configuration record")
            index = end + len(closing)
            continue
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return normalize_long_strings(text[start:index + 1])
        index += 1
    raise ValueError("Unterminated configuration record")


def selected_record(filename: str, key: str | int) -> tuple[dict, dict]:
    path = LUA_DIR / filename
    text = path.read_text("utf-8-sig")
    key_literal = re.escape(str(key)) if isinstance(key, int) else '["\']' + re.escape(key) + '["\']'
    match = re.search(r"\[" + key_literal + r"\]\s*=\s*\{", text)
    if match is None:
        raise ValueError(f"Missing configuration record {filename}:{key}")
    table = balanced_table(text, match.end() - 1)
    record = python_data(parse_lua_table(table))
    if not isinstance(record, dict):
        raise ValueError("Expected a named-field configuration record")
    evidence = {"file": f"03-unpack/lua/device-luascripts/{filename}", "key": key,
                "line": text[:match.start()].count("\n") + 1,
                "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return record, evidence


def first_array_record(filename: str) -> tuple[dict, dict]:
    path = LUA_DIR / filename
    text = path.read_text("utf-8-sig")
    assignment = re.search(r"_G\[[^\]]+\]\s*=\s*\{\s*\{", text)
    if assignment is None:
        raise ValueError(f"Missing first array record: {filename}")
    table = balanced_table(text, assignment.end() - 1)
    return python_data(parse_lua_table(table)), {
        "file": f"03-unpack/lua/device-luascripts/{filename}", "array_index": 1,
        "line": text[:assignment.start()].count("\n") + 1,
        "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def currency_item_ids() -> tuple[dict, list[dict]]:
    """Read integer enum assignments; no source statements are executed."""
    path = LUA_DIR / "GEnum.lua"
    text = path.read_text("utf-8-sig")
    item_ids = {}
    evidence = []
    for name in ("GOLD", "DIAMOND", "DIAMOND_PAY"):
        match = re.search(r"(?m)^ITEM_ID\." + name + r"\s*=\s*([0-9]+)\b", text)
        if match is None:
            raise ValueError(f"Missing currency enum ITEM_ID.{name}")
        item_ids[name.lower()] = int(match.group(1))
        evidence.append({"file": "03-unpack/lua/device-luascripts/GEnum.lua",
                         "symbol": f"ITEM_ID.{name}",
                         "line": text[:match.start()].count("\n") + 1,
                         "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return item_ids, evidence


def generate_seed(sex: int = 1) -> dict:
    if sex not in (1, 2):
        raise ValueError("Sex selection must be 1 or 2")
    evidence = []
    settings = {}
    keys = (
        "g_InitItems", "g_InitStoreExp", "g_InitCardIDs", "g_SexInitCardIds", "g_InitRoleId",
        "g_InitFormation", "g_CardGridInitSize", "g_EquipGridInitSize", "g_FormationDefaultNum",
        "g_DefaultAbilityId", "g_TPMax", "g_CardInitFriendlyRate", "g_new_player_fight_group_ids",
    )
    for key in keys:
        row, source = selected_record("cfgglobal_setting.lua", key)
        evidence.append(source)
        literal = row.get("value", "")
        kind = row["type"]
        if kind == "int":
            settings[key] = int(literal) if literal else 0
        elif kind == "int[]":
            settings[key] = [int(part.strip()) for part in literal.split(",") if part.strip()]
        elif kind == "json":
            settings[key] = json.loads(literal) if literal else {}
        else:
            raise ValueError(f"Unexpected initialization setting type: {kind}")
    upgrade, source = first_array_record("cfgCfgPlrUpgrade.lua")
    evidence.append(source)
    hot, source = first_array_record("cfgCfgPlrHot.lua")
    evidence.append(source)
    currency_ids, currency_evidence = currency_item_ids()
    evidence.extend(currency_evidence)
    inventory = {str(item_id): count for item_id, count in settings["g_InitItems"]}
    selected_commander = settings["g_SexInitCardIds"][sex - 1]
    initial_ids = list(dict.fromkeys([selected_commander, *settings["g_InitCardIDs"]]))
    roles = []
    role_states = {}
    for role_id in settings["g_InitRoleId"]:
        role_cfg, source = selected_record("cfgCfgCardRole.lua", role_id)
        evidence.append(source)
        state = {"lv": role_cfg.get("defaultLv", 1), "b_lv": 0, "tf": 0, "exp": 0,
                 "tv": settings["g_CardInitFriendlyRate"], "t_create": 0, "build_id": 0,
                 "clothes": [], "abilitys": {}, "audio": {}, "new": True,
                 "story_ids": [], "look_skins": []}
        role_states[role_id] = state
        roles.append({"id": role_id, "data": state})
    cards = []
    for instance_id, cfgid in enumerate(initial_ids, 1):
        card_cfg, source = selected_record("cfgCardData.lua", cfgid)
        evidence.append(source)
        # sSkillData carries the client's SkillMainType; without it GetSkillByType filters
        # every skill out and the role screen shows an empty 武装技能 panel.
        skills = build_card_skills(card_cfg.get("skills", []))
        display_role_id = card_cfg.get("role_id")
        # Lua treats numeric zero as truthy, so CRoleInfo:GetBreakLevel will not
        # fall back to 1. Only roles backed by an owned starter card begin at 1;
        # pre-created archive rows for unselected commanders remain locked.
        if display_role_id in role_states:
            role_states[display_role_id]["b_lv"] = 1
        role_id = card_cfg.get("add_role_id") or display_role_id
        card = {
            "cfgid": cfgid, "cid": instance_id, "name": card_cfg["name"],
            "skills": skills, "level": 1, "break_level": 1, "intensify_level": 1,
            "intensify_exp": 0, "hp": int(card_cfg["maxhp"]), "exp": 0,
            "cur_hot": 0, "equips": [], "equip_ids": {}, "performance": 0.0,
            "skin": card_cfg["model"], "skinIsl2d": 0, "skin_a": card_cfg["model"],
            "skinIsl2d_a": 0, "is_new": True, "get_cnt": 1,
            "sub_talent": {}, "tag": 0, "ctime": 0,
            "open_cards": [], "open_mechas": [],
        }
        if role_id in role_states:
            card["role"] = role_states[role_id]
        cards.append(card)
    commander_cfg, _ = selected_record("cfgCardData.lua", selected_commander)
    starter_positions = settings["g_InitFormation"][0]
    teams = []
    for index in range(1, settings["g_FormationDefaultNum"] + 1):
        placed = []
        if index == 1:
            for card, (row, col) in zip(cards, starter_positions):
                placed.append({"cid": card["cid"], "index": len(placed) + 1,
                               "row": row, "col": col, "bIsNpc": False})
        teams.append({"index": index, "data": placed, "leader": cards[0]["cid"] if placed else 0,
                      "name": f"队伍{index}", "skill_group_id": settings["g_DefaultAbilityId"],
                      "skill_group_lv": 1, "performance": 0, "bIsReserveSP": False, "nReserveNP": 0})
    player = {
        "uid": 0, "name": "本地总队长", "level": 1, "exp": 0,
        "hot": hot["adds"][2], "gold": inventory.get(str(currency_ids["gold"]), 0),
        "diamond": inventory.get(str(currency_ids["diamond"]), 0),
        "diamond_pay": inventory.get(str(currency_ids["diamond_pay"]), 0),
        "currtime": 0, "create_time": 0, "sign": "", "army_coin": 0,
        "tp": settings["g_TPMax"], "tpBeginTime": 0, "notLog": 0,
    }
    login = {
        "can_modify_name": 2, "icon_id": commander_cfg["model"], "panel_id": commander_cfg["model"],
        "ability_num": upgrade["nAbilityNum"], "serverID": 1, "sel_card_ix": sex,
        "use_vid": 0, "t_hot": 0, "hot_buy_cnt": 0, "birth": [],
        "icon_frame": 1, "role_panel_id": selected_commander, "background_id": 1,
        "icon_title": 1, "icon_emotes": [],
    }
    item_push = [{"id": int(cfgid), "num": count, "ix": 1, "time": 0, "expiry": 0}
                 for cfgid, count in inventory.items()]
    seed = {
        "schema_version": 1, "player": player, "login": login,
        "inventory": inventory, "cards": cards, "teams": teams,
        "max_card_size": settings["g_CardGridInitSize"], "max_equip_size": settings["g_EquipGridInitSize"],
        "card_roles": roles, "store_exp": settings["g_InitStoreExp"],
        "progress": {"cleared_stages": [], "claimed_rewards": [], "unlocked_functions": [],
                     "tutorial_completed": False},
        "client_data": {}, "next_card_id": len(cards) + 1,
        "initial_pushes": [
            {"name": "PlayerProto:CardsDataRet", "fields": {"store_exp": settings["g_InitStoreExp"], "rename_records": {}}},
            {"name": "PlayerProto:CardAdd", "fields": {"cards": cards, "cur_size": len(cards), "max_size": settings["g_CardGridInitSize"], "finish": True}},
            {"name": "PlayerProto:AddCardRole", "fields": {"roles": roles}},
            {"name": "PlayerProto:ItemBag", "fields": {"item": item_push, "ix": 1, "is_finish": True}},
            {"name": "PlayerProto:TeamData", "fields": {"data": teams, "count": len(teams), "isFinish": True}},
        ],
        "_source_evidence": evidence, "_initialization_settings": settings,
        "_currency_item_ids": currency_ids,
        "_policies": {
            "uid": "placeholder 0; Store.create_account allocates a fresh local UID and timestamps",
            "commander": "Sex selection indexes g_SexInitCardIds; no additional cards since g_InitCardIDs has no value",
            "hot": "Local initial amount equals CfgPlrHot[1].adds[3] normal recovery cap; exact original new-account grant was not captured",
            "tp": "Local initial amount equals configured g_TPMax; exact original new-account grant was not captured",
            "presentation": "Provisional commander model; can_modify_name=2 enters first-login sex/name selection (LoginCommFuns.lua:754). Default frame/title/background 1; not copied from an existing profile",
            "progression": "No stages cleared, rewards claimed, gacha history, or full feature unlocks are seeded",
            "credentials": "No official account, login key, SDK user object, or token is present",
            "currency": "GEnum ITEM_ID assignments map player gold/diamond/diamond_pay to inventory; PlayerClient.UpdateCoin/GetCoin confirms the same mapping",
            "roles": "Configured g_InitRoleId role entries; selected commander is the sole owned fighter by default",
            "card_upgrade_indices": "Unupgraded break/intensify table index is 1, rendered as index-1 by UI; CardCalculator.lua:120 indexes CardBreak directly",
            "pushes": "Templates derived from schema/captured message shapes; server must render current mutable state, not replay these fixed values forever",
        },
    }
    return seed


@lru_cache(maxsize=2)
def _commander_template_cached(sex: int) -> dict:
    return generate_seed(sex)["cards"][0]


def commander_template(sex: int = 1) -> dict:
    """Return a fresh male/female commander; caller assigns its local instance ID.

    Before initial selection the seed's default male commander is provisional.
    cfgid/model/skills/HP and role state all come from current client config.
    """
    return deepcopy(_commander_template_cached(sex))


def validate_seed(seed: dict) -> dict:
    from protocol_codec import IVProtoCodec, WireConfig
    schema = json.loads((ROOT / "05-protocol" / "endpoints.json").read_text("utf-8"))
    codec = IVProtoCodec(schema, WireConfig("little", max_frame_size=65535))
    assert seed["player"]["uid"] == 0 and seed["player"]["level"] == 1
    assert len({card["cid"] for card in seed["cards"]}) == len(seed["cards"])
    assert not seed["progress"]["cleared_stages"]
    frames = [{"name": "LoginProto:LoginGame", "fields": {"infos": seed["player"], **seed["login"]}},
              *seed["initial_pushes"]]
    for message in frames:
        raw = codec.encode_frame(message["name"], message["fields"])
        decoded = codec.decode_frame(raw)
        assert codec.encode_frame(decoded.name, decoded.fields) == raw
    return {"wire_messages_validated": len(frames), "owned_cards": len(seed["cards"]),
            "inventory_entries": len(seed["inventory"]), "team_presets": len(seed["teams"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sex", type=int, choices=(1, 2), default=1)
    parser.add_argument("--output", type=Path, default=app_path("data", "new_account_seed.json"))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    seed = generate_seed(args.sex)
    validation = validate_seed(seed)
    if args.self_test:
        other = generate_seed(3 - args.sex)
        validate_seed(other)
        assert other["cards"][0]["cfgid"] != seed["cards"][0]["cfgid"]
        assert seed["inventory"] == {"10001": 1000, "10002": 100, "11002": 1,
                                     "60101": 800, "60102": 100, "60103": 100}
        assert seed["store_exp"] == 1000 and seed["next_card_id"] == 2
        assert seed["player"]["diamond_pay"] == 0
        role_levels = {row["id"]: row["data"]["b_lv"] for row in seed["card_roles"]}
        owned_roles = {selected_record("cfgCardData.lua", card["cfgid"])[0]["role_id"]
                       for card in seed["cards"]}
        assert all(role_levels[role_id] == 1 for role_id in owned_roles)
        assert all(level == (1 if role_id in owned_roles else 0)
                   for role_id, level in role_levels.items())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(seed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"seed_generated": True, **validation, "self_test": args.self_test}))
