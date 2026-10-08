"""Build 07-server/data/card-skill-types.json from the client's skill table.

Source: 03-unpack/lua/device-luascripts/cfgskill.lua (_G["skill"]).  Each card skill
carries the client's SkillMainType in 'main_type'; the official server forwards that value
as sSkillData.type (verified against 05-protocol/samples/tcp/session1-stream02-s2c-frame0092:
official type=3/1/2 matches main_type=3/1/2 for 500100401/500101304/4500104, while the
table's own 'type' field holds a different code).

Run: python -B 07-server/data/card-skill-types-build.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "03-unpack" / "lua" / "device-luascripts" / "cfgskill.lua"
TARGET = Path(__file__).resolve().parent / "card-skill-types.json"

ROW = re.compile(r"\[(\d+)\]=\{")
MAIN_TYPE = re.compile(r'\["main_type"\]\s*=\s*(-?\d+)')


def scan(text):
    """Yield (skill_id, row_text) for every top-level '[id]={...}' row."""
    index = 0
    while True:
        match = ROW.search(text, index)
        if match is None:
            return
        start = match.end() - 1
        depth = 0
        position = start
        while position < len(text):
            char = text[position]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            position += 1
        yield int(match.group(1)), text[start:position + 1]
        index = position + 1


def main():
    text = SOURCE.read_text(encoding="utf-8-sig")
    types = {}
    for skill_id, row in scan(text):
        found = MAIN_TYPE.search(row)
        if found is not None:
            types[str(skill_id)] = int(found.group(1))
    payload = {"schema_version": 1,
               "source": "03-unpack/lua/device-luascripts/cfgskill.lua",
               "field": "main_type",
               "note": ("sSkillData.type forwarded by the official server equals the client's "
                        "SkillMainType from main_type; every value is copied, never invented."),
               "count": len(types),
               "types": types}
    TARGET.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print(json.dumps({"count": len(types), "target": str(TARGET)}))


if __name__ == "__main__":
    main()
