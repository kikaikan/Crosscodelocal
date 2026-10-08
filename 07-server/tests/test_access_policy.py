"""Regression boundaries: archived content must not unlock ordinary progression."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from database import Store, StorageError
from server_core import Context, LocalServer, load_dependencies
from access_policy import DEFAULTS, configure, migrate, activity_stage, options
import panel_service
from handlers import initialization, progression, shop, gacha, player_state, panels

CODEC, SEED = load_dependencies(SERVER.parent/'05-protocol/endpoints.json', SERVER/'data/new_account_seed.json')


class AccessPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'state.sqlite3'
        self.store = Store(self.path)
        seed = deepcopy(SEED)
        configure(seed, DEFAULTS)
        self.uid = self.store.create_account('progression-policy', seed)['uid']
        self.ctx = Context(LocalServer(CODEC, self.store, seed), 'game', self.uid, True)
        with self.store.transaction(self.uid) as tx:
            tx.state['offline_clock'] = 1791072000

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def state(self):
        return self.store.get_player(self.uid)

    def call(self, fn, fields):
        replies = asyncio.run(fn(self.ctx, fields))
        for r in replies:
            raw = CODEC.encode_frame(r.name, r.fields)
            self.assertEqual(CODEC.decode_frame(raw).name, r.name)
        return replies

    def shop_ready(self, money=5000):
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['cleared_stages'] = [1001, 1002]
            tx.add_currency('diamond', money)

    def buy(self):
        return self.call(shop.buy, {'id':80401, 'buy_sum':1, 'buy_time':1791072000})

    def test_legacy_flag_never_opens_mainline_or_features(self):
        s = self.state()
        s['offline_unlock_all'] = True
        s['player']['level'] = 90
        for feature in ('ShopView', 'Dorm', 'Matrix', 'ArchiveView', 'special20'):
            self.assertFalse(initialization.feature_open(s, feature))
        with self.assertRaises(StorageError):
            progression.gate(s, 1002)
        s['progress']['cleared_stages'] = [1001]
        self.assertEqual(progression.gate(s, 1002)['id'], 1002)
        self.assertFalse(initialization.feature_open(s, 'ShopView'))
        s['progress']['cleared_stages'].append(1002)
        self.assertTrue(initialization.feature_open(s, 'ShopView'))

    def test_level_and_guide_conditions_are_conjunctive(self):
        s=self.state()
        self.assertFalse(initialization.conditions_open(s, [1045]))
        s['player']['level']=45
        self.assertTrue(initialization.conditions_open(s, [1045]))
        self.assertFalse(initialization.conditions_open(s, [1045, 2002]))
        rules=initialization.config_table('cfgCfgOpenRules.lua')
        guide=next((r for r in rules.values() if r['type']==3),None)
        if guide:
            self.assertFalse(initialization.conditions_open(s,[guide['id']]))
            s['progress']['completed_guides']=[guide['val']]
            self.assertTrue(initialization.conditions_open(s,[guide['id']]))

    def test_activity_scope_uses_section_group_and_preserves_capability_checks(self):
        s=self.state()
        candidates=[v for v in progression.STAGES.values() if activity_stage(v)]
        self.assertTrue(candidates)
        self.assertFalse(activity_stage(progression.STAGES['1001']))
        self.assertFalse(activity_stage({'group':99999999}))
        for section in shop.catalog('cfgSection.lua').values():
            if section.get('group') in (1,2,5,6):
                self.assertFalse(activity_stage({'group':section['id']}))
        stage=next(v for v in candidates if v.get('type') == 8 and
                   all(v.get('star'+str(i),[0])[0] in range(1,10) for i in range(1,4)))
        self.assertEqual(progression.gate(s,stage['id'], tactical=True)['id'],stage['id'])
        with self.assertRaises(StorageError):
            progression.gate(s,1002)
        unsupported=next(v for v in candidates if v.get('type') not in (1,2,3,4,99))
        with self.assertRaises(StorageError):
            progression.gate(s,unsupported['id'])

    def test_archived_pools_keep_count_limit(self):
        s=self.state()
        pool=next(v for v in gacha.POOLS.values() if v.get('nUseCntLimt',0)>0)
        _,_,record=gacha.check_pool(s,pool['id'])
        record['count']=pool['nUseCntLimt']
        with self.assertRaises(StorageError):
            gacha.check_pool(s,pool['id'])
        self.assertFalse(initialization.feature_open(s,'Matrix'))

    def test_migration_is_idempotent_and_preserves_assets_and_progress(self):
        with self.store.transaction(self.uid) as tx:
            tx.state.pop('access_policy_version')
            tx.state.pop('offline_access')
            tx.state['offline_unlock_all']=True
        before=self.state()
        self.assertEqual(migrate(self.store),1)
        after=self.state()
        for key in ('player','cards','inventory','progress','login'):
            self.assertEqual(after[key],before[key])
        self.assertEqual(options(after),DEFAULTS)
        self.assertFalse(after['offline_unlock_all'])
        self.assertEqual(migrate(self.store),0)
        self.assertEqual(self.state(),after)

    def test_client_cannot_change_access_policy(self):
        for key in ('crosscore_ps_access','crosscore_ps_access_v2'):
            with self.assertRaises(StorageError):
                self.call(player_state.set_client_data,{'key':key,'type':1,'data':'1'})

    def test_locked_write_requests_do_not_change_assets(self):
        from handlers import mail, cards_items, tasks
        before=self.state()
        for fn in (mail.operate, cards_items.use_item, cards_items.talent_upgrade, tasks.get_reward):
            with self.assertRaisesRegex(StorageError, 'locked by original progression'):
                self.call(fn,{})
            self.assertEqual(self.state(),before)

    def test_atlas_gated_by_shop_and_does_not_open_other_archives(self):
        s=self.state(); cfg=shop.commodities()[80401]
        self.assertFalse(shop.visible(s,cfg))
        self.shop_ready(); s=self.state()
        self.assertTrue(shop.visible(s,cfg))
        self.assertEqual(shop.commodity_info(s,cfg)['close_time'],0)
        self.assertFalse(shop.page_open(s,904))
        configure(s,dict(DEFAULTS,illustrations=False))
        self.assertFalse(shop.visible(s,cfg))

    def test_all_atlas_products_map_to_source_inventory(self):
        pictures=shop.catalog('cfgCfgArchiveMultiPicture.lua')
        items=shop.catalog('cfgItemInfo.lua')
        products=[c for c in shop.commodities().values() if c.get('group')==5]
        self.assertEqual(len(products),61)
        for cfg in products:
            for item,num,kind in cfg['jGets']:
                self.assertEqual(kind,2)
                self.assertEqual(items[item]['type'],16)
                self.assertEqual(pictures[items[item]['dy_value1']]['itemId'],item)

    def test_atlas_purchase_replay_owned_and_insufficient_balance(self):
        # The new-account seed now ships the whole collection unlock; clear the
        # item this product grants so the original unowned precondition holds.
        granted=shop.commodities()[80401]['jGets'][0][0]
        with self.store.transaction(self.uid) as tx:
            tx.state['inventory'].pop(str(granted),None)
        self.shop_ready(0); before=self.state()
        with self.assertRaises(StorageError): self.buy()
        self.assertEqual(self.state(),before)
        self.shop_ready(); before=self.state()['player']['diamond']
        self.buy(); saved=self.state()
        self.assertEqual(saved['inventory']['61002'],1)
        self.assertEqual(saved['player']['diamond'],before-1580)
        self.buy()
        self.assertEqual(self.state(),saved)
        with self.assertRaises(StorageError):
            self.call(shop.buy,{'id':80401,'buy_sum':1,'buy_time':1791072001})
        self.assertEqual(self.state(),saved)

    def test_board_ownership_use_and_reopen(self):
        row={'idx':2,'ty':1,'ids':[2],'bg':1,'detail1':{'x':0,'y':0,'scale':1,'top':True,'live2d':False}}
        fields={'panels':{'2':row},'using':2,'setting':0,'random':0}
        # Board 2 is backed by item 61002 (panel_service.available); the seed now
        # ships it unlocked, so clear it to keep this admission boundary covered.
        granted=shop.commodities()[80401]['jGets'][0][0]
        with self.store.transaction(self.uid) as tx:
            tx.state['inventory'].pop(str(granted),None)
        before=self.state()
        with self.assertRaises(StorageError): self.call(initialization.set_panels,fields)
        self.assertEqual(self.state(),before)
        self.shop_ready(); self.buy()
        self.call(initialization.set_panels,fields)
        self.call(initialization.use_panel,{'using':1})
        self.call(initialization.use_panel,{'using':2})
        self.store.close(); self.store=Store(self.path); self.ctx.server.store=self.store
        # GetNewPanel answers the random frame first and GetNewPanelRet last.
        data=self.call(initialization.panels,{})[-1].fields
        self.assertEqual(data['using'],2)
        self.assertEqual(data['panels']['2']['ids'],[2])
        with self.assertRaises(StorageError): self.call(initialization.use_panel,{'using':6})

    def random_row(self, index, kind, ids, bg=1):
        return {'idx': index, 'ty': kind, 'ids': ids, 'bg': bg,
                'detail1': {'x': 0, 'y': 0, 'scale': 1, 'top': True, 'live2d': False},
                'detail2': {'x': 0, 'y': 0, 'scale': 1, 'top': False, 'live2d': False}}

    def create_random(self, index, kind, ids):
        return self.call(panels.set_random_panel, {'random_panel': self.random_row(index, kind, ids)})

    def test_random_board_save_read_reload_and_login_push(self):
        self.create_random(7, 2, [7101001])
        saved = self.call(initialization.set_panels,
                          {'panels': {}, 'setting': 2, 'random': 1, 'using': 7})[0].fields
        self.assertEqual((saved['random'], saved['using'], saved['random_type']), (1, 7, 1))
        self.assertEqual(saved['random_panels']['7']['ids'], [7101001])
        detail = self.call(panels.get_random_panel_detail, {'idx': 7})[0].fields
        self.assertEqual((detail['idx'], detail['random_panel']['ty']), (7, 2))
        using = self.call(initialization.use_panel, {'using': 7})[0].fields
        self.assertEqual(using['random_panel']['idx'], 7)
        # CRoleDisplayMgr:LoginCheck runs inside the GetNewPanelRet callback
        # (PlayerProto.lua:1377) and reads random_panels, so the random frame comes
        # first and finishes on its only frame (CRoleDisplayMgr.lua:174-177).
        replies = self.call(initialization.panels, {})
        self.assertEqual([reply.name for reply in replies],
                         ['PlayerProto:GetRandomPanelRet', 'PlayerProto:GetNewPanelRet'])
        self.assertIs(replies[0].fields['finish'], True)
        self.assertEqual(sorted(replies[0].fields['random_panels']), ['7'])
        self.assertEqual(replies[0].fields['random_idx'], 8)
        self.assertEqual((replies[1].fields['random'], replies[1].fields['using']), (1, 7))
        self.assertEqual(replies[1].fields['random_panel']['idx'], 7)
        # A restart keeps the boards, the next free index and the selection.
        self.store.close(); self.store = Store(self.path); self.ctx.server.store = self.store
        reloaded = self.call(initialization.panels, {})
        self.assertEqual(reloaded[0].fields['random_panels']['7']['ids'], [7101001])
        self.assertEqual(reloaded[0].fields['random_idx'], 8)
        self.assertEqual(reloaded[1].fields['using'], 7)
        with self.assertRaises(StorageError):
            self.call(panels.get_random_panel_detail, {'idx': 8})

    def test_legacy_random_type_zero_reads_as_single(self):
        # A store written before the random family existed holds random_type=0; Lua
        # treats 0 as truthy, so it must read back as SINGLE (CRoleDisplayMgr.lua:89).
        with self.store.transaction(self.uid) as tx:
            tx.state['panels'] = {'panels': {}, 'setting': 0, 'random': 0, 'using': 1,
                                  'update_time': 0, 'random_type': 0}
        replies = self.call(initialization.panels, {})
        self.assertEqual(replies[0].fields['random_idx'], 7)
        self.assertIs(replies[0].fields['finish'], True)
        self.assertEqual(replies[0].fields['random_panels'], {})
        self.assertEqual(replies[1].fields['random_type'], 1)
        self.assertEqual(replies[1].fields['using'], 1)
        self.assertNotIn('random_panel', replies[1].fields)

    def test_random_type_switch_and_using_fallback(self):
        self.create_random(7, 2, [7101001])
        self.create_random(8, 3, [7101001])
        self.call(initialization.set_panels, {'panels': {}, 'random': 1, 'using': 7})
        self.call(panels.set_panel_random_type, {'random_type': 2})
        self.assertEqual(sorted(panel_service.boards(self.state()['panels'], 2)), [8])
        self.call(panels.set_random_panel, {'random_panel': self.random_row(8, 3, [7101001])})
        saved = self.state()['panels']
        self.assertEqual((saved['random'], saved['using'], saved['random_type']), (1, 8, 2))
        self.call(panels.set_panel_random_type, {'random_type': 3})
        self.assertEqual(self.state()['panels']['using'], 8)
        self.call(panels.remove_random_panel, {'idx': 8})
        saved = self.state()['panels']
        self.assertEqual((saved['random'], saved['using']), (1, 7))
        self.call(panels.remove_random_panel, {'idx': 7})
        saved = self.state()['panels']
        self.assertEqual((saved['random'], saved['using'], saved['random_panels']), (0, 1, {}))
        for fields in ({'panels': {}, 'random': 1, 'using': 1},
                       {'panels': {}, 'random': 2, 'using': 1},
                       {'panels': {}, 'random': 0, 'using': 7},
                       {'panels': {}, 'random_type': 4}):
            with self.assertRaises(StorageError):
                self.call(initialization.set_panels, fields)

    def test_random_board_images_must_be_owned_including_family_forms(self):
        with self.store.transaction(self.uid) as tx:
            tx.state['cards'].append({'cfgid': 50040, 'cid': 2, 'skin': 0, 'skin_a': 0, 'level': 1,
                                      'break_level': 1, 'exp': 0, 'skills': {}, 'equip_ids': {},
                                      'equips': [], 'is_new': True})
        state = self.state()
        # The sibling card's form model is a display form of an owned card, while
        # its skin stays refused until it is really owned (skins_service.forms).
        self.assertTrue(panel_service.available(state, 5004101))
        self.assertFalse(panel_service.available(state, 5004103))
        self.create_random(7, 2, [5004101])
        with self.assertRaises(StorageError):
            self.create_random(8, 2, [5004103])
        for row in (self.random_row(9, 2, [999999999]),
                    self.random_row(9, 4, [7101001]),
                    self.random_row(3, 2, [7101001]),
                    self.random_row(9, 2, [7101001, 7101001]),
                    self.random_row(9, 3, [7101001, 7101001, 7101001]),
                    self.random_row(9, 3, [0, 0]),
                    dict(self.random_row(9, 2, [7101001]), bg=63002),
                    dict(self.random_row(9, 2, [7101001]),
                         detail1={'x': 0, 'y': 0, 'scale': 0, 'top': True, 'live2d': False}),
                    dict(self.random_row(9, 2, [7101001]), idx='9'),
                    self.random_row(9, 2, [True])):
            with self.assertRaises(StorageError):
                self.call(panels.set_random_panel, {'random_panel': row})
        self.assertEqual(sorted(self.state()['panels']['random_panels']), ['7'])

    def test_bulk_random_animation_survives_wire_reload_and_manual_static_choice(self):
        with self.store.transaction(self.uid) as tx:
            tx.state['cards'][0]['break_level'] = 2
        replies = self.call(panels.add_random_skins_all,
                            {'ids': [7101002, 7101001], 'random_type': 1})
        rows = replies[0].fields['panels']
        self.assertTrue(rows[0]['detail1']['live2d'])
        self.assertFalse(rows[1]['detail1']['live2d'])
        raw = CODEC.encode_frame(replies[0].name, replies[0].fields)
        self.assertTrue(CODEC.decode_frame(raw).fields['panels'][0]['detail1'].parsed['live2d'])
        index = rows[0]['idx']
        self.store.close(); self.store = Store(self.path); self.ctx.server.store = self.store
        detail = self.call(panels.get_random_panel_detail, {'idx': index})[0].fields['random_panel']
        self.assertTrue(detail['detail1']['live2d'])
        # An explicit editor choice is not overridden by later bulk-add retries.
        detail['detail1']['live2d'] = False
        self.call(panels.set_random_panel, {'random_panel': detail})
        self.call(panels.add_random_skins_all, {'ids': [7101002], 'random_type': 1})
        self.assertFalse(self.state()['panels']['random_panels'][str(index)]['detail1']['live2d'])

    def test_bulk_animation_does_not_unlock_unavailable_break_or_shop_skins(self):
        before = self.state()
        self.assertTrue(panel_service.supports_animation(7101002))
        self.assertTrue(panel_service.supports_animation(7802003))
        replies = self.call(panels.add_random_skins_all,
                            {'ids': [7101002, 7802003], 'random_type': 1})
        self.assertEqual(replies[0].fields['panels'], [])
        self.assertEqual(before, self.state())

    def test_bulk_add_keeps_double_page_illustrations_and_valid_boards(self):
        item = shop.commodities()[80401]['jGets'][0][0]
        # The seed now ships the whole collection unlock; clear the granted item
        # so this purchase keeps its original ownership transition.
        with self.store.transaction(self.uid) as tx:
            tx.state['inventory'].pop(str(item),None)
        self.shop_ready(); self.buy()
        illustration = int(shop.catalog('cfgItemInfo.lua')[item]['dy_value1'])
        self.assertEqual(shop.catalog('cfgCfgArchiveMultiPicture.lua')[illustration]['itemId'], item)
        self.assertTrue(panel_service.available(self.state(), illustration))
        # CRoleSelectView.lua:228-236 keeps the illustration tabs on a ty=3 random
        # page (IsTwoRole is true only for the six-slot idx 6), so the illustration
        # is stored as a double-page board with its unused pad zeroed.
        created = self.call(panels.add_random_skins_all,
                            {'ids': [illustration, 7101001, 7101001], 'random_type': 2})[0].fields
        self.assertIs(created['finish'], True)
        self.assertEqual([(row['idx'], row['ids'], row['ty']) for row in created['panels']],
                         [(7, [illustration, 0], 3), (8, [7101001, 0], 3)])
        self.assertEqual(created['random_idx'], 9)
        again = self.call(panels.add_random_skins_all, {'ids': [7101001], 'random_type': 2})[0].fields
        self.assertEqual(again['panels'], [])
        self.assertIs(again['finish'], True)
        self.assertEqual(again['random_idx'], 9)
        # The same illustration is also a legal single board on the other page.
        single = self.call(panels.add_random_skins_all,
                           {'ids': [illustration], 'random_type': 1})[0].fields
        self.assertEqual([(row['ids'], row['ty']) for row in single['panels']], [([illustration], 2)])
        self.assertEqual(sorted(self.state()['panels']['random_panels']), ['7', '8', '9'])
        with self.assertRaises(StorageError):
            self.call(panels.add_random_skins_all, {'ids': [7101001], 'random_type': 3})

    def test_six_slot_double_pad_still_rejects_illustrations(self):
        item = shop.commodities()[80401]['jGets'][0][0]
        # The seed now ships the whole collection unlock; clear the granted item
        # so the illustration starts unowned as this rejection boundary expects.
        with self.store.transaction(self.uid) as tx:
            tx.state['inventory'].pop(str(item),None)
        self.shop_ready(); self.buy()
        illustration = int(shop.catalog('cfgItemInfo.lua')[item]['dy_value1'])
        # IsTwoRole() is true exactly here, so the client hides the tabs and the
        # original character-only rule must survive.
        row = self.random_row(6, 1, [illustration, 7101001])
        with self.assertRaises(StorageError):
            self.call(initialization.set_panels,
                      {'panels': {'6': row}, 'using': 1, 'setting': 0, 'random': 0})
        self.assertEqual(self.state().get('panels', {}).get('panels', {}), {})

    def test_random_board_capacity_and_page_clear(self):
        for index in (7, 8, 9):
            self.create_random(index, 2, [7101001])
        self.create_random(10, 3, [7101001])
        self.call(initialization.set_panels, {'panels': {}, 'random': 1, 'using': 7})
        clean = self.call(panels.random_panel_clean, {'random_type': 1, 'idx': 8})[0].fields
        self.assertEqual(clean, {'random_type': 1, 'idx': 8})
        self.assertEqual(sorted(self.state()['panels']['random_panels']), ['10', '8'])
        with self.assertRaises(StorageError):
            self.call(panels.random_panel_clean, {'random_type': 3, 'idx': 8})
        # CRoleDisplaySItem.lua:60 stops at g_RandomKanbanQuantity=500 per page.
        with self.store.transaction(self.uid) as tx:
            section = panel_service.stored(tx.state)
            section['random_panels'] = {str(20 + offset): panel_service.new_random_row(
                tx.state, 2, 7101001, 20 + offset) for offset in range(500)}
            section['random_idx'] = 520
            tx.state['panels'] = section
        with self.assertRaises(StorageError):
            self.create_random(520, 2, [7101001])
        with self.assertRaises(StorageError):
            self.call(panels.add_random_skins_all, {'ids': [7101001], 'random_type': 1})
        # Updating a board that already exists is still allowed at the cap.
        self.create_random(20, 2, [7101001])

    def test_random_panel_names_are_bounded_and_returned_in_full(self):
        self.create_random(7, 2, [7101001])
        first = self.call(panels.set_random_panel_name, {'random_type': 1, 'name': '单人队'})[0].fields
        self.assertEqual(first['name_list'], [{'first': '单人队', 'second': 1}])
        both = self.call(panels.set_random_panel_name, {'random_type': 2, 'name': '双人队'})[0].fields
        self.assertEqual(both['name_list'], [{'first': '单人队', 'second': 1},
                                             {'first': '双人队', 'second': 2}])
        push = self.call(initialization.panels, {})[0].fields
        self.assertEqual(push['name_list'], both['name_list'])
        for fields in ({'random_type': 4, 'name': 'x'}, {'random_type': 1, 'name': ''},
                       {'random_type': 1, 'name': 'x' * 33},
                       {'random_type': 1, 'name': '\x00\x07'},
                       {'random_type': 1, 'name': 5}):
            with self.assertRaises(StorageError):
                self.call(panels.set_random_panel_name, fields)
        self.assertEqual(self.state()['panels']['name_list'], both['name_list'])


    def test_five_hundred_boards_split_into_bounded_frames(self):
        # 500 boards always exceed the 32767 B frame limit, so GetRandomPanel must
        # split {idx: sNewPanel} maps and finish only on the last frame
        # (server_core.py:288-296 pre-encodes every reply before writing any).
        with self.store.transaction(self.uid) as tx:
            section = panel_service.stored(tx.state)
            section['random_panels'] = {str(7 + offset): panel_service.new_random_row(
                tx.state, 2, 7101001, 7 + offset) for offset in range(500)}
            section['random_idx'] = 507
            section['name_list'] = [{'first': '单人队', 'second': 1}]
            tx.state['panels'] = section
        replies = self.call(initialization.panels, {})
        random_frames = [reply for reply in replies if reply.name == 'PlayerProto:GetRandomPanelRet']
        self.assertGreater(len(random_frames), 1)
        self.assertEqual([frame.fields['finish'] for frame in random_frames],
                         [False] * (len(random_frames) - 1) + [True])
        self.assertEqual(sum(len(frame.fields['random_panels']) for frame in random_frames), 500)
        for frame in random_frames:
            self.assertLess(len(frame.fields['random_panels']), 127)
            self.assertEqual(frame.fields['random_idx'], 507)
            self.assertEqual(frame.fields['name_list'], [{'first': '单人队', 'second': 1}])
        self.assertEqual(sorted(int(key) for frame in random_frames for key in frame.fields['random_panels']),
                         list(range(7, 507)))
        self.assertEqual(replies[-1].name, 'PlayerProto:GetNewPanelRet')
        self.assertNotIn('random_panels', replies[-1].fields)


if __name__ == '__main__': unittest.main()
