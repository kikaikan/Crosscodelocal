"""Non-payment costs/stock, real free grants, persistent refresh and rollback."""
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
from handlers import shop
from protocol_codec import IVProtoCodec, WireConfig


def stamp(value):
    return int(datetime.fromisoformat(value).timestamp())


class ShopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'players.sqlite3'
        self.store = Store(self.path)
        seed = json.loads((HERE / 'data/new_account_seed.json').read_text('utf-8'))
        self.uid = self.store.create_account('fresh-local-nonpayment-shop', seed)['uid']
        self.ctx = Context(SimpleNamespace(store=self.store), 'game', self.uid, True)
        self.clock('2026-10-04T14:00:00+08:00')
        self.codec = IVProtoCodec(json.loads((HERE.parent / '05-protocol/endpoints.json').read_text('utf-8')),
                                  WireConfig('little', max_frame_size=65535))
        self.ctx.server.codec = self.codec

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def state(self):
        return self.store.get_player(self.uid)

    def clock(self, value):
        with self.store.transaction(self.uid) as tx:
            tx.state['offline_clock'] = stamp(value)
            tx.state['player']['create_time'] = stamp('2026-10-04T10:00:00+08:00')
            tx.state['progress']['cleared_stages'] = [1002]

    def wire(self, replies):
        for reply in replies:
            raw = self.codec.encode_frame(reply.name, reply.fields)
            decoded = self.codec.decode_frame(raw)
            self.assertEqual(raw, self.codec.encode_frame(decoded.name, decoded.fields))

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.ctx.server.store = self.store

    async def rejects_unchanged(self, function, fields):
        before = self.state()
        with self.assertRaises(StorageError):
            await function(self.ctx, fields)
        self.assertEqual(self.state(), before)

    def all_access(self):
        with self.store.transaction(self.uid) as tx:
            tx.state['offline_unlock_all'] = True
            tx.state['progress']['cleared_stages'] = []

    async def test_legacy_all_access_cannot_open_ordinary_pages(self):
        self.all_access()
        before = self.state()
        pages = (await shop.get_open_time(self.ctx, {}))[0].fields['infos']
        for identifier in (1, 3, 102, 901, 904, 5):
            page=next(row for row in pages if row['shop_id']==identifier and 'group_id' not in row)
            self.assertEqual(page['close_time'],1)
        self.assertEqual(self.state(),before)

    async def test_legacy_all_access_does_not_bypass_expired_goods(self):
        self.all_access()
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['cleared_stages']=[1002]
        await self.rejects_unchanged(shop.buy, {'id':80001,'buy_time':self.state()['offline_clock']})

    async def test_legacy_all_access_does_not_bypass_goods_level_or_stage(self):
        self.all_access()
        for identifier in (30003,80314):
            await self.rejects_unchanged(shop.buy, {'id':identifier,'buy_time':self.state()['offline_clock']})
        self.assertFalse(shop.limit_pass(self.state(),6,1))
        self.assertFalse(shop.limit_pass(self.state(),7,1))

    async def test_legacy_all_access_does_not_open_hidden_or_arena_shop(self):
        self.all_access()
        await self.rejects_unchanged(shop.buy, {'id':90116,'buy_time':self.state()['offline_clock']})
        await self.rejects_unchanged(shop.get_exchange, {'cfgid':80002})

    async def test_legacy_all_access_keeps_random_reward_level_and_stage_filters(self):
        rows = [row for cfg in shop.catalog('cfgRewardInfo.lua').values()
                if isinstance(cfg.get('item'), list) for row in cfg['item']
                if isinstance(row, dict) and row.get('price') and (row.get('level') or row.get('dupID'))]
        self.assertGreater(len(rows), 0)
        normal = self.state()
        normal['progress']['cleared_stages'] = []
        normal['player']['level'] = 1
        gated = [row for row in rows if not shop.eligible_reward(normal, row)]
        self.assertGreater(len(gated), 0)
        opened = deepcopy(normal)
        opened['offline_unlock_all'] = True
        for row in gated:
            original = deepcopy(row)
            self.assertFalse(shop.eligible_reward(opened, row))
            self.assertEqual(row, original)  # No rewritten price, count, weight or stock.

    async def test_catalogs_wire_quotes_and_paid_pages_closed(self):
        rows = await shop.get_commodity(self.ctx, {})
        self.wire(rows)
        infos = [info for reply in rows for info in reply.fields['infos']]
        self.assertGreater(len(infos), 100)
        self.assertTrue(all(info["shop_config"]["isShow"] == 0 for info in infos))
        # Source CommodityData.IsShow treats integer1 as hidden, not visible.
        self.assertTrue(rows[-1].fields['is_finish'])
        self.assertTrue(all(shop.nonpayment(shop.commodities()[row['id']]) for row in infos))
        for function in (shop.get_open_time, shop.get_infos, shop.get_reset_time):
            self.wire(await function(self.ctx, {}))
        pages = (await shop.get_open_time(self.ctx, {}))[0].fields['infos']
        paid = next(row for row in pages if row['shop_id'] == 2 and 'group_id' not in row)
        self.assertEqual(paid['close_time'], 1)
        all_ids = [row['id'] for row in infos]
        self.assertNotIn(810001, all_ids)  # jCosts -1 invokes SDK payment in source
        self.assertNotIn(80001, all_ids)   # September trade expired onOctober1

    async def test_source_free_daily_grant_and_sqlite_retry(self):
        result = await shop.buy(self.ctx, {'id': 30002, 'buy_sum': 1})
        self.wire(result)
        self.assertEqual(self.state()['inventory']['58005'], 1)
        self.assertEqual(self.state()['shop']['purchases']['30002']['buy_sum'], 1)
        self.assertEqual(result[-1].fields['info']['can_buy_cnt'], 0)
        self.assertEqual(self.state()['player']['gold'], 1000)
        after = self.state()
        self.reopen()
        retry = await shop.buy(self.ctx, {'id': 30002, 'buy_sum': 1})
        self.assertEqual(retry[-1].fields['gets'], [])
        self.assertEqual(self.state(), after)
        self.assertEqual(after['task_state']['stats']['daily']['shop_exchange']['total'], 1)

    async def test_free_stock_source_midnight_cycle(self):
        await shop.buy(self.ctx, {'id': 30002})
        self.clock('2026-10-04T23:59:59+08:00')
        self.assertEqual((await shop.buy(self.ctx, {'id': 30002}))[-1].fields['gets'], [])
        self.clock('2026-10-05T00:00:00+08:00')
        result = await shop.buy(self.ctx, {'id': 30002})
        self.wire(result)
        self.assertEqual(self.state()['inventory']['58005'], 2)
        self.assertEqual(result[-1].fields['info']['reset_time'], stamp('2026-10-06T00:00:00+08:00'))

    async def test_paid_locked_expired_and_bad_counts_cannot_grant(self):
        for fields in ({'id': 810001}, {'id': 30003}, {'id': 80001}, {'id': 9999999},
                       {'id': 30002, 'buy_sum': 2}, {'id': 30002, 'buy_sum': True},
                       {'id': 30002, 'vouchers': [{'id': 10002, 'num': 0}]}):
            await self.rejects_unchanged(shop.buy, fields)
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['cleared_stages'] = []
        await self.rejects_unchanged(shop.buy, {'id': 30002})
        reply = (await shop.get_commodity(self.ctx, {}))[-1]
        self.assertEqual(reply.fields['infos'], [])

    async def test_real_monthly_resource_cost_stock_and_timestamp_receipt(self):
        cfg = shop.commodities()[80012]
        with self.store.transaction(self.uid) as tx:
            tx.add_item(10033, 100)
        fields = {'id': 80012, 'buy_sum': 3, 'buy_time': self.state()['offline_clock'], 'useJCost': 'jCosts'}
        result = await shop.buy(self.ctx, fields)
        self.wire(result)
        self.assertEqual(self.state()['inventory']['10033'], 100 - 3 * cfg['jCosts'][0][1])
        self.assertEqual(self.state()['inventory']['14204'], 3)
        self.assertEqual(result[-1].fields['info']['can_buy_cnt'], cfg['nSumBuyLimit'] - 3)
        after = self.state()
        self.assertEqual((await shop.buy(self.ctx, fields))[-1].fields['gets'], [])
        self.assertEqual(self.state(), after)
        fields['buy_time'] += 1
        await shop.buy(self.ctx, fields)
        self.assertEqual(self.state()['inventory']['14204'], 6)
        self.clock('2026-11-01T00:00:00+08:00')
        rows = await shop.get_commodity(self.ctx, {'shop_id': 102})
        item = next(row for result in rows for row in result.fields['infos'] if row['id'] == 80012)
        self.assertEqual(item['can_buy_cnt'], cfg['nSumBuyLimit'])

    async def test_cost_insufficiency_and_reward_overflow_atomic(self):
        await self.rejects_unchanged(shop.buy, {'id': 80012, 'buy_time': self.state()['offline_clock']})
        with self.store.transaction(self.uid) as tx:
            tx.add_item(10033, 100)
            tx.add_item(14204, 2147483647)
        await self.rejects_unchanged(shop.buy, {'id': 80012, 'buy_time': self.state()['offline_clock']})

    async def test_no_arbitrary_cost_and_no_backdated_new_purchase(self):
        with self.store.transaction(self.uid) as tx:
            tx.add_item(10033, 100)
        now = self.state()['offline_clock']
        for fields in ({'id': 80012}, {'id': 80012, 'buy_time': now - 121},
                       {'id': 80012, 'buy_time': now, 'useJCost': 'zero'},
                       {'id': 80012, 'buy_time': now, 'useJCost': 'jCosts1'},
                       {'id': 80012, 'buy_time': now, 'useCost': 'free'}):
            await self.rejects_unchanged(shop.buy, fields)

    async def test_random_stock_persists_reopen_and_paid_manual_refresh(self):
        first = await shop.get_exchange(self.ctx, {'cfgid': 80001})
        self.wire(first)
        stock = deepcopy(first[-1].fields)
        self.assertEqual(len(stock['infos']), 10)
        self.reopen()
        self.assertEqual((await shop.get_exchange(self.ctx, {'cfgid': 80001}))[-1].fields, stock)
        before = self.state()
        refreshed = await shop.get_exchange(self.ctx, {'cfgid': 80001, 'is_flush': True})
        self.wire(refreshed)
        cost = shop.catalog('cfgCfgRandCommodity.lua')[80001]['aManFlushCosts'][0][1]
        self.assertEqual(self.state()['player']['diamond'], before['player']['diamond'] - cost)
        self.assertEqual(self.state()['shop']['random']['80001']['refresh_count'], 1)
        await self.rejects_unchanged(shop.get_exchange, {'cfgid': 80001, 'is_flush': True})

    async def test_actual_random_trade_debits_price_and_stock_limits(self):
        result = await shop.get_exchange(self.ctx, {'cfgid': 80001})
        item = result[-1].fields['infos'][0]
        with self.store.transaction(self.uid) as tx:
            tx.add_item(item['price'][0], item['price'][1] * 2)
        before = self.state()
        fields = {'cfgid': 80001, 'index': 1, 'id': item['id'], 'num': 1}
        reply = await shop.exchange(self.ctx, fields)
        self.wire(reply)
        self.assertEqual(self.state()['inventory'][str(item['id'])], before['inventory'].get(str(item['id']), 0) + item['num'])
        self.assertEqual(reply[-1].fields['had_get'], 1)
        await self.rejects_unchanged(shop.exchange, fields)
        await self.rejects_unchanged(shop.exchange, dict(fields, id=9999))

    async def test_random_clock_restock_and_never_manual_arena_free_refresh(self):
        await shop.get_exchange(self.ctx, {'cfgid': 80001})
        self.clock('2026-10-04T17:59:59+08:00')
        current = await shop.get_exchange(self.ctx, {'cfgid': 80001})
        self.assertEqual(current[-1].fields['next_hour'], stamp('2026-10-04T18:00:00+08:00'))
        self.clock('2026-10-04T18:00:00+08:00')
        new = await shop.get_exchange(self.ctx, {'cfgid': 80001})
        self.wire(new)
        self.assertEqual(new[-1].fields['next_hour'], stamp('2026-10-05T06:00:00+08:00'))
        self.assertTrue(all(row['had_get'] == 0 for row in new[-1].fields['infos']))
        await self.rejects_unchanged(shop.get_exchange, {'cfgid': 80002})
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['cleared_stages'].append(1114)
        self.wire(await shop.get_exchange(self.ctx, {'cfgid': 80002}))
        await self.rejects_unchanged(shop.get_exchange, {'cfgid': 80002, 'is_flush': True})

    async def test_fuel_reward_updates_player_not_ordinary_inventory(self):
        with self.store.transaction(self.uid) as tx:
            rendered, replies = shop.award(tx, [{'id': 10035, 'num': 120, 'type': 2}])
        self.wire(replies)
        self.assertEqual(self.state()['player']['hot'], 202)
        self.assertNotIn('10035', self.state()['inventory'])
        self.assertEqual(rendered, [{'id': 10035, 'num': 120, 'type': 2}])

    async def test_source_fuel_cost_debits_authority_and_failure_rolls_back(self):
        source = shop.commodities()[90629]['jCosts']
        self.assertEqual(source, [[10035, 30]])
        with self.store.transaction(self.uid) as tx:
            replies = shop.debit(tx, {item: cost for item, cost in source})
        self.wire(replies)
        self.assertEqual(self.state()['player']['hot'], 52)
        self.assertNotIn('10035', self.state()['inventory'])
        self.assertEqual(replies[-1].fields['infos']['hot'], 52)
        before = self.state()
        for costs in ({10035: 53}, {10035: 30, 10002: 100000}):
            with self.assertRaises(StorageError):
                with self.store.transaction(self.uid) as tx:
                    shop.debit(tx, costs)
            self.assertEqual(self.state(), before)

    async def test_all_registered_shop_requests_require_local_login(self):
        self.ctx.logged_in = False
        for function, fields in ((shop.get_open_time, {}), (shop.get_commodity, {}), (shop.get_infos, {}),
                                 (shop.get_reset_time, {}), (shop.buy, {'id': 30002}),
                                 (shop.get_exchange, {'cfgid': 80001}),
                                 (shop.exchange, {'cfgid': 80001, 'index': 1, 'id': 10001, 'num': 1})):
            with self.assertRaises(StorageError):
                await function(self.ctx, fields)


if __name__ == '__main__':
    unittest.main()
