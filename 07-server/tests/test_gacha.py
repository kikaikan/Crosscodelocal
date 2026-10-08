import asyncio
from copy import deepcopy
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from database import Store, StorageError
from server_core import Context, HANDLERS, LocalServer, ROOT, load_dependencies
from handlers import gacha

CODEC, SEED = load_dependencies(ROOT / '05-protocol/endpoints.json', SERVER / 'data/new_account_seed.json')

class GachaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SERVER / 'tests', prefix='gacha-')
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.store = Store(self.path)
        self.uid = self.store.create_account('gacha-fresh-account', SEED)['uid']
        self.server = LocalServer(CODEC, self.store, SEED)
        self.ctx = Context(self.server, 'game', self.uid, True)
        with self.store.transaction(self.uid) as tx:
            data = gacha.gacha_state(tx.state)
            data['rng_seed'] = '12' * 32
    def tearDown(self):
        self.store.close()
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to((SERVER / 'tests').resolve()))
        self.temp.cleanup()
    def call(self, name, fields):
        frame = CODEC.decode_frame(CODEC.encode_frame(name, fields))
        replies = asyncio.run(self.server.dispatch(self.ctx, frame))
        for reply in replies:
            raw = CODEC.encode_frame(reply.name, reply.fields)
            self.assertEqual(CODEC.decode_frame(raw).name, reply.name)
        return replies
    def get(self):
        return self.store.get_player(self.uid)
    def fund(self, value=100):
        with self.store.transaction(self.uid) as tx:
            tx.add_item(11002, value - tx.item_count(11002))
    def unlock(self, *stages):
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['cleared_stages'] = list(stages)

    def test_new_account_and_factory_no_free_assets(self):
        before = self.get()
        replies = self.call('PlayerProto:CardFactoryInfo', {})
        after = self.get()
        self.assertEqual(before['cards'], after['cards'])
        self.assertEqual(before['inventory'], after['inventory'])
        self.assertEqual(replies[0].fields['free_cnt'], 0)
        self.assertEqual(len(replies[0].fields['sum_pool_cnts']), 74)
        self.assertFalse(replies[0].fields['firt_create_infos'][1003]['first_10'])

    def test_first_normal_card_cost_role_and_unique_id(self):
        result = self.call('PlayerProto:CardCreate', {'card_pool_id': 1001, 'cnt': 1})[-1].fields
        state = self.get()
        card = next(row for row in state['cards'] if row['cfgid'] == 30200)
        self.assertEqual(result['infos'][0]['id'], card['cid'])
        self.assertEqual(result['infos'][0]['num'], 1)
        self.assertNotIn('11002', state['inventory'])
        self.assertEqual(state['inventory']['10033'], 1)
        self.assertTrue(any(row['id'] == 30200 for row in state['card_roles']))

    def test_insufficient_funds_and_invalid_count_are_atomic(self):
        before = self.get()
        for count in [10, 3, 0, 65535]:
            with self.assertRaises(StorageError):
                self.call('PlayerProto:CardCreate', {'card_pool_id': 1001, 'cnt': count})
            self.assertEqual(before, self.get())

    def test_award_failure_rolls_back_tickets_rng_and_cards(self):
        self.fund()
        before = self.get()
        with patch.object(gacha, 'award', side_effect=RuntimeError('test failure after random choice')):
            with self.assertRaises(RuntimeError):
                self.call('PlayerProto:CardCreate', {'card_pool_id': 1001, 'cnt': 10})
        self.assertEqual(before, self.get())

    def test_ten_result_count_history_and_duplicate_materials(self):
        self.fund()
        with patch.object(gacha, 'draw', return_value=30200):
            result = self.call('PlayerProto:CardCreate', {'card_pool_id': 1001, 'cnt': 10})[-1].fields
        state = self.get()
        cards = [row for row in state['cards'] if row['cfgid'] == 30200]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]['get_cnt'], 10)
        self.assertEqual([row['num'] for row in result['infos']], list(range(1, 11)))
        self.assertEqual(state['inventory']['103020'], 9)
        self.assertEqual(state['inventory']['10034'], 270)
        self.assertEqual(state['inventory']['11002'], 90)
        history = self.call('PlayerProto:GetCreateCardLogs', {'card_pool_id': 1001, 'skip': 0})[0].fields
        self.assertEqual(history['logs'][0]['cfgIds'], [30200] * 10)
        self.assertTrue(history['is_end'])

    def test_restart_continues_identical_random_sequence(self):
        self.fund()
        state = self.get()
        other_path = Path(self.temp.name) / 'clone.sqlite3'
        other = Store(other_path)
        other_uid = other.create_account('clone', SEED)['uid']
        with other.transaction(other_uid) as tx:
            tx.state = deepcopy(state)
            tx.state['player']['uid'] = other_uid
        other_ctx = Context(LocalServer(CODEC, other, SEED), 'game', other_uid, True)
        self.store.close()
        self.store = Store(self.path)
        self.server.store = self.store
        left = self.call('PlayerProto:CardCreate', {'card_pool_id': 1001, 'cnt': 10})[-1].fields
        right = asyncio.run(gacha.card_create(other_ctx, {'card_pool_id': 1001, 'cnt': 10}))[-1].fields
        self.assertEqual(left, right)
        self.assertEqual(self.get()['gacha']['rng_counter'], other.get_player(other_uid)['gacha']['rng_counter'])
        other.close()

    def test_locked_progression_rejects_and_preview_changes_no_assets(self):
        before = self.get()
        with self.assertRaises(StorageError):
            self.call('PlayerProto:FirstCardCreate', {'card_pool_id': 1003})
        self.assertEqual(before, self.get())
        self.unlock(1002)
        before = self.get()
        replies = self.call('PlayerProto:FirstCardCreate', {'card_pool_id': 1003})
        self.assertEqual(before['inventory'], self.get()['inventory'])
        self.assertEqual(before['cards'], self.get()['cards'])
        preview = replies[0].fields['hadGetLog']
        self.assertEqual(len(preview), 10)
        self.assertTrue(any(gacha.CARDS[str(row['id'])]['quality'] == 6 for row in preview))

    def test_saved_preview_confirmation_claims_once_and_survives_restart(self):
        self.unlock(1002)
        self.call('PlayerProto:FirstCardCreate', {'card_pool_id': 1003})
        self.call('PlayerProto:FirstCardCreateAddLog', {'card_pool_id': 1003})
        saved = self.get()['gacha']['pools']['1003']['logs'][0]['rewards']
        self.call('PlayerProto:FirstCardCreate', {'card_pool_id': 1003})
        replies = self.call('PlayerProto:FirstCardCreateAffirm', {'card_pool_id': 1003, 'ix': 1})
        state = self.get()
        self.assertEqual(state['gacha']['history'][-1]['cfgIds'], [row['id'] for row in saved])
        self.assertEqual(replies[-1].fields['create_cnt'], 10)
        self.assertTrue(state['gacha']['pools']['1003']['affirmed'])
        with self.assertRaises(StorageError):
            self.call('PlayerProto:FirstCardCreateAffirm', {'card_pool_id': 1003, 'ix': 1})
        self.assertEqual(state, self.get())

    def test_preview_limit_and_invalid_candidate_are_atomic(self):
        self.unlock(1002)
        with self.store.transaction(self.uid) as tx:
            gacha.pool_state(tx.state['gacha'], 1003)['tries'] = 30
        before = self.get()
        for name, fields in [('PlayerProto:FirstCardCreate', {'card_pool_id': 1003}),
                             ('PlayerProto:FirstCardCreateAffirm', {'card_pool_id': 1003, 'ix': 9})]:
            with self.assertRaises(StorageError):
                self.call(name, fields)
            self.assertEqual(before, self.get())

    def test_local_hard_pity_and_selection_force(self):
        self.unlock(1002)
        with self.store.transaction(self.uid) as tx:
            pool, data, progress = gacha.check_pool(tx.state, 10401)
            data['pity'][gacha.pity_group(pool)] = {'miss_six': 49}
            cid = gacha.draw(tx.state, pool, data, progress, 8)
            self.assertEqual(gacha.CARDS[str(cid)]['quality'], 6)
            self.assertEqual(progress['misses']['6'], 0)
            chosen, group = gacha.selection(data, pool)
            data['selection']['group:' + group] = 2
            data['pity'][gacha.pity_group(pool)] = {'miss_six': 49}
            cid = gacha.draw(tx.state, pool, data, progress, 9)
            self.assertEqual(cid, chosen['cid'])
            self.assertEqual(data['selection']['group:' + group], 0)

    def test_self_choice_requires_two_six_three_five_and_preserves_first(self):
        pool = gacha.POOLS['3001']
        six = [cid for cid in pool['sel_card_ids'] if gacha.CARDS[str(cid)]['quality'] == 6]
        five = [cid for cid in pool['sel_card_ids'] if gacha.CARDS[str(cid)]['quality'] == 5]
        picks = six[:2] + five[:3]
        before = self.get()
        with self.assertRaises(StorageError):
            self.call('PlayerProto:SetSelfChoiceCardPoolCard', {'id': 3001, 'cids': [six[0]] * 5})
        self.assertEqual(before, self.get())
        self.call('PlayerProto:SetSelfChoiceCardPoolCard', {'id': 3001, 'cids': picks})
        changed = six[1:3] + five[1:4]
        reply = self.call('PlayerProto:SetSelfChoiceCardPoolCard', {'id': 3001, 'cids': changed})[0].fields
        self.assertEqual(reply['firstCids'], picks)
        self.fund()
        self.assertEqual(self.call('PlayerProto:CardCreate', {'card_pool_id': 3001, 'cnt': 10})[-1].fields['cnt'], 10)

    def test_schedule_and_invalid_target_reject_without_changes(self):
        self.unlock(1002, 1125)
        with self.store.transaction(self.uid) as tx:
            tx.state['offline_clock'] = gacha.POOLS['3001']['nEnd']
        before = self.get()
        with self.assertRaises(StorageError):
            self.call('PlayerProto:SetSelfChoiceCardPoolCard', {'id': 3001, 'cids': []})
        with self.assertRaises(StorageError):
            self.call('PlayerProto:SetCardPoolSelCard', {'card_pool_id': 1005, 'cid': 30200})
        self.assertEqual(before, self.get())

    def test_free_single_does_not_create_free_tens(self):
        with self.store.transaction(self.uid) as tx:
            tx.state['gacha']['free_cnt'] = 1
            tx.add_item(11002, -tx.item_count(11002))
        self.call('PlayerProto:CardCreate', {'card_pool_id': 1001, 'cnt': 1})
        self.assertEqual(self.get()['gacha']['free_cnt'], 0)
        before = self.get()
        with self.assertRaises(StorageError):
            self.call('PlayerProto:CardCreate', {'card_pool_id': 1001, 'cnt': 10})
        self.assertEqual(before, self.get())

    def test_soft_pity_at_100_percent_has_no_lower_quality_leak(self):
        with self.store.transaction(self.uid) as tx:
            pool, data, progress = gacha.check_pool(tx.state, 3001)
            data['choice']['3001'] = {'id': 3001, 'cids': [10010, 10260, 10020, 10030, 10240], 'firstCids': []}
            data['pity']['choice'] = {'miss_six': 99}
            for _ in range(10):
                data['pity']['choice']['miss_six'] = 99
                cid = gacha.draw(tx.state, pool, data, progress, 1)
                self.assertEqual(gacha.CARDS[str(cid)]['quality'], 6)

    def test_all_pools_have_thirty_percent_base_six_star_mass(self):
        # Inspect the actual candidates handed to RNG, including self-choice
        # redistribution, instead of only checking configuration constants.
        for pool in gacha.POOLS.values():
            with self.subTest(pool=pool['id']):
                state = self.get()
                data = gacha.gacha_state(state)
                progress = gacha.pool_state(data, pool['id'])
                if pool['nType'] == 6:
                    ids = pool['sel_card_ids']
                    picks = [cid for cid in ids if gacha.CARDS[str(cid)]['quality'] == 6][:2]
                    picks += [cid for cid in ids if gacha.CARDS[str(cid)]['quality'] == 5][:3]
                    data['choice'][str(pool['id'])] = {'cids': picks}
                with patch.object(gacha, 'weighted_pick', return_value=10010) as pick:
                    gacha.draw(state, pool, data, progress, 2)
                self.assertEqual(pick.call_count, 1)
                with localcontext() as context:
                    context.prec = 80
                    candidates = pick.call_args.args[1]
                    total = sum(row['weight'] for row in candidates)
                    six = sum(row['weight'] for row in candidates if gacha.CARDS[str(row['cfgid'])]['quality'] == 6)
                    self.assertLess(abs(six / total - Decimal('0.30')), Decimal('1e-60'))

    def test_choice_duplicate_compensation_uses_published_item_and_cap(self):
        with self.store.transaction(self.uid) as tx:
            for _ in range(10):
                gacha.award(tx, 10010, gacha.POOLS['3001'])
            self.assertEqual(tx.item_count(10053), 10 + 8 * 100 + 300)
            self.assertEqual(tx.item_count(gacha.CARDS['10010']['coreItemId']), 8)

    def test_simultaneous_clients_cannot_spend_one_ticket_twice(self):
        async def requests():
            return await asyncio.gather(
                gacha.card_create(self.ctx, {'card_pool_id': 1001, 'cnt': 1}),
                gacha.card_create(self.ctx, {'card_pool_id': 1001, 'cnt': 1}), return_exceptions=True)
        outcomes = asyncio.run(requests())
        self.assertEqual(sum(isinstance(row, StorageError) for row in outcomes), 1)
        self.assertEqual(self.get()['gacha']['pools']['1001']['count'], 1)

    def test_page_size_matches_client_global(self):
        self.assertEqual(gacha.PAGE_SIZE, 5)
        with self.store.transaction(self.uid) as tx:
            for _ in range(6):
                gacha.record(tx.state, 1001, [30200])
        first = self.call('PlayerProto:GetCreateCardLogs', {'card_pool_id': 1001, 'skip': 0})[0].fields
        second = self.call('PlayerProto:GetCreateCardLogs', {'card_pool_id': 1001, 'skip': 1})[0].fields
        self.assertEqual(len(first['logs']), 5)
        self.assertFalse(first['is_end'])
        self.assertEqual(len(second['logs']), 1)
        self.assertTrue(second['is_end'])

if __name__ == '__main__':
    unittest.main()
