"""Build the offline sub-talent (副天赋) tables from extracted client Lua, with no Lua execution.

Sources (read-only authority, never modified):
  03-unpack/lua/device-luascripts/cfgCfgSubTalentOpenCnt.lua   break level -> open slot count
  03-unpack/lua/device-luascripts/cfgCfgSubTalentSkillPool.lua card pool -> 4 starting talent ids
  03-unpack/lua/device-luascripts/cfgCfgSubTalentSkill.lua     talent id -> group/lv/next_id/costId
  03-unpack/lua/device-luascripts/cfgCfgSubTalentMaterial.lua  costId -> material costs
  03-unpack/lua/device-luascripts/cfgCfgSubTalentTypeEnu.lua   talent category names
  03-unpack/lua/device-luascripts/cfgCardData.lua              CardData[cfgid].subTfSkills = {poolId}
                                (the client table is the
                                 authority for the card -> pool mapping; admin templates only
                                 cross-check, because they cover 217 vs the client's 536 rows)

Output: 07-server/data/sub-talent.json

Every emitted field is copied verbatim from the parsed tables (skills/materials are projected
to the fields the local server consumes). No Lua is executed and nothing is invented. A sha256
per source file is recorded so a rebuilt JSON can be traced back to exact bytes.

Reproduce:  python -B 02-tools/scripts/sub-talent-build.py
"""
from pathlib import Path
import hashlib
import json
import re
import sys

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "02-tools/scripts"))
import config_codec
from protocol_codec import lua_data

# This only widens the bounded parser inside this standalone build process.
config_codec.MAX_INPUT_BYTES = 16 * 1024 * 1024

# nFightSkillId / jPropertys are required by the property port (CardCalculator.lua:410-443):
# nFightSkillId enters ret.skills; jPropertys is accumulated via CfgCardPropertyEnum.sFieldName.
SKILL_FIELDS = ("group", "lv", "name", "next_id", "costId", "nFightSkillId", "jPropertys")
MATERIAL_FIELDS = ("key", "costs", "costAdds")


class ConfigParser(config_codec.LuaDataParser):
    """Bounded data parser plus Lua long strings (parity with the other local builders)."""

    LONG_STRING = re.compile(r"\[(=*)\[")

    def value(self, depth=0):
        self.whitespace()
        match = self.LONG_STRING.match(self.text, self.position)
        if match:
            if depth > self.max_depth or self.nodes >= self.max_nodes:
                self.fail("Config parser bounds exceeded")
            self.nodes += 1
            start = self.position + len(match[0])
            end_marker = "]" + match[1] + "]"
            end = self.text.find(end_marker, start)
            if end < 0:
                self.fail("Unterminated data long string")
            self.position = end + len(end_marker)
            return self.text[start:end]
        return super().value(depth)


def table(name):
    """Parse one fixed-path Lua data table and record its exact bytes."""
    path = PROJECT / "03-unpack/lua/device-luascripts" / name
    contents = path.read_text(encoding="utf-8-sig")
    body = contents.split("=", 1)[1]
    body = body[:body.rfind("}") + 1]
    parsed = ConfigParser(body, max_depth=64, max_nodes=4000000).parse()
    return lua_data(parsed), {"file": path.relative_to(PROJECT).as_posix(),
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

def records(raw, label):
    """Yield catalog rows from either a keyed Lua table or an implicit-index array.

    The extracted tables are keyed dictionaries ([10230]={...}); cfgCfgSubTalentTypeEnu is the
    only implicit-index array. Both shapes are accepted, nothing else.
    """
    rows = list(raw.values()) if isinstance(raw, dict) else list(raw)
    if not rows:
        raise SystemExit("Empty %s catalog" % label)
    for row in rows:
        if not isinstance(row, dict) or "id" not in row:
            raise SystemExit("%s row without a declared id" % label)
    return rows


def unique(rows, label):
    """Index catalog rows by their declared id, failing closed on duplicates."""
    result = {}
    for row in rows:
        identifier = str(int(row["id"]))
        if identifier in result:
            raise SystemExit("Duplicate %s id %s" % (label, identifier))
        result[identifier] = row
    return result


def slots(pool):
    """One pool's four starting talent ids, ordered by their declared index."""
    rows = sorted(pool.get("ids", []), key=lambda row: int(row["index"]))
    return [{"index": int(row["index"]), "id": int(row["id"])} for row in rows]


def card_pools(cards):
    """CardData[cfgid].subTfSkills[1] -> pool id; the client table is authoritative."""
    if not isinstance(cards, dict):
        raise SystemExit("CfgCardData must be a keyed table")
    result = {}
    for identifier, row in cards.items():
        skills = row.get("subTfSkills") if isinstance(row, dict) else None
        if isinstance(skills, list) and skills:
            result[str(int(identifier))] = int(skills[0])
    return {key: result[key] for key in sorted(result, key=int)}


def crosscheck(mapping):
    """Compare against the service-side admin templates; client wins on any conflict."""
    path = PROJECT / "07-server/data/admin-role-templates.json"
    try:
        templates = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False}
    admin = {str(int(key)): int(row["subTfSkills"][0]) for key, row in templates.items()
             if isinstance(row, dict) and row.get("subTfSkills")}
    conflicts = sorted((key, mapping[key], admin[key]) for key in set(mapping) & set(admin)
                       if mapping[key] != admin[key])
    return {"available": True,
            "admin_entries": len(admin),
            "conflicts": [list(row) for row in conflicts],
            "client_only": sorted(set(mapping) - set(admin), key=int),
            "admin_only": sorted(set(admin) - set(mapping), key=int)}


def validate(open_count, pools, skills, materials, mapping):
    """Refuse a table set the local server could not serve consistently."""
    if sorted(int(key) for key in open_count) != list(range(1, 8)):
        raise SystemExit("CfgSubTalentOpenCnt must cover break levels 1..7")
    counts = [int(open_count[str(level)]) for level in range(1, 8)]
    if counts != [0, 1, 2, 3, 4, 4, 4]:
        raise SystemExit("Unexpected CfgSubTalentOpenCnt ladder: %s" % counts)
    if not pools or not skills or not materials:
        raise SystemExit("Empty sub-talent catalog")
    unresolved = set()
    for identifier, pool in pools.items():
        rows = slots(pool)
        if len(rows) != 4:
            raise SystemExit("Pool %s does not declare four slots" % identifier)
        for row in rows:
            if str(row["id"]) not in skills:
                unresolved.add(row["id"])
    for identifier, skill in skills.items():
        if "costId" not in skill:
            continue
        if str(int(skill["costId"])) not in materials:
            raise SystemExit("Skill %s references missing material %s" % (identifier, skill["costId"]))
        nxt = skill.get("next_id")
        if nxt is not None and str(int(nxt)) not in skills:
            raise SystemExit("Skill %s references missing next_id %s" % (identifier, nxt))
    missing_pools = sorted({str(pool) for pool in mapping.values() if str(pool) not in pools}, key=int)
    return {"starting_skills_missing_from_CfgSubTalentSkill": sorted(unresolved),
            "card_pools_missing_from_CfgSubTalentSkillPool": missing_pools}


def main():
    open_raw, source_open = table("cfgCfgSubTalentOpenCnt.lua")
    pool_raw, source_pool = table("cfgCfgSubTalentSkillPool.lua")
    skill_raw, source_skill = table("cfgCfgSubTalentSkill.lua")
    material_raw, source_material = table("cfgCfgSubTalentMaterial.lua")
    type_raw, source_type = table("cfgCfgSubTalentTypeEnu.lua")
    card_raw, source_card = table("cfgCardData.lua")
    open_count = {key: int(row["cnt"])
                  for key, row in unique(records(open_raw, "CfgSubTalentOpenCnt"), "open count").items()}
    pools = {key: {"ids": slots(row)}
             for key, row in unique(records(pool_raw, "CfgSubTalentSkillPool"), "pool").items()}
    skills = {key: {field: row[field] for field in SKILL_FIELDS if field in row}
              for key, row in unique(records(skill_raw, "CfgSubTalentSkill"), "skill").items()}
    materials = {key: {field: row[field] for field in MATERIAL_FIELDS if field in row}
                 for key, row in unique(records(material_raw, "CfgSubTalentMaterial"), "material").items()}
    types = {key: row.get("sName", "")
             for key, row in unique(records(type_raw, "CfgSubTalentTypeEnu"), "type").items()}
    mapping = card_pools(card_raw)
    report = validate(open_count, pools, skills, materials, mapping)
    comparison = crosscheck(mapping)
    output = {
        "schema_version": 1,
        "generator": "02-tools/scripts/sub-talent-build.py",
        "sources": [source_open, source_pool, source_skill, source_material, source_type, source_card],
        "counts": {"open_count": len(open_count), "pools": len(pools), "skills": len(skills),
                   "materials": len(materials), "types": len(types),
                   "card_pools": len(mapping),
                   "missing_pool_ids": len(report["card_pools_missing_from_CfgSubTalentSkillPool"]),
                   "cards_without_defined_pool": len([key for key, pool in mapping.items()
                                                      if str(pool) not in pools])},
        "openCnt": {key: open_count[key] for key in sorted(open_count, key=int)},
        "pools": {key: pools[key] for key in sorted(pools, key=int)},
        "skills": {key: skills[key] for key in sorted(skills, key=int)},
        "materials": {key: materials[key] for key in sorted(materials, key=int)},
        "types": {key: types[key] for key in sorted(types, key=int)},
        "cardPools": mapping,
        "crosscheck": comparison,
        "validation": report,
        "local_policy": [
            "槽位物化规则与升级/装备校验见本文件实现（本地策略，无官服样本）",
            "had 只保留已学会的槽位、绝不补 0；只有 use 允许补 0 占位 —— 客户端实证（RoleCenter.lua:262 是真值判断；had 含 0 会被当成已解锁 id=0，RoleInfoTalentItem2.lua:34-35 随即对 nil 配置取 cfg.icon 报错）（审计 F1）",
            "use 长度取 4 属本地策略推断：依据是 CfgSubTalentOpenCnt 的上限 max(cnt)=4；客户端不断言长度、协议 array|uint 也不限长（审计 F4）",
            "装备属性按 CardCalculator.lua:410-443 移植：nFightSkillId 进技能列表、jPropertys 经 cfgCfgCardPropertyEnum.sFieldName 累加；无法精确表达的形状由服务端显式拒绝",
            "costId 在链尾缺失（全表 429 行），校验顺序必须先判 next_id 再取 costId（审计 F2）",
            "无池卡（pool id 不在 CfgSubTalentSkillPool）静默跳过、不物化槽位（审计 F5）"
        ],
    }
    destination = PROJECT / "07-server/data/sub-talent.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    print(json.dumps({"path": destination.relative_to(PROJECT).as_posix(), "sha256": digest,
                      "bytes": destination.stat().st_size, **output["counts"],
                      "crosscheck_conflicts": len(comparison.get("conflicts", [])),
                      "crosscheck_available": comparison.get("available", False)},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
