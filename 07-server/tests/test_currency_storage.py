"""Persistent authority/mirror consistency and atomic resource costs."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from database import PlayerTxn, Store, StorageError


class CurrencyStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="crosscore-currency-")
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.seed = json.loads((HERE / "data" / "new_account_seed.json").read_text("utf-8"))
        self.uid = self.store.create_account("new-currency-account", self.seed)["uid"]

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_source_player_and_login_balances_are_available_in_item_bag(self):
        state = self.store.get_player(self.uid)
        self.assertEqual(state["inventory"]["10010"], state["player"]["army_coin"])
        self.assertEqual(state["inventory"]["10020"], self.seed["login"]["ability_num"])
        self.assertEqual(state["inventory"]["10040"], state["login"]["BIND_DIAMOND"])
        self.assertEqual(state["login"]["ability_num"], 50)
        self.assertNotIn("10035", state["inventory"])
        self.assertEqual(self.store.connection.execute("SELECT revision FROM accounts WHERE uid=?", (self.uid,)).fetchone()[0], 0)

    def test_item_awards_and_currency_costs_share_persistent_authority(self):
        with self.store.transaction(self.uid) as tx:
            self.assertEqual(tx.add_item(10010, 70), 70)
            self.assertEqual(tx.currency("army_coin"), 70)
            self.assertEqual(tx.add_currency("army_coin", -20), 50)
            self.assertEqual(tx.add_item(10020, -15), 35)
            self.assertEqual(tx.currency("ability_num"), 35)
            self.assertEqual(tx.add_item(10040, 8), 8)
            self.assertEqual(tx.add_currency("BIND_DIAMOND", -3), 5)
            self.assertEqual(tx.item_count(10040), 5)
        self.store.close()
        self.store = Store(self.path)
        state = self.store.get_player(self.uid)
        self.assertEqual((state["player"]["army_coin"], state["inventory"]["10010"]), (50, 50))
        self.assertEqual((state["login"]["ability_num"], state["inventory"]["10020"]), (35, 35))
        self.assertEqual((state["login"]["BIND_DIAMOND"], state["inventory"]["10040"]), (5, 5))

    def test_insufficient_later_cost_rolls_back_all_prior_assets(self):
        before = self.store.get_player(self.uid)
        for item in (10010, 10020, 10040):
            with self.subTest(item=item), self.assertRaises(StorageError):
                with self.store.transaction(self.uid) as tx:
                    tx.add_item(10001, -100)
                    tx.add_item(10040, 1)
                    tx.add_item(item, -100000)
            self.assertEqual(self.store.get_player(self.uid), before)

    def test_currency_overflow_rolls_back_and_does_not_wrap(self):
        before = self.store.get_player(self.uid)
        with self.assertRaises(StorageError):
            with self.store.transaction(self.uid) as tx:
                tx.add_item(10002, -1)
                tx.add_currency("ability_num", 2147483647)
        self.assertEqual(self.store.get_player(self.uid), before)

    def test_existing_authority_wins_and_missing_field_uses_local_inventory(self):
        state = deepcopy(self.seed)
        state["player"]["army_coin"] = 11
        state["inventory"].update({"10010": 99, "10040": 7})
        state["login"].pop("BIND_DIAMOND", None)
        tx = PlayerTxn(state)
        self.assertEqual(tx.item_count(10010), 11)
        self.assertEqual(state["inventory"]["10010"], 11)
        self.assertEqual(tx.currency("BIND_DIAMOND"), 7)
        self.assertEqual(state["login"]["BIND_DIAMOND"], 7)
        with self.assertRaises(StorageError):
            tx.add_currency("unconfigured_coin", 1)


if __name__ == "__main__":
    unittest.main()
