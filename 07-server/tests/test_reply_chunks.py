"""Byte-budget chunking keeps the four large player snapshots encodable."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "02-tools" / "scripts"))
from database import StorageError
from protocol_codec import CodecError, IVProtoCodec, WireConfig
import reply_chunks

CODEC = IVProtoCodec(json.loads((HERE.parent / "05-protocol" / "endpoints.json").read_text("utf-8")),
                     WireConfig("little"))
LIMIT = CODEC.config.max_frame_size
SEED = json.loads((HERE / "data" / "new_account_seed.json").read_text("utf-8"))


def cards(count, *, name_length=0, equips=0):
    rows = []
    for index in range(count):
        card = deepcopy(SEED["cards"][0])
        card["cid"] = index + 1
        if name_length:
            card["name"] = "x" * name_length
        if equips:
            card["equips"] = [{"cfgid": 20001, "sid": 800000000 + index * equips + slot, "level": 1,
                               "exp": 0, "lock": 0, "rand_skill_type": 0, "rand_skill_value": 0,
                               "card_id": index + 1, "is_new": 0, "num": 1, "skills": []}
                              for slot in range(equips)]
        rows.append(card)
    return rows


def roles(count):
    rows = []
    for index in range(count):
        role = deepcopy(SEED["card_roles"][0])
        role["id"] = 500000 + index
        rows.append(role)
    return rows


def items(count):
    return [{"id": 10000 + index, "num": 1, "time": 0, "ix": 0, "expiry": 0, "get_infos": {}}
            for index in range(count)]


def encoded_size(reply):
    return len(CODEC.encode_frame(reply.name, reply.fields))


class ChunkFrameTests(unittest.TestCase):
    def assert_frames_fit(self, frames):
        for frame in frames:
            self.assertLessEqual(encoded_size(frame), LIMIT, frame.name)

    def test_card_add_single_frame_matches_todays_push(self):
        rows = cards(3)
        frames = reply_chunks.card_add(CODEC, rows, len(rows), 150)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].name, "PlayerProto:CardAdd")
        self.assertEqual(frames[0].fields["cards"], rows)
        self.assertEqual(frames[0].fields["cur_size"], 3)
        self.assertEqual(frames[0].fields["max_size"], 150)
        self.assertIs(frames[0].fields["finish"], True)

    def test_211_card_snapshot_splits_instead_of_failing(self):
        rows = cards(211)
        with self.assertRaises(CodecError):
            CODEC.encode_frame("PlayerProto:CardAdd",
                               {"cards": rows, "cur_size": 211, "max_size": 150, "finish": True})
        frames = reply_chunks.card_add(CODEC, rows, 211, 150)
        self.assertGreater(len(frames), 1)
        self.assert_frames_fit(frames)
        self.assertEqual(sum(len(frame.fields["cards"]) for frame in frames), 211)
        self.assertEqual([card["cid"] for frame in frames for card in frame.fields["cards"]],
                         [card["cid"] for card in rows])
        self.assertEqual([frame.fields["finish"] for frame in frames],
                         [False] * (len(frames) - 1) + [True])
        for frame in frames:
            self.assertEqual((frame.fields["cur_size"], frame.fields["max_size"]), (211, 150))

    def test_211_card_archive_round_trips_and_stays_encodable(self):
        import tempfile
        from database import Store
        seed = json.loads((HERE / "data" / "new_account_seed.json").read_text("utf-8"))
        seed["cards"] = cards(211)
        with tempfile.TemporaryDirectory(prefix="crosscore-chunk-") as directory:
            store = Store(Path(directory) / "state.sqlite3")
            try:
                uid = store.create_account("chunk-account", seed)["uid"]
                state = store.get_player(uid)
                self.assertEqual(len(state["cards"]), 211)
                frames = reply_chunks.card_add(CODEC, state["cards"], len(state["cards"]),
                                               state["max_card_size"])
                self.assertGreater(len(frames), 1)
                self.assert_frames_fit(frames)
                self.assertEqual(sum(len(frame.fields["cards"]) for frame in frames), 211)
            finally:
                store.close()

    def test_equipped_cards_split_well_below_the_grid_cap(self):
        rows = cards(120, equips=5)
        frames = reply_chunks.card_add(CODEC, rows, 120, 150)
        self.assertGreater(len(frames), 1)
        self.assert_frames_fit(frames)
        self.assertEqual(sum(len(frame.fields["cards"]) for frame in frames), 120)

    def test_single_oversized_row_raises_storage_error(self):
        with self.assertRaises(StorageError):
            reply_chunks.card_add(CODEC, cards(1, name_length=40000), 1, 150)

    def test_add_card_role_has_no_completion_field(self):
        rows = roles(600)
        frames = reply_chunks.add_card_role(CODEC, rows)
        self.assertGreater(len(frames), 1)
        self.assert_frames_fit(frames)
        for frame in frames:
            self.assertNotIn("finish", frame.fields)
            self.assertNotIn("is_finish", frame.fields)
        self.assertEqual([role["id"] for frame in frames for role in frame.fields["roles"]],
                         [role["id"] for role in rows])

    def test_update_card_role_finishes_only_on_the_last_frame(self):
        rows = roles(600)
        frames = reply_chunks.update_card_role(CODEC, rows)
        self.assertGreater(len(frames), 1)
        self.assert_frames_fit(frames)
        self.assertEqual([frame.fields["is_finish"] for frame in frames],
                         [False] * (len(frames) - 1) + [True])

    def test_item_bag_single_frame_keeps_ix_zero(self):
        rows = items(3)
        frames = reply_chunks.item_bag(CODEC, rows)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].fields["ix"], 0)
        self.assertIs(frames[0].fields["is_finish"], True)

    def test_item_bag_split_starts_at_ix_one_and_increments(self):
        rows = items(1500)
        frames = reply_chunks.item_bag(CODEC, rows)
        self.assertGreater(len(frames), 1)
        self.assert_frames_fit(frames)
        self.assertEqual([frame.fields["ix"] for frame in frames], list(range(1, len(frames) + 1)))
        self.assertEqual([frame.fields["is_finish"] for frame in frames],
                         [False] * (len(frames) - 1) + [True])
        self.assertEqual(sum(len(frame.fields["item"]) for frame in frames), 1500)

    def test_empty_snapshots_still_reach_the_client(self):
        self.assertEqual(reply_chunks.card_add(CODEC, [], 0, 150)[0].fields,
                         {"cards": [], "cur_size": 0, "max_size": 150, "finish": True})
        self.assertEqual(reply_chunks.add_card_role(CODEC, [])[0].fields, {"roles": []})
        self.assertEqual(reply_chunks.update_card_role(CODEC, [])[0].fields,
                         {"roles": [], "is_finish": True})
        self.assertEqual(reply_chunks.item_bag(CODEC, [])[0].fields,
                         {"item": [], "is_finish": True, "ix": 0})


if __name__ == "__main__":
    unittest.main()
