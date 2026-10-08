"""Configured item exchange, reserve authority, atomicity and wire shape."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from admin_resources import apply_resource, maximum, resource_key
from database import Store, StorageError
from equip_service import config_record
from handlers import item_exchange
from protocol_codec import IVProtoCodec, WireConfig
from server_core import Context


class ItemExchangeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "players.sqlite3"
        self.store = Store(self.path)
        seed = json.loads((HERE / "data/new_account_seed.json").read_text("utf-8"))
        self.uid = self.store.create_account("item-exchange", seed)["uid"]
        self.ctx = Context(SimpleNamespace(store=self.store), "game", self.uid, True)
        self.codec = IVProtoCodec(
            json.loads((HERE.parent / "05-protocol/endpoints.json").read_text("utf-8")),
            WireConfig("little", max_frame_size=65535),
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def state(self):
        return self.store.get_player(self.uid)

    def set_item(self, cfgid, amount):
        with self.store.transaction(self.uid) as tx:
            apply_resource(tx, "item:" + str(cfgid), "set", amount)

    def item(self, cfgid):
        return int(self.state()["inventory"].get(str(cfgid), 0))

    def wire(self, replies):
        for reply in replies:
            raw = self.codec.encode_frame(reply.name, reply.fields)
            decoded = self.codec.decode_frame(raw)
            self.assertEqual(raw, self.codec.encode_frame(decoded.name, decoded.fields))

    async def rejected_unchanged(self, fields):
        before = self.state()
        with self.assertRaises(StorageError):
            await item_exchange.exchange_item(self.ctx, fields)
        self.assertEqual(self.state(), before)

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.ctx.server.store = self.store

    async def test_all_ordinary_rules_have_exact_cost_reward_persistence_and_no_card_creation(self):
        rules = item_exchange.exchange_rules()
        self.assertEqual([1001, 1002, 1003, 1004, 1005],
                         sorted(key for key, value in rules.items() if value["type"] == 1))
        original_cards = deepcopy(self.state()["cards"])
        for identifier in range(1001, 1006):
            rule = rules[identifier]
            cost_id, cost = rule["costs"][0]
            reward_id, reward = rule["gets"][0]
            self.set_item(cost_id, cost * 3 + 7)
            before_reward = self.item(reward_id)
            replies = await item_exchange.exchange_item(self.ctx, {
                "exchanges": [{"id": identifier, "num": 3, "type": 2}],
                "card_pool_id": 7000 + identifier,
            })
            self.assertEqual(self.item(cost_id), 7)
            self.assertEqual(self.item(reward_id), before_reward + reward * 3)
            self.assertEqual(replies[-1].name, "ClientProto:ExchangeItemRet")
            self.assertEqual(replies[-1].fields, {
                "rewards": [{"id": reward_id, "num": reward * 3, "type": 2}],
                "card_pool_id": 7000 + identifier,
            })
            self.wire(replies)
            self.reopen()
            self.assertEqual(self.item(cost_id), 7)
            self.assertEqual(self.state()["cards"], original_cards)

    async def test_star_source_ty1_reserves_commander_core_and_talent(self):
        reserve = item_exchange.required_reserve(self.state(), 107101)
        self.assertGreater(reserve, 0)
        self.set_item(107101, reserve + 2)
        replies = await item_exchange.exchange_item(self.ctx, {
            "exchanges": [{"id": 107101, "num": 2, "type": 2}], "ty": 1,
        })
        self.assertEqual(self.item(107101), reserve)
        self.assertEqual(replies[-1].fields["ty"], 1)
        self.assertEqual(replies[-1].fields["rewards"], [{"id": 10033, "num": 10, "type": 2}])
        self.wire(replies)
        await self.rejected_unchanged({
            "exchanges": [{"id": 107101, "num": 1, "type": 2}], "ty": 1,
        })

    async def test_star_source_ty2_batches_unowned_fighters_and_aggregates_rewards(self):
        selected = [101001, 101002]
        for identifier in selected:
            self.assertEqual(item_exchange.required_reserve(self.state(), identifier), 0)
            self.set_item(identifier, 3)
        rules = item_exchange.exchange_rules()
        expected = sum(rules[identifier]["gets1"][0][1] * count
                       for identifier, count in zip(selected, (2, 3)))
        replies = await item_exchange.exchange_item(self.ctx, {
            "exchanges": [
                {"id": selected[0], "num": 2, "type": 2},
                {"id": selected[1], "num": 3, "type": 2},
            ],
            "ty": 2,
        })
        self.assertEqual(self.item(selected[0]), 1)
        self.assertEqual(self.item(selected[1]), 0)
        self.assertEqual(replies[-1].fields, {
            "rewards": [{"id": 10053, "num": expected, "type": 2}], "ty": 2,
        })
        self.wire(replies)

    def test_reserve_sums_all_related_owned_fighters_and_zeroes_at_max(self):
        before = item_exchange.required_reserve(self.state(), 107101)
        cfg = config_record("cfgCardData.lua", 71020)
        with self.store.transaction(self.uid) as tx:
            tx.add_card(71020, {"skills": {str(value): {"id": value, "exp": 0}
                                            for value in cfg["skills"]}})
        after = item_exchange.required_reserve(self.state(), 107101)
        self.assertGreater(after, before)

        with self.store.transaction(self.uid) as tx:
            for card in tx.state["cards"]:
                if card["cfgid"] not in {71010, 71020}:
                    continue
                quality = config_record("cfgCardData.lua", card["cfgid"])["quality"]
                card["mix_data"] = {"cl": len(item_exchange.row("cfgCfgCardCoreLv.lua", quality)["infos"])}
                for key, value in list(card["skills"].items()):
                    skill = config_record("cfgskill.lua", value["id"])
                    if skill.get("main_type") != 2:
                        continue
                    while skill.get("next_id"):
                        skill = config_record("cfgskill.lua", skill["next_id"])
                    card["skills"].pop(key)
                    card["skills"][str(skill["id"])] = {"id": skill["id"], "exp": 0}
        self.assertEqual(item_exchange.required_reserve(self.state(), 107101), 0)

    async def test_invalid_shapes_overflow_capacity_and_mixed_batches_roll_back(self):
        self.set_item(10002, 1000)
        self.set_item(10040, 1000)
        self.set_item(101001, 10)
        invalid = [
            {},
            {"exchanges": []},
            {"exchanges": [{"id": 99999999, "num": 1, "type": 2}]},
            {"exchanges": [{"id": 1003, "num": 1, "type": 2},
                            {"id": 1003, "num": 1, "type": 2}]},
            {"exchanges": [{"id": 1003, "num": 1, "type": 2},
                            {"id": 101001, "num": 1, "type": 2}], "ty": 1},
            {"exchanges": [{"id": 101001, "num": 1, "type": 2}], "ty": 3},
            {"exchanges": [{"id": 1003, "num": 1, "type": 9}]},
            {"exchanges": [{"id": 1003, "num": 1, "type": 2, "c_id": 1}]},
            {"exchanges": [{"id": 1001, "num": 2147483647, "type": 2}]},
            {"exchanges": [{"id": 1001, "num": 1, "type": 2}], "unexpected": 1},
        ]
        for fields in invalid:
            await self.rejected_unchanged(fields)

        self.set_item(11002, maximum(self.state(), *resource_key("item:11002")))
        await self.rejected_unchanged({
            "exchanges": [{"id": 1001, "num": 1, "type": 2}],
        })
        self.set_item(10002, 0)
        await self.rejected_unchanged({
            "exchanges": [{"id": 1003, "num": 1, "type": 2}],
        })


if __name__ == "__main__":
    unittest.main()
