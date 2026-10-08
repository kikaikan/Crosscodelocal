"""Configured task progression, real rewards, reset IDs and atomic retries."""
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
from handlers import tasks
from protocol_codec import IVProtoCodec, WireConfig


def stamp(value):
    return int(datetime.fromisoformat(value).timestamp())


class TaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "players.sqlite3"
        self.store = Store(self.path)
        seed = json.loads((HERE / "data" / "new_account_seed.json").read_text("utf-8"))
        self.uid = self.store.create_account("new-local-tasks-test", seed)["uid"]
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['cleared_stages'] = [1002]
        self.ctx = Context(SimpleNamespace(store=self.store), "game", self.uid, True)
        self.clock("2026-10-01T04:00:00+08:00")
        self.codec = IVProtoCodec(json.loads((HERE.parent / "05-protocol" / "endpoints.json").read_text("utf-8")),
                                  WireConfig("little", max_frame_size=65535))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def clock(self, value):
        with self.store.transaction(self.uid) as tx:
            tx.state["offline_clock"] = stamp(value)
            tx.state["player"]["create_time"] = stamp("2026-10-01T04:00:00+08:00")

    def state(self):
        return self.store.get_player(self.uid)

    def find(self, kind, cfgid):
        return next(row for row in self.state()["tasks"] if row["type"] == kind and row["cfgid"] == cfgid)

    def wire(self, replies):
        for reply in replies:
            raw = self.codec.encode_frame(reply.name, reply.fields)
            frame = self.codec.decode_frame(raw)
            self.assertEqual(raw, self.codec.encode_frame(frame.name, frame.fields))

    def event(self, event, amount=1, identifier=None, passed=None):
        with self.store.transaction(self.uid) as tx:
            if passed:
                tx.state["progress"]["cleared_stages"] = sorted(set(passed) | {1002})
            replies = tasks.advance_tasks(tx.state, event, amount, identifier)
        self.wire(replies)

    async def test_new_account_assignments_wire_and_real_level_condition(self):
        replies = await tasks.get_tasks(self.ctx, {})
        self.wire(replies)
        self.assertGreater(len(self.state()["tasks"]), 40)
        self.assertEqual(self.find(1, 11001)["state"], 3)  # level1, not artificial completion
        self.assertEqual(self.find(1, 13001)["state"], 2)
        self.assertEqual(self.find(17, 20101)["state"], 2)
        self.assertNotIn(13003, [row["cfgid"] for row in self.state()["tasks"] if row["type"] == 1])
        self.assertTrue(replies[-2].fields["is_finish"])
        self.assertFalse(any(row["is_get"] == 2 for row in self.state()["tasks"]))

    async def test_main_awards_idempotent_reopen_and_unlock_claimed_chain(self):
        await tasks.get_tasks(self.ctx, {})
        before = self.state()
        self.event("stage_clear", identifier=1001, passed=[1001])
        instance = self.find(1, 13001)["id"]
        replies = await tasks.get_reward(self.ctx, {"id": instance})
        self.wire(replies)
        after = self.state()
        self.assertEqual(after["player"]["gold"], before["player"]["gold"] + 1000)
        self.assertEqual(after["inventory"]["2000201"], 1)
        self.assertEqual(self.find(1, 13002)["state"], 3)
        self.assertEqual(self.find(1, 13001)["is_get"], 2)
        self.store.close()
        self.store = Store(self.path)
        self.ctx.server.store = self.store
        repeat = await tasks.get_reward(self.ctx, {"ids": [instance, instance]})
        self.wire(repeat)
        self.assertEqual(repeat[-1].fields["gets"], [])
        self.assertEqual(self.state(), after)
        #13002 is a second root,13003 is its configured successor.
        self.event("stage_clear", identifier=1002, passed=[1001, 1002])
        await tasks.get_reward(self.ctx, {"id": self.find(1, 13002)["id"]})
        self.assertEqual(self.find(1, 13003)["state"], 2)

    async def test_incomplete_and_unknown_claims_rollback_everything(self):
        await tasks.get_tasks(self.ctx, {})
        before = self.state()
        for fields in ({"id": self.find(1, 13001)["id"]}, {"id": 999999}, {"id": True}, {"ids": [False]}):
            with self.assertRaises(StorageError):
                await tasks.get_reward(self.ctx, fields)
            self.assertEqual(self.state(), before)

    async def test_daily_login_star_awards_once_and_next_reset_new_instance(self):
        await tasks.get_tasks(self.ctx, {})
        task = self.find(3, 31005)
        self.assertEqual(task["state"], 3)
        before = self.state()
        replies = await tasks.get_reward(self.ctx, {"id": task["id"]})
        self.wire(replies)
        after = self.state()
        cfg = tasks.catalog("cfgCfgTaskDaily.lua")[31005]
        self.assertEqual(after["task_state"]["dailyStar"], cfg["nStar"])
        expected = [row for row in tasks.catalog("cfgCfgTaskDailyStarReward.lua").values() if row["star"] <= cfg["nStar"]]
        for row in expected:
            for item, count, kind in row["jAwardId"]:
                old = before["store_exp"] if item == 10003 else before["inventory"].get(str(item), 0)
                new = after["store_exp"] if item == 10003 else after["inventory"].get(str(item), 0)
                self.assertEqual(new - old, count)
        self.assertEqual((await tasks.get_reward(self.ctx, {"id": task["id"]}))[-1].fields["gets"], [])
        self.clock("2026-10-02T02:59:59+08:00")
        await tasks.get_tasks(self.ctx, {})
        self.assertEqual(self.find(3, 31005)["id"], task["id"])
        self.clock("2026-10-02T03:00:00+08:00")
        reset = await tasks.get_tasks(self.ctx, {})
        self.wire(reset)
        self.assertIn("TaskProto:TaskDelete", [reply.name for reply in reset])
        self.assertNotEqual(self.find(3, 31005)["id"], task["id"])
        self.assertEqual(self.state()["task_state"]["dailyStar"], 0)
        before = self.state()
        with self.assertRaises(StorageError):
            await tasks.get_reward(self.ctx, {"id": task["id"]})
        self.assertEqual(self.state(), before)

    async def test_stage_repeat_counters_are_period_scoped_and_specific(self):
        await tasks.get_tasks(self.ctx, {})
        self.event("stage_clear", 3, 1001, [1001])
        self.assertEqual(self.find(3, 30003)["state"], 3)
        self.assertEqual(self.find(3, 30004)["finish_ids"][0]["num"], 3)
        self.clock("2026-10-02T04:00:00+08:00")
        await tasks.get_tasks(self.ctx, {})
        self.assertEqual(self.find(3, 30003)["finish_ids"][0]["num"], 0)
        self.assertEqual(self.state()["task_state"]["stats"]["weekly"]["stage_clear"]["total"], 3)
        self.assertEqual(self.state()["task_state"]["stats"]["lifetime"]["stage_clear"]["by_id"]["1001"], 3)

    async def test_card_threshold_reads_actual_break_index_and_group_claims(self):
        await tasks.get_tasks(self.ctx, {})
        with self.store.transaction(self.uid) as tx:
            tx.state["cards"][0]["level"] = 20
            tx.state["cards"][0]["break_level"] = 2
            replies = tasks.advance_tasks(tx.state, "state_changed")
        self.wire(replies)
        self.assertEqual(self.find(17, 20102)["state"], 3)
        before = self.state()
        replies = await tasks.get_by_types(self.ctx, {"taskType": [{"type": 17, "nGroup": 0}]})
        self.wire(replies)
        after = self.state()
        self.assertEqual(after["player"]["level"], 2)  # 500 playerEXP, level1 threshold300
        self.assertEqual(after["player"]["exp"], 200)
        self.assertNotIn("10004", after["inventory"])
        self.assertEqual(after["store_exp"], before["store_exp"] + 1500)
        self.assertEqual(replies[-1].name, "TaskProto:GetRewardByTypeRet")
        self.assertEqual((await tasks.get_by_type(self.ctx, {"type": 17}))[-1].fields["gets"], [])

    async def test_reward_overflow_rolls_back_claim_and_star_ledgers(self):
        await tasks.get_tasks(self.ctx, {})
        with self.store.transaction(self.uid) as tx:
            tx.state["player"]["gold"] = MAX = 2147483647
            tx.state["inventory"]["10001"] = MAX
        before = self.state()
        with self.assertRaises(StorageError):
            await tasks.get_reward(self.ctx, {"id": self.find(1, 11001)["id"]})
        self.assertEqual(self.state(), before)

    async def test_guide_stages_and_nonpayment_domain_validation(self):
        await tasks.get_tasks(self.ctx, {})
        day = await tasks.get_days(self.ctx, {"type": 17})
        self.assertEqual(day[0].fields["c_day"], 1)
        self.clock("2026-10-07T04:00:00+08:00")
        await tasks.get_tasks(self.ctx, {})
        self.assertEqual((await tasks.get_days(self.ctx, {"type": 17}))[0].fields["c_day"], 7)
        self.assertEqual(len([row for row in self.state()["tasks"] if row["type"] == 17]), 35)
        before = self.state()
        for kind in (43, 99, 0, True):  # cumulative paid recharge is never enrolled
            with self.assertRaises(StorageError):
                await tasks.get_by_type(self.ctx, {"type": kind})
            self.assertEqual(self.state(), before)

    def test_safe_long_strings_and_unknown_event_are_rejected(self):
        value = tasks.normalize_long_strings("{['s']=[=[literal {braces} and \"quotes\"]=]}")
        from config_codec import parse_lua_table
        from seed_generator import python_data
        self.assertEqual(python_data(parse_lua_table(value))["s"], 'literal {braces} and "quotes"')
        with self.assertRaises(ValueError):
            parse_lua_table(tasks.normalize_long_strings("{x=os.execute('anything')}"))
        with self.assertRaises(StorageError):
            tasks.advance_tasks(deepcopy(self.state()), "untrusted_complete_all")

    async def test_every_entry_requires_authenticated_local_account(self):
        self.ctx.logged_in = False
        for func, fields in ((tasks.get_tasks, {}), (tasks.get_reset, {}), (tasks.get_days, {}),
                             (tasks.get_reward, {"id": 1}), (tasks.get_by_type, {"type": 1}),
                             (tasks.get_by_types, {"taskType": [{"type": 1}]})):
            with self.assertRaises(StorageError):
                await func(self.ctx, fields)

    async def test_every_configured_task_award_has_valid_state_and_wire(self):
        class Rollback(Exception):
            pass
        count = 0
        before = self.state()
        for filename in tasks.CATALOGS.values():
            for cfg in tasks.catalog(filename).values():
                with self.subTest(catalog=filename, cfgid=cfg['id']), self.assertRaises(Rollback):
                    with self.store.transaction(self.uid) as tx:
                        rendered, replies = tasks.grant_rewards(tx, tasks.reward_rows(cfg))
                        self.wire(replies)
                        self.wire([tasks.Reply('TaskProto:GetRewardRet', {'infos': [], 'gets': rendered,
                                           'dailyStar': 0, 'weeklyStar': 0, 'anvsStarInfo': []})])
                        self.assertEqual(len(rendered), len(cfg.get('jAwardId', [])))
                        count += 1
                        raise Rollback
        self.assertEqual(count, 651)
        self.assertEqual(self.state(), before)

    async def test_nonempty_equipment_reward_push_matches_saved_instance_on_wire(self):
        from handlers.progression import EQUIPS
        cfgid = int(next(iter(EQUIPS)))
        before = self.state()
        with self.store.transaction(self.uid) as tx:
            rendered, replies = tasks.grant_rewards(tx, [{"id": cfgid, "num": 1, "type": 4}])
        after = self.state()
        self.assertEqual(len(after["equips"]), len(before.get("equips", [])) + 1)
        saved = after["equips"][-1]
        reply = next(row for row in replies if row.name == "EquipProto:EquipAdd")
        self.assertNotIn("data", reply.fields)
        raw = self.codec.encode_frame(reply.name, reply.fields)
        decoded = self.codec.decode_frame(raw)
        self.assertEqual(len(decoded.fields["equips"]), 1)
        self.assertEqual(decoded.fields["equips"][0]["sid"], saved["sid"])
        self.assertEqual(decoded.fields["equips"][0]["cfgid"], saved["cfgid"])
        self.assertEqual(decoded.fields["equips"][0]["skills"], saved["skills"])
        self.assertTrue(decoded.fields["is_finish"])
        self.assertEqual(decoded.fields["cur_size"], len(after["equips"]))
        self.assertEqual(decoded.fields["max_size"], after["max_equip_size"])
        self.assertEqual(rendered[0]["c_id"], saved["sid"])
        self.assertEqual(raw, self.codec.encode_frame(decoded.name, decoded.fields))

    async def test_core_construction_pool_filter_excludes_preview_pool(self):
        await tasks.get_tasks(self.ctx, {})
        self.clock('2026-10-07T04:00:00+08:00')
        await tasks.get_tasks(self.ctx, {})
        self.event('card_create', 10, 1003)
        self.assertEqual(self.find(17, 20133)['finish_ids'][0]['num'], 0)
        self.event('card_create', 40, 1001)
        self.assertEqual(self.find(17, 20133)['state'], 3)
        self.assertEqual(self.find(17, 20133)['finish_ids'][0]['num'], 40)

    async def test_weekly_reset_keeps_lifetime_and_cannot_award_prior_week(self):
        await tasks.get_tasks(self.ctx, {})
        self.event('stage_clear', 20, 1001, [1001])
        weekly = self.find(4, 40001)
        await tasks.get_reward(self.ctx, {'id': weekly['id']})
        self.clock('2026-10-05T02:59:59+08:00')
        await tasks.get_tasks(self.ctx, {})
        self.assertEqual(self.find(4, 40001)['id'], weekly['id'])
        self.clock('2026-10-05T03:00:00+08:00')
        self.wire(await tasks.get_reset(self.ctx, {}))
        self.assertEqual(self.state()['task_state']['weeklyStar'], 0)
        self.assertEqual(self.find(4, 40001)['finish_ids'][0]['num'], 0)
        self.assertEqual(self.state()['task_state']['stats']['lifetime']['stage_clear']['total'], 20)

    async def test_actual_card_upgrade_and_skill_eventhooks_are_atomic(self):
        from handlers.cards_items import upgrade, skill_upgrade
        await tasks.get_tasks(self.ctx, {})
        self.wire(await upgrade(self.ctx, {'cid': 1, 'use_store_exp': 100}))
        self.assertEqual(self.find(3, 31004)['state'], 3)
        before = self.state()
        rejected = await upgrade(self.ctx, {'cid': 1, 'use_store_exp': 99999})
        self.assertEqual([reply.name for reply in rejected], ['SystemProto:Tips'])
        self.assertEqual(rejected[0].fields['strId'], 'notEnoughStoreExp')
        self.wire(rejected)
        self.assertEqual(self.state(), before)  # no debit, award or task progress on rejected upgrade
        with self.store.transaction(self.uid) as tx:
            tx.add_item(15001, 4)
        self.wire(await skill_upgrade(self.ctx, {'cid': 1, 'skill_id': 710100101}))
        self.assertEqual(self.find(3, 31003)['state'], 3)
        with self.store.transaction(self.uid) as tx:
            tx.state['task_state']['stats']['lifetime']['card_upgrade']['total'] = 2147483647
        before = self.state()
        with self.assertRaises(StorageError):
            await upgrade(self.ctx, {'cid': 1, 'use_store_exp': 150})
        self.assertEqual(self.state(), before)  # level, gold, poolXP and task counter all rollback


if __name__ == "__main__":
    unittest.main()
