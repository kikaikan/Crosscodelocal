"""Actual configured awards, replay safety, reset boundaries and rollback."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from database import Store, StorageError
from server_core import Context
from handlers.sign_in import add_sign
from handlers.initialization import sign_info
from protocol_codec import IVProtoCodec, WireConfig, JSONValue


def stamp(value):
    return int(datetime.fromisoformat(value).timestamp())


class SignInTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "players.sqlite3"
        self.store = Store(self.path)
        self.seed = json.loads((HERE / "data" / "new_account_seed.json").read_text("utf-8"))
        self.uid = self.store.create_account("new-local-calendar-test", self.seed)["uid"]
        self.ctx = Context(SimpleNamespace(store=self.store), "game", self.uid, True)
        self.clock("2026-10-01T04:00:00+08:00")
        self.codec = IVProtoCodec(json.loads((HERE.parent / "05-protocol" / "endpoints.json").read_text("utf-8")),
                                  WireConfig("little", max_frame_size=65535))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def clock(self, value, unlocked=True):
        with self.store.transaction(self.uid) as tx:
            tx.state["offline_clock"] = stamp(value)
            tx.state["progress"]["cleared_stages"] = [1007] if unlocked else []

    def wire(self, replies):
        for reply in replies:
            raw = self.codec.encode_frame(reply.name, reply.fields)
            frame = self.codec.decode_frame(raw)
            self.assertEqual(raw, self.codec.encode_frame(frame.name, frame.fields))

    async def test_configured_gold_award_and_retry_after_sqlite_reopen(self):
        before = self.store.get_player(self.uid)
        replies = await add_sign(self.ctx, {"id": 2026})
        self.wire(replies)
        after = self.store.get_player(self.uid)
        # CfgSignReward[2026].infos[10] -> CfgSignRewardItem[1001] day1.
        self.assertEqual(after["player"]["gold"], before["player"]["gold"] + 20000)
        self.assertEqual(after["inventory"]["10001"], after["player"]["gold"])
        self.assertEqual(after["signs"]["2026_10"]["indexs"], {"1": True})
        self.assertEqual(len(after["sign_claims"]), 1)
        self.store.close()
        self.store = Store(self.path)
        self.ctx.server.store = self.store
        retry = await add_sign(self.ctx, {"id": 2026, "index": 10})
        self.wire(retry)
        self.assertEqual([reply.name for reply in retry], ["ClientProto:AddSignRet"])
        self.assertTrue(retry[0].fields["isOk"])
        self.assertEqual(self.store.get_player(self.uid), after)

    async def test_new_day_at_three_grants_store_experience_once(self):
        await add_sign(self.ctx, {"id": 2026})
        self.clock("2026-10-02T02:59:59+08:00")
        before = self.store.get_player(self.uid)
        repeat = await add_sign(self.ctx, {"id": 2026})
        self.assertEqual(len(repeat), 1)
        self.assertEqual(self.store.get_player(self.uid), before)
        self.clock("2026-10-02T03:00:00+08:00")
        before = self.store.get_player(self.uid)
        replies = await add_sign(self.ctx, {"id": 2026})
        self.wire(replies)
        after = self.store.get_player(self.uid)
        self.assertEqual(after["store_exp"], before["store_exp"] + 20000)
        update = next(reply for reply in replies if reply.name == "PlayerProto:CardUpdate")
        self.assertEqual(update.fields["store_exp"], after["store_exp"])
        self.assertEqual(replies[-1].fields["subIndex"], 2)

    async def test_invalid_calendar_paid_continuous_and_locked_do_not_award(self):
        before = self.store.get_player(self.uid)
        for fields in ({"id": 5001}, {"id": 2003}, {"id": 2026, "index": 9}, {"id": 999999}):
            replies = await add_sign(self.ctx, fields)
            self.wire(replies)
            self.assertFalse(replies[-1].fields["isOk"])
            self.assertEqual(self.store.get_player(self.uid), before)
        self.clock("2026-10-01T04:00:00+08:00", unlocked=False)
        before = self.store.get_player(self.uid)
        self.assertFalse((await add_sign(self.ctx, {"id": 2026}))[-1].fields["isOk"])
        self.assertEqual(self.store.get_player(self.uid), before)
        self.clock("2030-10-01T04:00:00+08:00")
        before = self.store.get_player(self.uid)
        self.assertFalse((await add_sign(self.ctx, {"id": 2026}))[-1].fields["isOk"])
        self.assertEqual(self.store.get_player(self.uid), before)

    async def test_overflow_rolls_back_award_and_receipt_together(self):
        with self.store.transaction(self.uid) as tx:
            tx.state["player"]["gold"] = 2147483647
            tx.state["inventory"]["10001"] = 2147483647
        before = self.store.get_player(self.uid)
        with self.assertRaises(StorageError):
            await add_sign(self.ctx, {"id": 2026})
        self.assertEqual(self.store.get_player(self.uid), before)
        self.assertNotIn("signs", before)

    async def test_sign_read_preserves_numeric_lua_keys_and_state(self):
        await add_sign(self.ctx, {"id": 2026})
        state = self.store.get_player(self.uid)
        before = deepcopy(state)
        replies = sign_info(state, {"id": 2026}, state["offline_clock"])
        self.wire(replies)
        self.assertEqual(replies[0].fields["rewardsInfos"]["indexs"], {1: True})
        raw = self.codec.encode_frame(replies[0].name, replies[0].fields)
        value = self.codec.decode_frame(raw).fields["rewardsInfos"]["indexs"]
        self.assertIsInstance(value, JSONValue)
        self.assertIn("[1]=true", value.text)
        self.assertEqual(state, before)

    async def test_no_login_or_boolean_identifier_rejected(self):
        self.ctx.logged_in = False
        with self.assertRaises(StorageError):
            await add_sign(self.ctx, {"id": 2026})
        self.ctx.logged_in = True
        with self.assertRaises(StorageError):
            await add_sign(self.ctx, {"id": True})


if __name__ == "__main__":
    unittest.main()
