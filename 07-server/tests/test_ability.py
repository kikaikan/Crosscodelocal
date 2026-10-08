"""P2.4 regressions: a real tactical-ability unlock over AbilityProto.

Wire: AbilityProto:GetAbility (3310) and AbilityProto:AddAbility (3312) both answer with
AbilityProto:GetAbilityRet (3311) - that family has no AddAbilityRet schema, and
PlayerAbilityMgr.lua:22-35 SetData is the only consumer of num / abilitys / lastResetTime.

Wire added with the skill-group family:
  GetSkillGroup 3302 -> GetSkillGroupRet 3303 {groups: map|sSkillGroup|id}
  SkillGroupUpgrade 3304 -> SkillGroupUpgradeRet 3305 {group: sSkillGroup}
  SkillGroupUse 3306 -> SkillGroupUseRet 3307 {id, team_id}
and sSkillGroup = {id, lv, skill_ids}.  TacticsMgr:SetData/UpdateData (TacticsMgr.lua:17-47),
TacticsData:SetData/GetLv/GetSkills (TacticsData.lua:12-99), TeamView.lua:1855-1874 and
AbilityInfoView.lua:196-207 are the consumers; the official 3303 sample
05-protocol/samples/tcp/session1-stream02-s2c-frame0175.inner.bin pins the id/lv/skill_ids rule.

Every expectation is cross-checked against the client config tables so neither generated JSON
file can drift from the recovery.
"""
import asyncio
from copy import deepcopy
from pathlib import Path
import re
import secrets
import socket
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
from database import Store, StorageError
from server_core import Context, HANDLERS, LocalServer, load_dependencies
from protocol_codec import encode_packet, readable
from handlers import ability, initialization

CODEC, SEED = load_dependencies(ROOT / "05-protocol" / "endpoints.json",
                                SERVER / "data" / "new_account_seed.json")
LUA = ROOT / "03-unpack" / "lua" / "device-luascripts"
ABILITY_COIN = 10020  # cfgglobal_setting.lua:156 g_AbilityCoinId
GATE_STAGE = 1007  # 战术 PlayerAbility = 通关 0-7 工厂
DEFAULT_GROUP = 1003  # cfgglobal_setting.lua:43 g_DefaultAbilityId (cf. seed team default)
SKILL_SOURCE = LUA / "cfgskill.lua"
OFFICIAL_SAMPLE = (ROOT / "05-protocol" / "samples" / "tcp"
                   / "session1-stream02-s2c-frame0175.inner.bin")


def source_text(path):
    return path.read_text(encoding="utf-8-sig")


def source_rows(path):
    """Top-level rows of a '_G[\"Table\"]={{...},{...}}' config file."""
    text = source_text(path)
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


def source_number(row, name):
    match = re.search(r'\["%s"\]\s*=\s*(-?\d+)' % name, row)
    return int(match.group(1)) if match else None


def source_text_value(row, name):
    match = re.search(r'\["%s"\]\s*=\s*\'([^\']*)\'' % name, row)
    return match.group(1) if match else None


def source_ids(row, name):
    match = re.search(r'\["%s"\]\s*=\s*\{([^}]*)\}' % name, row)
    if not match:
        return []
    return [int(value) for value in re.findall(r"-?\d+", match.group(1))]


def source_upgrade_entries(row):
    """Independent parse of one CfgPlrSkillGroupUpgrade row's infos[] entries."""
    marker = '["infos"]={'
    body = row[row.index(marker) + len(marker) - 1:]
    entries, depth, start = [], 0, None
    for position, char in enumerate(body[1:], 1):  # skip the infos opening brace
        if char == "{":
            if depth == 0:
                start = position
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start is not None:
                block = body[start:position + 1]
                index = re.search(r'\["index"\]\s*=\s*(\d+)', block)
                costs = re.search(r'\["costs"\]\s*=\s*\{([^}]*)\}', block)
                entries.append({"index": int(index.group(1)),
                                "costs": [int(value) for value in re.findall(r"-?\d+", costs.group(1))]
                                if costs else None})
                start = None
    return entries


def ability_group_owners():
    owners = {}
    for row in source_rows(LUA / "cfgCfgPlrAbility.lua"):
        if source_number(row, "type") == 1:
            owners[source_number(row, "active_id")] = source_number(row, "id")
    return owners


def free_port():
    for _ in range(100):
        port = 10000 + secrets.randbelow(22000)
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                pass
    raise RuntimeError("No local test port available in protocol signed-short range")


class AbilityHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=SERVER / "tests", prefix="ability-")
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.uid = self.store.create_account("ability-test", SEED)["uid"]
        self.ctx = Context(LocalServer(CODEC, self.store, SEED), "game", self.uid, True)
        self.set_stages([GATE_STAGE])

    def tearDown(self):
        self.store.close()
        assert Path(self.temporary.name).resolve().is_relative_to((SERVER / "tests").resolve())
        self.temporary.cleanup()

    def set_stages(self, stages):
        with self.store.transaction(self.uid) as tx:
            tx.state["progress"]["cleared_stages"] = list(stages)

    def grant(self, amount):
        with self.store.transaction(self.uid) as tx:
            tx.add_currency("ability_num", amount)

    def state(self):
        return self.store.get_player(self.uid)

    def revision(self):
        return self.store.connection.execute(
            "SELECT revision FROM accounts WHERE uid=?", (self.uid,)).fetchone()[0]

    def call(self, handler, fields):
        replies = asyncio.run(handler(self.ctx, fields))
        for reply in replies:
            frames, tail = CODEC.decode_stream(CODEC.encode_frame(reply.name, reply.fields))
            self.assertFalse(tail)
            self.assertEqual(frames[0].name, reply.name)
        return replies

    def decoded(self, reply):
        return readable(CODEC.decode_frame(CODEC.encode_frame(reply.name, reply.fields)).fields)

    def unlock(self, ability_id=1):
        self.call(ability.add_ability, {"id": ability_id})

    def test_generated_table_matches_every_client_row(self):
        rows = {}
        for row in source_rows(LUA / "cfgCfgPlrAbility.lua"):
            identifier = source_number(row, "id")
            if identifier is not None:
                rows[identifier] = row
        self.assertTrue(rows)
        self.assertEqual(set(rows), {int(key) for key in ability.configs()})
        for key, entry in ability.configs().items():
            row = rows[entry["id"]]
            self.assertEqual(source_number(row, "id"), entry["id"])
            self.assertEqual(source_number(row, "cost_num"), entry["cost_num"])
            self.assertEqual(source_number(row, "open_lv"), entry["open_lv"])
            self.assertEqual(source_number(row, "type"), entry["type"])
            self.assertEqual(source_text_value(row, "name"), entry["name"])
            self.assertEqual(source_ids(row, "prev_id"), entry["prev_id"])
            self.assertEqual(source_ids(row, "next_id"), entry["next_id"])
            self.assertTrue(entry["can_reset"])

    def test_skill_group_table_matches_both_client_tables_and_the_ability_owners(self):
        base = {}
        for row in source_rows(LUA / "cfgCfgPlrSkillGroup.lua"):
            identifier = source_number(row, "id")
            if identifier is not None:
                base[identifier] = row
        upgrade = {}
        for row in source_rows(LUA / "cfgCfgPlrSkillGroupUpgrade.lua"):
            identifier = source_number(row, "id")
            if identifier is not None:
                upgrade[identifier] = row
        owners = ability_group_owners()
        self.assertTrue(base and upgrade)
        self.assertEqual(set(base), set(upgrade))
        self.assertEqual(set(base), {int(key) for key in ability.skill_groups()})
        default = re.search(r'\["g_DefaultAbilityId"\]=\{[^}]*\["value"\]\s*=\s*\'(\d+)\'',
                            source_text(LUA / "cfgglobal_setting.lua"))
        self.assertEqual(int(default.group(1)), ability.default_group())
        self.assertEqual(DEFAULT_GROUP, ability.default_group())
        for key, entry in ability.skill_groups().items():
            row = base[entry["id"]]
            self.assertEqual(source_number(row, "id"), entry["id"])
            self.assertEqual(source_text_value(row, "sName"), entry["name"])
            self.assertEqual(source_text_value(row, "sIcon"), entry["icon"])
            self.assertEqual(source_ids(row, "aSkillIds"), entry["skill_ids"])
            self.assertEqual(owners[entry["id"]], entry["ability_id"])
            entries = source_upgrade_entries(upgrade[entry["id"]])
            self.assertEqual(entries, entry["upgrades"])
            self.assertEqual(len(entries), entry["max_lv"])
            # Every source entry is index 1..max in order and only the terminal one is free.
            self.assertEqual([row["index"] for row in entries], list(range(1, entry["max_lv"] + 1)))
            self.assertIsNone(entries[-1]["costs"])
            for paid in entries[:-1]:
                self.assertEqual(3, len(paid["costs"]))
                self.assertIsInstance(paid["costs"], list)
            self.assertEqual([ABILITY_COIN, ABILITY_COIN], [row["costs"][0] for row in entries[:-1]])

    def test_skill_group_level_rule_matches_the_captured_official_frame(self):
        raw = OFFICIAL_SAMPLE.read_bytes()
        frame = CODEC.decode_frame(raw)
        self.assertEqual(frame.name, "AbilityProto:GetSkillGroupRet")
        groups = readable(frame.fields)["groups"]
        self.assertEqual({int(key) for key in groups}, {int(key) for key in ability.skill_groups()})
        for key, observed in groups.items():
            entry = ability.skill_groups()[str(int(key))]
            self.assertEqual(entry["id"], observed["id"])
            self.assertEqual(ability.group_wire(entry, observed["lv"]), observed)
            expected = [value + observed["lv"] - 1 for value in entry["skill_ids"]]
            self.assertEqual(expected, observed["skill_ids"])

    def test_every_derived_skill_id_exists_in_the_client_skill_table(self):
        text = SKILL_SOURCE.read_text(encoding="utf-8-sig", errors="surrogateescape")
        present = {int(value) for value in re.findall(r"\[(\d+)\]=", text)}
        checked = 0
        for entry in ability.skill_groups().values():
            for level in range(1, entry["max_lv"] + 1):
                for skill_id in ability.group_wire(entry, level)["skill_ids"]:
                    self.assertIn(skill_id, present)
                    checked += 1
        self.assertEqual(checked, sum(3 * entry["max_lv"] for entry in ability.skill_groups().values()))

    def test_locked_feature_reads_empty_and_never_writes(self):
        self.set_stages([])
        before, revision = deepcopy(self.state()), self.revision()
        self.assertFalse(initialization.feature_open(before, "PlayerAbility"))
        replies = self.call(ability.get_ability, {})
        self.assertEqual([reply.name for reply in replies], ["AbilityProto:GetAbilityRet"])
        self.assertEqual(self.decoded(replies[0]),
                         {"num": 0, "abilitys": [], "lastResetTime": 0})
        skill_replies = self.call(ability.get_skill_group, {})
        self.assertEqual([reply.name for reply in skill_replies], ["AbilityProto:GetSkillGroupRet"])
        self.assertEqual(self.decoded(skill_replies[0]), {"groups": {}})
        self.assertEqual(before, self.state())
        self.assertEqual(revision, self.revision())
        with self.assertRaises(StorageError) as raised:
            self.call(ability.add_ability, {"id": 1})
        self.assertIn("尚未开放", str(raised.exception))
        with self.assertRaises(StorageError) as raised:
            self.call(ability.skill_group_upgrade, {"id": DEFAULT_GROUP})
        self.assertIn("尚未开放", str(raised.exception))
        with self.assertRaises(StorageError) as raised:
            self.call(ability.skill_group_use, {"id": DEFAULT_GROUP, "team_id": 1})
        self.assertIn("尚未开放", str(raised.exception))
        self.assertEqual(before, self.state())
        self.assertEqual(revision, self.revision())

    def test_open_gate_read_is_lazy_and_does_not_bump_the_revision(self):
        self.assertTrue(initialization.feature_open(self.state(), "PlayerAbility"))
        self.assertNotIn(ability.STATE_KEY, self.state())
        self.assertNotIn(ability.SKILL_GROUP_STATE_KEY, self.state())
        replies = self.call(ability.get_ability, {})
        self.assertIn(ability.STATE_KEY, self.state())
        self.assertEqual(self.decoded(replies[0])["num"], SEED["login"]["ability_num"])
        skill_replies = self.call(ability.get_skill_group, {})
        self.assertIn(ability.SKILL_GROUP_STATE_KEY, self.state())
        self.assertEqual(self.decoded(skill_replies[0]), {"groups": {}})
        revision = self.revision()
        self.call(ability.get_ability, {})
        self.call(ability.get_skill_group, {})
        self.assertEqual(revision, self.revision())

    def test_add_ability_spends_points_persists_and_pushes_the_refresh(self):
        self.assertEqual(self.state()["login"]["ability_num"], 50)
        replies = self.call(ability.add_ability, {"id": 1})
        self.assertEqual([reply.name for reply in replies], ["AbilityProto:GetAbilityRet"])
        fields = self.decoded(replies[0])
        self.assertEqual(fields["abilitys"], [{"id": 1}])
        self.assertEqual(fields["num"], 0)
        self.assertEqual(self.state()["login"]["ability_num"], 0)
        self.assertEqual(self.state()["inventory"]["10020"], 0)
        self.assertEqual(self.state()[ability.STATE_KEY]["abilitys"], [1])
        self.store.close()
        self.store = Store(self.path)
        self.ctx = Context(LocalServer(CODEC, self.store, SEED), "game", self.uid, True)
        again = self.decoded(self.call(ability.get_ability, {})[0])
        self.assertEqual(again["abilitys"], [{"id": 1}])
        self.assertEqual(again["num"], 0)

    def test_add_ability_rejections_leave_the_save_untouched(self):
        self.grant(1000)  # enough points, so only the intended rule can refuse
        self.call(ability.add_ability, {"id": 1})
        cases = [({"id": 1}, "已经解锁过"),          # duplicate
                 ({"id": 3}, "需要先解锁"),          # prev_id [2] is still locked
                 ({"id": 9999}, "没有这个能力配置"),  # unknown client id
                 ({}, "缺少能力编号"),
                 ({"id": True}, "缺少能力编号")]
        for fields, needle in cases:
            before, revision = deepcopy(self.state()), self.revision()
            with self.assertRaises(StorageError) as raised:
                self.call(ability.add_ability, fields)
            self.assertIn(needle, str(raised.exception))
            self.assertEqual(before, self.state())
            self.assertEqual(revision, self.revision())

    def test_insufficient_points_reject_and_keep_the_balance(self):
        with self.store.transaction(self.uid) as tx:
            tx.add_currency("ability_num", -50)
        before, revision = deepcopy(self.state()), self.revision()
        with self.assertRaises(StorageError) as raised:
            self.call(ability.add_ability, {"id": 1})
        self.assertIn("战术点数不足", str(raised.exception))
        self.assertEqual(before, self.state())
        self.assertEqual(revision, self.revision())

    def test_skill_groups_appear_only_once_their_owning_ability_is_unlocked(self):
        self.assertEqual(self.decoded(self.call(ability.get_skill_group, {})[0]), {"groups": {}})
        self.unlock(1)  # 续行战术 -> active_id 1003
        owner = ability.skill_groups()[str(DEFAULT_GROUP)]
        self.assertEqual(owner["ability_id"], 1)
        fields = self.decoded(self.call(ability.get_skill_group, {})[0])
        self.assertEqual(list(fields["groups"]), [str(DEFAULT_GROUP)])
        self.assertEqual(fields["groups"][str(DEFAULT_GROUP)],
                         {"id": DEFAULT_GROUP, "lv": 1, "skill_ids": owner["skill_ids"]})
        # A second unlocked group is derived from its own CfgPlrAbility active_id, not the default.
        with self.store.transaction(self.uid) as tx:
            tx.state[ability.STATE_KEY]["abilitys"] = sorted(
                tx.state[ability.STATE_KEY]["abilitys"] + [8, 13])
        fields = self.decoded(self.call(ability.get_skill_group, {})[0])
        self.assertEqual(sorted(int(key) for key in fields["groups"]), [1001, 1002, 1003])
        self.assertEqual(fields["groups"]["1002"]["skill_ids"],
                         ability.skill_groups()["1002"]["skill_ids"])

    def test_skill_group_upgrade_spends_points_pushes_the_group_and_survives_reopen(self):
        self.unlock(1)
        self.grant(1000)
        team2_lv = self.state()["teams"][1]["skill_group_lv"]
        self.assertEqual(team2_lv, 1)
        reply = self.call(ability.skill_group_upgrade, {"id": DEFAULT_GROUP})[0]
        self.assertEqual(reply.name, "AbilityProto:SkillGroupUpgradeRet")
        group = self.decoded(reply)["group"]
        self.assertEqual(group, {"id": DEFAULT_GROUP, "lv": 2,
                                 "skill_ids": [1020102, 1020202, 1020302]})
        self.assertEqual(self.state()["login"]["ability_num"], 900)
        self.assertEqual(self.state()["inventory"]["10020"], 900)
        self.assertEqual(self.state()[ability.SKILL_GROUP_STATE_KEY]["lv"], {str(DEFAULT_GROUP): 2})
        # SkillGroupUpgrade refreshes every team already using the group.
        self.assertEqual([team["skill_group_lv"] for team in self.state()["teams"]], [2, 2, 2])
        group = self.decoded(self.call(ability.skill_group_upgrade, {"id": DEFAULT_GROUP})[0])["group"]
        self.assertEqual(group, {"id": DEFAULT_GROUP, "lv": 3,
                                 "skill_ids": [1020103, 1020203, 1020303]})
        self.assertEqual(self.state()["login"]["ability_num"], 700)
        self.store.close()
        self.store = Store(self.path)
        self.ctx = Context(LocalServer(CODEC, self.store, SEED), "game", self.uid, True)
        reopened = self.decoded(self.call(ability.get_skill_group, {})[0])
        self.assertEqual(reopened["groups"][str(DEFAULT_GROUP)]["lv"], 3)
        self.assertEqual(reopened["groups"][str(DEFAULT_GROUP)]["skill_ids"],
                         [1020103, 1020203, 1020303])
        self.assertEqual(self.state()[ability.SKILL_GROUP_STATE_KEY]["lv"], {str(DEFAULT_GROUP): 3})

    def test_skill_group_upgrade_rejections_leave_the_save_untouched(self):
        self.unlock(1)  # spends the seed's 50 points
        cases = [({"id": 9999}, "没有这个战术配置"),      # unknown client id
                 ({"id": True}, "缺少战术编号"),
                 ({}, "缺少战术编号"),
                 ({"id": 1001}, "还没有解锁"),            # ability 13 not unlocked
                 ({"id": DEFAULT_GROUP}, "战术点数不足")]  # 0 points, 1->2 needs 100
        for fields, needle in cases:
            before, revision = deepcopy(self.state()), self.revision()
            with self.assertRaises(StorageError) as raised:
                self.call(ability.skill_group_upgrade, fields)
            self.assertIn(needle, str(raised.exception))
            self.assertEqual(before, self.state())
            self.assertEqual(revision, self.revision())
        self.grant(1000)
        self.call(ability.skill_group_upgrade, {"id": DEFAULT_GROUP})
        self.call(ability.skill_group_upgrade, {"id": DEFAULT_GROUP})
        before, revision = deepcopy(self.state()), self.revision()
        with self.assertRaises(StorageError) as raised:
            self.call(ability.skill_group_upgrade, {"id": DEFAULT_GROUP})
        self.assertIn("已经满级", str(raised.exception))
        self.assertEqual(before, self.state())
        self.assertEqual(revision, self.revision())

    def test_skill_group_use_assigns_the_team_and_survives_reopen(self):
        with self.assertRaises(StorageError) as raised:
            self.call(ability.skill_group_use, {"id": DEFAULT_GROUP, "team_id": 1})
        self.assertIn("还没有解锁", str(raised.exception))
        self.unlock(1)
        reply = self.call(ability.skill_group_use, {"id": DEFAULT_GROUP, "team_id": 2})[0]
        self.assertEqual(reply.name, "AbilityProto:SkillGroupUseRet")
        self.assertEqual(self.decoded(reply), {"id": DEFAULT_GROUP, "team_id": 2})
        team = next(row for row in self.state()["teams"] if row["index"] == 2)
        self.assertEqual((team["skill_group_id"], team["skill_group_lv"]), (DEFAULT_GROUP, 1))
        self.grant(1000)
        self.call(ability.skill_group_upgrade, {"id": DEFAULT_GROUP})
        self.call(ability.skill_group_use, {"id": DEFAULT_GROUP, "team_id": 1})
        team = next(row for row in self.state()["teams"] if row["index"] == 1)
        self.assertEqual((team["skill_group_id"], team["skill_group_lv"]), (DEFAULT_GROUP, 2))
        # id=0 is the client's "clear", which TeamData.lua:622-653 resolves to the default group.
        self.assertEqual(self.decoded(self.call(ability.skill_group_use, {"id": 0, "team_id": 3})[0]),
                         {"id": DEFAULT_GROUP, "team_id": 3})
        self.assertEqual(next(row for row in self.state()["teams"] if row["index"] == 3)["skill_group_id"],
                         DEFAULT_GROUP)
        self.store.close()
        self.store = Store(self.path)
        self.ctx = Context(LocalServer(CODEC, self.store, SEED), "game", self.uid, True)
        self.assertEqual(next(row for row in self.state()["teams"] if row["index"] == 2)["skill_group_id"],
                         DEFAULT_GROUP)

    def test_skill_group_use_rejections_leave_the_save_untouched(self):
        cases = [({"id": 9999, "team_id": 1}, "未知的战术编号"),
                 ({"id": DEFAULT_GROUP, "team_id": 1}, "还没有解锁"),  # gate open, ability locked
                 ({"id": DEFAULT_GROUP, "team_id": 99}, "队伍不存在"),
                 ({"id": DEFAULT_GROUP}, "缺少队伍编号"),
                 ({"team_id": 1}, "缺少战术编号"),
                 ({"id": DEFAULT_GROUP, "team_id": True}, "缺少队伍编号")]
        for fields, needle in cases:
            before, revision = deepcopy(self.state()), self.revision()
            with self.assertRaises(StorageError) as raised:
                self.call(ability.skill_group_use, fields)
            self.assertIn(needle, str(raised.exception))
            self.assertEqual(before, self.state())
            self.assertEqual(revision, self.revision())

    def test_skill_group_use_zero_without_an_unlocked_default_stores_no_tactic(self):
        # The default group is only unlocked once ability 1 is; before that "clear" stores 0,
        # matching TeamData:GetSkillGroupID's final fallback.
        reply = self.call(ability.skill_group_use, {"id": 0, "team_id": 1})[0]
        self.assertEqual(self.decoded(reply), {"id": 0, "team_id": 1})
        team = next(row for row in self.state()["teams"] if row["index"] == 1)
        self.assertEqual((team["skill_group_id"], team["skill_group_lv"]), (0, 0))

    def test_this_module_owns_exactly_its_five_registered_names(self):
        for name in ("AbilityProto:GetAbility", "AbilityProto:AddAbility",
                     "AbilityProto:GetSkillGroup", "AbilityProto:SkillGroupUpgrade",
                     "AbilityProto:SkillGroupUse"):
            self.assertEqual(HANDLERS[name].__module__, ability.__name__)
        # The family's remaining request and every fabricated Ret stay explicit gaps.
        self.assertNotIn("AbilityProto:ResetAbility", HANDLERS)
        self.assertNotIn("AbilityProto:AddAbilityRet", CODEC.schemas)
        self.assertNotIn("AbilityProto:ResetAbilityRet", CODEC.schemas)
        for name in ("AbilityProto:GetSkillGroupRet", "AbilityProto:SkillGroupUpgradeRet",
                     "AbilityProto:SkillGroupUseRet"):
            self.assertIn(name, CODEC.schemas)


class AbilitySocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=SERVER / "tests", prefix="ability-socket-")
        self.directory = Path(self.temporary.name)
        self.store = Store(self.directory / "state.sqlite3")
        self.events = self.directory / "events.jsonl"
        query, game = free_port(), free_port()
        while game == query:
            game = free_port()
        self.server = LocalServer(CODEC, self.store, SEED, "127.0.0.1", query, game, self.events)
        await self.server.start()
        self.clients = []

    async def asyncTearDown(self):
        for reader, writer in self.clients:
            writer.close()
            await writer.wait_closed()
        await self.server.close()
        await asyncio.sleep(0)
        self.store.close()
        assert Path(self.temporary.name).resolve().is_relative_to((SERVER / "tests").resolve())
        self.temporary.cleanup()

    async def connect(self, port):
        result = await asyncio.open_connection("127.0.0.1", port)
        self.clients.append(result)
        return result

    async def read_reply(self, reader):
        import struct
        prefix = await asyncio.wait_for(reader.readexactly(2), 2)
        size = struct.unpack(">H", prefix)[0]
        body = await asyncio.wait_for(reader.readexactly(size), 2)
        return CODEC.decode_frame(body[9:])

    async def send(self, writer, name, fields):
        writer.write(encode_packet(CODEC.encode_frame(name, fields)))
        await writer.drain()

    async def login(self, account):
        query_reader, query_writer = await self.connect(self.server.query_port)
        await self.send(query_writer, "ClientProto:QueryAccount",
                        {"account": account, "SvnVersion": "3.3.0", "pwd": "not-persisted"})
        uid = (await self.read_reply(query_reader)).fields["uid"]
        await self.send(query_writer, "ClientProto:PreLoginGame", {"uid": uid, "distinctId": "local"})
        pre = await self.read_reply(query_reader)
        reader, writer = await self.connect(self.server.game_port)
        await self.send(writer, "ClientProto:LoginGame",
                        {"uid": uid, "key": pre.fields["key"], "SvnVersion": "3.3.0"})
        self.assertEqual((await self.read_reply(reader)).name, "LoginProto:LoginGame")
        return uid, reader, writer

    async def test_unlock_survives_a_real_socket_and_keeps_the_session(self):
        uid, reader, writer = await self.login("ability-socket")
        with self.store.transaction(uid) as tx:
            tx.state["progress"]["cleared_stages"] = [GATE_STAGE]
        await self.send(writer, "AbilityProto:GetAbility", {})
        before = await self.read_reply(reader)
        self.assertEqual(before.name, "AbilityProto:GetAbilityRet")
        self.assertEqual(before.fields["abilitys"], [])
        await self.send(writer, "AbilityProto:AddAbility", {"id": 1})
        unlocked = await self.read_reply(reader)
        self.assertEqual(unlocked.name, "AbilityProto:GetAbilityRet")
        self.assertEqual(unlocked.fields["abilitys"], [{"id": 1}])
        await self.send(writer, "AbilityProto:AddAbility", {"id": 1})
        tip = await self.read_reply(reader)
        self.assertEqual(tip.name, "SystemProto:Tips")
        self.assertEqual(tip.fields["opName"], "AbilityProto:AddAbility")
        await self.send(writer, "ClientProto:Heartbeat", {})
        self.assertEqual((await self.read_reply(reader)).name, "LoginProto:Heartbeat")

    async def test_skill_group_family_survives_a_real_socket(self):
        uid, reader, writer = await self.login("ability-skill-group")
        with self.store.transaction(uid) as tx:
            tx.state["progress"]["cleared_stages"] = [GATE_STAGE]
            tx.add_currency("ability_num", 1000)
        await self.send(writer, "AbilityProto:AddAbility", {"id": 1})
        await self.read_reply(reader)
        await self.send(writer, "AbilityProto:GetSkillGroup", {})
        groups = await self.read_reply(reader)
        self.assertEqual(groups.name, "AbilityProto:GetSkillGroupRet")
        self.assertEqual(groups.fields["groups"][DEFAULT_GROUP]["lv"], 1)
        await self.send(writer, "AbilityProto:SkillGroupUpgrade", {"id": DEFAULT_GROUP})
        upgraded = await self.read_reply(reader)
        self.assertEqual(upgraded.name, "AbilityProto:SkillGroupUpgradeRet")
        self.assertEqual(upgraded.fields["group"]["lv"], 2)
        self.assertEqual(list(upgraded.fields["group"]["skill_ids"]), [1020102, 1020202, 1020302])
        await self.send(writer, "AbilityProto:SkillGroupUse", {"id": DEFAULT_GROUP, "team_id": 2})
        used = await self.read_reply(reader)
        self.assertEqual(used.name, "AbilityProto:SkillGroupUseRet")
        self.assertEqual((used.fields["id"], used.fields["team_id"]), (DEFAULT_GROUP, 2))
        await self.send(writer, "AbilityProto:SkillGroupUse", {"id": DEFAULT_GROUP, "team_id": 99})
        tip = await self.read_reply(reader)
        self.assertEqual(tip.name, "SystemProto:Tips")
        self.assertEqual(tip.fields["opName"], "AbilityProto:SkillGroupUse")
        await self.send(writer, "ClientProto:Heartbeat", {})
        self.assertEqual((await self.read_reply(reader)).name, "LoginProto:Heartbeat")


if __name__ == "__main__":
    unittest.main()
