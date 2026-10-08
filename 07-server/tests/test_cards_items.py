import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.dont_write_bytecode = True
SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from database import Store, StorageError
from server_core import Context, HANDLERS, ROOT, load_dependencies
from protocol_codec import readable
import handlers.cards_items
from card_roles_service import card_roles

CODEC, SEED = load_dependencies(ROOT / '05-protocol/endpoints.json', SERVER / 'data/new_account_seed.json')


class CardsItemsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SERVER / 'tests', prefix='cards-')
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.store = Store(self.path)
        self.uid = self.store.create_account('fresh-local-cards-test', SEED)['uid']
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['cleared_stages'] = [1001,1002]
        # The real server exposes its codec on ctx.server (server_core.py:236);
        # the one-key CardUpdate guard measures the frame with it.
        self.context = Context(SimpleNamespace(store=self.store, codec=CODEC), 'game', self.uid, True)

    async def asyncTearDown(self):
        self.store.close()
        assert Path(self.temp.name).resolve().is_relative_to((SERVER / 'tests').resolve())
        self.temp.cleanup()

    def state(self):
        return self.store.get_player(self.uid)

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.context.server.store = self.store
        return self.state()

    async def request(self, name, fields):
        # Every input and output crosses the actual IVProto codec, including
        # the numeric-key card/skill maps used by Lua callbacks.
        frames, tail = CODEC.decode_stream(CODEC.encode_frame(name, fields))
        self.assertFalse(tail)
        replies = await HANDLERS[name](self.context, readable(frames[0].fields))
        for reply in replies:
            encoded = CODEC.encode_frame(reply.name, reply.fields)
            decoded, remaining = CODEC.decode_stream(encoded)
            self.assertFalse(remaining)
            self.assertEqual(CODEC.encode_frame(decoded[0].name, decoded[0].fields), encoded)
        return replies

    async def rejects_unchanged(self, name, fields):
        before = self.state()
        with self.assertRaises(StorageError):
            await self.request(name, fields)
        self.assertEqual(self.reopen(), before)

    async def denies_unchanged(self, fields, key):
        before = self.state()
        revision = self.store.connection.execute('SELECT revision FROM accounts WHERE uid=?', (self.uid,)).fetchone()[0]
        replies = await self.request('PlayerProto:CardUpgrade', fields)
        self.assertEqual([r.name for r in replies], ['SystemProto:Tips'])
        self.assertEqual(replies[0].fields['strId'], key)
        self.assertEqual(replies[0].fields['opId'], 2544)
        self.assertEqual(self.reopen(), before)
        self.assertEqual(self.store.connection.execute('SELECT revision FROM accounts WHERE uid=?', (self.uid,)).fetchone()[0], revision)
        self.assertTrue(self.context.logged_in)
        return replies

    async def item_denies_unchanged(self, name, fields, key):
        before = self.state()
        revision = self.store.connection.execute('SELECT revision FROM accounts WHERE uid=?', (self.uid,)).fetchone()[0]
        replies = await self.request(name, fields)
        self.assertEqual([r.name for r in replies], ['SystemProto:Tips'])
        self.assertEqual(replies[0].fields['strId'], key)
        self.assertEqual(replies[0].fields['opId'], 3684 if name.endswith('List') else 2516)
        self.assertEqual(self.reopen(), before)
        self.assertEqual(self.store.connection.execute('SELECT revision FROM accounts WHERE uid=?', (self.uid,)).fetchone()[0], revision)
        self.assertTrue(self.context.logged_in)
        return replies

    async def test_level_up_real_config_cost_and_persistence(self):
        replies = await self.request('PlayerProto:CardUpgrade', {'cid': 1, 'use_store_exp': 100})
        state = self.reopen()
        # CardLevel[1] needs 100 XP and costs 0.5 gold per XP; level 2 HP
        # coefficient is 1.041, commander base HP 1387 (floor -> 1443).
        self.assertEqual((state['cards'][0]['level'], state['cards'][0]['exp']), (2, 0))
        self.assertEqual(state['cards'][0]['hp'], 1443)
        self.assertEqual(state['store_exp'], 900)
        self.assertEqual((state['player']['gold'], state['inventory']['10001']), (950, 950))
        self.assertEqual(replies[-1].name, 'PlayerProto:CardUpgradeRet')

    async def test_set_card_info_only_changes_owned_flag_and_returns_false_on_wire(self):
        before = self.state()
        self.assertTrue(before['cards'][0]['is_new'])
        replies = await self.request('PlayerProto:SetCardInfo', {'cid': 1, 'is_new': False})
        self.assertEqual([(r.name, r.fields) for r in replies], [
            ('PlayerProto:SetCardInfoRet', {'cid': 1, 'is_new': False})])
        before['cards'][0]['is_new'] = False
        self.assertEqual(self.reopen(), before)
        # A repeated view is safe and must not alter level, XP, other cards,
        # currencies, inventory, or training data.
        await self.request('PlayerProto:SetCardInfo', {'cid': 1, 'is_new': False})
        self.assertEqual(self.reopen(), before)
        await self.request('PlayerProto:SetCardInfo', {'cid': 1, 'is_new': True})
        before['cards'][0]['is_new'] = True
        self.assertEqual(self.reopen(), before)

    async def test_set_card_info_rejects_invalid_flag_ownership_auth_and_extra_fields(self):
        await self.rejects_unchanged('PlayerProto:SetCardInfo', {'cid': 77, 'is_new': False})
        # The codec intentionally normalizes booleans; direct handler checks
        # ensure malformed calls cannot bypass the persistence boundary.
        for fields in ({'cid': 1}, {'cid': 1, 'is_new': None},
                       {'cid': 1, 'is_new': 0}, {'cid': 1, 'is_new': 'false'},
                       {'cid': 1, 'is_new': False, 'level': 90},
                       {'cid': True, 'is_new': False}):
            before = self.state()
            revision = self.store.connection.execute(
                'SELECT revision FROM accounts WHERE uid=?', (self.uid,)).fetchone()[0]
            with self.assertRaises(StorageError):
                await HANDLERS['PlayerProto:SetCardInfo'](self.context, fields)
            self.assertEqual(self.reopen(), before)
            self.assertEqual(self.store.connection.execute(
                'SELECT revision FROM accounts WHERE uid=?', (self.uid,)).fetchone()[0], revision)
        self.context.logged_in = False
        await self.rejects_unchanged('PlayerProto:SetCardInfo', {'cid': 1, 'is_new': False})

    async def test_upgrade_view_flag_then_upgrade_again_preserves_connection_and_costs(self):
        # Reproduce the real client sequence which previously reached an
        # unsupported 2645 handler immediately after the successful 2544.
        await self.request('PlayerProto:CardUpgrade', {'cid': 1, 'use_store_exp': 100})
        await self.request('PlayerProto:SetCardInfo', {'cid': 1, 'is_new': False})
        replies = await self.request('PlayerProto:CardUpgrade', {'cid': 1, 'use_store_exp': 150})
        self.assertTrue(self.context.logged_in)
        state = self.reopen()
        self.assertEqual((state['cards'][0]['level'], state['cards'][0]['exp']), (3, 0))
        self.assertFalse(state['cards'][0]['is_new'])
        self.assertEqual(state['store_exp'], 750)
        self.assertEqual((state['player']['gold'], state['inventory']['10001']), (875, 875))
        self.assertFalse(replies[-1].fields['card']['is_new'])

    async def test_level_up_bad_input_gold_xp_ownership_and_cap_roll_back(self):
        for fields in ({'cid': 1, 'use_store_exp': -1},
                       {'cid': 77, 'use_store_exp': 100}):
            await self.rejects_unchanged('PlayerProto:CardUpgrade', fields)
        await self.denies_unchanged({'cid': 1, 'use_store_exp': 1001}, 'notEnoughStoreExp')
        with self.store.transaction(self.uid) as tx:
            tx.add_currency('gold', -tx.currency('gold'))
        await self.denies_unchanged({'cid': 1, 'use_store_exp': 100}, 'itemNumNotEnough')
        with self.store.transaction(self.uid) as tx:
            tx.state['cards'][0]['level'] = 20
            tx.add_currency('gold', 10000)
        await self.denies_unchanged({'cid': 1, 'use_store_exp': 1}, 'reachMaxLvl')

    async def test_nullable_mix_data_accepts_bare_card_and_reopens(self):
        with self.store.transaction(self.uid) as tx:
            tx.state['cards'][0]['mix_data'] = None
        await self.request('PlayerProto:CardUpgrade', {'cid': 1, 'use_store_exp': 100})
        self.assertEqual(self.reopen()['cards'][0]['level'], 2)

    async def test_malformed_mix_data_still_rolls_back(self):
        for invalid in ([], False, 'invalid'):
            with self.store.transaction(self.uid) as tx:
                tx.state['cards'][0]['mix_data'] = invalid
            await self.rejects_unchanged('PlayerProto:CardUpgrade', {'cid': 1, 'use_store_exp': 100})

    async def test_partial_experience_and_multi_level_source_sum(self):
        with self.store.transaction(self.uid) as tx:
            tx.state['cards'][0]['exp'] = 40
        await self.request('PlayerProto:CardUpgrade', {'cid': 1, 'use_store_exp': 60})
        self.assertEqual((self.state()['cards'][0]['level'], self.state()['cards'][0]['exp']), (2, 0))
        amount = sum(handlers.cards_items.row('cfgCardLevel.lua', n)['exp'] for n in range(2, 5))
        with self.store.transaction(self.uid) as tx:
            tx.state['store_exp'] = amount
            tx.add_currency('gold', amount)
        await self.request('PlayerProto:CardUpgrade', {'cid': 1, 'use_store_exp': amount})
        self.assertEqual((self.reopen()['cards'][0]['level'], self.state()['cards'][0]['exp']), (5, 0))

    async def test_business_tip_params_and_recovery_without_relogin(self):
        replies = await self.denies_unchanged({'cid': 1, 'use_store_exp': 1001}, 'notEnoughStoreExp')
        self.assertEqual(replies[0].fields['args'], [{'type': 0, 'param': '1000'}])
        await self.request('PlayerProto:CardUpgrade', {'cid': 1, 'use_store_exp': 100})
        self.assertEqual(self.state()['cards'][0]['level'], 2)

    async def test_existing_cap_keeps_auth_and_capacity_protections(self):
        with self.store.transaction(self.uid) as tx:
            tx.state['store_exp'] = 100000
            tx.add_currency('gold', 100000)
        await self.rejects_unchanged('PlayerProto:CardUpgrade', {'cid': 1, 'use_store_exp': 100000})
        self.context.logged_in = False
        await self.rejects_unchanged('PlayerProto:CardUpgrade', {'cid': 1, 'use_store_exp': 100})

    async def test_jump_cost_failure_rolls_back_earlier_material_debits(self):
        await self.rejects_unchanged('PlayerProto:CardBreak', {'cid': 1})
        with self.store.transaction(self.uid) as tx:
            tx.state['cards'][0]['level'] = 20
            tx.add_item(14111, 1)
        # First required material exists, second does not: first debit must roll back.
        await self.rejects_unchanged('PlayerProto:CardBreak', {'cid': 1})
        with self.store.transaction(self.uid) as tx:
            tx.add_item(14012, 1)
        # Both materials exist but 15,000 gold does not: both debits must roll back.
        await self.rejects_unchanged('PlayerProto:CardBreak', {'cid': 1})
        with self.store.transaction(self.uid) as tx:
            tx.add_currency('gold', 14000)
        await self.request('PlayerProto:CardBreak', {'cid': 1})
        state = self.reopen()
        self.assertEqual(state['cards'][0]['break_level'], 2)
        role_id = card_roles()[state['cards'][0]['cfgid']]
        role = next(row for row in state['card_roles'] if row['id'] == role_id)
        self.assertEqual(role['data']['b_lv'], 2)
        self.assertEqual(state['player']['gold'], 0)
        self.assertNotIn('14111', state['inventory'])
        self.assertNotIn('14012', state['inventory'])

    async def test_skill_cost_progression_replay_and_partial_failure(self):
        name = 'PlayerProto:CardSkillUpgrade'
        await self.rejects_unchanged(name, {'cid': 1, 'skill_id': 710100101})
        with self.store.transaction(self.uid) as tx:
            tx.add_item(15001, 4)
        replies = await self.request(name, {'cid': 1, 'skill_id': 710100101})
        state = self.reopen()
        self.assertNotIn('710100101', state['cards'][0]['skills'])
        self.assertIn('710100102', state['cards'][0]['skills'])
        self.assertEqual(replies[-1].fields['infos']['1']['ids'], [710100102, 710100101])
        await self.rejects_unchanged(name, {'cid': 1, 'skill_id': 710100101})
        with self.store.transaction(self.uid) as tx:
            tx.add_item(15002, 1)
        # Level 2 also costs 8 low-tier books; high-tier first debit must roll back.
        await self.rejects_unchanged(name, {'cid': 1, 'skill_id': 710100102})
        await self.rejects_unchanged(name, {'cid': 1, 'skill_id': 4710101})

    async def test_core_and_passive_costs_use_owned_card_config(self):
        # A test-only owned quality-3 card proves configured alternate costs, without
        # changing the production fresh-account seed.
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['cleared_stages'].append(1006)
            card = tx.add_card(10110, {'skills': {'4101101': {'id': 4101101, 'exp': 0}}})
        cid = card['cid']
        await self.rejects_unchanged('PlayerProto:CardCoreLv', {'cid': cid, 'uf': 'costArr'})
        with self.store.transaction(self.uid) as tx:
            tx.add_item(100008, 1)
            tx.add_item(101011, 1)
        core_replies = await self.request('PlayerProto:CardCoreLv', {'cid': cid, 'uf': 'costArr'})
        talent_replies = await self.request('PlayerProto:MainTalentUpgrade', {'cid': cid, 'skill_id': 4101101, 'uf': 'costNum'})
        for reply in core_replies + talent_replies:
            if reply.name == 'PlayerProto:CardUpdate':
                self.assertEqual(reply.fields['store_exp'], 1000)
        state = self.reopen()
        owned = next(card for card in state['cards'] if card['cid'] == cid)
        self.assertEqual(owned['mix_data']['cl'], 2)
        self.assertIn('4101102', owned['skills'])
        self.assertNotIn('101011', state['inventory'])
        self.assertNotIn('100008', state['inventory'])
        await self.rejects_unchanged('PlayerProto:CardCoreLv', {'cid': cid, 'uf': 'arbitrary'})
        await self.rejects_unchanged('PlayerProto:MainTalentUpgrade', {'cid': cid, 'skill_id': 4101102, 'uf': 'costNum'})
        # Commander quality-5 core costNum=0 is explicit config, but its camp
        # has no costArr value and must not receive an invented free alternate.
        await self.rejects_unchanged('PlayerProto:CardCoreLv', {'cid': 1, 'uf': 'costArr'})
        await self.request('PlayerProto:CardCoreLv', {'cid': 1, 'uf': 'costNum'})

    async def test_fuel_use_and_batch_rollback_with_persistence(self):
        with self.store.transaction(self.uid) as tx:
            tx.add_item(10036, 2)
        await self.request('PlayerProto:UseItem', {'info': {'id': 10036, 'cnt': 1}})
        self.assertEqual(self.reopen()['player']['hot'], 92)
        await self.item_denies_unchanged('PlayerProto:UseItemList', {'infos': [
            {'id': 10036, 'cnt': 1}, {'id': 10036, 'cnt': 1}]}, 'itemNumNotEnough')
        await self.rejects_unchanged('PlayerProto:UseItem', {'info': {'id': 10036, 'cnt': 1, 'arg1': 1}})
        with self.store.transaction(self.uid) as tx:
            tx.add_currency('hot', 990 - tx.currency('hot'))
        await self.item_denies_unchanged('PlayerProto:UseItem', {'info': {'id': 10036, 'cnt': 1}}, 'plrHotUseMaxLimit')

    async def test_item_use_is_not_universal_empty_success(self):
        await self.item_denies_unchanged('PlayerProto:UseItem', {'info': {'id': 10001, 'cnt': 1}}, 'canNotUse')
        await self.rejects_unchanged('PlayerProto:UseItemList', {'infos': []})
        self.context.logged_in = False
        await self.rejects_unchanged('PlayerProto:CardUpgrade', {'cid': 1, 'use_store_exp': 100})

    async def test_unsupported_modified_stats_roll_back_level_costs(self):
        with self.store.transaction(self.uid) as tx:
            tx.state['cards'][0]['mix_data'] = {'weaponLv': 1}
        await self.rejects_unchanged('PlayerProto:CardUpgrade', {'cid': 1, 'use_store_exp': 100})

    async def test_selected_material_boxes_preserve_source_index_zero_options_and_wire(self):
        with self.store.transaction(self.uid) as tx:
            tx.add_item(58001, 4)
            tx.add_item(58003, 1)
        before = self.state()
        replies = await self.request('PlayerProto:UseItemList', {'infos': [
            {'id': 58001, 'cnt': 0, 'arg1': 1},
            {'id': 58001, 'cnt': 2, 'arg1': 2},
            {'id': 58001, 'cnt': 1, 'arg1': 5},
            {'id': 58003, 'cnt': 1, 'arg1': 1}]})
        state = self.reopen()
        self.assertEqual(state['inventory']['58001'], 1)
        self.assertNotIn('58003', state['inventory'])
        self.assertEqual(state['inventory']['14012'], before['inventory'].get('14012', 0) + 2)
        self.assertEqual(state['inventory']['14111'], before['inventory'].get('14111', 0) + 1)
        # Source 58003 index1 count=2, while dropCnt=3 is not a multiplier.
        self.assertEqual(state['inventory']['14311'], before['inventory'].get('14311', 0) + 2)
        ret = replies[-1]
        self.assertEqual(ret.name, 'PlayerProto:UseItemListRet')
        self.assertEqual(ret.fields['gets'], [{'id': 14012, 'type': 2, 'num': 2},
                                           {'id': 14111, 'type': 2, 'num': 1},
                                           {'id': 14311, 'type': 2, 'num': 2}])
        self.assertEqual(ret.fields['infos'][0]['cnt'], 0)
        self.assertEqual(state['store_exp'], before['store_exp'])

    async def test_selection_invalid_index_overdraft_and_full_target_roll_back(self):
        with self.store.transaction(self.uid) as tx:
            tx.add_item(58001, 2)
        await self.rejects_unchanged('PlayerProto:UseItemList', {'infos': [
            {'id': 58001, 'cnt': 1, 'arg1': 2}, {'id': 58001, 'cnt': 1, 'arg1': 999}]})
        await self.item_denies_unchanged('PlayerProto:UseItemList', {'infos': [
            {'id': 58001, 'cnt': 2, 'arg1': 2}, {'id': 58001, 'cnt': 1, 'arg1': 5}]}, 'itemNumNotEnough')
        with self.store.transaction(self.uid) as tx:
            tx.add_item(14012, 999999 - tx.item_count(14012))
        await self.item_denies_unchanged('PlayerProto:UseItemList', {'infos': [
            {'id': 58001, 'cnt': 1, 'arg1': 2}]}, 'GeneralTips')

    async def test_selected_equipment_box_grants_real_unique_instances_and_full_bag_tip(self):
        with self.store.transaction(self.uid) as tx:
            tx.add_item(57119, 2)
        replies = await self.request('PlayerProto:UseItemList', {'infos': [
            {'id': 57119, 'cnt': 1, 'arg1': 1}]})
        state = self.reopen()
        self.assertEqual(state['equips'][-1]['cfgid'], 2230501)
        self.assertEqual(state['equips'][-1]['level'], 0)
        self.assertTrue(state['equips'][-1]['skills'])
        self.assertEqual(replies[-1].fields['gets'], [{'id': 2230501, 'type': 4, 'num': 1}])
        self.assertIn('EquipProto:EquipAdd', [r.name for r in replies])
        with self.store.transaction(self.uid) as tx:
            tx.state['max_equip_size'] = len(tx.state['equips'])
        await self.item_denies_unchanged('PlayerProto:UseItemList', {'infos': [
            {'id': 57119, 'cnt': 1, 'arg1': 1}]}, 'equipBagSpaceLimit')

    async def test_item_business_denial_logs_only_numeric_source_fields_and_keeps_session(self):
        logs = []
        self.context.server.event = lambda kind, **fields: logs.append((kind, fields))
        with self.store.transaction(self.uid) as tx:
            tx.add_item(58001, 1)
        await self.item_denies_unchanged('PlayerProto:UseItemList', {'infos': [
            {'id': 58001, 'cnt': 2, 'arg1': 2}]}, 'itemNumNotEnough')
        self.assertEqual(logs[-1][1]['infos'], [{'id': 58001, 'cnt': 2, 'arg1': 2}])
        self.assertEqual(set(logs[-1][1]), {'name', 'infos', 'reason'})
        replies = await self.request('PlayerProto:UseItemList', {'infos': [
            {'id': 58001, 'cnt': 1, 'arg1': 2}]})
        self.assertEqual(replies[-1].name, 'PlayerProto:UseItemListRet')
        self.assertTrue(self.context.logged_in)


    def add_talent_fighter(self, cfgid, skill_id):
        # Test-only owned fighters prove the configured talent cost table without
        # changing the production fresh-account seed.
        with self.store.transaction(self.uid) as tx:
            if 1006 not in tx.state['progress']['cleared_stages']:
                tx.state['progress']['cleared_stages'].append(1006)
            card = tx.add_card(cfgid, {'skills': {str(skill_id): {'id': skill_id, 'exp': 0}}})
        return card['cid']

    async def test_one_key_talent_upgrades_every_selected_fighter_to_configured_max(self):
        # RoleListTX.lua:78-91 sends cid+skill_id per selected card, and the
        # client previews 1 -> 5 using only the card's own chip
        # (CharacterCardsData.lua:1134-1155).
        first = self.add_talent_fighter(10110, 4101101)   # core item 101011
        second = self.add_talent_fighter(10040, 4100401)  # core item 101004
        with self.store.transaction(self.uid) as tx:
            tx.add_item(101011, 4)
            tx.add_item(101004, 4)
        replies = await self.request('PlayerProto:OneKeyMainTalentUpgrade', {'infoArr': [
            {'cid': first, 'skill_id': 4101101}, {'cid': second, 'skill_id': 4100401}]})
        state = self.reopen()
        cards = {card['cid']: card for card in state['cards']}
        # CfgMainTalentSkillUpgrade quality 3 costs costNum=1 per step for levels
        # 1-4 and the 410110x/410040x chains stop at lv 5.
        self.assertEqual(list(cards[first]['skills']), ['4101105'])
        self.assertEqual(list(cards[second]['skills']), ['4100405'])
        self.assertNotIn('101011', state['inventory'])
        self.assertNotIn('101004', state['inventory'])
        names = [reply.name for reply in replies]
        # The client registers only PlayerProto.lua:1840-1843 for this request;
        # a per-fighter MainTalentUpgradeRet would drive the single-fighter UI
        # event (RoleSkillMgr.lua:207-209) once per selected card.
        self.assertEqual(names.count('PlayerProto:MainTalentUpgradeRet'), 0)
        self.assertEqual(names[-1], 'PlayerProto:OneKeyMainTalentUpgradeRet')
        self.assertEqual(replies[-1].fields, {})
        updates = [reply for reply in replies if reply.name == 'PlayerProto:CardUpdate']
        self.assertEqual(len(updates), 1)
        self.assertEqual({card['cid']: list(card['skills']) for card in updates[0].fields['cards']},
                         {first: ['4101105'], second: ['4100405']})
        data = next(reply for reply in replies if reply.name == 'PlayerProto:ItemUpdate').fields['data']
        self.assertEqual(sorted((row['id'], row['add'], row['num']) for row in data),
                         [(101004, -4, 0), (101011, -4, 0)])
        self.assertTrue(self.context.logged_in)

    async def test_one_key_talent_stops_at_last_affordable_level_without_over_debit(self):
        cid = self.add_talent_fighter(10110, 4101101)
        with self.store.transaction(self.uid) as tx:
            tx.add_item(101011, 3)
        replies = await self.request('PlayerProto:OneKeyMainTalentUpgrade',
                                     {'infoArr': [{'cid': cid, 'skill_id': 4101101}]})
        state = self.reopen()
        card = next(card for card in state['cards'] if card['cid'] == cid)
        self.assertEqual(list(card['skills']), ['4101104'])
        self.assertNotIn('101011', state['inventory'])
        names = [reply.name for reply in replies]
        self.assertEqual(names.count('PlayerProto:MainTalentUpgradeRet'), 0)
        self.assertEqual(names.count('PlayerProto:CardUpdate'), 1)
        update = next(reply for reply in replies if reply.name == 'PlayerProto:CardUpdate')
        self.assertEqual({card['cid']: list(card['skills']) for card in update.fields['cards']},
                         {cid: ['4101104']})
        self.assertEqual(replies[-1].name, 'PlayerProto:OneKeyMainTalentUpgradeRet')
        data = next(reply for reply in replies if reply.name == 'PlayerProto:ItemUpdate').fields['data']
        self.assertEqual([(row['id'], row['add'], row['num']) for row in data], [(101011, -3, 0)])

    async def test_one_key_talent_denies_and_rolls_back_when_a_fighter_cannot_upgrade(self):
        rich = self.add_talent_fighter(10110, 4101101)
        poor = self.add_talent_fighter(10040, 4100401)
        with self.store.transaction(self.uid) as tx:
            tx.add_item(101011, 4)
        before = self.state()
        revision = self.store.connection.execute(
            'SELECT revision FROM accounts WHERE uid=?', (self.uid,)).fetchone()[0]
        replies = await self.request('PlayerProto:OneKeyMainTalentUpgrade', {'infoArr': [
            {'cid': rich, 'skill_id': 4101101}, {'cid': poor, 'skill_id': 4100401}]})
        self.assertEqual([reply.name for reply in replies], ['SystemProto:Tips'])
        self.assertEqual(replies[0].fields['strId'], 'itemNumNotEnough')
        self.assertEqual(replies[0].fields['opId'], 3703)
        self.assertEqual(replies[0].fields['opName'], 'PlayerProto:OneKeyMainTalentUpgrade')
        self.assertEqual([arg['param'] for arg in replies[0].fields['args']], ['安第斯星源', '1'])
        state = self.reopen()
        self.assertEqual(state, before)
        self.assertEqual(self.store.connection.execute(
            'SELECT revision FROM accounts WHERE uid=?', (self.uid,)).fetchone()[0], revision)
        self.assertEqual(list(next(card for card in state['cards'] if card['cid'] == rich)['skills']),
                         ['4101101'])
        self.assertEqual(state['inventory']['101011'], 4)
        self.assertTrue(self.context.logged_in)

    async def test_one_key_talent_wire_round_trip_and_malformed_batches_roll_back(self):
        cid = self.add_talent_fighter(10110, 4101101)
        fields = {'infoArr': [{'cid': cid, 'skill_id': 4101101}]}
        frames, tail = CODEC.decode_stream(CODEC.encode_frame('PlayerProto:OneKeyMainTalentUpgrade', fields))
        self.assertFalse(tail)
        self.assertEqual((frames[0].opcode, readable(frames[0].fields)), (3703, fields))
        # GameMsg.lua:5358-5362 declares the answer with no fields at all.
        decoded, remaining = CODEC.decode_stream(
            CODEC.encode_frame('PlayerProto:OneKeyMainTalentUpgradeRet', {}))
        self.assertFalse(remaining)
        self.assertEqual((decoded[0].opcode, readable(decoded[0].fields)), (3704, {}))
        # GameMsg.lua:5353-5357 freezes the request to one field, so the codec
        # drops anything else the caller adds before it reaches the handler.
        extra, extra_tail = CODEC.decode_stream(CODEC.encode_frame(
            'PlayerProto:OneKeyMainTalentUpgrade', {**fields, 'uf': 'costNum'}))
        self.assertFalse(extra_tail)
        self.assertEqual(readable(extra[0].fields), fields)
        for malformed in ({}, {'infoArr': []}, {'infoArr': {}},
                          {'infoArr': [{'cid': cid}]},
                          {'infoArr': [{'cid': cid, 'skill_id': 4101101, 'lv': 5}]},
                          {'infoArr': [{'cid': cid, 'skill_id': 4101101},
                                       {'cid': cid, 'skill_id': 4101101}]},
                          {'infoArr': [{'cid': cid, 'skill_id': 4101101}], 'uf': 'costNum'},
                          {'infoArr': [{'cid': cid, 'skill_id': 4101105}]}):
            before = self.state()
            with self.assertRaises(StorageError):
                await HANDLERS['PlayerProto:OneKeyMainTalentUpgrade'](self.context, malformed)
            self.assertEqual(self.reopen(), before)
        # A card without cfgCardData coreItemId is excluded by the client
        # (CharacterCardsData.lua:1121-1123) and must never be upgraded.
        coreless = self.add_talent_fighter(91110, 4101101)
        await self.rejects_unchanged('PlayerProto:OneKeyMainTalentUpgrade',
                                     {'infoArr': [{'cid': coreless, 'skill_id': 4101101}]})
        # A talent already at the end of its chain cannot be batched.
        maxed = self.add_talent_fighter(10110, 4101105)
        await self.rejects_unchanged('PlayerProto:OneKeyMainTalentUpgrade',
                                     {'infoArr': [{'cid': maxed, 'skill_id': 4101105}]})

    async def test_one_key_talent_large_batch_answers_one_card_update_and_no_per_card_ret(self):
        # Live incident 2026-10-05 12:19:59: one request that selected 127
        # fighters was answered with 127 PlayerProto:MainTalentUpgradeRet frames
        # plus one CardUpdate. A >=120 fighter batch must now answer zero
        # per-card rets, exactly one CardUpdate carrying every changed fighter,
        # and exactly one OneKeyMainTalentUpgradeRet, with every frame under the
        # codec limit.
        batch = 120
        cids = [self.add_talent_fighter(10110, 4101101) for _ in range(batch)]
        with self.store.transaction(self.uid) as tx:
            tx.add_item(101011, 4 * batch)
        replies = await self.request('PlayerProto:OneKeyMainTalentUpgrade', {'infoArr': [
            {'cid': cid, 'skill_id': 4101101} for cid in cids]})
        state = self.reopen()
        skills = {card['cid']: list(card['skills']) for card in state['cards']}
        self.assertEqual([skills[cid] for cid in cids], [['4101105']] * batch)
        names = [reply.name for reply in replies]
        self.assertEqual(names.count('PlayerProto:MainTalentUpgradeRet'), 0)
        updates = [reply for reply in replies if reply.name == 'PlayerProto:CardUpdate']
        self.assertEqual(len(updates), 1)
        self.assertEqual([card['cid'] for card in updates[0].fields['cards']], cids)
        self.assertEqual(names.count('PlayerProto:OneKeyMainTalentUpgradeRet'), 1)
        self.assertEqual(names[-1], 'PlayerProto:OneKeyMainTalentUpgradeRet')
        data = next(reply for reply in replies if reply.name == 'PlayerProto:ItemUpdate').fields['data']
        self.assertEqual([(row['id'], row['add'], row['num']) for row in data],
                         [(101011, -4 * batch, 0)])
        sizes = [len(CODEC.encode_frame(reply.name, reply.fields)) for reply in replies]
        self.assertTrue(all(size < 65535 for size in sizes), max(sizes))
        self.assertLessEqual(max(sizes), CODEC.config.max_frame_size)

    def test_card_update_keeps_one_frame_and_splits_only_past_the_codec_limit(self):
        # 127 real fighters (live save uid 900000002) encode to 21184 B, so the
        # measured batch stays one frame. A payload that cannot fit must use the
        # existing chunking helper: server_core pre-encodes every reply, and an
        # oversized one closes the connection with close_reason='frame_encode_failed'.
        measured = [{'cid': cid, 'cfgid': 10110, 'level': 1,
                     'skills': {'4101101': {'id': 4101101, 'exp': 0}}}
                    for cid in range(1, 128)]
        single = handlers.cards_items.card_update_replies(self.context, measured, 0)
        self.assertEqual(len(single), 1)
        self.assertLess(len(CODEC.encode_frame(single[0].name, single[0].fields)),
                        CODEC.config.max_frame_size)
        # The Lua map encoder refuses 127 or more entries, so a card carries at
        # most 126 skills; that is already enough to push the batch past one frame.
        fat = [dict(card, skills={str(4000000 + n): {'id': 4000000 + n, 'exp': 0}
                                  for n in range(126)}) for card in measured]
        frames = handlers.cards_items.card_update_replies(self.context, fat, 0)
        self.assertGreater(len(frames), 1)
        self.assertEqual([card['cid'] for frame in frames for card in frame.fields['cards']],
                         [card['cid'] for card in fat])
        for frame in frames:
            self.assertLessEqual(len(CODEC.encode_frame(frame.name, frame.fields)),
                                 CODEC.config.max_frame_size)


if __name__ == '__main__':
    unittest.main()
