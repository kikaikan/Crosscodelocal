"""task-28：档案回忆/插画 + 心间私语 全开放（数据表 / apply / migrate / 帧安全）。

权威：PlotMgr.lua:36-47（读 line_ 键）、PlotMgr.lua:122-137（写 max）、
cfgCfgArchiveMultiPicture.lua（144 条 type16）、cfgCfgASMR.lua（7 条 type27）、
cfgCfgArchiveStory.lua（28 组 607 条 infos）。数据表由 02-tools/scripts/collection-unlock-build.py 生成。
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
from database import Store
import collection_unlock
import reply_chunks
import server_core
from protocol_codec import IVProtoCodec, WireConfig

CODEC = IVProtoCodec(json.loads((ROOT / '05-protocol/endpoints.json').read_text(encoding='utf-8')),
                     WireConfig('little'))
SEED = json.loads((SERVER / 'data/new_account_seed.json').read_text(encoding='utf-8'))
CATALOG = json.loads((SERVER / 'data/collection-unlock.json').read_text(encoding='utf-8'))
# Lead 复算的期望值：151 件道具 + CfgArchiveStory 实际引用的 20 条线。
EXPECTED_LINES = {1: 10733, 2: 20114, 3: 20229, 5: 20311, 6: 20429, 7: 20516, 8: 20609,
                  9: 20715, 10: 20821, 11: 20917, 12: 21012, 13: 21131, 14: 21213, 15: 21316,
                  16: 21422, 17: 21509, 18: 21626, 19: 21713, 20: 21821, 21: 21923}
PLOT_KEY = 'plot_data'


def plot_of(state):
    entry = state['client_data'][PLOT_KEY]
    return entry['type'], json.loads(entry['data'])


class UnlockDataTests(unittest.TestCase):
    """数据表数值与 apply() 的语义。"""

    def setUp(self):
        self.state = deepcopy(SEED)

    def test_build_output_matches_the_expected_collections(self):
        self.assertEqual(len(CATALOG['items']), 151)
        self.assertEqual(sorted(CATALOG['items']), CATALOG['items'])       # 升序
        self.assertEqual(len(set(CATALOG['items'])), 151)                  # 去重
        self.assertEqual({int(k): v for k, v in CATALOG['plot_lines'].items()}, EXPECTED_LINES)
        # 插画 144 + ASMR 7，交集 0：ASMR 62001..62006/62010 在数值上落在插画
        # 区间 61001..64026 之内，只能按集合剔除，不能按数值窗口剔除。
        asmr = {62001, 62002, 62003, 62004, 62005, 62006, 62010}
        pictures = [i for i in CATALOG['items'] if i not in asmr]
        self.assertEqual(len(pictures), 144)
        self.assertTrue(all(61001 <= i <= 64026 for i in pictures))
        self.assertEqual([i for i in CATALOG['items'] if i in asmr], sorted(asmr))
        # catalog() 读到的就是同一份
        self.assertEqual(collection_unlock.catalog()['items'], CATALOG['items'])
        self.assertEqual(collection_unlock.catalog()['lines'], EXPECTED_LINES)

    def test_apply_on_empty_state_unlocks_everything(self):
        empty = {}
        self.assertTrue(collection_unlock.apply(empty))
        self.assertEqual(len(empty['inventory']), 151)
        self.assertEqual(set(empty['inventory'].values()), {1})
        kind, values = plot_of(empty)
        self.assertEqual(kind, 3)
        self.assertEqual(values, {'line_%d' % line: story for line, story in EXPECTED_LINES.items()})

    def test_apply_is_monotonic_and_never_lowers_progress(self):
        self.state['inventory']['61001'] = 5                 # 已有更多，保持
        self.state['client_data'][PLOT_KEY] = {'type': 3, 'data': json.dumps(
            {'line_1': 99999, 'line_7': 20517}, separators=(',', ':'))}
        self.assertTrue(collection_unlock.apply(self.state))
        inventory = self.state['inventory']
        self.assertEqual(inventory['61001'], 5)
        self.assertEqual(inventory['62010'], 1)              # 缺失的补 1
        _, values = plot_of(self.state)
        self.assertEqual(values['line_1'], 99999)            # 更高值不被降低
        self.assertEqual(values['line_7'], 20517)            # 引用最大值 20516 < 现值
        self.assertEqual(values['line_2'], 20114)

    def test_apply_is_idempotent_and_leaves_the_state_byte_identical(self):
        self.assertTrue(collection_unlock.apply(self.state))
        snapshot = json.dumps(self.state, sort_keys=True, ensure_ascii=False)
        self.assertFalse(collection_unlock.apply(self.state))
        self.assertEqual(json.dumps(self.state, sort_keys=True, ensure_ascii=False), snapshot)

    def test_other_client_data_and_plot_keys_are_preserved(self):
        self.state['client_data']['plot_data'] = {'type': 3, 'data': json.dumps(
            {'line_1': 1, 'keep_me': 7}, separators=(',', ':'))}
        self.state['client_data']['setting_key'] = {'type': 3, 'data': '{"a":1}'}
        self.assertTrue(collection_unlock.apply(self.state))
        self.assertEqual(self.state['client_data']['setting_key'], {'type': 3, 'data': '{"a":1}'})
        _, values = plot_of(self.state)
        self.assertEqual(values['keep_me'], 7)

    def test_corrupt_plot_data_is_rebuilt_into_a_valid_type3_entry(self):
        for broken in ({'type': 4, 'data': '{}'}, {'type': 3, 'data': 'not json'},
                       {'type': 3, 'data': '[1,2]'}, 'not-a-dict'):
            with self.subTest(broken=broken):
                state = deepcopy(SEED)
                state['client_data'][PLOT_KEY] = broken
                self.assertTrue(collection_unlock.apply(state))
                kind, values = plot_of(state)
                self.assertEqual(kind, 3)
                self.assertEqual(len(values), len(EXPECTED_LINES))


class MigrateTests(unittest.TestCase):
    """migrate(store)：只动需要动的账号，第二次为 0，只在有待改动项时备份。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SERVER / 'tests', prefix='collection-')
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.store = Store(self.path)
        self.untouched = self.store.create_account('collection-fresh', SEED)['uid']
        self.settled = self.store.create_account('collection-settled', SEED)['uid']
        with self.store.transaction(self.settled) as tx:
            collection_unlock.apply(tx.state)

    def tearDown(self):
        self.store.close()
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to((SERVER / 'tests').resolve()))
        self.temp.cleanup()

    def revision(self, uid):
        return self.store.connection.execute('SELECT revision FROM accounts WHERE uid=?', (uid,)).fetchone()[0]

    def backups(self):
        directory = self.path.parent / 'backups'
        return sorted(directory.iterdir()) if directory.exists() else []

    def test_migrate_touches_only_accounts_that_need_it(self):
        before = self.revision(self.settled)
        self.assertEqual(self.backups(), [])
        self.assertEqual(collection_unlock.migrate(self.store), 1)
        self.assertEqual(self.store.get_player(self.untouched)['inventory']['61001'], 1)
        self.assertEqual(self.revision(self.settled), before)      # 无变化的账号没有被写
        self.assertEqual(len(self.backups()), 1)                    # 有待改动项 → 备份一次
        self.assertEqual(collection_unlock.migrate(self.store), 0)  # 幂等
        self.assertEqual(len(self.backups()), 1)


class FrameSafetyTests(unittest.TestCase):
    """满背包/满快照下每一帧都必须低于 codec 上限（否则 frame_encode_failed 断连）。"""

    def frames(self, replies):
        for reply in replies:
            raw = CODEC.encode_frame(reply.name, reply.fields)
            self.assertLess(len(raw), CODEC.config.max_frame_size, reply.name)
            self.assertEqual(CODEC.decode_frame(raw).name, reply.name)

    def test_initial_pushes_with_the_unlocked_seed(self):
        seed = deepcopy(SEED)
        self.assertTrue(collection_unlock.apply(seed))
        self.frames(server_core.initial_pushes(seed, CODEC))

    def test_item_bag_frames_with_the_full_bag(self):
        items = [{'id': identifier, 'num': 1, 'time': 0, 'ix': 0, 'expiry': 0, 'get_infos': {}}
                 for identifier in CATALOG['items']]
        items.extend({'id': identifier, 'num': 1, 'time': 0, 'ix': 0, 'expiry': 0, 'get_infos': {}}
                     for identifier in range(10001, 10300))
        self.frames(reply_chunks.item_bag(CODEC, items))
