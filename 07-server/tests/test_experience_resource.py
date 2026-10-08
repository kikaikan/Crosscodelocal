"""Source experience-resource entries and atomic settlements on temporary saves."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_battle as base
from handlers import battle, progression
from database import StorageError


class ExperienceResourceTests(unittest.TestCase):
    setUp = base.BattleTests.setUp
    tearDown = base.BattleTests.tearDown
    get = base.BattleTests.get
    call = base.BattleTests.call
    start = base.BattleTests.start
    report = base.BattleTests.report

    def ready(self):
        with self.store.transaction(self.uid) as tx:
            tx.state['player']['level'] = 90
            tx.state['progress']['cleared_stages'] = [1106]
            tx.add_currency('hot', 200 - tx.currency('hot'))

    def test_6201_source_entry_win_repeat_and_next_layer(self):
        self.ready()
        before = self.get()
        with self.assertRaises(StorageError):
            self.start(6202)
        self.assertEqual(before, self.get())
        self.start(6201)
        self.assertEqual(self.fight['groupID'], 305011)
        self.assertEqual(self.get()['player']['hot'], before['player']['hot'])
        self.assertEqual(self.get()['store_exp'], before['store_exp'])
        replies = self.call('FightProtocol:OnFightOver', self.report())
        state = self.get()
        over = next(r.fields for r in replies if r.name == 'FightProto:FightOver')
        ordinary = over['reward'][0]['num']
        self.assertEqual(over['reward'][0]['id'], 10003)
        self.assertTrue(3900 <= ordinary <= 4100)
        self.assertEqual(over['star'], 3)
        self.assertEqual(state['player']['hot'], before['player']['hot'] - 10)
        self.assertEqual(state['store_exp'], before['store_exp'] + ordinary + 3000)
        self.assertEqual(state['player']['diamond'], before['player']['diamond'] + 60)
        self.assertEqual(state['player']['exp'], before['player']['exp'] + 100)
        self.assertNotIn('active_battle', state)
        with self.assertRaises(StorageError):
            self.call('FightProtocol:OnFightOver', self.report())
        self.assertEqual(state, self.get())
        self.start(6201)
        replies = self.call('FightProtocol:OnFightOver', self.report())
        over = next(r.fields for r in replies if r.name == 'FightProto:FightOver')
        self.assertEqual(over['fisrtPassReward'], [])
        self.assertEqual(over['fisrt3StarReward'], [])
        repeat = self.get()
        self.assertEqual(repeat['store_exp'], state['store_exp'] + over['reward'][0]['num'])
        self.assertEqual(repeat['player']['diamond'], state['player']['diamond'])
        self.start(6202)
        self.assertEqual(self.fight['groupID'], 305012)

    def test_retry_loss_and_quit_never_award_or_charge_win_fuel(self):
        self.ready()
        before = self.get()
        self.start(6201)
        pending = self.get()
        self.start(6201)
        self.assertEqual(pending, self.get())
        self.call('FightProtocol:OnFightOver', self.report(False))
        after = self.get()
        for key in ('inventory', 'store_exp', 'cards'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after['player'], before['player'])
        self.assertNotIn(6201, after['progress']['cleared_stages'])
        self.start(6201)
        self.call('FightProtocol:QuitDuplicate', {'nDuplicateID': 6201})
        self.assertEqual(after, self.get())

    def test_chapter_fuel_and_other_modes_remain_gated_atomically(self):
        before = self.get()
        with self.assertRaisesRegex(StorageError, 'Chapter prerequisite'):
            self.start(6201)
        self.assertEqual(before, self.get())
        self.ready()
        with self.store.transaction(self.uid) as tx:
            tx.add_currency('hot', 9 - tx.currency('hot'))
        before = self.get()
        with self.assertRaisesRegex(StorageError, 'Insufficient energy'):
            self.start(6201)
        self.assertEqual(before, self.get())
        foreign = deepcopy(progression.STAGES['6201'])
        foreign['group'] = 0
        with patch.dict(progression.STAGES, {'6201': foreign}):
            with self.assertRaisesRegex(StorageError, 'special mode'):
                progression.gate(deepcopy(before), 6201)
        self.assertEqual(before, self.get())

    def test_all_six_source_layers_keep_their_groups_and_rewards(self):
        self.ready()
        state = self.get()
        state['progress']['cleared_stages'] += [6201, 6202, 6203, 6204, 6205, 6206]
        for stage_id in range(6201, 6207):
            with self.subTest(stage=stage_id):
                stage = progression.gate(state, stage_id)
                _, _, team = battle.team(state, {}, stage)
                payload = battle.entry_payload(stage, 1, team)
                self.assertEqual(payload['groupID'], stage['nGroupID'])
                self.assertIn(str(stage['nGroupID']), battle.MONSTERS)
                reward = progression.reward_graph(state, stage['reward'], quantity_ranges=True)
                source = progression.REWARDS[str(stage['reward'])]['item'][0]
                self.assertTrue(source['count'] <= reward[0]['num'] <= source['countUplimit'])

    def test_resource_quantity_inclusive_bounds_and_invalid_ranges(self):
        state = self.get()
        for draw, expected in [(0, 3900), (200, 4100)]:
            with patch.object(progression.gacha, 'random_below', return_value=draw):
                self.assertEqual(progression.reward_graph(state, 1200001, quantity_ranges=True)[0]['num'], expected)
        for upper in (True, '4100', 3899, 2147483648):
            with self.assertRaises(StorageError):
                progression.resource_reward_count(state, {'count': 3900, 'countUplimit': upper})
