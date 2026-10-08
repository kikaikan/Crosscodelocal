"""Synthetic offline transactions; these are not observed official battle packets."""
import asyncio
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from database import Store, StorageError
from server_core import Context, LocalServer, ROOT, load_dependencies
from handlers import battle, progression, gacha

CODEC, SEED = load_dependencies(ROOT / '05-protocol/endpoints.json', SERVER / 'data/new_account_seed.json')

class BattleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SERVER / 'tests', prefix='battle-')
        self.store = Store(Path(self.temp.name) / 'state.sqlite3')
        self.uid = self.store.create_account('new-battle-account', SEED)['uid']
        self.server = LocalServer(CODEC, self.store, SEED)
        self.ctx = Context(self.server, 'game', self.uid, True)
        with self.store.transaction(self.uid) as tx:
            gacha.gacha_state(tx.state)['rng_seed'] = '34' * 32
        self.initial = self.get()
    def tearDown(self):
        self.store.close()
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to((SERVER / 'tests').resolve()))
        self.temp.cleanup()
    def get(self):
        return self.store.get_player(self.uid)
    def call(self, name, fields):
        frame = CODEC.decode_frame(CODEC.encode_frame(name, fields))
        replies = asyncio.run(self.server.dispatch(self.ctx, frame))
        for reply in replies:
            raw = CODEC.encode_frame(reply.name, reply.fields)
            self.assertEqual(CODEC.decode_frame(raw).name, reply.name)
        return replies
    def start(self, stage=1001):
        replies = self.call('FightProtocol:EnterFightDuplicate', {'nDuplicateID': stage, 'nTeamIndex': 1})
        self.fight = next(reply.fields for reply in replies if reply.name == 'FightProto:SingleFight')
        return replies
    def report(self, won=True, grade=None):
        cards = self.fight['data']['data']
        hp = {str(row['data']['cuid']): {'hp': row['data']['maxhp'], 'maxhp': row['data']['maxhp'], 'sp': 0} for row in cards} if won else {}
        return {'winer': 1 if won else 2, 'myOID': 1, 'monsterOID': 2,
                'data': hp, 'nGrade': grade or ([1, 1, 1] if won else [0, 0, 0]),
                'exdata': {'turnNum': 5, 'deathCnt': 0, 'cardCnt': len(cards), 'nMinHpPercent': 100 if won else 0}}

    def test_real_group_and_stats_no_rewards_on_entry(self):
        self.start()
        state = self.get()
        self.assertEqual(self.fight['groupID'], 100011)
        self.assertEqual(self.fight['data']['data'][0]['data']['maxhp'], 1387)
        self.assertEqual(self.fight['data']['data'][0]['data']['cuid'], 1)
        normal = self.fight['data']['data'][0]['data']
        self.assertEqual(normal['nStrategyIndex'], 0)
        self.assertNotIn('fuid', normal)  # Lua regards numeric zero as truthy.
        self.assertNotIn('npcid', normal)
        self.assertNotIn('damage', normal)  # Omitted coefficient defaults to 1 in FightCardBase.
        self.assertNotIn('bedamage', normal)
        self.assertEqual(self.fight['data']['tCommanderSkill'], battle.COMMANDERS['1003']['aSkillIds'])
        self.assertEqual(state['player']['hot'], self.initial['player']['hot'] - 1)
        self.assertEqual(state['inventory'], self.initial['inventory'])
        self.assertEqual(state['cards'], self.initial['cards'])
        self.assertEqual(state['active_battle']['stage_id'], 1001)

    def test_winning_settlement_persists_unlock_and_real_rewards_once(self):
        self.start()
        replies = self.call('FightProtocol:OnFightOver', self.report())
        state = self.get()
        self.assertNotIn('active_battle', state)
        self.assertIn(1001, state['progress']['cleared_stages'])
        self.assertEqual(state['progress']['mainLine'][0]['star'], 3)
        self.assertEqual(state['player']['hot'], self.initial['player']['hot'] - 6)
        self.assertEqual(state['player']['gold'], self.initial['player']['gold'] + 750)
        self.assertEqual(state['player']['diamond'], self.initial['player']['diamond'] + 60)
        self.assertEqual(state['store_exp'], self.initial['store_exp'] + 3000)
        self.assertNotIn('10003', state['inventory'])
        self.assertNotIn('10004', state['inventory'])
        self.assertEqual(state['inventory']['2030302'], 1)
        self.assertEqual(len(state['equips']), 3)
        self.assertEqual(state['cards'][0]['level'], 3)
        self.assertEqual(state['cards'][0]['exp'], 50)
        self.assertEqual(state['player']['exp'], 210)
        self.assertEqual(state['progress']['mainLine'][0]['data'], [1, 1, 1])
        over = next(reply.fields for reply in replies if reply.name == 'FightProto:FightOver')
        self.assertEqual(over['star'], 3)
        self.assertEqual(len(over['fisrtPassReward']), 2)
        self.assertEqual(len(over['fisrt3StarReward']), 2)
        before = deepcopy(state)
        with self.assertRaises(StorageError):
            self.call('FightProtocol:OnFightOver', self.report())
        self.assertEqual(before, self.get())
        self.start()
        self.call('FightProtocol:OnFightOver', self.report())
        repeat = self.get()
        self.assertEqual(repeat['player']['diamond'], state['player']['diamond'])
        self.assertEqual(repeat['store_exp'], state['store_exp'])
        self.assertEqual(repeat['inventory']['2030302'], 1)
        self.assertEqual(len(repeat['equips']), 6)
        self.assertEqual(len(repeat['progress']['battle_history']), 2)
        self.assertEqual(len(repeat['progress']['mainLine']), 1)

    def test_locked_stage_bad_team_and_energy_do_not_mutate(self):
        before = self.get()
        for fields in [{'nDuplicateID': 1002}, {'nDuplicateID': 1001, 'list': [{'nTeamIndex': 2, 'team': []}]},
                       {'nDuplicateID': 1001, 'isMultiReward': True}]:
            with self.assertRaises(StorageError):
                self.call('FightProtocol:EnterFightDuplicate', fields)
            self.assertEqual(before, self.get())
        with self.store.transaction(self.uid) as tx:
            tx.add_currency('hot', 5 - tx.currency('hot'))
        before = self.get()
        with self.assertRaises(StorageError):
            self.start()
        self.assertEqual(before, self.get())

    def test_loss_charges_only_entry_and_never_unlocks(self):
        self.start()
        self.call('FightProtocol:OnFightOver', self.report(False))
        state = self.get()
        self.assertEqual(state['player']['hot'], self.initial['player']['hot'] - 1)
        self.assertEqual(state['cards'], self.initial['cards'])
        self.assertEqual(state['inventory'], self.initial['inventory'])
        self.assertEqual(state['progress']['cleared_stages'], [])
        self.assertEqual(state['progress']['claimed_rewards'], [])

    def test_invalid_report_and_full_equipment_bag_rollback_settlement(self):
        self.start()
        before = self.get()
        bad = self.report()
        bad['myOID'] = 7
        with self.assertRaises(StorageError):
            self.call('FightProtocol:OnFightOver', bad)
        self.assertEqual(before, self.get())
        with self.store.transaction(self.uid) as tx:
            tx.state['max_equip_size'] = 0
        before = self.get()
        with self.assertRaises(StorageError):
            self.call('FightProtocol:OnFightOver', self.report())
        self.assertEqual(before, self.get())

    def test_quit_restart_and_next_stage_gate(self):
        self.start()
        before = self.get()
        first = deepcopy(self.fight)
        self.start()
        self.assertEqual(first, self.fight)
        self.assertEqual(before, self.get())
        self.call('FightProtocol:QuitDuplicate', {'nDuplicateID': 1001})
        self.assertNotIn('active_battle', self.get())
        self.assertEqual(self.get()['inventory'], before['inventory'])
        self.start()
        self.call('FightProtocol:OnFightOver', self.report())
        self.start(1002)
        self.assertEqual(self.fight['groupID'], 100021)
        self.store.close()
        self.store = Store(Path(self.temp.name) / 'state.sqlite3')
        self.server.store = self.store
        self.call('FightProtocol:OnFightOver', self.report())
        state = self.get()
        self.assertEqual(state['progress']['cleared_stages'], [1001, 1002])
        self.assertIsNotNone(gacha.check_pool(state, 1003))

    def test_level_threshold_and_equipped_stats_fail_explicitly(self):
        with self.store.transaction(self.uid) as tx:
            tx.state['player']['exp'] = 280
        self.start()
        self.call('FightProtocol:OnFightOver', self.report())
        state = self.get()
        self.assertEqual((state['player']['level'], state['player']['exp']), (2, 190))
        with self.store.transaction(self.uid) as tx:
            tx.state['cards'][0]['equips'] = [1]
        before = self.get()
        with self.assertRaisesRegex(StorageError, 'Equipped stat'):
            self.start()
        self.assertEqual(before, self.get())

    def test_stage_rating_uses_config_death_allowance_and_retains_best(self):
        with self.store.transaction(self.uid) as tx:
            award = gacha.award(tx, 30200, gacha.POOLS['1001'])
            tx.state['teams'][0]['data'].append({'cid': award[1]['cid'], 'index': 2, 'row': 1, 'col': 1, 'bIsNpc': False})
        self.start()
        report = self.report(grade=[1, 0, 1])
        second = self.fight['data']['data'][1]['data']['cuid']
        report['data'][str(second)]['hp'] = 0
        report['exdata'].update(deathCnt=1, nMinHpPercent=0)
        replies = self.call('FightProtocol:OnFightOver', report)
        over = next(reply.fields for reply in replies if reply.name == 'FightProto:FightOver')
        self.assertEqual(over['nGrade'], [1, 0, 1])
        self.assertEqual(over['nDupGrade'], [1, 1, 1])  # Source star2 allows two deaths.
        self.assertEqual(over['star'], 3)
        self.start()
        report = self.report(grade=[1, 1, 0])
        report['exdata']['turnNum'] = 30
        self.call('FightProtocol:OnFightOver', report)
        row = self.get()['progress']['mainLine'][0]
        self.assertEqual((row['star'], row['data']), (3, [1, 1, 1]))

    def test_numeric_command_schema_and_explicit_legacy_roster_gap(self):
        raw = CODEC.encode_frame('FightProtocol:RecvCmd', {1: 1, 2: 2})
        frame = CODEC.decode_frame(raw)
        self.assertEqual(CODEC.encode_frame(frame.name, frame.fields), raw)
        before = self.get()
        with self.assertRaisesRegex(StorageError, 'test-roster'):
            self.call('FightProtocol:StartMainLineFight', {'nDuplicateID': 1001, 'data': [{'id': 1001, 'row': 1, 'col': 1}]})
        self.assertEqual(before, self.get())

    def test_scripted_training_uses_source_npc_without_owned_experience(self):
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['cleared_stages'] = [1001, 1003, 8001]
        before = self.get()
        self.start(8002)
        cards = self.fight['data']['data']
        self.assertEqual(self.fight['nTeamIndex'], 80021)
        self.assertNotIn('tCommanderSkill', self.fight['data'])
        self.assertEqual([(row['data']['npcid'], row['row'], row['col']) for row in cards],
                         [(98002005, 2, 2), (98002001, 1, 1), (98002002, 1, 3)])
        for row in cards:
            npc = battle.NPCS[str(row['data']['npcid'])]
            self.assertEqual(row['data']['maxhp'], npc['maxhp'])
            self.assertEqual(row['data']['skills'], npc['skills'])
            self.assertEqual(row['data']['nStrategyIndex'], 0)
            self.assertNotIn('fuid', row['data'])
        self.assertEqual(self.get()['active_battle']['cids'], [])
        self.assertEqual(self.get()['active_battle']['report_ids'], [98002005, 98002001, 98002002])
        replies = self.call('FightProtocol:OnFightOver', self.report())
        state = self.get()
        self.assertEqual(state['cards'], before['cards'])
        self.assertIn(8002, state['progress']['cleared_stages'])
        self.assertEqual(state['player']['diamond'], before['player']['diamond'] + 60)
        self.assertEqual(state['player']['gold'], before['player']['gold'] + 3000)
        self.assertEqual(next(reply.fields for reply in replies if reply.name == 'FightProto:FightOver')['cards'], [])

    def test_scripted_slots_and_optional_npcs_reject_foreign_fighters(self):
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['cleared_stages'] = [1001, 1090]
        before = self.get()
        wrong = {'nDuplicateID': 8001, 'list': [{'nTeamIndex': 80011, 'team': [
            {'cid': 98001001, 'npcid': 98001001, 'row': 1, 'col': 1, 'index': 1}]}]}
        with self.assertRaisesRegex(StorageError, 'scripted roster'):
            self.call('FightProtocol:EnterFightDuplicate', wrong)
        self.assertEqual(before, self.get())
        for npc in [98001001, 99999999]:
            with self.assertRaisesRegex(StorageError, 'Unconfigured'):
                self.call('FightProtocol:EnterFightDuplicate', {'nDuplicateID': 1101, 'list': [
                    {'nTeamIndex': 1, 'team': [{'cid': npc, 'npcid': npc, 'row': 2, 'col': 2}]}]})
            self.assertEqual(before, self.get())
        replies = self.call('FightProtocol:EnterFightDuplicate', {'nDuplicateID': 1101, 'list': [
            {'nTeamIndex': 1, 'team': [{'cid': 91101001, 'npcid': 91101001, 'row': 2, 'col': 2}]}]})
        self.fight = next(reply.fields for reply in replies if reply.name == 'FightProto:SingleFight')
        self.assertEqual(self.fight['groupID'], 101011)
        report = self.report()
        report['data'] = {'1': report['data']['91101001']}
        with self.assertRaisesRegex(StorageError, 'participant HP'):
            self.call('FightProtocol:OnFightOver', report)
        self.call('FightProtocol:QuitDuplicate', {'nDuplicateID': 1101})

    def test_source_gender_choice_changes_scripted_roster(self):
        state = deepcopy(self.initial)
        stage = progression.STAGES['1319']
        state['login']['sel_card_ix'] = 1
        self.assertEqual(battle.default_forced_positions(state, stage)[0]['npcid'], 91319001)
        state['login']['sel_card_ix'] = 2
        self.assertEqual(battle.default_forced_positions(state, stage)[0]['npcid'], 91319002)

    def test_integer_monster_formation_footprints_and_current_78020_entry(self):
        self.assertEqual(battle.CONFIGS['78020']['grids'], 7)
        self.assertEqual(battle.footprint(battle.CONFIGS['78020'], 2, 1), {(2, 1), (2, 2)})
        configured = list(battle.CONFIGS.values()) + list(battle.NPCS.values())
        for cfg in configured:
            grids = cfg.get('grids')
            if isinstance(grids, int) and not isinstance(grids, bool):
                self.assertIn(str(grids), battle.FORMATIONS)
                self.assertTrue(battle.footprint(cfg, 1, 1))
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['cleared_stages'] = [1001, 1002, 1003]
            tx.state['cards'].append({
                'cfgid': 78020, 'cid': 73, 'level': 1, 'break_level': 1,
                'intensify_level': 1, 'skills': {}, 'equip_ids': {}, 'equips': [],
                'sub_talent': {}, 'skin': 7802001, 'skin_a': 7802001})
            tx.state['teams'][0]['data'] = [{
                'cid': 73, 'index': 1, 'row': 2, 'col': 1,
                'nStrategyIndex': 1, 'bIsNpc': False}]
            tx.state['teams'][0]['leader'] = 73
        replies = self.call('FightProtocol:EnterFightDuplicate', {
            'nDuplicateID': 1004, 'list': [{'nTeamIndex': 1, 'nSkillGroup': 1003,
                'team': [{'cid': 73, 'id': 78020, 'index': 1, 'row': 2, 'col': 1,
                          'nStrategyIndex': 1}]}]})
        self.fight = next(reply.fields for reply in replies if reply.name == 'FightProto:SingleFight')
        self.assertEqual(self.fight['data']['data'][0]['data']['cuid'], 73)

    def test_training_8001_real_force_team_shape_is_canonical_and_type_safe(self):
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['cleared_stages'] = [1001, 1003]
        before = self.get()
        malformed = {'nDuplicateID': 8001, 'list': [{'team': [{}]}]}
        with self.assertRaises(StorageError) as caught:
            self.call('FightProtocol:EnterFightDuplicate', malformed)
        self.assertNotIsInstance(caught.exception, TypeError)
        self.assertEqual(before, self.get())
        request = {'nDuplicateID': 8001, 'list': [{'nTeamIndex': 80011, 'team': [{
            'cid': 98001001, 'id': 98001001, 'npcid': 98001001,
            'row': 2, 'col': 2, 'index': 1, 'nStrategyIndex': 0}]}]}
        replies = self.call('FightProtocol:EnterFightDuplicate', request)
        self.fight = next(reply.fields for reply in replies if reply.name == 'FightProto:SingleFight')
        self.assertEqual(self.fight['nTeamIndex'], 80011)
        card = self.fight['data']['data'][0]
        self.assertEqual((card['data']['npcid'], card['row'], card['col']), (98001001, 2, 2))
        self.assertEqual(card['data']['nStrategyIndex'], 0)
        self.assertEqual(self.get()['active_battle']['cids'], [])

    def test_battle_ai_switch_persists_zero_to_four_and_updates_snapshot(self):
        self.start(1001)
        strategy = {1: [101, 2], 3: {1: 7, 3: 9}, 'bOverLoad': False}
        replies = self.call('FightProtocol:SwitchAIStrategy', {
            'index': 1, 'oid': 1, 'data': [{
                'cuid': 1, 'nStrategyIndex': 4, 'tStrategyData': strategy}]})
        self.assertEqual(replies[0].name, 'PlayerProto:SwitchAIStrategyRes')
        self.assertTrue(replies[0].fields['ret'])
        state = self.get()
        self.assertEqual(state['teams'][0]['data'][0]['nStrategyIndex'], 4)
        self.assertEqual(state['active_battle']['entry_reply']['data']['data'][0]['data']['nStrategyIndex'], 4)
        self.assertEqual(state['ai_strategies']['1']['4']['3']['1'], 7)
        before = deepcopy(state)
        with self.assertRaises(StorageError):
            self.call('FightProtocol:SwitchAIStrategy', {
                'index': 1, 'oid': 1, 'data': [{
                    'cuid': 999, 'nStrategyIndex': 0, 'tStrategyData': {1: [1]}}]})
        self.assertEqual(self.get(), before)

    def test_top_break_level_settlement_uses_the_configured_cap(self):
        # battle-card-level-cap.json keys rows 1..6, but break level 7 is reachable
        # (cfgCardBreak.lua has seven rows, card_break stops at jump 6). The top row's
        # MaxLv is the ceiling, exactly like cards_items.upgrade_transaction.
        self.assertEqual(progression.level_cap(1), 20)
        self.assertEqual(progression.level_cap(6), 85)
        self.assertEqual(progression.level_cap(7), 90)
        for invalid in (8, 0, True, '7', None):
            with self.assertRaises(StorageError):
                progression.level_cap(invalid)
        self.start()
        cids = self.get()['active_battle']['cids']
        with self.store.transaction(self.uid) as tx:
            for card in tx.state['cards']:
                if card['cid'] in cids:
                    card['break_level'] = 7
                    card['level'] = 89
                    card['exp'] = 147999
        self.call('FightProtocol:OnFightOver', self.report())
        state = self.get()
        self.assertNotIn('active_battle', state)
        card = next(c for c in state['cards'] if c['cid'] in cids)
        self.assertEqual(card['level'], 90)
        self.assertEqual(card['exp'], 299)

    def test_legacy_battle_session_rejects_without_crashing(self):
        self.start()
        with self.store.transaction(self.uid) as tx:
            existing = tx.state['active_battle']
            tx.state['active_battle'] = {'stage_id': existing['stage_id'],
                                         'started_at': existing['started_at']}
        before = self.get()
        for name, fields in [('FightProtocol:OnFightOver', self.report()),
                             ('FightProtocol:EnterFightDuplicate', {'nDuplicateID': 1001, 'nTeamIndex': 1}),
                             ('FightProtocol:QuitDuplicate', {'nDuplicateID': 1001})]:
            with self.assertRaises(StorageError) as caught:
                self.call(name, fields)
            self.assertNotIsInstance(caught.exception, (KeyError, TypeError))
        self.assertEqual(before, self.get())

    def test_retry_that_changes_the_encounter_keeps_the_stored_session(self):
        self.start()
        with self.store.transaction(self.uid) as tx:
            tx.state['active_battle']['cids'] = []
        before = self.get()
        with self.assertRaises(StorageError):
            self.call('FightProtocol:EnterFightDuplicate', {'nDuplicateID': 1001, 'nTeamIndex': 1})
        self.assertEqual(before, self.get())

if __name__ == '__main__':
    unittest.main()
