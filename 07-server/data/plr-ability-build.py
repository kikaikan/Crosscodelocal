"""Build 07-server/data/plr-ability.json from the client's CfgPlrAbility table.

Source: 03-unpack/lua/device-luascripts/cfgCfgPlrAbility.lua (the recovered client config).
Only fields the local server reasons about are kept; every value is copied verbatim.
Run: python -B 07-server/data/plr-ability-build.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "03-unpack" / "lua" / "device-luascripts" / "cfgCfgPlrAbility.lua"
TARGET = Path(__file__).resolve().parent / "plr-ability.json"


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


def number(row, name):
    match = re.search(r'\["%s"\]\s*=\s*(-?\d+)' % name, row)
    return int(match.group(1)) if match else None


QUOTE = chr(34)


def text_value(row, name):
    pattern = r"\[" + QUOTE + name + QUOTE + r"\]\s*=\s*'([^']*)'"
    match = re.search(pattern, row)
    return match.group(1) if match else None


def id_list(row, name):
    match = re.search(r'\["%s"\]\s*=\s*\{([^}]*)\}' % name, row)
    if not match:
        return []
    return [int(value) for value in re.findall(r"-?\d+", match.group(1))]


def main():
    rows = table_rows(SOURCE.read_text(encoding="utf-8-sig"))
    entries = {}
    for row in rows:
        ability_id = number(row, "id")
        if ability_id is None:
            continue
        entries[str(ability_id)] = {
            "id": ability_id,
            "name": text_value(row, "name"),
            "type": number(row, "type"),
            "cost_num": number(row, "cost_num"),
            "open_lv": number(row, "open_lv"),
            "can_reset": "true" in (re.search(r'\["can_reset"\]\s*=\s*(\w+)', row).group(1)
                                    if re.search(r'\["can_reset"\]\s*=\s*(\w+)', row) else ""),
            "prev_id": id_list(row, "prev_id"),
            "next_id": id_list(row, "next_id"),
        }
    payload = {"schema_version": 1,
               "source": "03-unpack/lua/device-luascripts/cfgCfgPlrAbility.lua",
               "note": "Verbatim field copy; no hand-written numbers.",
               "count": len(entries),
               "abilities": entries}
    TARGET.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"count": len(entries), "target": str(TARGET)}))


if __name__ == "__main__":
    main()
