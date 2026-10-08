"""Dorm room reads, the self/friend fid contract and persisted layout edits.

The wire expectations mirror the decoded client:
  * DormMgr.lua:860 `if (fid)` -- a self GetOpenDormRet must not carry fid at all.
  * DormMgr.lua:874 `fid ~= nil` -- the same for GetDormRet.
  * DormRoom.lua:122-153 holds its mask until GetDormRet arrives for a cfg room.
"""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
# server_core puts 02-tools/scripts on sys.path; import it before protocol_codec.
from database import Store, StorageError
from server_core import Context, HANDLERS
from protocol_codec import IVProtoCodec, WireConfig, readable
from handlers import dorm


SELF_ROOM = 101          # CfgDorm id=1 x infos[1].index=1 -> GCalculatorHelp.lua:1311
SECOND_FLOOR_ROOM = 201  # CfgDorm id=2 x infos[1].index=1, onlyShow=true
FRIEND = 1234


class DormTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="crosscore-dorm-")
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path)
        seed = json.loads((HERE / "data" / "new_account_seed.json").read_text("utf-8"))
        self.uid = self.store.create_account("new-local-dorm-account", seed)["uid"]
        self.codec = IVProtoCodec(json.loads((HERE.parent / "05-protocol" / "endpoints.json").read_text("utf-8")),
                                  WireConfig("little", max_frame_size=65535))
        self.ctx = Context(SimpleNamespace(store=self.store, codec=self.codec), "game", self.uid, True)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def state(self):
        return self.store.get_player(self.uid)

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.ctx.server.store = self.store

    def wire(self, replies):
        """True codec round trip: encode -> decode -> encode must be byte exact."""
        decoded = []
        for reply in replies:
            raw = self.codec.encode_frame(reply.name, reply.fields)
            frame = self.codec.decode_frame(raw)
            self.assertEqual(raw, self.codec.encode_frame(frame.name, frame.fields))
            self.assertLess(len(raw), 32767)
            decoded.append(frame)
        return decoded

    async def call(self, handler, name, fields):
        """Drive a handler with fields that survived a real client encode/decode."""
        frame = self.codec.decode_frame(self.codec.encode_frame(name, fields))
        for key, value in fields.items():
            if value is None:
                self.assertNotIn(key, frame.fields)
            else:
                self.assertEqual(frame.fields.get(key), value)
        return await handler(self.ctx, readable(frame.fields))

    async def test_self_open_list_omits_fid_and_round_trips(self):
        result = await self.call(dorm.get_open_dorm, "DormProto:GetOpenDorm", {"fid": None})
        self.assertEqual([reply.name for reply in result], ["DormProto:GetOpenDormRet"])
        fields = result[0].fields
        self.assertNotIn("fid", fields)
        self.assertNotIn("fid", json.dumps(fields, ensure_ascii=False))
        self.assertEqual(fields["infos"], [{"id": SELF_ROOM, "num": 0, "roleIds": [],
                                          "lv": 1, "comfort": 0}])
        decoded, = self.wire(result)
        self.assertNotIn("fid", decoded.fields)
        self.assertEqual([index for index, _ in decoded.fields.wire], [1])

    async def test_zero_fid_is_the_same_self_request(self):
        for value in (0, None):
            result = await self.call(dorm.get_open_dorm, "DormProto:GetOpenDorm", {"fid": value})
            self.assertNotIn("fid", result[0].fields)
            # Lua reads fid=0 as truthy (DormMgr.lua:860), so the key must be absent.
            encoded = self.codec.encode_frame(result[0].name, result[0].fields)
            self.assertNotIn("fid", self.codec.decode_frame(encoded).fields)

    async def test_friend_reads_echo_fid_without_reusing_local_layout(self):
        furniture = {2037: {"id": 2037, "planeType": 1, "rotateY": 0, "parentID": 0}}
        await self.call(dorm.mod_furniture, "DormProto:ModFurniture",
                        {"id": SELF_ROOM, "furnitures": furniture, "img": "own.png"})
        await self.call(dorm.open_room, "DormProto:Open", {"id": SECOND_FLOOR_ROOM})

        result = await self.call(dorm.get_open_dorm, "DormProto:GetOpenDorm", {"fid": FRIEND})
        self.assertEqual(result[0].fields["fid"], FRIEND)
        # The config default set, not the locally opened 201 or the own layout.
        self.assertEqual([row["id"] for row in result[0].fields["infos"]], [SELF_ROOM])
        self.assertNotIn("img", result[0].fields["infos"][0])
        decoded, = self.wire(result)
        self.assertEqual(decoded.fields["fid"], FRIEND)

        detail = await self.call(dorm.get_dorm, "DormProto:GetDorm", {"fid": FRIEND, "id": SELF_ROOM})
        self.assertEqual(detail[0].fields["fid"], FRIEND)
        self.assertEqual(detail[0].fields["info"]["data"]["furnitures"], {})
        self.assertNotIn("img", detail[0].fields["info"]["data"])
        self.wire(detail)

        own = await self.call(dorm.get_dorm, "DormProto:GetDorm", {"id": SELF_ROOM})
        self.assertNotIn("fid", own[0].fields)
        self.assertEqual(own[0].fields["info"]["data"]["img"], "own.png")
        self.assertIn(2037, own[0].fields["info"]["data"]["furnitures"])
        self.wire(own)

    async def test_get_dorm_answers_for_self_and_friend(self):
        result = await self.call(dorm.get_dorm, "DormProto:GetDorm", {"fid": None, "id": SELF_ROOM})
        self.assertEqual([reply.name for reply in result], ["DormProto:GetDormRet"])
        self.assertNotIn("fid", result[0].fields)
        info = result[0].fields["info"]
        self.assertEqual(info["id"], SELF_ROOM)
        self.assertEqual(info["lv"], 1)
        self.assertEqual(info["num"], 0)
        self.assertEqual(info["data"]["roleIds"], [])
        self.assertEqual(info["data"]["furnitures"], {})
        self.assertEqual(info["data"]["petIds"], [])
        self.assertNotIn("img", info["data"])
        decoded, = self.wire(result)
        self.assertNotIn("fid", decoded.fields)
        self.assertEqual([index for index, _ in decoded.fields.wire], [1])

        # A config-valid room answers even while still locked: DormRoom.lua:150
        # would otherwise hold its mask forever waiting for this callback.
        locked = await self.call(dorm.get_dorm, "DormProto:GetDorm", {"id": SECOND_FLOOR_ROOM})
        self.assertEqual(locked[0].fields["info"]["id"], SECOND_FLOOR_ROOM)
        self.wire(locked)

        friend = await self.call(dorm.get_dorm, "DormProto:GetDorm", {"fid": FRIEND, "id": SELF_ROOM})
        self.assertEqual(friend[0].fields["fid"], FRIEND)
        self.assertEqual(friend[0].fields["info"]["id"], SELF_ROOM)
        self.wire(friend)

    async def test_open_persists_and_replays_after_reopen(self):
        result = await self.call(dorm.open_room, "DormProto:Open", {"id": SECOND_FLOOR_ROOM})
        self.assertEqual([reply.name for reply in result], ["DormProto:OpenRet", "DormProto:Update"])
        self.assertEqual(result[0].fields, {"id": SECOND_FLOOR_ROOM})
        self.assertEqual(result[1].fields["infos"][0]["id"], SECOND_FLOOR_ROOM)
        self.wire(result)

        self.assertTrue(self.state()["dorms"]["opened"]["201"])
        self.reopen()
        rows = (await self.call(dorm.get_open_dorm, "DormProto:GetOpenDorm", {}))[0].fields["infos"]
        self.assertEqual(sorted(row["id"] for row in rows), [SELF_ROOM, SECOND_FLOOR_ROOM])
        self.assertTrue(all(row["lv"] == 1 for row in rows))

        opened_state = self.state()
        again = await self.call(dorm.open_room, "DormProto:Open", {"id": SECOND_FLOOR_ROOM})
        self.wire(again)
        self.assertEqual(self.state(), opened_state)

    async def test_open_of_an_already_open_room_does_not_rewrite_the_save(self):
        before = self.state()
        self.assertNotIn("dorms", before)
        result = await self.call(dorm.open_room, "DormProto:Open", {"id": SELF_ROOM})
        self.wire(result)
        self.assertNotIn("dorms", self.state())
        self.assertEqual(self.state(), before)

    async def test_mod_furniture_persists_the_layout(self):
        furniture = {2037: {"id": 2037, "point": {"x": 1.5, "y": 0.0, "z": -2.25},
                            "planeType": 2, "rotateY": 90, "parentID": 0}}
        result = await self.call(dorm.mod_furniture, "DormProto:ModFurniture",
                                 {"id": SELF_ROOM, "furnitures": furniture, "img": "dorm_101.png"})
        self.assertEqual([reply.name for reply in result], ["DormProto:ModFurnitureRet"])
        self.assertEqual(result[0].fields, {"id": SELF_ROOM})
        self.wire(result)

        room = self.state()["dorms"]["rooms"]["101"]
        self.assertEqual(room["lv"], 1)
        self.assertEqual(room["img"], "dorm_101.png")
        self.assertEqual(room["furnitures"]["2037"]["id"], 2037)
        self.assertEqual(room["furnitures"]["2037"]["rotateY"], 90)

        self.reopen()
        replies = await self.call(dorm.get_dorm, "DormProto:GetDorm", {"id": SELF_ROOM})
        info = replies[0].fields["info"]
        self.assertEqual(info["data"]["img"], "dorm_101.png")
        # Read-path sanitizing re-keys the map by the sFurniture id field.
        self.assertEqual(info["data"]["furnitures"][2037]["point"]["x"], 1.5)
        self.assertEqual(info["data"]["furnitures"][2037]["planeType"], 2)
        self.wire(replies)

    async def test_rejections_keep_the_save_unchanged(self):
        before = self.state()
        furniture = {2037: {"id": 2037, "planeType": 1, "rotateY": 0, "parentID": 0}}
        wire_rejections = [
            (dorm.mod_furniture, "DormProto:ModFurniture",
             {"id": SECOND_FLOOR_ROOM, "furnitures": furniture, "img": ""}, "locked room"),
            (dorm.get_dorm, "DormProto:GetDorm", {"id": 999}, "unknown room"),
            (dorm.get_dorm, "DormProto:GetDorm", {"id": 0}, "zero room"),
            (dorm.get_dorm, "DormProto:GetDorm", {}, "missing room"),
            (dorm.use_gift, "DormProto:UseGift",
             {"roleId": 1, "items": [{"id": 10001, "num": 1, "type": 2}]}, "use gift"),
            (dorm.buy_furniture, "DormProto:BuyFurniture",
             {"infos": [{"id": 2037, "num": 1}], "useCost": "price_1"}, "buy furniture"),
        ]
        for handler, name, fields, label in wire_rejections:
            with self.subTest(label):
                with self.assertRaises(StorageError):
                    await self.call(handler, name, fields)
                self.assertEqual(self.state(), before)
        # Fields the codec can never deliver are still refused, not persisted raw.
        for label, fields in [
            ("list instead of map", {"id": SELF_ROOM, "furnitures": [2037], "img": ""}),
            ("key and id differ", {"id": SELF_ROOM, "furnitures": {2037: {"id": 2038}}, "img": ""}),
            ("point not a table", {"id": SELF_ROOM, "furnitures": {2037: {"id": 2037, "point": 5}}, "img": ""}),
            ("image not a string", {"id": SELF_ROOM, "furnitures": {}, "img": 7}),
            ("unencodable childID", {"id": SELF_ROOM, "furnitures": {2037: {"id": 2037, "childID": [1]}}, "img": ""}),
            ("unknown furniture field", {"id": SELF_ROOM, "furnitures": {2037: {"id": 2037, "color": 1}}, "img": ""}),
        ]:
            with self.subTest(label):
                with self.assertRaises(StorageError):
                    await dorm.mod_furniture(self.ctx, fields)
                self.assertEqual(self.state(), before)
        opened = await self.call(dorm.get_open_dorm, "DormProto:GetOpenDorm", {})
        self.assertEqual([row["id"] for row in opened[0].fields["infos"]], [SELF_ROOM])
        self.assertNotIn("dorms", self.state())

    async def test_registered_names_are_exactly_the_dorm_contract(self):
        for name in ("DormProto:GetOpenDorm", "DormProto:GetDorm", "DormProto:Open",
                     "DormProto:ModFurniture", "DormProto:UseGift", "DormProto:BuyFurniture"):
            self.assertIn(name, HANDLERS)

    async def test_login_required(self):
        anon = Context(SimpleNamespace(store=self.store, codec=self.codec), "game", None, False)
        for handler in (dorm.get_open_dorm, dorm.get_dorm, dorm.open_room, dorm.mod_furniture):
            with self.assertRaises(StorageError):
                await handler(anon, {})


if __name__ == "__main__":
    unittest.main()
