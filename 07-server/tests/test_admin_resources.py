from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from database import Store, StorageError
from server_core import ROOT, load_dependencies
import admin_resources as resources
import admin_roles as roles
from handlers import gacha, progression, battle_tactical

CODEC, SEED = load_dependencies(ROOT / '05-protocol/endpoints.json', SERVER / 'data/new_account_seed.json')

class AdminResourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SERVER / 'tests', prefix='admin-catalog-')
        self.store = Store(Path(self.temp.name) / 'state.sqlite3')
        self.uid = self.store.create_account('catalog-tests', SEED)['uid']

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def wire(self, replies):
        for reply in replies:
            raw = CODEC.encode_frame(reply.name, reply.fields)
            frame = CODEC.decode_frame(raw)
            self.assertEqual(frame.name, reply.name)

    def test_canonical_balances_and_pushes(self):
        with self.store.transaction(self.uid) as tx:
            changed = [resources.apply_resource(tx, key, 'set', 12) for key in
                       ('gold', 'diamond', 'army_coin', 'ability_num', 'BIND_DIAMOND', 'store_exp', 'hot')]
            changed.append(resources.apply_resource(tx, 'tp', 'set', 2))
            for key, cid in ((key, value[0]) for key, value in resources.RESOURCES.items() if value[0]):
                self.assertEqual(resources.balance(tx.state, key, cid), 12)
            for key, cfgid in (('gold',10001), ('diamond',10002), ('army_coin',10010), ('ability_num',10020), ('BIND_DIAMOND',10040)):
                self.assertEqual(tx.item_count(cfgid), 12)
            self.assertNotIn('10003', tx.state['inventory'])
            self.assertNotIn('10035', tx.state['inventory'])
            replies = resources.resource_pushes(tx.state, changed)
            self.wire(replies)
            hot = next(reply for reply in replies if reply.name == 'LoginProto:PlrUpdate')
            self.assertEqual(hot.fields['infos']['hot'], 12)
            self.assertGreater(hot.fields['t_hot'], 0)
        reopened = Store(self.store.path)
        try:
            state = reopened.get_player(self.uid)
            self.assertEqual(state['login']['BIND_DIAMOND'], 12)
            self.assertEqual(state['store_exp'], 12)
        finally:
            reopened.close()

    def test_set_add_alias_and_signed_bounds(self):
        with self.store.transaction(self.uid) as tx:
            first = resources.apply_resource(tx, 'item:10001', 'set', 100)
            self.assertEqual(first['key'], 'gold')
            self.assertEqual(resources.apply_resource(tx, 'gold', 'add', -100)['after'], 0)
            resources.apply_resource(tx, 'item:11002', 'set', 9999)
            resources.apply_resource(tx, 'hot', 'set', 999)
            self.assertEqual(tx.state['login']['t_hot'], 0)
            for key, mode, value in [('gold','add',-1), ('diamond','set',2147483648),
                    ('hot','set',1000), ('tp','set',4), ('item:11002','add',1),
                    ('gold','set',True), ('gold','add',1.1), ('gold','set',-1)]:
                state = deepcopy(tx.state)
                with self.assertRaises(StorageError):
                    resources.apply_resource(tx, key, mode, value)
                self.assertEqual(state, tx.state)

    def test_objects_unknown_paid_and_player_exp_rejected(self):
        for cfgid in (10004, 10998, 10999, 47805, 999999999):
            self.assertFalse(resources.item_allowed(cfgid))
        forbidden = next(int(key) for key, row in resources.ITEMS.items() if row['type'] == 6)
        before = self.store.get_player(self.uid)
        for key in ('level', 'exp', 'item:10004', 'item:47805', 'item:' + str(forbidden), 'item:01'):
            with self.assertRaises(StorageError):
                with self.store.transaction(self.uid) as tx:
                    resources.apply_resource(tx, key, 'set', 1)
            self.assertEqual(self.store.get_player(self.uid), before)

    def test_limited_role_grant_full_template_repeat_and_capacity(self):
        with self.store.transaction(self.uid) as tx:
            initial = len(tx.state['cards'])
            result = roles.grant_role(tx, 78050)
            self.assertFalse(result['duplicate'])
            self.assertEqual(len(tx.state['cards']), initial + 1)
            card = next(card for card in tx.state['cards'] if card['cid'] == result['cid'])
            self.assertTrue(card['skills'])
            self.assertEqual((card['break_level'], card['intensify_level']), (1,1))
            repeat = roles.grant_role(tx, 78050)
            self.assertTrue(repeat['duplicate'])
            self.assertEqual(repeat['cid'], result['cid'])
            self.assertEqual(len(tx.state['cards']), initial + 1)
            self.assertTrue(repeat['compensation'])
            self.wire(roles.role_pushes(tx.state, [result['cid']]) + resources.resource_pushes(tx.state, ['inventory']))
            tx.state['max_card_size'] = len(tx.state['cards'])
        before = self.store.get_player(self.uid)
        with self.assertRaises(StorageError):
            with self.store.transaction(self.uid) as tx:
                roles.grant_role(tx, 78020)
        self.assertEqual(before, self.store.get_player(self.uid))

    def test_archive_visibility_revoke_and_source_distribution_unchanged(self):
        candidates = [(row['cfgid'], row['weight']) for row in gacha.leaves( gacha.POOLS['1070']['jCardsId'][0][1])]
        with self.store.transaction(self.uid) as tx:
            tx.state['offline_clock'] = 1791000000
            with self.assertRaises(StorageError):
                gacha.check_pool(tx.state, 1070)
            roles.set_archive_pools(tx, {'pool_ids':[1070], 'enabled':True})
            gacha.check_pool(tx.state, 1070)
            self.assertEqual(gacha.factory(tx.state)['dy_open_pool'][1070], [4102444800])
            self.wire([__import__('server_core').Reply('PlayerProto:CardFactoryInfoRet', gacha.factory(tx.state))])
            roles.set_archive_pools(tx, {'pool_ids':[1070], 'enabled':False})
            self.assertLess(gacha.factory(tx.state)['dy_open_pool'][1070][0], tx.state['offline_clock'])
            self.assertEqual(candidates, [(row['cfgid'], row['weight']) for row in gacha.leaves(gacha.POOLS['1070']['jCardsId'][0][1])])

    def test_content_access_does_not_create_completion_or_rewards(self):
        with self.store.transaction(self.uid) as tx:
            progression.progress(tx.state)
            before = deepcopy(tx.state['progress'])
            assets = deepcopy((tx.state['cards'], tx.state['inventory']))
            roles.set_content_access(tx, True)
            self.assertEqual(tx.state['client_data']['crosscore_ps_access'], {'type': 1, 'data': '0'})
            with self.assertRaises(StorageError):
                progression.gate(tx.state, 1101)
            gacha.check_pool(tx.state, 1003)
            self.assertEqual(before, tx.state['progress'])
            self.assertEqual(assets, (tx.state['cards'], tx.state['inventory']))
            dynamic = gacha.factory(tx.state)['dy_open_pool']
            self.assertEqual(len(dynamic), 74)
            self.wire(battle_tactical.initial_pushes(tx.state))
            roles.set_content_access(tx, False)
            with self.assertRaises(StorageError):
                progression.gate(tx.state, 1101)

    def test_catalog_archived_only_data_and_supported_tactical(self):
        value = roles.catalog()
        self.assertEqual(len(value['roles']), 217)
        self.assertEqual({row['id'] for row in value['pools'] if row['limited']}, {1026,1049,1060,1070,1074})
        self.assertTrue(next(row for row in value['stages'] if row['id'] == 12301)['supported'])
        with self.store.transaction(self.uid) as tx:
            for payload in ({'pool_ids':[1001], 'enabled':True}, {'pool_ids':[1005], 'enabled':True},
                            {'pool_ids':[1070,1070], 'enabled':True}, {'pool_ids':[True], 'enabled':True}):
                with self.assertRaises(StorageError):
                    roles.set_archive_pools(tx, payload)

    def test_every_source_role_template_and_access_push_is_wire_valid(self):
        with self.store.transaction(self.uid) as tx:
            baseline = deepcopy(tx.state)
            for cfgid in roles.ROLES:
                tx.state = deepcopy(baseline)
                result = roles.grant_role(tx, int(cfgid))
                self.wire(roles.role_pushes(tx.state, [result['cid']]))
                self.assertEqual(result['cfgid'], int(cfgid))
            roles.set_content_access(tx, True)
            self.wire(roles.content_pushes(tx.state))

if __name__ == '__main__':
    unittest.main()
