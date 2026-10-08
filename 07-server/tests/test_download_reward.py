"""Isolated download-reward storage and recovered wire-protocol regressions."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from database import Store, StorageError
from server_core import Context, load_dependencies
from handlers import download_reward
from protocol_codec import CodecError

CODEC, SEED = load_dependencies(SERVER.parent / "05-protocol/endpoints.json",
                                SERVER / "data/new_account_seed.json")


class DownloadRewardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SERVER / "tests", prefix="download-reward-")
        self.path = Path(self.temp.name) / "players.sqlite3"
        self.store = Store(self.path)
        self.uid = self.store.create_account("isolated-download-test", SEED)["uid"]
        self.ctx = Context(SimpleNamespace(store=self.store, codec=CODEC), "game", self.uid, True)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def revision(self):
        return self.store.connection.execute("SELECT revision FROM accounts WHERE uid=?",
                                             (self.uid,)).fetchone()[0]

    def wire(self, replies):
        for reply in replies:
            raw = CODEC.encode_frame(reply.name, reply.fields)
            frame = CODEC.decode_frame(raw)
            self.assertEqual(frame.name, reply.name)
            self.assertEqual(raw, CODEC.encode_frame(frame.name, frame.fields))

    def response(self, replies):
        return next(reply.fields["result"] for reply in replies
                    if reply.name == "DownloadProto:GetDownloadRewardRet")

    async def test_source_reward_and_recovered_empty_request_bodies(self):
        self.assertEqual(download_reward.reward_rows(), [{"id": 11002, "num": 2, "type": 2}])
        for name in ("DownloadProto:CheckDownloadReward", "DownloadProto:GetDownloadReward"):
            raw = CODEC.encode_frame(name, {})
            self.assertEqual(CODEC.decode_frame(raw).fields, {})
            self.assertEqual(len(raw), 5)  # recovered inner header, no fields
        captured = json.loads((SERVER.parent / "05-protocol/samples/tcp/"
                    "session1-stream02-s2c-frame0012.decoded.json").read_text("utf-8"))
        self.assertEqual(captured["name"], "DownloadProto:CheckDownloadRewardRet")
        self.assertEqual(captured["fields"], {"isGet": True})
        self.wire(download_reward.initial_pushes(self.store.get_player(self.uid)))

    async def test_new_account_query_does_not_grant_or_write_history(self):
        before, revision = self.store.get_player(self.uid), self.revision()
        replies = await download_reward.check_download_reward(self.ctx, {})
        self.assertEqual(replies[0].fields, {"isGet": False})
        self.wire(replies)
        self.assertEqual(self.store.get_player(self.uid), before)
        self.assertEqual(self.revision(), revision)

    async def test_claim_updates_real_inventory_and_opens_source_reward(self):
        before = self.store.get_player(self.uid)
        replies = await download_reward.get_download_reward(self.ctx, {})
        self.assertTrue(self.response(replies))
        self.wire(replies)
        state = self.store.get_player(self.uid)
        self.assertEqual(state["inventory"]["11002"], before["inventory"].get("11002", 0) + 2)
        self.assertEqual(state["cards"], before["cards"])
        self.assertEqual(state["player"], before["player"])
        self.assertEqual(state["progress"], before["progress"])
        self.assertEqual(state["download_reward"]["rewards"], [{"id": 11002, "num": 2, "type": 2}])
        item = next(reply.fields["data"][0] for reply in replies if reply.name == "PlayerProto:ItemUpdate")
        self.assertEqual((item["id"], item["num"]), (11002, state["inventory"]["11002"]))
        self.assertEqual(replies[-1].name, "ClientProto:RewardNotice")
        self.assertEqual(replies[-1].fields, {"rewards": [{"id": 11002, "num": 2, "type": 2}], "is_finish": True})

    async def test_repeat_and_reopen_preserve_once_only_receipt_revision(self):
        await download_reward.get_download_reward(self.ctx, {})
        state, revision = self.store.get_player(self.uid), self.revision()
        self.store.close()
        self.store = Store(self.path)
        self.ctx.server.store = self.store
        for _ in range(3):
            replies = await download_reward.get_download_reward(self.ctx, {})
            self.assertEqual(len(replies), 1)
            self.assertTrue(self.response(replies))
            self.wire(replies)
            self.assertEqual(self.store.get_player(self.uid), state)
            self.assertEqual(self.revision(), revision)
        queried = await download_reward.check_download_reward(self.ctx, {})
        self.assertEqual(queried[0].fields, {"isGet": True})

    async def test_authentication_role_and_empty_fields_are_required(self):
        before = self.store.get_player(self.uid)
        for role, uid, logged_in, fields in (("game", None, False, {}),
                ("query", self.uid, True, {}), ("game", self.uid, True, {"id": self.uid + 1}),
                ("game", self.uid, True, {"complete": True})):
            ctx = Context(self.ctx.server, role, uid, logged_in)
            for handler in (download_reward.check_download_reward, download_reward.get_download_reward):
                with self.assertRaises(StorageError):
                    await handler(ctx, fields)
            self.assertEqual(self.store.get_player(self.uid), before)

    async def test_inventory_overflow_fails_without_claiming_or_changing_revision(self):
        with self.store.transaction(self.uid) as tx:
            tx.state["inventory"]["11002"] = 2147483646
        before, revision = self.store.get_player(self.uid), self.revision()
        replies = await download_reward.get_download_reward(self.ctx, {})
        self.assertFalse(self.response(replies))
        self.wire(replies)
        self.assertEqual(self.store.get_player(self.uid), before)
        self.assertEqual(self.revision(), revision)

    async def test_partial_award_and_encoding_failures_roll_back_receipt_and_assets(self):
        before, revision = self.store.get_player(self.uid), self.revision()
        with patch.object(download_reward, "reward_rows", return_value=[
                {"id": 11002, "num": 2, "type": 2}, {"id": 999999999, "num": 1, "type": 2}]):
            replies = await download_reward.get_download_reward(self.ctx, {})
            self.assertFalse(self.response(replies))
        self.assertEqual(self.store.get_player(self.uid), before)
        self.assertEqual(self.revision(), revision)
        def fail_reward(name, fields):
            if name == "ClientProto:RewardNotice":
                raise CodecError("isolated forced reward encoding failure")
            return CODEC.encode_frame(name, fields)
        self.ctx.server.codec = SimpleNamespace(encode_frame=fail_reward)
        with self.assertRaises(CodecError):
            await download_reward.get_download_reward(self.ctx, {})
        self.assertEqual(self.store.get_player(self.uid), before)
        self.assertEqual(self.revision(), revision)

    async def test_existing_local_claim_is_not_overwritten_by_current_configuration(self):
        receipt = {"version": 1, "claimed": True, "claimed_at": 123,
                   "source_key": "g_DownloadReward", "rewards": [{"id": 11002, "num": 2, "type": 2}]}
        with self.store.transaction(self.uid) as tx:
            tx.state["download_reward"] = deepcopy(receipt)
        before, revision = self.store.get_player(self.uid), self.revision()
        with patch.object(download_reward, "reward_rows", side_effect=StorageError("changed source")):
            self.assertTrue(self.response(await download_reward.get_download_reward(self.ctx, {})))
        self.assertEqual(self.store.get_player(self.uid), before)
        self.assertEqual(self.revision(), revision)

    async def test_two_local_connections_cannot_award_twice(self):
        other_store = Store(self.path)
        try:
            other = Context(SimpleNamespace(store=other_store, codec=CODEC), "game", self.uid, True)
            before = self.store.get_player(self.uid)["inventory"]["11002"]
            replies = await asyncio.gather(download_reward.get_download_reward(self.ctx, {}),
                                            download_reward.get_download_reward(other, {}))
            self.assertTrue(all(self.response(value) for value in replies))
            self.assertEqual(sum(any(reply.name == "ClientProto:RewardNotice" for reply in value)
                                 for value in replies), 1)
            self.assertEqual(self.store.get_player(self.uid)["inventory"]["11002"], before + 2)
        finally:
            other_store.close()


if __name__ == "__main__":
    unittest.main()
