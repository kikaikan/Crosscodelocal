"""Synthetic local star rewards and UI tutorial reports, not capture evidence."""
from copy import deepcopy
import json
import unittest
import test_battle as battle_tests
from database import StorageError
from handlers import progression

class ProgressionTests(unittest.TestCase):
    setUp = battle_tests.BattleTests.setUp
    tearDown = battle_tests.BattleTests.tearDown
    get = battle_tests.BattleTests.get
    call = battle_tests.BattleTests.call
    start = battle_tests.BattleTests.start
    report = battle_tests.BattleTests.report
    def test_chapter_claim_has_source_rewards_and_is_idempotent(self):
        for stage in [1001, 1002, 1003]:
            self.start(stage)
            self.call('FightProtocol:OnFightOver', self.report())
        before = self.get()
        self.assertEqual(progression.star_total(before, progression.STAR_REWARDS['101']), 9)
        replies = self.call('ClientProto:GetDupSumStarReward', {'infos': [{'id': 101, 'index': 1}]})
        state = self.get()
        self.assertEqual(state['store_exp'], before['store_exp'] + 5000)
        self.assertEqual(state['player']['gold'], before['player']['gold'] + 5000)
        self.assertEqual(state['progress']['star_claims']['101'], [1])
        notice = next(row.fields for row in replies if row.name == 'ClientProto:RewardNotice')
        self.assertEqual(notice['rewards'], [{'id': 10003, 'num': 5000, 'type': 2}, {'id': 10001, 'num': 5000, 'type': 2}])
        info = self.call('ClientProto:DupSumStarRewardInfo', {'ids': [101]})[0].fields
        self.assertEqual(info['infos'], [{'id': 101, 'indexs': [1]}])
        self.call('ClientProto:GetDupSumStarReward', {'infos': [{'id': 101, 'index': 1}]})
        self.assertEqual(state, self.get())

    def test_star_threshold_batch_and_foreign_stages_cannot_fund_claim(self):
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['mainLine'] = [{'id': 1001, 'star': 3}, {'id': 1002, 'star': 3}, {'id': 1003, 'star': 1}, {'id': 8004, 'star': 3}]
        before = self.get()
        self.assertEqual(progression.star_total(before, progression.STAR_REWARDS['101']), 7)
        with self.assertRaises(StorageError):
            self.call('ClientProto:GetDupSumStarReward', {'infos': [{'id': 101, 'index': 1}]})
        self.assertEqual(before, self.get())
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['mainLine'][2]['star'] = 3
        before = self.get()
        with self.assertRaises(StorageError):
            self.call('ClientProto:GetDupSumStarReward', {'infos': [{'id': 101, 'index': 1}, {'id': 101, 'index': 2}]})
        self.assertEqual(before, self.get())
        with self.assertRaises(StorageError):
            self.call('ClientProto:GetDupSumStarReward', {'infos': [{'id': 101, 'index': 3}]})
        self.assertEqual(before, self.get())

    def test_tutorial_known_monotonic_and_same_line_order(self):
        state = deepcopy(self.initial)
        for bad in [{'k999999': 1}, {'k10': True}, {'k30': 1}, {'k110': 1}]:
            before = deepcopy(state)
            with self.assertRaises(StorageError):
                progression.validate_guide_progress(state, bad)
            self.assertEqual(before, state)
        self.assertEqual(progression.validate_guide_progress(state, {'k10': 1}), [10])
        self.assertEqual(progression.validate_guide_progress(state, {'k10': 1, 'k1': 1}), [1, 10])
        self.assertEqual(progression.validate_guide_progress(state, {'k10': 1, 'k1': 1, 'k30': 1}), [1, 10, 30])
        before = deepcopy(state)
        with self.assertRaises(StorageError):
            progression.validate_guide_progress(state, {'k30': 1})
        self.assertEqual(before, state)
        state['progress']['cleared_stages'] = [1002]
        parsed = {'k10': 1, 'k1': 1, 'k30': 1, 'k110': 1}
        self.assertEqual(progression.validate_guide_progress(state, parsed), [1, 10, 30, 110])
        parsed['k113'] = 1
        self.assertEqual(progression.validate_guide_progress(state, parsed), [1, 10, 30, 110, 113])
        self.assertEqual(progression.validate_guide_progress(state, parsed), [1, 10, 30, 110, 113])

    def test_tutorial_skip_all_records_every_remaining_group_as_skipped(self):
        # GuideView.lua:355 OnClickSkipAll -> GuideMgr:SkipAll (GuideMgr.lua:621) submits
        # every remaining group in one update. The removed one-group cap used to refuse it
        # and permanently wedge the save because the client can never drop a group again.
        state = deepcopy(self.initial)
        payload = {('k%d' % group): 1 for group in progression.guide_groups()}
        expected = sorted(progression.guide_groups())
        self.assertEqual(progression.validate_guide_progress(state, payload), expected)
        self.assertEqual(state['progress']['completed_guides'], expected)
        self.assertEqual(state['progress']['skipped_guides'], expected)
        self.assertEqual(progression.validate_guide_progress(state, payload), expected)

    def test_tutorial_skip_line_sequence_is_refused_then_unblocked(self):
        # GuideMgr:GuideSkipLine (GuideMgr.lua:669) records every group on one line, one
        # message per group. A group that cannot be earned is refused without touching the
        # save; the accumulated dictionary that follows is accepted as an explicit skip.
        state = deepcopy(self.initial)
        before = deepcopy(state)
        with self.assertRaises(StorageError):
            progression.validate_guide_progress(state, {'k1': 1})
        self.assertEqual(before, state)
        self.assertEqual(progression.validate_guide_progress(state, {'k1': 1, 'k10': 1}), [1, 10])
        self.assertEqual(state['progress']['skipped_guides'], [1, 10])
        self.assertEqual(progression.validate_guide_progress(state, {'k1': 1, 'k10': 1, 'k30': 1}), [1, 10, 30])
        self.assertEqual(state['progress']['completed_guides'], [1, 10, 30])
        self.assertEqual(state['progress']['skipped_guides'], [1, 10])

    def test_tutorial_bulk_report_still_rejects_unknown_and_broken_entries(self):
        for bad in [{'k10': 1, 'k40': 1, 'k999999': 1}, {'k10': 1, 'k40': 1, 'k30': True}]:
            state = deepcopy(self.initial)
            before = deepcopy(state)
            with self.assertRaises(StorageError):
                progression.validate_guide_progress(state, bad)
            self.assertEqual(before, state)

    def test_story_save_then_quit_unlocks_next_direct_stage_once(self):
        before = self.get()
        with self.assertRaises(StorageError):
            self.call('FightProtocol:QuitDuplicate', {'index': 1, 'nDuplicateID': 1090})
        self.assertEqual(before, self.get())
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['cleared_stages'] = [1008]
            tx.state['client_data']['plot_data'] = {'type': 3, 'data': json.dumps({'line_1': 10013})}
        before = self.get()
        replies = self.call('FightProtocol:QuitDuplicate', {'index': 1, 'nDuplicateID': 1090})
        after = self.get()
        self.assertEqual(after['player']['hot'], before['player']['hot'])
        self.assertEqual(after['player']['diamond'], before['player']['diamond'] + 60)
        self.assertEqual(after['player']['exp'], before['player']['exp'] + 150)
        self.assertEqual(after.get('equips', []), before.get('equips', []))  # No ordinary combat drops.
        self.assertEqual(after['cards'], before['cards'])
        self.assertIn(1090, after['progress']['cleared_stages'])
        self.assertEqual(after['progress']['mainLine'][-1]['data'], [1, 0, 0])
        self.assertIn('FightProto:AskQuitDuplicate', [row.name for row in replies])
        self.assertIn('ClientProto:RewardNotice', [row.name for row in replies])
        self.call('FightProtocol:QuitDuplicate', {'index': 1, 'nDuplicateID': 1090})
        self.assertEqual(after, self.get())
        self.start(1101)
        self.assertEqual(self.fight['groupID'], 101011)

    def test_story_requires_matching_known_plot_line_and_no_active_encounter(self):
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['cleared_stages'] = [1008]
        for value in [True, 999999, 10012, None]:
            with self.store.transaction(self.uid) as tx:
                tx.state['client_data']['plot_data'] = {'type': 3, 'data': json.dumps({'line_1': value})}
            before = self.get()
            with self.assertRaises(StorageError):
                self.call('FightProtocol:QuitDuplicate', {'nDuplicateID': 1090})
            self.assertEqual(before, self.get())
        self.start(1001)
        with self.store.transaction(self.uid) as tx:
            tx.state['client_data']['plot_data'] = {'type': 3, 'data': json.dumps({'line_1': 10013})}
        before = self.get()
        with self.assertRaises(StorageError):
            self.call('FightProtocol:QuitDuplicate', {'nDuplicateID': 1090})
        self.assertEqual(before, self.get())

def suite():
    return unittest.TestSuite(ProgressionTests(name) for name in [
        'test_chapter_claim_has_source_rewards_and_is_idempotent',
        'test_star_threshold_batch_and_foreign_stages_cannot_fund_claim',
        'test_tutorial_known_monotonic_and_same_line_order',
        'test_tutorial_skip_all_records_every_remaining_group_as_skipped',
        'test_tutorial_skip_line_sequence_is_refused_then_unblocked',
        'test_tutorial_bulk_report_still_rejects_unknown_and_broken_entries',
        'test_story_save_then_quit_unlocks_next_direct_stage_once',
        'test_story_requires_matching_known_plot_line_and_no_active_encounter'])

if __name__ == '__main__':
    result = unittest.TextTestRunner().run(suite())
    raise SystemExit(not result.wasSuccessful())
