"""Source Lua report shapes and unfinished-entry retries; no real save writes."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_battle as base
from database import StorageError
from protocol_codec import JSONValue, lua_data
from config_codec import parse_lua_table

class ReportShapeTests(unittest.TestCase):
    setUp = base.BattleTests.setUp
    tearDown = base.BattleTests.tearDown
    get = base.BattleTests.get
    call = base.BattleTests.call
    start = base.BattleTests.start
    report = base.BattleTests.report

    def test_exact_lua_numeric_cid_one_winning_report(self):
        self.start()
        report = self.report()
        text = '{[1]={hp=1100,maxhp=1387,sp=0}}'
        value = lua_data(parse_lua_table(text))
        self.assertIsInstance(value, list)
        report['data'] = JSONValue(text, value, 'lua_table')
        report['exdata']['nMinHpPercent'] = 79
        replies = self.call('FightProtocol:OnFightOver', report)
        self.assertNotIn('active_battle', self.get())
        self.assertIn(1001, self.get()['progress']['cleared_stages'])
        self.assertTrue(next(reply for reply in replies if reply.name == 'FightProto:FightOver').fields['bIsWin'])

    def test_contiguous_two_cids_and_integral_lua_numbers(self):
        from handlers import gacha
        with self.store.transaction(self.uid) as tx:
            _, card, _, _ = gacha.award(tx, 30200, {'nType': 1})
            tx.state['teams'][0]['data'].append({'cid': card['cid'], 'index': 2, 'row': 1, 'col': 1})
        self.start()
        report = self.report()
        report['data'] = [dict(row, hp=float(row['hp'])) for row in report['data'].values()]
        report['exdata'] = {key: float(value) for key, value in report['exdata'].items()}
        self.call('FightProtocol:OnFightOver', report)
        self.assertIn(1001, self.get()['progress']['cleared_stages'])

    def test_array_indices_are_cids_not_party_order(self):
        self.start()
        with self.store.transaction(self.uid) as tx:
            tx.state['active_battle']['report_ids'] = [91101001]
        before = self.get()
        report = self.report()
        report['data'] = list(report['data'].values())
        with self.assertRaisesRegex(StorageError, 'lacks participant HP'):
            self.call('FightProtocol:OnFightOver', report)
        self.assertEqual(before, self.get())

    def test_buffed_hp_above_reported_base_maxhp_settles_and_allows_changed_team(self):
        from handlers import gacha
        with self.store.transaction(self.uid) as tx:
            _, card, _, _ = gacha.award(tx, 30200, {'nType': 1})
            second_cid = card['cid']
            tx.state['teams'][0]['data'].append({'cid': second_cid, 'index': 2, 'row': 1, 'col': 1})
        self.start()
        report = self.report()
        # Exact HP pair captured from Kunlun's legal entry buff in stage 1104.
        report['data']['1'].update(hp=26473, maxhp=17970)
        self.call('FightProtocol:OnFightOver', report)
        self.assertNotIn('active_battle', self.get())
        self.assertIn(1001, self.get()['progress']['cleared_stages'])
        with self.store.transaction(self.uid) as tx:
            tx.state['teams'][0]['data'] = [row for row in tx.state['teams'][0]['data'] if row['cid'] != second_cid]
        self.start()
        self.assertEqual(self.get()['active_battle']['cids'], [1])

    def test_source_negative_dead_hp_allows_surviving_party_win(self):
        from handlers import gacha
        with self.store.transaction(self.uid) as tx:
            _, card, _, _ = gacha.award(tx, 30200, {'nType': 1})
            tx.state['teams'][0]['data'].append({'cid': card['cid'], 'index': 2, 'row': 1, 'col': 1})
        self.start()
        report = self.report()
        report['data']['1']['hp'] = -10
        report['nGrade'][1] = 0
        report['exdata'].update(deathCnt=1, nMinHpPercent=0)
        self.call('FightProtocol:OnFightOver', report)
        self.assertNotIn('active_battle', self.get())

    def test_invalid_maxhp_and_win_without_survivors_preserve_session(self):
        self.start()
        before = self.get()
        for maximum in [0, -1, True, '1387']:
            report = self.report()
            report['data']['1']['maxhp'] = maximum
            with self.subTest(maxhp=maximum), self.assertRaisesRegex(StorageError, 'participant HP'):
                self.call('FightProtocol:OnFightOver', report)
            self.assertEqual(before, self.get())
        report = self.report()
        report['data']['1']['hp'] = -10
        report['nGrade'][1] = 0
        report['exdata'].update(deathCnt=1, nMinHpPercent=0)
        with self.assertRaisesRegex(StorageError, 'no surviving participant'):
            self.call('FightProtocol:OnFightOver', report)
        self.assertEqual(before, self.get())

    def test_fraction_and_boolean_numbers_reject_without_settlement(self):
        self.start()
        before = self.get()
        for target, value in [('turnNum', 3.5), ('cardCnt', True), ('nMinHpPercent', float('inf'))]:
            report = self.report()
            report['exdata'][target] = value
            with self.assertRaises((StorageError, ValueError)):
                self.call('FightProtocol:OnFightOver', report)
            self.assertEqual(before, self.get())

    def test_retry_after_connection_or_store_reopen_charges_once(self):
        self.start()
        first, before = deepcopy(self.fight), self.get()
        self.store.close()
        from database import Store
        self.store = Store(Path(self.temp.name) / 'state.sqlite3')
        self.server.store = self.store
        self.start()
        self.assertEqual(first, self.fight)
        self.assertEqual(before, self.get())

    def test_legacy_session_reconstructs_matching_party_without_new_fuel(self):
        self.start()
        first = deepcopy(self.fight)
        with self.store.transaction(self.uid) as tx:
            tx.state['active_battle'].pop('entry_reply')
        before = self.get()
        self.start()
        after = self.get()
        self.assertEqual(first, self.fight)
        self.assertEqual(before['player']['hot'], after['player']['hot'])
        self.assertEqual(before['active_battle']['started_at'], after['active_battle']['started_at'])
        self.assertEqual(before['progress'], after['progress'])

    def test_retry_different_stage_or_formation_preserves_session(self):
        self.start()
        with self.store.transaction(self.uid) as tx:
            tx.state['offline_unlock_all'] = True
        before = self.get()
        for fields in ({'nDuplicateID': 1002, 'nTeamIndex': 1},
                       {'nDuplicateID': 1001, 'list': [{'nTeamIndex': 1, 'team': [
                           {'cid': 1, 'index': 1, 'row': 1, 'col': 1}]}]}):
            with self.assertRaises(StorageError):
                self.call('FightProtocol:EnterFightDuplicate', fields)
            self.assertEqual(before, self.get())

if __name__ == '__main__':
    unittest.main()
