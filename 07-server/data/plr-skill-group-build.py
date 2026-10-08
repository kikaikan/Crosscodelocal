"""Build 07-server/data/plr-skill-group.json from the client tactic tables.

Sources (device client config, recovered):
  03-unpack/lua/device-luascripts/cfgCfgPlrSkillGroup.lua         CfgPlrSkillGroup
  03-unpack/lua/device-luascripts/cfgCfgPlrSkillGroupUpgrade.lua  CfgPlrSkillGroupUpgrade
  03-unpack/lua/device-luascripts/cfgCfgPlrAbility.lua            CfgPlrAbility (type==1 = SkillGroup)
  03-unpack/lua/device-luascripts/cfgglobal_setting.lua           g_DefaultAbilityId

TacticsMgr.lua:8 iterates Cfgs.CfgPlrSkillGroup:GetAll(), TacticsData.lua:26 resolves the same
table, and TacticsData.lua:71/82 uses Cfgs.CfgPlrSkillGroupUpgrade:GetByID(id).infos[lv].costs.
Every number below is copied verbatim; nothing here invents a balance value.

Run: python -B 07-server/data/plr-skill-group-build.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LUA = ROOT / "03-unpack" / "lua" / "device-luascripts"
SOURCE_GROUP = LUA / "cfgCfgPlrSkillGroup.lua"
SOURCE_UPGRADE = LUA / "cfgCfgPlrSkillGroupUpgrade.lua"
SOURCE_ABILITY = LUA / "cfgCfgPlrAbility.lua"
SOURCE_SETTINGS = LUA / "cfgglobal_setting.lua"
TARGET = Path(__file__).resolve().parent / "plr-skill-group.json"


def table_rows(text):
    """Top-level rows of a '_G["Table"]={{...},{...}}' config file."""
    rows, current, depth, index = [], None, 0, text.index("{")
    while index < len(text):
        char = text[index]
        if char == "{":
            depth += 1
            if depth == 2:
                current = index
        elif char == "}":
            if depth == 2 and current is not None:
                rows.append(text[current:index + 1])
                current = None
            depth -= 1
            if depth == 0:
                break
        index += 1
    return rows


def first_block(text):
    """Return the first balanced '{...}' block in text."""
    start, depth = text.index("{"), 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise ValueError("Unbalanced braces in client table row")


def blocks(text):
    """Return the top-level '{...}' blocks of text, in order."""
    result, depth, start = [], 0, None
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start is not None:
                result.append(text[start:index + 1])
                start = None
    return result


def number(row, name):
    match = re.search(r'\["%s"\]\s*=\s*(-?\d+)' % name, row)
    return int(match.group(1)) if match else None


def id_list(row, name):
    match = re.search(r'\["%s"\]\s*=\s*\{([^}]*)\}' % name, row)
    if not match:
        return []
    return [int(value) for value in re.findall(r"-?\d+", match.group(1))]


def text_value(row, name):
    match = re.search(r'\["%s"\]\s*=\s*\'([^\']*)\'' % name, row)
    return match.group(1) if match else None


def upgrade_entries(row):
    """Verbatim infos[] entries: index plus the costs triple (absent at max level)."""
    marker = '["infos"]='
    if marker not in row:
        return []
    infos = first_block(row[row.index(marker) + len(marker):])
    entries = []
    for entry in blocks(infos[1:-1]):
        costs = re.search(r'\["costs"\]\s*=\s*\{([^}]*)\}', entry)
        entries.append({"index": number(entry, "index"),
                        "costs": [int(value) for value in re.findall(r"-?\d+", costs.group(1))] if costs else None})
    return entries


def default_group(text):
    match = re.search(r'\["g_DefaultAbilityId"\]=\{[^}]*\["value"\]\s*=\s*\'(\d+)\'', text)
    if not match:
        raise ValueError("g_DefaultAbilityId is missing from cfgglobal_setting.lua")
    return int(match.group(1))


def ability_owner(text):
    """CfgPlrAbility rows of type 1 map to the tactic group named by active_id."""
    owners = {}
    for row in table_rows(text):
        if number(row, "type") == 1:
            ability_id, group_id = number(row, "id"), number(row, "active_id")
            if ability_id is not None and group_id is not None:
                owners[str(group_id)] = ability_id
    return owners


def main():
    base_rows = {}
    for row in table_rows(SOURCE_GROUP.read_text(encoding="utf-8-sig")):
        group_id = number(row, "id")
        if group_id is not None:
            base_rows[group_id] = row
    upgrade_rows = {}
    for row in table_rows(SOURCE_UPGRADE.read_text(encoding="utf-8-sig")):
        group_id = number(row, "id")
        if group_id is not None:
            upgrade_rows[group_id] = row
    owners = ability_owner(SOURCE_ABILITY.read_text(encoding="utf-8-sig"))

    groups = {}
    for group_id, row in base_rows.items():
        upgrades = upgrade_entries(upgrade_rows[group_id]) if group_id in upgrade_rows else []
        groups[str(group_id)] = {
            "id": group_id,
            "name": text_value(row, "sName"),
            "icon": text_value(row, "sIcon"),
            "skill_ids": id_list(row, "aSkillIds"),
            "max_lv": len(upgrades),
            "upgrades": upgrades,
            "ability_id": owners.get(str(group_id)),
        }
    payload = {
        "schema_version": 1,
        "source": [str(path.relative_to(ROOT)).replace("\\", "/") for path in
                   (SOURCE_GROUP, SOURCE_UPGRADE, SOURCE_ABILITY, SOURCE_SETTINGS)],
        "note": "Verbatim field copy; skill_ids are the lv-1 base list from CfgPlrSkillGroup.",
        "default_group": default_group(SOURCE_SETTINGS.read_text(encoding="utf-8-sig")),
        "count": len(groups),
        "groups": groups,
    }
    TARGET.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"count": len(groups), "default_group": payload["default_group"], "target": str(TARGET)},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
