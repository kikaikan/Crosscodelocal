"""task-18：扭蛋币购买恢复（商店隐藏页）+ 无限购买（限购解除）。

用户诉求：「扭蛋币不是通用的，扭蛋币的那个购买恢复一下，变成无限购买就行」。
根因：非付费的道具池货币商品所在的商店页 isHide=1 且没有 enabled_pages 记录，
所以 page_open=False -> visible=False -> GetShopCommodity 不返回 -> buy 在
handlers/shop.py 的可见性校验处被拒。运行期事实：客户端会先发
ShopProto:GetShopOpenTime(2820)，未开启的页回 (0,1)，而 ShopPageData:IsOpen()
（ShopPageData.lua:114-125）只有 open_time/close_time 都为 0 才算开启，因此页级开口
也必须一起修。

识别规则（本地策略，无官服样本）：非付费（nonpayment）且奖励里含任一「配置道具池消耗
货币」的商品；货币集合从 07-server/data/item-pool-pools.json 的池 cost 行推导，不写死 id：
{10402, 10406, 10408, 10412, 10420, 10421, 10422}（costtype=2 的行的道具在 cost[index][2]，
ItemPoolInfo.lua:220）。识别出的 5 个商品：
91526 扭蛋币(10408) / 90632 扭蛋币(10408) / 1400102 幸运凭证(10422) /
31068 幸运扭蛋币I(10408) / 80208 万物贸易所(10406)。
付费行（jCosts 首项 -1 = 人民币列）继续被 nonpayment() 排除：
31060/31061/31066/31067/1400101。
"""
import asyncio
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from database import Store, StorageError
from server_core import Context
from handlers import shop
from protocol_codec import IVProtoCodec, WireConfig

# 非付费且产出道具池货币的商品（本测试的主对象）。
POOL_COINS = (91526, 90632, 1400102)
# 付费行：jCosts 首项为 -1（人民币价格列），项目铁律不含支付，必须保持不可见/不可买。
PAID = (31060, 31061, 31066, 31067, 1400101)
PAGES = {91526: 916, 90632: 906, 1400102: 14001}
# 02-tools/scripts/protocol_codec.py:42 的 codec 默认上限。
MAX_FRAME = 32767


def stamp(value):
    return int(datetime.fromisoformat(value).timestamp())


class ShopPoolCoinTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=HERE / 'tests', prefix='shop-coins-')
        self.store = Store(Path(self.temp.name) / 'state.sqlite3')
        seed = json.loads((HERE / 'data/new_account_seed.json').read_text('utf-8'))
        self.uid = self.store.create_account('shop-pool-coins', seed)['uid']
        self.codec = IVProtoCodec(json.loads((ROOT / '05-protocol/endpoints.json').read_text('utf-8')),
                                  WireConfig('little'))
        self.ctx = Context(SimpleNamespace(store=self.store, codec=self.codec), 'game', self.uid, True)
        self.clock = stamp('2026-10-04T14:00:00+08:00')
        with self.store.transaction(self.uid) as tx:
            tx.state['offline_clock'] = self.clock
            tx.state['player']['create_time'] = stamp('2026-10-04T10:00:00+08:00')
            tx.state['progress']['cleared_stages'] = [1002]
            tx.add_item(10112, 1000)     # 91526 的消耗：优品百货袋 x140
            tx.add_item(10101, 1000)     # 90632 的消耗：宇宙口香糖 x140
            tx.add_item(10002, 100000)   # 1400102 的消耗：粲晶 x100

    def tearDown(self):
        self.store.close()
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to((HERE / 'tests').resolve()))
        self.temp.cleanup()

    def state(self):
        return self.store.get_player(self.uid)

    def cfg(self, identifier):
        return shop.commodities()[identifier]

    def wire(self, replies):
        for reply in replies:
            raw = self.codec.encode_frame(reply.name, reply.fields)
            self.assertLess(len(raw), MAX_FRAME)
            decoded = self.codec.decode_frame(raw)
            self.assertEqual(self.codec.encode_frame(decoded.name, decoded.fields), raw)
        return replies

    async def buy(self, identifier, quantity, when=None):
        return self.wire(await shop.buy(self.ctx, {
            'id': identifier, 'buy_sum': quantity,
            'buy_time': self.clock if when is None else when}))

    async def rejects(self, identifier, quantity, when=None):
        before = self.state()
        with self.assertRaises(StorageError):
            await self.buy(identifier, quantity, when)
        self.assertEqual(self.state(), before)

    async def pages(self):
        replies = self.wire(await shop.get_open_time(self.ctx, {}))
        return {row['shop_id']: row for row in replies[0].fields['infos'] if 'group_id' not in row}
    async def test_pool_coin_pages_report_open_and_their_rows_are_visible(self):
        # 修复前：这些页 isHide=1 且 enabled_pages 为空 -> 回 (0,1)，客户端视为未开启。
        pages = await self.pages()
        for identifier in (916, 906, 14001):
            with self.subTest(page=identifier):
                self.assertEqual(pages[identifier]['close_time'], 0)
                self.assertEqual(pages[identifier]['open_time'], 0)
        self.assertEqual(pages[3]['close_time'], 0)
        state = self.state()
        for identifier in (91526, 90632, 1400102, 31068):
            with self.subTest(commodity=identifier):
                self.assertTrue(shop.visible(state, self.cfg(identifier)))
                self.assertTrue(shop.can_purchase(state, self.cfg(identifier)))

    async def test_gift_tab_returns_the_coin_row_with_unlimited_fields(self):
        # 用户在「礼包」页找扭蛋币：group=3 / tabID=3003。
        replies = await shop.get_commodity(self.ctx, {'shop_id': 3, 'group_id': 3003})
        rows = [info for reply in replies for info in reply.fields['infos']]
        row = next((info for info in rows if info['id'] == 31068), None)
        self.assertIsNotNone(row, '幸运扭蛋币I 必须出现在 GetShopCommodity(3, 3003)')
        self.assertEqual((row['shop_id'], row['group_id']), (3, 3003))
        # 客户端 CommodityData:GetNowTimeCanBuy 用 open_time/close_time 判定是否灰掉；
        # 0/0 表示常开，且不再显示已过期的倒计时。
        self.assertEqual((row['open_time'], row['close_time']), (0, 0))
        self.assertEqual(row['can_buy_cnt'], -1)
        self.assertEqual(row['cnt'], 3)
        self.assertEqual(row['shop_config']['nOnecBuyLimit'], 99)
        self.assertEqual(row['shop_config']['jGets'], [[10408, 1, 2]])
        self.assertEqual(row['shop_config']['jCosts'], [[10002, 120]])
        self.assertEqual(row['shop_config']['isShow'], 0)

    async def test_pool_coin_commodities_ignore_their_original_purchase_limits(self):
        expected = {91526: (4, 5, 10112), 90632: (4, 5, 10101), 31068: (3, 5, 10002)}
        for identifier, (original, purchases, item) in expected.items():
            with self.subTest(commodity=identifier):
                self.assertEqual(int(self.cfg(identifier)['nSumBuyLimit']), original)
                before = self.state()['inventory'].get(str(item), 0)
                for index in range(purchases):
                    replies = await self.buy(identifier, 1, self.clock + index)
                    self.assertEqual(replies[-1].name, 'ShopProto:BuyRet')
                    self.assertEqual(replies[-1].fields['info']['can_buy_cnt'], -1)
                state = self.state()
                self.assertEqual(state['shop']['purchases'][str(identifier)]['buy_sum'], purchases)
                self.assertGreater(purchases, original)
                self.assertEqual(shop.commodity_info(state, self.cfg(identifier))['can_buy_cnt'], -1)
                self.assertLess(state['inventory'].get(str(item), 0), before)
        # 1400102 幸运凭证一次买满原上限 34 再继续买，同样不受限。
        self.assertEqual(int(self.cfg(1400102)['nSumBuyLimit']), 34)
        replies = await self.buy(1400102, 35, self.clock + 40)
        state = self.state()
        self.assertEqual(state['shop']['purchases']['1400102']['buy_sum'], 35)
        self.assertEqual(state['inventory'].get('10422', 0), 35)
        self.assertEqual(replies[-1].fields['info']['can_buy_cnt'], -1)
        # 到账：91526/90632/31068 每份奖励都是 10408 x1，共 3 x 5 = 15。
        self.assertEqual(self.state()['inventory'].get('10408', 0), 5 * 3)

    async def test_paid_rows_stay_invisible_and_unbuyable(self):
        state = self.state()
        for identifier in PAID:
            with self.subTest(commodity=identifier):
                cfg = self.cfg(identifier)
                self.assertEqual(int(cfg['jCosts'][0][0]), -1)  # 人民币价格列
                self.assertFalse(shop.nonpayment(cfg))
                self.assertFalse(shop.visible(state, cfg))
                await self.rejects(identifier, 1)
        # 放宽购买档期不得让付费行露出：它们仍全部被 nonpayment() 挡住。
        self.assertEqual(sorted(self.cfg(identifier)['id'] for identifier in PAID
                              if shop.pool_coin(self.cfg(identifier))), [])
    async def test_ordinary_commodities_keep_their_original_limits_and_pages(self):
        state = self.state()
        # 13004 在隐藏页 2001 上：本来不可见，本次改动不得让它出现，限购值也不能被改写。
        self.assertFalse(shop.page_open(state, 2001))
        self.assertFalse(shop.visible(state, self.cfg(13004)))
        self.assertEqual(shop.commodity_info(state, self.cfg(13004))['can_buy_cnt'], 5)
        await self.rejects(13004, 1)
        # 31051 是正常可见的免费礼包，nSumBuyLimit=1：买一次后剩余 0，第二次必须被拒且不改档。
        self.assertTrue(shop.visible(state, self.cfg(31051)))
        self.assertEqual(shop.commodity_info(state, self.cfg(31051))['can_buy_cnt'], 1)
        await self.buy(31051, 1, self.clock + 10)
        self.assertEqual(shop.commodity_info(self.state(), self.cfg(31051))['can_buy_cnt'], 0)
        await self.rejects(31051, 1, self.clock + 11)
        # 其它页的判定语义不变：未解锁的 904 仍是 closed，没配 enabled_pages 的 901/2001/2002 仍是隐藏。
        self.assertEqual(shop.page_gate(state, 904), shop.CLOSED)
        for identifier in (901, 2001, 2002):
            self.assertEqual(shop.page_gate(state, identifier), shop.HIDDEN)
            self.assertFalse(shop.page_open(state, identifier))

    async def test_legacy_unlock_flag_still_cannot_open_unrelated_hidden_pages(self):
        with self.store.transaction(self.uid) as tx:
            tx.state['offline_unlock_all'] = True
        state = self.state()
        self.assertFalse(shop.page_open(state, 2001))
        self.assertFalse(shop.visible(state, self.cfg(13004)))
        await self.rejects(13004, 1)

    def test_currency_set_is_derived_from_the_item_pool_catalog(self):
        catalog = json.loads((HERE / 'data' / 'item-pool-pools.json').read_text('utf-8'))
        derived = set()
        for pool in catalog['pools'].values():
            # costtype 2 的行是 [index, item, num]（ItemPoolInfo.lua:220 读 cost[index][2]），
            # 其余是 [item, num]；货币集合完全由配置推导，不写死 id。
            column = 1 if int(pool['costtype']) == 2 else 0
            derived.update(int(row[column]) for row in pool['cost'])
        self.assertEqual(set(shop.pool_currency_items()), derived)
        self.assertEqual(sorted(derived), [10402, 10406, 10408, 10412, 10420, 10421, 10422])
        identified = sorted(identifier for identifier, cfg in shop.commodities().items()
                            if shop.pool_coin(cfg))
        # 命中 5 行：任务点名的 4 行，加上 80208（万物贸易所，10406 x1 换粲晶 x380）——
        # 10406 正是池 1003（幸运扭蛋/Once）的消耗货币，同一规则必须覆盖它，不能漏。
        self.assertEqual(identified, [31068, 80208, 90632, 91526, 1400102])
        for identifier in (91526, 90632, 1400102, 31068):
            self.assertIn(identifier, identified)
        for identifier in PAID:
            self.assertNotIn(identifier, identified)

    async def test_only_pool_coin_rows_become_visible(self):
        # 爆破半径守卫：本地策略只新增这 5 行，不隐藏任何原有行。
        state = self.state()
        after = {identifier for identifier, cfg in shop.commodities().items() if shop.visible(state, cfg)}
        with patch.object(shop, 'pool_coin', lambda cfg: False), \
                patch.object(shop, 'pool_coin_commodity', lambda identifier: False), \
                patch.object(shop, 'pool_coin_page_open', lambda state, page_id: False):
            before = {identifier for identifier, cfg in shop.commodities().items() if shop.visible(state, cfg)}
            # 关掉策略后，这 5 行必须回到「不可见」，证明改动确实是它们的唯一原因。
            for identifier in (91526, 90632, 1400102, 31068, 80208):
                with self.subTest(commodity=identifier):
                    self.assertNotIn(identifier, before)
        self.assertEqual(after - before, {91526, 90632, 1400102, 31068, 80208})
        self.assertEqual(before - after, set())
        self.assertGreater(len(before), 400)

    async def test_every_reply_frame_stays_below_the_codec_limit(self):
        # 全量回包（含 1373 个商品）也必须低于 02-tools/scripts/protocol_codec.py:42 的 32767。
        for replies in (await shop.get_open_time(self.ctx, {}),
                        await shop.get_commodity(self.ctx, {}),
                        await shop.get_infos(self.ctx, {})):
            self.wire(replies)
