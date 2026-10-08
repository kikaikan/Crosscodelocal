"""Build offline gacha tables from local extracted TextAssets, with no Lua execution."""
from pathlib import Path
import hashlib
import json
import re
import sys

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "02-tools/scripts"))
import config_codec
from protocol_codec import lua_data

# This only changes the bounded parser in this standalone build process.
config_codec.MAX_INPUT_BYTES = 16 * 1024 * 1024


class ConfigParser(config_codec.LuaDataParser):
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
    path = PROJECT / "03-unpack/lua/device-luascripts" / name
    contents = path.read_text(encoding="utf-8-sig")
    body = contents.split("=", 1)[1]
    body = body[:body.rfind("}") + 1]
    parsed = ConfigParser(body, max_depth=64, max_nodes=2000000).parse()
    return lua_data(parsed), {"file": path.relative_to(PROJECT).as_posix(),
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    pools, source_pool = table("cfgCfgCardPool.lua")
    rewards, source_reward = table("cfgRewardInfo.lua")
    cards, source_card = table("cfgCardData.lua")
    raw_rewards, source_raw = table("cfgRewardInfo2.lua")
    add_weight, source_weight = table("cfgCfgCardPoolAddWeight.lua")
    qualities, source_quality = table("cfgCfgCardQuality.lua")
    compensation, source_compensation = table("cfgCfgCardElem.lua")
    open_rules, source_open = table("cfgCfgOpenRules.lua")
    language, source_language = table("cfgCfgLanguage.lua")
    team_rules, source_team = table("cfgCfgCardPoolTeam.lua")
    settings, source_settings = table("cfgglobal_setting.lua")
    items, source_item = table("cfgItemInfo.lua")
    # Retain original raw-row constraints when the compact client table omitted them.
    for row in raw_rewards["data"]:
        reward_id = int(row[0])
        if not row[8]:
            continue
        index = int(row[8])
        reward = rewards.get(reward_id)
        if not reward:
            continue
        for item in reward.get("item", []):
            if (item.get("index") != index or item.get("type") != int(row[9])
                    or item.get("id") != int(row[13])):
                continue
            if row[16]:
                item.setdefault("mustUseCnt", int(row[16]))
            if row[17]:
                item.setdefault("s_probability", row[17])
            if row[18]:
                item.setdefault("s_up_probability", row[18])
    reward_ids = set()
    card_ids = set()
    stack = [int(pair[1]) for pool in pools.values() for pair in pool.get("jCardsId", [])]
    missing = set()
    while stack:
        key = stack.pop()
        if key in reward_ids:
            continue
        reward_ids.add(key)
        reward = rewards.get(key)
        if not reward:
            missing.add(key)
            continue
        for item in reward.get("item", []):
            if item.get("type") == 1:
                stack.append(int(item["id"]))
            elif item.get("type") == 3:
                card_ids.add(int(item["id"]))
    directory = Path(__file__).resolve().parent
    outputs = {"gacha-pools.json": pools,
               "gacha-rewards.json": {key: rewards[key] for key in sorted(reward_ids) if key in rewards},
               "gacha-cards.json": {key: cards[key] for key in sorted(card_ids) if key in cards},
               "gacha-compensation.json": compensation,
               "gacha-text-rules.json": {key: {"id": key, "text": language[str(key)]["language1"]}
                                         for key in sorted({key for pool in pools.values() for key in pool.get("cardRule", [])})},
               "gacha-team-rules.json": team_rules,
               "gacha-policy.json": {"page_size": int(settings["g_CardCreateLogsCnt"]["value"]),
                   "normal_rates": {"3": "0.22", "4": "0.4", "5": "0.08", "6": "0.30"},
                   "normal_max_six": 50, "normal_rule_ids": [17109, 17122, 17137],
                   "choice_rates": {"3": "0.22", "4": "0.4", "5": "0.08", "6": "0.30"},
                   "choice_soft_pity_after": 50, "choice_soft_pity_step": "0.02",
                   "choice_rule_ids": [17172, 17173, 17174, 17175],
                   "choice_selected_share_local": "0.5", "choice_item": items[10053],
                   "choice_first_compensation": 10,
                   "choice_repeat_compensation": {"6": [100, 300], "5": [50, 130], "4": [5, 13], "3": [1, 2]},
                   "choice_compensation_rule_ids": [17190, 17191, 17192, 17193, 17194]},
               "gacha-open-rules.json": {key: open_rules[key] for key in
                                        sorted({key for pool in pools.values() for key in pool.get("conditions", [])})},
               "gacha-rules.json": {"sources": [source_pool, source_reward, source_card, source_raw,
                                                source_weight, source_quality, source_compensation, source_open,
                                                source_language, source_team, source_settings, source_item],
                                    "add_weight": add_weight, "card_quality": qualities,
                                    "counts": {"pools": len(pools), "reward_graph_nodes": len(reward_ids),
                                               "cards": len(card_ids)},
                                    "missing_reward_nodes": sorted(missing),
                                    "missing_card_ids": sorted(card_ids - set(cards)),
                                    "probability_policy": "User local override: six 30%, five 8%, four 40%, three 22%; source leaf role weights and existing guarantees preserved",
                                    "hard_pity_policy": "Normal max50 and choice +2 percentage points after50 misses from local card-rule text; raw mustUseCnt retained as evidence",
                                    "unsupported": ["official server RNG", "normal hidden soft-pity algorithm",
                                                    "all special-activity inheritance details", "quality-up server mechanics",
                                                    "paid currency purchase", "real payments"]}}
    for name, data in outputs.items():
        (directory / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(outputs["gacha-rules.json"]["counts"]))
    print("missing rewards", len(missing), "missing cards", len(card_ids - set(cards)))


if __name__ == "__main__":
    main()
