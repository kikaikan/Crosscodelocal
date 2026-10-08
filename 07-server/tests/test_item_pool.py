"""task-11：道具池/扭蛋 ItemPoolInfo + ItemPoolDraw 的线格式、抽取语义与拒绝策略。

权威：05-protocol/endpoints.json(4206-4209)、GameMsg.lua:5448-5472、
cfgCfgItemPool*.lua、ItemPoolInfo.lua、ItemPoolActivityMgr.lua、LuckyGachaMain.lua。
修复前运行期证据：07-server/logs/server.jsonl 里 opcode 4206 的 response 恒为 8 字节
（{"info": []}），客户端 LuckyGachaMain.lua:35-39 因此 LogError 并放弃打开界面。
"""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import secrets
import socket
import struct
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
from database import Store, StorageError
import error_policy
# server_core puts 02-tools/scripts on sys.path, so it must precede protocol_codec.
from server_core import Context, LocalServer, load_dependencies
from protocol_codec import encode_packet, readable
import handlers.item_pool as item_pool

CODEC, SEED = load_dependencies(ROOT / '05-protocol' / 'endpoints.json',
                                SERVER / 'data' / 'new_account_seed.json')
INFO, DRAW = item_pool.INFO_REQUEST, item_pool.DRAW_REQUEST
# 02-tools/scripts/protocol_codec.py:42 的 codec 默认上限：超过就 frame_encode_failed 断连。
MAX_FRAME = 32767


def free_port():
    for _ in range(100):
        port = 10000 + secrets.randbelow(22000)
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', port))
                return port
            except OSError:
                pass
    raise RuntimeError('No local test port available in protocol signed-short range')


class ItemPoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SERVER / 'tests', prefix='item-pool-')
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.store = Store(self.path)
        self.uid = self.store.create_account('item-pool-local', SEED)['uid']
        self.server = LocalServer(CODEC, self.store, SEED)
        self.ctx = Context(self.server, 'game', self.uid, True)
        with self.store.transaction(self.uid) as tx:
            item_pool.pools_state(tx.state)['rng_seed'] = 'ab' * 32

    async def asyncTearDown(self):
        self.store.close()
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to((SERVER / 'tests').resolve()))
        self.temp.cleanup()

    def state(self):
        return self.store.get_player(self.uid)

    def revision(self):
        return self.store.connection.execute(
            'SELECT revision FROM accounts WHERE uid=?', (self.uid,)).fetchone()[0]

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.server.store = self.store
        return self.store.get_player(self.uid)

    def fund(self, cfgid, amount=10000):
        with self.store.transaction(self.uid) as tx:
            tx.add_item(cfgid, amount)

    def stash(self, pool_id, indexes):
        """Persist drawArr bookkeeping directly, to reach deep pool states."""
        with self.store.transaction(self.uid) as tx:
            data = item_pool.pools_state(tx.state)
            entry = item_pool.pool_entry(data, int(pool_id))
            for index, count in indexes.items():
                entry['drawArr'][str(int(index))] = int(count)

    async def request(self, name, fields):
        # Every request and reply crosses the real IVProto codec: numeric drawArr keys
        # must survive as a Lua table literal and every frame must stay under the limit.
        frames, tail = CODEC.decode_stream(CODEC.encode_frame(name, fields))
        self.assertFalse(tail)
        replies = await self.server.dispatch(self.ctx, frames[0])
        for reply in replies:
            inner = CODEC.encode_frame(reply.name, reply.fields)
            self.assertLess(len(inner), MAX_FRAME)
            decoded = CODEC.decode_frame(inner)
            self.assertEqual(CODEC.encode_frame(decoded.name, decoded.fields), inner)
        return replies

    def decoded(self, reply):
        return readable(CODEC.decode_frame(CODEC.encode_frame(reply.name, reply.fields)).fields)

    async def rejects(self, name, fields):
        """A business rejection must keep the save byte-identical and roll back."""
        before, revision = deepcopy(self.state()), self.revision()
        with self.assertRaises(StorageError) as raised:
            await self.request(name, fields)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.revision(), revision)
        return raised.exception

    async def test_info_without_id_returns_every_pool_the_config_defines(self):
        # 修复前这里恒为 {"info": []}（8 字节空壳），客户端因此拿不到任何池。
        before, revision = deepcopy(self.state()), self.revision()
        replies = await self.request(INFO, {})
        self.assertEqual([reply.name for reply in replies], [item_pool.INFO_REPLY])
        rows = replies[0].fields['info']
        configured = sorted(int(key) for key in item_pool.POOLS)
        self.assertEqual([row['id'] for row in rows], configured)
        self.assertEqual(len(rows), 10)
        for row in rows:
            self.assertEqual(row['round'], 1)
            self.assertEqual(row['drawTimes'], 0)
            self.assertEqual(row['drawArr'], {})
            self.assertGreater(row['startTime'], 0)
        # 用户要求“扭蛋启用全部”：六个幸运扭蛋(type=2) 与回归/奇趣/皮肤池都在列表里。
        types = {int(key): int(pool['type']) for key, pool in item_pool.POOLS.items()}
        self.assertEqual(sorted(key for key, kind in types.items() if kind == 2),
                         [1003, 1004, 1005, 1006, 1008, 1010])
        self.assertIn(1001, configured)
        self.assertIn(1005, configured)
        self.assertIn(1009, configured)
        # 空壳是 8 字节；全量列表必须远大于它，但仍低于 codec 上限。
        self.assertGreater(len(CODEC.encode_frame(replies[0].name, replies[0].fields)), 8)
        # 纯读取不写存档：状态与 revision 都不变。
        self.assertEqual(self.state(), before)
        self.assertEqual(self.revision(), revision)

    async def test_single_pool_request_returns_only_that_pool(self):
        for identifier in (1004, 1009, 1001):
            rows = (await self.request(INFO, {'id': identifier}))[0].fields['info']
            self.assertEqual([row['id'] for row in rows], [identifier])
        # id=0 与省略 id 等价（客户端 nil 时字段根本不出现）。
        self.assertEqual(len((await self.request(INFO, {'id': 0}))[0].fields['info']), 10)

    async def test_draw_costs_the_configured_item_and_records_drawarr_on_the_wire(self):
        self.fund(10408, 5)
        replies = await self.request(DRAW, {'id': 1004, 'times': 2})
        self.assertEqual(replies[-1].name, item_pool.DRAW_REPLY)
        result = replies[-1].fields
        group = item_pool.GROUPS[str(int(item_pool.POOLS['1004']['group']))]
        round_one = {int(row['index']) for row in group['pool'] if 1 in [int(r) for r in row['rounds']]}
        self.assertEqual(result['drawRound'], 1)
        self.assertEqual(len(result['drawArr']), 2)
        self.assertTrue(set(result['drawArr']) <= round_one)
        self.assertEqual(result['info']['id'], 1004)
        self.assertEqual(result['info']['drawTimes'], 2)
        state = self.reopen()
        entry = state['item_pools']['pools']['1004']
        self.assertEqual(entry['drawTimes'], 2)
        self.assertEqual(sum(entry['drawArr'].values()), 2)
        self.assertEqual(sorted(int(key) for key in entry['drawArr']), sorted(result['drawArr']))
        self.assertEqual(state['inventory'].get('10408'), 3)
        # 抽中的奖励物品真的进包（ItemPoolInfo.lua 只支持物品类型）。
        for index in result['drawArr']:
            row = next(row for row in group['pool'] if int(row['index']) == index)
            item, amount = int(row['reward'][0]), int(row['reward'][1])
            self.assertGreaterEqual(state['inventory'].get(str(item), 0), amount)
        updates = [reply.fields['data'] for reply in replies if reply.name == 'PlayerProto:ItemUpdate']
        self.assertEqual(len(updates), 1)
        delta = {row['id']: (row['add'], row['num']) for row in updates[0]}
        self.assertEqual(delta[10408], (-2, 3))
        # 线格式：drawArr 作为 Lua 表字面量解码回来仍是数字键映射（客户端 loadstring 路径）。
        wire = self.decoded([reply for reply in replies if reply.name == item_pool.DRAW_REPLY][0])
        self.assertEqual({int(key): int(value) for key, value in wire['info']['drawArr'].items()},
                         {int(key): int(value) for key, value in entry['drawArr'].items()})

    async def test_rejections_are_business_tips_mapped_and_leave_the_save_untouched(self):
        error = await self.rejects(DRAW, {'id': 1004, 'times': 1})
        self.assertIn('消耗', str(error))
        disposition, reason = error_policy.classify(error)
        self.assertEqual((disposition, reason), ('continue', 'business_rejected'))
        name, tip = error_policy.tips_fields(DRAW, CODEC.schemas[DRAW]['opcode'],
                                             error_policy.client_message(error, DRAW))
        self.assertEqual(name, 'SystemProto:Tips')
        self.assertEqual(tip['strId'], 'GeneralTips')
        self.assertEqual(tip['opId'], 4208)
        self.assertEqual(tip['opName'], DRAW)
        self.assertEqual(tip['args'][0]['type'], 0)
        CODEC.encode_frame(name, tip)
        # 单次次数上限来自 cfgCfgItemPool.maxcostnum(1004)=5；未知池与非法次数同样拒绝。
        for times in (0, 6, 65535):
            await self.rejects(DRAW, {'id': 1004, 'times': times})
        await self.rejects(DRAW, {'id': 9999, 'times': 1})
        await self.rejects(INFO, {'id': 9999})
        # 直连 handler 才能带进 bool（uint 字段解码后一定是 int）。
        before = deepcopy(self.state())
        with self.assertRaises(StorageError):
            await item_pool.item_pool_info(self.ctx, {'id': True})
        self.assertEqual(self.state(), before)

    async def test_once_pool_draws_every_configured_row_then_rejects_and_rolls_back(self):
        # 1003：extracttype=3(Once)、group 2 共 6 行、每行 rewardnum=1、costtype=2 逐次计费。
        self.fund(10406, 10)
        for _ in range(6):
            result = (await self.request(DRAW, {'id': 1003, 'times': 1}))[-1].fields
            self.assertEqual(result['drawRound'], 1)
        state = self.state()
        entry = state['item_pools']['pools']['1003']
        self.assertEqual(entry['drawTimes'], 6)
        self.assertEqual(sorted(entry['drawArr'].values()), [1] * 6)
        self.assertEqual(state['inventory'].get('10406'), 4)
        # 全部有限奖励抽完后必须拒绝（业务拒绝），且这次扣费被回滚。
        error = await self.rejects(DRAW, {'id': 1003, 'times': 1})
        self.assertIn('抽完', str(error))
        self.assertEqual(self.state()['inventory'].get('10406'), 4)

    async def test_next_round_waits_for_key_rewards_then_advances_and_draws_next_round(self):
        # 1001：extracttype=1(RoundLoop)，group 1 有 4 轮，第 1 轮还有 4 个关键奖励。
        error = await self.rejects(INFO, {'id': 1001, 'nextRound': True})
        self.assertIn('关键奖励', str(error))
        group = item_pool.GROUPS[str(int(item_pool.POOLS['1001']['group']))]
        keys = {int(row['index']): int(row.get('rewardnum', 1)) for row in group['pool']
                if 1 in [int(r) for r in row['rounds']] and row.get('iskeyreward')}
        self.assertTrue(keys)
        self.stash(1001, keys)
        rows = (await self.request(INFO, {'id': 1001, 'nextRound': True}))[0].fields['info']
        self.assertEqual(rows[0]['id'], 1001)
        self.assertEqual(rows[0]['round'], 2)
        self.assertEqual(self.reopen()['item_pools']['pools']['1001']['round'], 2)
        # 第 2 轮抽奖必须使用第 2 轮的配置行。
        self.fund(int(item_pool.POOLS['1001']['cost'][0][0]), 5)
        result = (await self.request(DRAW, {'id': 1001, 'times': 1}))[-1].fields
        self.assertEqual(result['drawRound'], 2)
        round_two = {int(row['index']) for row in group['pool'] if 2 in [int(r) for r in row['rounds']]}
        self.assertTrue(set(result['drawArr']) <= round_two)
        self.assertEqual(result['info']['round'], 2)

    async def test_costtype3_consume_table_free_first_draw_then_second_item_fallback(self):
        # 1009：costtype=3，specialCost=1，CfgItemPoolConsume.infos[1].costNum=0。
        pool = item_pool.POOLS['1009']
        self.assertEqual(int(pool['costtype']), 3)
        infos = item_pool.CONSUME[str(int(pool['specialCost']))]['infos']
        self.assertEqual(int(infos[0]['costNum']), 0)
        self.assertEqual(int(infos[1]['costNum']), 1)
        result = (await self.request(DRAW, {'id': 1009, 'times': 1}))[-1].fields
        self.assertEqual(result['info']['drawTimes'], 1)
        self.assertNotIn('10421', self.state()['inventory'])
        # 第二次需要 1 个 10421；一个都没有时拒绝且不消耗第二种道具。
        error = await self.rejects(DRAW, {'id': 1009, 'times': 1})
        self.assertIn('不足', str(error))
        # 只有第二种道具时按 ItemPoolInfo.lua:240-242 用它补足。
        self.fund(10422, 1)
        await self.request(DRAW, {'id': 1009, 'times': 1})
        state = self.reopen()
        self.assertNotIn('10422', state['inventory'])
        self.assertEqual(state['item_pools']['pools']['1009']['drawTimes'], 2)

    async def test_control_pool_keeps_drawing_its_infinite_reward(self):
        # 1005(奇趣扭蛋)：extracttype=5(Control)，group 4 有 1 行 isInfinite=true。
        group = item_pool.GROUPS[str(int(item_pool.POOLS['1005']['group']))]
        infinite = [row for row in group['pool'] if row.get('isInfinite')]
        self.assertEqual(len(infinite), 1)
        self.stash(1005, {int(row['index']): int(row.get('rewardnum', 1))
                          for row in group['pool'] if not row.get('isInfinite')})
        self.fund(10412, 5)
        result = (await self.request(DRAW, {'id': 1005, 'times': 5}))[-1].fields
        self.assertEqual(result['info']['drawTimes'], 5)
        self.assertEqual(set(result['drawArr']), {int(infinite[0]['index'])})

    async def test_worst_case_frames_stay_below_the_codec_limit(self):
        # 把所有池的 drawArr 撑到最大，再看 info 帧大小（上限 32767，否则断连）。
        with self.store.transaction(self.uid) as tx:
            data = item_pool.pools_state(tx.state)
            for key, pool in item_pool.POOLS.items():
                entry = item_pool.pool_entry(data, int(key))
                group = item_pool.GROUPS[str(int(pool['group']))]
                for row in group['pool']:
                    entry['drawArr'][str(int(row['index']))] = int(row.get('rewardnum', 1))
                entry['drawTimes'] = 99999
        reply = (await self.request(INFO, {}))[0]
        inner = CODEC.encode_frame(reply.name, reply.fields)
        self.assertLess(len(inner), MAX_FRAME)
        self.assertEqual(len(reply.fields['info']), 10)
        expected = sum(len(item_pool.GROUPS[str(int(pool['group']))]['pool'])
                       for pool in item_pool.POOLS.values())
        self.assertEqual(sum(len(row['drawArr']) for row in reply.fields['info']), expected)
        self.assertEqual(len(self.decoded(reply)['info']), 10)

    def test_generated_catalog_reproduces_and_references_only_existing_rows(self):
        catalog = json.loads((SERVER / 'data' / 'item-pool-pools.json').read_text(encoding='utf-8'))
        self.assertEqual(catalog['counts'], {'pools': 10, 'reward_groups': 9,
                                             'reward_entries': 218, 'consume_tables': 1})
        for source in catalog['sources']:
            recorded = hashlib.sha256((ROOT / source['file']).read_bytes()).hexdigest()
            self.assertEqual(recorded, source['sha256'])
        for key, pool in catalog['pools'].items():
            self.assertEqual(str(int(pool['id'])), key)
            self.assertEqual(int(pool['id']), int(key))
            group = catalog['reward_groups'][str(int(pool['group']))]
            self.assertTrue(group['pool'])
            if int(pool['costtype']) == 3:
                self.assertIn(str(int(pool['specialCost'])), catalog['consume'])
        total = 0
        for group in catalog['reward_groups'].values():
            indexes = sorted(int(row['index']) for row in group['pool'])
            self.assertEqual(indexes, list(range(1, len(indexes) + 1)))
            total += len(indexes)
        self.assertEqual(total, 218)

class ItemPoolSocketTests(unittest.IsolatedAsyncioTestCase):
    """真实 dispatcher 路径：业务拒绝回 Tips、会话保持、帧大小受控。"""

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=SERVER / 'tests', prefix='item-pool-socket-')
        directory = Path(self.temporary.name)
        self.store = Store(directory / 'state.sqlite3')
        self.events = directory / 'events.jsonl'
        query, game = free_port(), free_port()
        while game == query:
            game = free_port()
        self.server = LocalServer(CODEC, self.store, SEED, '127.0.0.1', query, game, self.events,
                                  assembly_timeout=0.2)
        await self.server.start()
        self.clients = []

    async def asyncTearDown(self):
        for reader, writer in self.clients:
            writer.close()
            await writer.wait_closed()
        await self.server.close()
        await asyncio.sleep(0)
        self.store.close()
        self.assertTrue(Path(self.temporary.name).resolve().is_relative_to((SERVER / 'tests').resolve()))
        self.temporary.cleanup()

    async def connect(self, port):
        result = await asyncio.open_connection('127.0.0.1', port)
        self.clients.append(result)
        return result

    async def read_reply(self, reader):
        prefix = await asyncio.wait_for(reader.readexactly(2), 2)
        size = struct.unpack('>H', prefix)[0]
        body = await asyncio.wait_for(reader.readexactly(size), 2)
        self.assertEqual(body[0], 3)
        return CODEC.decode_frame(body[9:])

    async def send(self, writer, name, fields):
        writer.write(encode_packet(CODEC.encode_frame(name, fields)))
        await writer.drain()

    async def login(self):
        qr, qw = await self.connect(self.server.query_port)
        await self.send(qw, 'ClientProto:QueryAccount',
                        {'account': 'item-pool-socket', 'SvnVersion': '3.3.0', 'pwd': 'not-persisted'})
        uid = (await self.read_reply(qr)).fields['uid']
        await self.send(qw, 'ClientProto:PreLoginGame', {'uid': uid, 'distinctId': 'local'})
        pre = await self.read_reply(qr)
        gr, gw = await self.connect(self.server.game_port)
        await self.send(gw, 'ClientProto:LoginGame',
                        {'uid': uid, 'key': pre.fields['key'], 'SvnVersion': '3.3.0'})
        self.assertEqual((await self.read_reply(gr)).name, 'LoginProto:LoginGame')
        return uid, gr, gw

    async def test_pool_info_then_rejected_draw_answers_tips_and_keeps_the_session(self):
        uid, reader, writer = await self.login()
        # 客户端启动时发的正是 id=nil / nextRound=nil。
        await self.send(writer, INFO, {})
        reply = await self.read_reply(reader)
        self.assertEqual(reply.name, item_pool.INFO_REPLY)
        self.assertEqual(len(reply.fields['info']), 10)
        self.assertEqual(reply.fields['info'][0]['drawTimes'], 0)
        # 没有消耗道具：业务拒绝 -> SystemProto:Tips，连接不断。
        await self.send(writer, DRAW, {'id': 1004, 'times': 1})
        tip = await self.read_reply(reader)
        self.assertEqual(tip.name, 'SystemProto:Tips')
        self.assertEqual(tip.fields['strId'], 'GeneralTips')
        self.assertEqual(tip.fields['opId'], 4208)
        self.assertEqual(tip.fields['opName'], DRAW)
        await self.send(writer, 'ClientProto:Heartbeat', {})
        self.assertEqual((await self.read_reply(reader)).name, 'LoginProto:Heartbeat')
        events = [json.loads(line) for line in self.events.read_text(encoding='utf-8').splitlines()]
        self.assertFalse([event for event in events if event['event'] == 'connection_closed'])
        failure = next(event for event in events if event['event'] == 'request_failed')
        self.assertEqual((failure['disposition'], failure['reason']), ('continue', 'business_rejected'))
        response = next(event for event in events
                        if event['event'] == 'response' and event['name'] == item_pool.INFO_REPLY)
        self.assertLess(response['bytes'], MAX_FRAME)

class ActivityTimeListTests(unittest.IsolatedAsyncioTestCase):
    """task-22：OperateActiveProto:GetActiveTimeList 下发 6 期扭蛋活动窗口。

    权威：GameMsg.lua:5778-5792（sOperateActive = id/openTime/closeTime/payRate/
    noticeId/state）、GMsgNo.lua:1158-1159（4704/4705）、官服样本
    session1-stream02-s2c-frame0146.decoded.json；客户端消费在
    ActivityMgr.lua:253-267 + ActivityData.lua:127-134。字段 openTime/closeTime 的线类型是
    int32，所以 4102444800 这类 uint32 值会被 codec 拒绝（Value does not fit i），
    在 dispatcher 里等于 frame_encode_failed 断连。
    """

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SERVER / 'tests', prefix='active-')
        self.store = Store(Path(self.temp.name) / 'state.sqlite3')
        self.uid = self.store.create_account('active-time-list', SEED)['uid']
        self.server = LocalServer(CODEC, self.store, SEED)
        self.ctx = Context(self.server, 'game', self.uid, True)
        self.clock = 1791097800  # 2026-10-05 15:10 +08:00
        with self.store.transaction(self.uid) as tx:
            tx.state['offline_clock'] = self.clock

    async def asyncTearDown(self):
        self.store.close()
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to((SERVER / 'tests').resolve()))
        self.temp.cleanup()

    async def request(self, name, fields):
        frames, tail = CODEC.decode_stream(CODEC.encode_frame(name, fields))
        self.assertFalse(tail)
        replies = await self.server.dispatch(self.ctx, frames[0])
        for reply in replies:
            inner = CODEC.encode_frame(reply.name, reply.fields)
            self.assertLess(len(inner), MAX_FRAME)
            decoded = CODEC.decode_frame(inner)
            self.assertEqual(CODEC.encode_frame(decoded.name, decoded.fields), inner)
        return replies

    async def test_six_gacha_activities_are_sent_open_with_real_windows(self):
        replies = await self.request(item_pool.ACTIVE_REQUEST, {})
        self.assertEqual([reply.name for reply in replies], [item_pool.ACTIVE_REPLY])
        fields = replies[0].fields
        # 修复前这里是空壳 {"operateActiveList": [], "isFinish": True}（ReadSpec 默认值）。
        self.assertTrue(fields['isFinish'])
        rows = fields['operateActiveList']
        self.assertEqual([row['id'] for row in rows], list(item_pool.POOL_ACTIVITIES))
        self.assertEqual(len(rows), 6)
        self.assertNotEqual(rows, [])
        for row in rows:
            with self.subTest(activity=row['id']):
                # ActivityData.lua:131 的判定是 now > sTime and now <= eTime（严格大于），
                # 所以 openTime 必须严格早于当前时间，closeTime 必须在未来。
                self.assertLess(row['openTime'], self.clock)
                self.assertLessEqual(self.clock, row['closeTime'])
                self.assertEqual(row['openTime'], self.clock - item_pool.ACTIVITY_LEAD_SECONDS)
                # openTime/closeTime 是 int32：必须能被 codec 编码（否则断连）。
                self.assertLessEqual(row['closeTime'], 2147483647)
                self.assertGreater(row['closeTime'], 2147483647 - 86400 * 365)
                # 官服样本每条只带 id/openTime/closeTime，不伪造充值或开启态字段。
                self.assertEqual(set(row), {'id', 'openTime', 'closeTime'})

    async def test_window_tracks_the_local_offline_clock(self):
        later = self.clock + 30 * 86400
        with self.store.transaction(self.uid) as tx:
            tx.state['offline_clock'] = later
        rows = (await self.request(item_pool.ACTIVE_REQUEST, {}))[0].fields['operateActiveList']
        for row in rows:
            self.assertEqual(row['openTime'], later - item_pool.ACTIVITY_LEAD_SECONDS)
            self.assertLess(row['openTime'], later)
            self.assertLessEqual(later, row['closeTime'])

    async def test_frame_size_and_handler_ownership(self):
        # 6 条 × 3 个字段远小于 32767；同时确认没有重复注册（initialization 那条已删）。
        from server_core import HANDLERS
        import handlers.initialization as initialization
        self.assertEqual(HANDLERS[item_pool.ACTIVE_REQUEST].__module__, 'handlers.item_pool')
        self.assertNotIn(item_pool.ACTIVE_REQUEST, initialization.READS)
        raw = CODEC.encode_frame(item_pool.ACTIVE_REPLY,
                                 {'operateActiveList': [{'id': i, 'openTime': 1, 'closeTime': 2147483647}
                                                        for i in item_pool.POOL_ACTIVITIES],
                                  'isFinish': True})
        self.assertLess(len(raw), MAX_FRAME)

    def test_uint32_close_time_would_break_the_frame(self):
        # 固化「不能照抄 4102444800」这条约束：超 int32 的值会让 encode 失败，
        # 在真实 dispatcher 里就是 close_reason='frame_encode_failed' 断连。
        from protocol_codec import CodecError
        with self.assertRaises(CodecError):
            CODEC.encode_frame(item_pool.ACTIVE_REPLY,
                               {'operateActiveList': [{'id': 1017, 'openTime': 1,
                                                       'closeTime': 4102444800}], 'isFinish': True})
        self.assertLessEqual(item_pool.ACTIVITY_CLOSE_TIME, 2147483647)

    async def test_other_activity_reads_are_untouched(self):
        # 只有 GetActiveTimeList 换主人；同文件的另外 6 条 OperateActiveProto 读取仍在 initialization。
        import handlers.initialization as initialization
        for request in ('OperateActiveProto:GetSkinRebateInfo', 'OperateActiveProto:GetActiveTimeList',
                        'OperateActiveProto:GetDragonBoatFestivalInfo', 'OperateActiveProto:GetBreakfastCardData',
                        'OperateActiveProto:GetOldSkinRebateInfo', 'OperateActiveProto:GetHalloweenGameData',
                        'OperateActiveProto:GetChristmasGiftData'):
            with self.subTest(request=request):
                if request == 'OperateActiveProto:GetActiveTimeList':
                    self.assertNotIn(request, initialization.READS)
                else:
                    self.assertIn(request, initialization.READS)
