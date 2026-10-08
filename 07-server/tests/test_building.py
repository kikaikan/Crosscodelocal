"""task-8 regressions: a real base facility list, a valid power state, construction,
upgrades and staffing for BuildingProto.

The loading-mask root cause is MatrixView.lua:25/57-69/74-79 - the scene (and therefore
the release of the matrix_scene_enter weight) is only reached when
MatrixMgr:GetBuildingDatas() is non-empty and BuildsListRet carries is_finish=true
(BuildingProto.lua:19-25).  Every expectation below is also checked against the client
configuration tables, so the handler constants cannot silently drift.
"""
import asyncio
from copy import deepcopy
from pathlib import Path
import secrets
import socket
import struct
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
from handlers import building

CODEC, SEED = load_dependencies(ROOT / "05-protocol" / "endpoints.json",
                                SERVER / "data" / "new_account_seed.json")
LUA = ROOT / "03-unpack" / "lua" / "device-luascripts"
GRID_X, GRID_Y = 22, 16  # cfgCfgBOpenArea.lua open area 1: startPos {1,1}, scale {22,16}


def lua_rows(path):
    """Top-level rows of a '_G["Table"]={{...},{...}}' config file."""
    text = path.read_text(encoding="utf-8")
    rows, current, depth = [], None, 0
    index = text.index("{")
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


def lua_scalars(row):
    """Scalar '["name"]=value' pairs of one row; nested tables are skipped."""
    result = {}
    position = 0
    while True:
        start = row.find('["', position)
        if start < 0:
            break
        name_end = row.find('"]=', start)
        if name_end < 0:
            break
        name = row[start + 2:name_end]
        value_start = name_end + 3
        if row[value_start] == "'":
            value_end = row.find("'", value_start + 1)
            result[name] = row[value_start + 1:value_end]
        else:
            value_end = value_start
            while value_end < len(row) and row[value_end] not in ",}":
                value_end += 1
            result[name] = row[value_start:value_end]
        position = max(value_end, value_start) + 1
    return result


_CONFIGS = {}


def config_rows(table):
    if table not in _CONFIGS:
        path = LUA / ("cfg" + table + ".lua")
        if not path.is_file():
            raise AssertionError("missing client config table: " + str(path))
        rows = {}
        for row in lua_rows(path):
            scalars = lua_scalars(row)
            if "id" in scalars:
                rows[int(scalars["id"])] = scalars
        _CONFIGS[table] = rows
    return _CONFIGS[table]


def base_rows():
    return config_rows("CfgBuidingBase")


def level_rows(table):
    return {key: {"powerVal": int(row["powerVal"]), "maxHp": int(row["maxHp"])}
            for key, row in config_rows(table).items()}


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


class BuildingHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=SERVER / "tests", prefix="building-")
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.uid = self.store.create_account("building-test", SEED)["uid"]
        self.ctx = Context(LocalServer(CODEC, self.store, SEED), "game", self.uid, True)

    def tearDown(self):
        self.store.close()
        assert Path(self.temporary.name).resolve().is_relative_to((SERVER / "tests").resolve())
        self.temporary.cleanup()

    def state(self):
        return self.store.get_player(self.uid)

    def rows(self):
        state = self.state()
        if building.STATE_KEY not in state:
            self.call(building.builds_list, {})  # the handler owns lazy initialization
            state = self.state()
        return state[building.STATE_KEY]

    def revision(self):
        return self.store.connection.execute(
            "SELECT revision FROM accounts WHERE uid=?", (self.uid,)).fetchone()[0]

    def call(self, handler, fields):
        replies = asyncio.run(handler(self.ctx, fields))
        for reply in replies:
            inner = CODEC.encode_frame(reply.name, reply.fields)
            frames, tail = CODEC.decode_stream(inner)
            self.assertFalse(tail)
            self.assertEqual(frames[0].name, reply.name)
        return replies

    def decoded(self, reply):
        return readable(CODEC.decode_frame(CODEC.encode_frame(reply.name, reply.fields)).fields)

    def tower(self):
        return next(row for row in self.rows().values() if row["cfgid"] == 1001)

    def test_builds_list_is_non_empty_and_every_row_indexes_a_real_client_cfg(self):
        replies = self.call(building.builds_list, {})
        self.assertEqual([reply.name for reply in replies], ["BuildingProto:BuildsListRet"])
        fields = self.decoded(replies[0])
        self.assertTrue(fields["is_finish"])  # dispatches Matrix_Building_Update
        builds = fields["builds"]
        self.assertTrue(builds)  # MatrixView.lua:63-68 opens the scene only for non-empty ids
        ids = [row["id"] for row in builds]
        self.assertEqual(len(ids), len(set(ids)))  # MatrixMgr.lua:168-174 indexes by v.id
        self.assertEqual([row["id"] for row in builds], sorted(ids))
        schema = {field["name"] for field in CODEC.schemas["sBuildInfo"]["fields"]}
        base = base_rows()
        levels = {}
        for row in builds:
            self.assertTrue(set(row) <= schema)
            self.assertIn(row["cfgId"], base)
            self.assertEqual(base[row["cfgId"]]["key"], str(row["cfgId"]))
            table = base[row["cfgId"]]["upCfg"]
            levels.setdefault(table, level_rows(table))
            self.assertIn(row["lv"], levels[table])
            self.assertEqual(row["hp"], levels[table][row["lv"]]["maxHp"])
            self.assertEqual(len(row["pos"]), 2)
            self.assertTrue(1 <= row["pos"][0] <= GRID_X and 1 <= row["pos"][1] <= GRID_Y)
            self.assertEqual(row["roleIds"], [])
        self.assertEqual({row["cfgId"] for row in builds}, {1001, 1003, 1006})
        self.assertIn(1001, [row["cfgId"] for row in builds])  # 行星指挥部/指挥中心

    def test_every_hardcoded_level_table_matches_the_client_config(self):
        tables = {1001: "CfgBControlTowerLvl", 1002: "CfgBPowerHouseLvl", 1003: "CfgBProductLvl",
                  1004: "CfgBTradeLvl", 1006: "CfgBCompoundLvl", 1009: "CfgBRemouldLvl"}
        self.assertEqual(set(tables), set(building.BUILDING_CFGS))
        base = base_rows()
        for cfgid, table in tables.items():
            self.assertEqual(base[cfgid]["key"], str(cfgid))  # CfgBase.lua:34 needs id and key
            self.assertEqual(base[cfgid]["upCfg"], table)
            self.assertEqual(int(base[cfgid]["type"]), building.BUILDING_CFGS[cfgid]["type"])
            client = level_rows(table)
            self.assertEqual(len(client), building.level_count(cfgid))
            self.assertEqual([client[level]["powerVal"] for level in sorted(client)],
                             list(building._POWER_VAL[cfgid]))
            self.assertEqual([client[level]["maxHp"] for level in sorted(client)],
                             list(building._MAX_HP[cfgid]))
        for cfgid, pos in building.INITIAL_BUILDINGS:
            self.assertEqual(base[cfgid]["initOpen"], "true")
            self.assertIn(base[cfgid]["upCfg"], tables.values())
            self.assertTrue(1 <= pos[0] <= GRID_X and 1 <= pos[1] <= GRID_Y)
        self.assertEqual(base[1002]["initOpen"], "false")  # tutorial group 120 builds this one

    def test_builds_base_info_power_state_hits_cfg_b_global_power(self):
        replies = self.call(building.builds_base_info, {})
        self.assertEqual([reply.name for reply in replies], ["BuildingProto:BuildsBaseInfoRet"])
        fields = self.decoded(replies[0])
        schema = {field["name"] for field in CODEC.schemas["BuildingProto:BuildsBaseInfoRet"]["fields"]}
        self.assertEqual(set(fields), schema)
        power_ids = set(config_rows("CfgBGobalPower"))
        self.assertTrue(power_ids)
        self.assertIn(fields["runTypeCfgId"], power_ids)
        self.assertNotEqual(fields["runTypeCfgId"], 0)  # 0 is truthy in Lua: '0 or 1' is 0
        self.assertEqual(fields["warningLv"], 1)
        self.assertIn(1, config_rows("CfgBAssault"))  # MatrixScene:SetWarning lookups
        base = base_rows()
        supply = demand = 0
        for row in self.rows().values():
            value = level_rows(base[row["cfgid"]]["upCfg"])[row["level"]]["powerVal"]
            if value >= 0:
                supply += value
            else:
                demand += -value
        self.assertEqual(fields["power"]["add"], supply)
        self.assertEqual(fields["power"]["realCost"], demand)
        self.assertEqual(fields["buildCnts"], {"1001": 1, "1003": 1, "1006": 1})
        self.assertEqual(fields["roleCnt"], 0)
        self.assertEqual(fields["extraPresetTeamNum"], 1)

    def test_initial_state_is_lazy_idempotent_and_never_rewritten_by_reads(self):
        first = self.call(building.builds_list, {})[0]
        revision = self.revision()
        second = self.call(building.builds_list, {})[0]
        self.assertEqual(first.fields, second.fields)
        self.assertEqual(revision, self.revision())
        with self.store.transaction(self.uid) as tx:
            tx.state.pop(building.STATE_KEY)
        self.assertNotIn(building.STATE_KEY, self.state())
        third = self.call(building.builds_list, {})[0]
        self.assertEqual(third.fields, first.fields)
        self.assertIn(building.STATE_KEY, self.state())
        revision = self.revision()
        self.call(building.builds_base_info, {})
        self.assertEqual(revision, self.revision())

    def test_build_create_grows_the_list_persists_and_pushes_the_notice(self):
        replies = self.call(building.build_create, {"cfgId": 1002, "pos": [0, 0]})
        self.assertEqual([reply.name for reply in replies],
                         ["BuildingProto:AddNotice", "BuildingProto:BuildCreateRet"])
        notice = self.decoded(replies[0])
        self.assertTrue(notice["is_finish"])
        created = notice["builds"][0]
        self.assertEqual(created["cfgId"], 1002)
        self.assertEqual(created["lv"], 1)
        self.assertEqual(created["hp"], level_rows("CfgBPowerHouseLvl")[1]["maxHp"])
        ret = self.decoded(replies[1])
        self.assertEqual(ret, {"cfgId": 1002, "pos": [1, 1], "ok": True})
        self.assertEqual(created["pos"], [1, 1])
        self.assertTrue(1 <= created["pos"][0] <= GRID_X and 1 <= created["pos"][1] <= GRID_Y)
        self.assertEqual(len(self.rows()), 4)
        self.store.close()
        self.store = Store(self.path)
        self.ctx = Context(LocalServer(CODEC, self.store, SEED), "game", self.uid, True)
        builds = self.decoded(self.call(building.builds_list, {})[0])["builds"]
        self.assertIn(1002, [row["cfgId"] for row in builds])
        self.assertEqual(len(builds), 4)

    def test_dorm_and_consulting_entries_are_buildable_once_and_stop_at_level_one(self):
        """task-1/P1.4: MatrixCreate.lua:94-96 lists every isShow CfgBuidingBase row, and
        MatrixCreateInfo.lua:74 sends BuildCreate(cfg.id) for the tapped entry, so
        BuildCreate(2001) must produce a real facility instead of leaving the player on a
        dead 建造 button.  2001/2002 have no cost row, so the client's own quote is zero."""
        base = base_rows()
        self.assertEqual(int(base[2001]["type"]), building.ENTRY_CFGS[2001]["type"])
        self.assertEqual(base[2001]["name"], building.ENTRY_CFGS[2001]["name"])
        self.assertEqual(int(base[2002]["type"]), building.ENTRY_CFGS[2002]["type"])
        self.assertEqual(base[2002]["name"], building.ENTRY_CFGS[2002]["name"])
        self.assertNotIn("upCfg", base[2001])  # the dormitory has no level table at all
        self.assertEqual(base[2002]["upCfg"], "CfgPhyRoomLvl")
        self.assertEqual({int(row["powerVal"]) for row in config_rows("CfgPhyRoomLvl").values()},
                         {0})
        for cfgid in (2001, 2002):
            replies = self.call(building.build_create, {"cfgId": cfgid, "pos": [0, 0]})
            self.assertEqual([reply.name for reply in replies],
                             ["BuildingProto:AddNotice", "BuildingProto:BuildCreateRet"])
            created = self.decoded(replies[0])["builds"][0]
            self.assertEqual(created["cfgId"], cfgid)
            self.assertEqual(created["lv"], 1)
            self.assertEqual(created["hp"], building.ENTRY_MAX_HP)
            self.assertTrue(self.decoded(replies[1])["ok"])
            before, revision = deepcopy(self.state()), self.revision()
            with self.assertRaises(StorageError) as raised:
                self.call(building.build_create, {"cfgId": cfgid, "pos": [0, 0]})
            self.assertIn("已经建造过", str(raised.exception))
            self.assertEqual(before, self.state())
            self.assertEqual(revision, self.revision())
            built = next(row for row in self.rows().values() if row["cfgid"] == cfgid)
            before, revision = deepcopy(self.state()), self.revision()
            with self.assertRaises(StorageError) as raised:
                self.call(building.upgrade, {"id": built["id"]})
            self.assertIn("最高等级", str(raised.exception))
            self.assertEqual(before, self.state())
            self.assertEqual(revision, self.revision())

    def test_build_create_rejections_leave_the_save_untouched(self):
        cases = [(building.build_create, {"cfgId": 1001, "pos": [1, 1]}, "已经建造过"),
                 (building.build_create, {"cfgId": 9999, "pos": [1, 1]}, "没有可建造"),
                 (building.build_create, {}, "缺少建筑配置编号"),
                 (building.build_create, {"cfgId": True}, "缺少建筑配置编号"),
                 (building.upgrade, {"id": 999}, "没有这座设施"),
                 (building.upgrade, {}, "缺少建筑编号"),
                 (building.build_set_role, {"infos": []}, "缺少建筑信息"),
                 (building.build_set_role, {"infos": [{"roleIds": []}]}, "缺少建筑编号"),
                 (building.build_set_role, {"infos": [{"id": 1, "roleIds": 7}]}, "必须是数组"),
                 (building.build_set_role, {"infos": [{"id": 1, "roleIds": [True]}]}, "必须是整数"),
                 (building.get_build_update, {"ids": "1"}, "格式不正确"),
                 (building.get_build_update, {"ids": [True]}, "必须是整数")]
        for handler, fields, needle in cases:
            before, revision = deepcopy(self.state()), self.revision()
            with self.assertRaises(StorageError) as raised:
                self.call(handler, fields)
            self.assertIn(needle, str(raised.exception))
            self.assertEqual(before, self.state())
            self.assertEqual(revision, self.revision())

    def test_upgrade_changes_the_level_and_stops_at_the_client_max_level(self):
        tower = self.tower()
        levels = level_rows("CfgBControlTowerLvl")
        before_revision = self.revision()
        replies = self.call(building.upgrade, {"id": tower["id"]})
        self.assertEqual([reply.name for reply in replies],
                         ["BuildingProto:AddNotice", "BuildingProto:UpgradeRet"])
        row = self.decoded(replies[0])["builds"][0]
        self.assertEqual(row["id"], tower["id"])
        self.assertEqual(row["lv"], 2)
        self.assertEqual(row["hp"], levels[2]["maxHp"])
        self.assertEqual(self.decoded(replies[1]), {"id": tower["id"], "ok": True})
        stored = self.rows()[str(tower["id"])]
        self.assertEqual(stored["level"], 2)
        self.assertEqual(stored["hp"], levels[2]["maxHp"])
        self.assertGreater(self.revision(), before_revision)
        for expected in range(3, max(levels) + 1):
            self.call(building.upgrade, {"id": tower["id"]})
            self.assertEqual(self.rows()[str(tower["id"])]["level"], expected)
        before, revision = deepcopy(self.state()), self.revision()
        with self.assertRaises(StorageError) as raised:
            self.call(building.upgrade, {"id": tower["id"]})
        self.assertIn("最高等级", str(raised.exception))
        self.assertEqual(before, self.state())
        self.assertEqual(revision, self.revision())

    def test_build_set_role_stores_facility_roles_and_skips_dorm_room_ids(self):
        tower = self.tower()
        replies = self.call(building.build_set_role, {"infos": [
            {"id": tower["id"], "roleIds": [71010, 71020]},
            {"id": 101, "roleIds": [71010], "teamId": 2}]})
        self.assertEqual([reply.name for reply in replies],
                         ["BuildingProto:AddNotice", "BuildingProto:BuildSetRoleRet"])
        self.assertEqual(self.decoded(replies[1])["infos"],
                         [{"id": tower["id"], "roleIds": [71010, 71020], "teamId": 0}])
        self.assertEqual(self.decoded(replies[0])["builds"][0]["roleIds"], [71010, 71020])
        self.assertEqual(self.rows()[str(tower["id"])]["roleIds"], [71010, 71020])
        self.assertNotIn("101", self.rows())  # a dorm room id is never stored as a facility
        self.assertEqual(self.decoded(self.call(building.builds_base_info, {})[0])["roleCnt"], 2)
        self.call(building.build_set_role, {"infos": [{"id": tower["id"], "roleIds": []}]})
        self.assertEqual(self.rows()[str(tower["id"])]["roleIds"], [])
        self.assertEqual(self.decoded(self.call(building.builds_base_info, {})[0])["roleCnt"], 0)

    def test_get_build_update_answers_with_update_notices(self):
        self.assertNotIn("BuildingProto:GetBuildUpdateRet", CODEC.schemas)
        ids = sorted(int(key) for key in self.rows())
        replies = self.call(building.get_build_update, {"ids": ids})
        self.assertEqual([reply.name for reply in replies], ["BuildingProto:UpdateNotices"])
        fields = self.decoded(replies[0])
        self.assertTrue(fields["is_finish"])
        self.assertEqual([row["id"] for row in fields["builds"]], ids)
        self.assertEqual(self.decoded(self.call(building.get_build_update, {"ids": [999999]})[0]),
                         {"builds": [], "is_finish": True})
        before, revision = deepcopy(self.state()), self.revision()
        self.call(building.get_build_update, {"ids": ids})
        self.assertEqual(before, self.state())
        self.assertEqual(revision, self.revision())

    def test_this_module_owns_exactly_its_six_registered_names(self):
        names = {"BuildingProto:BuildsList", "BuildingProto:BuildsBaseInfo",
                 "BuildingProto:BuildCreate", "BuildingProto:Upgrade",
                 "BuildingProto:BuildSetRole", "BuildingProto:GetBuildUpdate"}
        from handlers import initialization
        for name in names:
            self.assertIn(name, HANDLERS)
            self.assertEqual(HANDLERS[name].__module__, building.__name__)
        self.assertEqual(HANDLERS["BuildingProto:AssualtInfo"].__module__,
                         initialization.__name__)


class BuildingSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=SERVER / "tests", prefix="building-socket-")
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
        prefix = await asyncio.wait_for(reader.readexactly(2), 2)
        size = struct.unpack(">H", prefix)[0]
        body = await asyncio.wait_for(reader.readexactly(size), 2)
        self.assertEqual(body[0], 3)
        return CODEC.decode_frame(body[9:])

    async def send(self, writer, name, fields):
        writer.write(encode_packet(CODEC.encode_frame(name, fields)))
        await writer.drain()

    async def login(self):
        query_reader, query_writer = await self.connect(self.server.query_port)
        await self.send(query_writer, "ClientProto:QueryAccount",
                        {"account": "building-socket", "SvnVersion": "3.3.0", "pwd": "not-persisted"})
        uid = (await self.read_reply(query_reader)).fields["uid"]
        await self.send(query_writer, "ClientProto:PreLoginGame", {"uid": uid, "distinctId": "local"})
        pre = await self.read_reply(query_reader)
        game_reader, game_writer = await self.connect(self.server.game_port)
        await self.send(game_writer, "ClientProto:LoginGame",
                        {"uid": uid, "key": pre.fields["key"], "SvnVersion": "3.3.0"})
        self.assertEqual((await self.read_reply(game_reader)).name, "LoginProto:LoginGame")
        return uid, game_reader, game_writer

    async def test_loading_weight_list_and_build_actions_survive_a_real_socket(self):
        uid, reader, writer = await self.login()
        await self.send(writer, "BuildingProto:BuildsList", {})
        reply = await self.read_reply(reader)
        self.assertEqual(reply.name, "BuildingProto:BuildsListRet")
        self.assertTrue(reply.fields["builds"])  # MatrixView.lua:63 opens the scene now
        self.assertTrue(reply.fields["is_finish"])
        tower_id = sorted(row["id"] for row in reply.fields["builds"])[0]
        await self.send(writer, "BuildingProto:BuildsBaseInfo", {})
        info = await self.read_reply(reader)
        self.assertEqual(info.name, "BuildingProto:BuildsBaseInfoRet")
        self.assertIn(info.fields["runTypeCfgId"], set(config_rows("CfgBGobalPower")))
        await self.send(writer, "BuildingProto:BuildCreate", {"cfgId": 1002, "pos": [0, 0]})
        notice = await self.read_reply(reader)
        self.assertEqual(notice.name, "BuildingProto:AddNotice")
        self.assertTrue(notice.fields["is_finish"])
        created = await self.read_reply(reader)
        self.assertEqual(created.name, "BuildingProto:BuildCreateRet")
        self.assertTrue(created.fields["ok"])
        self.assertEqual(created.fields["cfgId"], 1002)
        await self.send(writer, "BuildingProto:Upgrade", {"id": tower_id})
        self.assertEqual((await self.read_reply(reader)).name, "BuildingProto:AddNotice")
        upgraded = await self.read_reply(reader)
        self.assertEqual(upgraded.name, "BuildingProto:UpgradeRet")
        self.assertEqual(upgraded.fields["id"], tower_id)
        self.assertTrue(upgraded.fields["ok"])
        await self.send(writer, "ClientProto:Heartbeat", {})
        self.assertEqual((await self.read_reply(reader)).name, "LoginProto:Heartbeat")
        events = [line for line in self.events.read_text(encoding="utf-8").splitlines()]
        self.assertFalse([line for line in events if "connection_closed" in line])


if __name__ == "__main__":
    unittest.main()
