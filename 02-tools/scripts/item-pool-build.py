"""Build the offline item-pool / gacha tables from extracted client Lua, with no Lua execution.

Sources (read-only authority, never modified):
  03-unpack/lua/device-luascripts/cfgCfgItemPool.lua
  03-unpack/lua/device-luascripts/cfgCfgItemPoolReward.lua
  03-unpack/lua/device-luascripts/cfgCfgItemPoolConsume.lua

Output: 07-server/data/item-pool-pools.json

The client loads exactly these three tables in ItemPoolInfo.lua:11-39 (InitCfg via
Cfgs.CfgItemPool:GetByID / Cfgs.CfgItemPoolReward:GetByID), groups reward rows by
their `rounds` array, and reads the draw cost in ItemPoolInfo.lua:162-249
(GetCostGoods, costtype 1/2/3) and the special consume table in ItemPoolInfo.lua:476-481
(GetPoolConsume -> Cfgs.CfgItemPoolConsume). This script copies every emitted field
verbatim from the parsed table; it invents nothing and executes no Lua. A sha256 of each
source file is recorded so a rebuilt JSON can be traced back to exact bytes.

Reproduce:  python -B 02-tools/scripts/item-pool-build.py
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


class ConfigParser(config_codec.LuaDataParser):
    """Bounded data parser plus Lua long strings (kept for parity with the card builder)."""

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
    parsed = ConfigParser(body, max_depth=64, max_nodes=2000000).parse()
    return lua_data(parsed), {"file": path.relative_to(PROJECT).as_posix(),
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def keyed(rows, label):
    """Index a list catalog by its declared id, failing closed on duplicates."""
    result = {}
    for row in rows:
        identifier = str(int(row["id"]))
        if identifier in result:
            raise SystemExit("Duplicate %s id %s" % (label, identifier))
        result[identifier] = row
    return result


def validate(pools, groups, consume):
    """Reject a config set the server could not serve consistently.

    A pool without its reward group, a reward row without reward/index/rounds, or a
    costtype without its cost table would otherwise surface as an invented default
    at draw time. Failing here keeps the local tables honest.
    """
    for identifier, pool in pools.items():
        for field in ("id", "group", "type", "extracttype", "costtype", "cost"):
            if field not in pool:
                raise SystemExit("Pool %s is missing %s" % (identifier, field))
        if int(pool["id"]) != int(identifier):
            raise SystemExit("Pool %s declares id %s" % (identifier, pool["id"]))
        group = groups.get(str(int(pool["group"])))
        if group is None:
            raise SystemExit("Pool %s references missing reward group %s" % (identifier, pool["group"]))
        if not group.get("pool"):
            raise SystemExit("Reward group %s has no rows" % pool["group"])
        for row in group["pool"]:
            if "index" not in row or "weight" not in row or "rounds" not in row:
                raise SystemExit("Reward group %s has a row without index/weight/rounds" % pool["group"])
            if not row["rounds"]:
                raise SystemExit("Reward index %s has no rounds" % row["index"])
            reward = row.get("reward")
            if not isinstance(reward, list) or len(reward) < 2:
                raise SystemExit("Reward index %s has no [item, num] pair" % row["index"])
        if int(pool["costtype"]) == 3:
            special = pool.get("specialCost")
            if special is None or str(int(special)) not in consume:
                raise SystemExit("Pool %s needs consume table %s" % (identifier, special))


def main():
    pools_raw, source_pool = table("cfgCfgItemPool.lua")
    rewards_raw, source_reward = table("cfgCfgItemPoolReward.lua")
    consume_raw, source_consume = table("cfgCfgItemPoolConsume.lua")
    pools = keyed(pools_raw.values(), "pool")
    groups = keyed(rewards_raw, "reward group")
    consume = keyed(consume_raw, "consume")
    validate(pools, groups, consume)
    output = {
        "schema_version": 1,
        "generator": "02-tools/scripts/item-pool-build.py",
        "sources": [source_pool, source_reward, source_consume],
        "counts": {"pools": len(pools), "reward_groups": len(groups),
                   "reward_entries": sum(len(group["pool"]) for group in groups.values()),
                   "consume_tables": len(consume)},
        "pools": {key: pools[key] for key in sorted(pools, key=int)},
        "reward_groups": {key: groups[key] for key in sorted(groups, key=int)},
        "consume": {key: consume[key] for key in sorted(consume, key=int)},
    }
    destination = PROJECT / "07-server/data/item-pool-pools.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
