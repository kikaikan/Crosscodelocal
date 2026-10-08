"""task-25：副天赋（SubTalent）2620/2622 的线协议、槽位物化与对抗性边界。

权威（只读）
  opcode：03-unpack/lua/device-luascripts/GMsgNo.lua:440-442（2620/2621/2622），
    GMsgNo.lua:1746-1748 为 GMsgKey 反查。RandSubTalent / SetReplaceSubTalent /
    OpenSubTalentSlot 在 GMsgNo.lua 中 0 命中 ⇒ 无 opcode、不可达
    。
  线格式：GameMsg.lua:2193-2207 —— UpgradeSubTalent/Ret {cid:uint, index:byte}；
    SetUseSubTalent {cid:uint, indexs:array|uint}，2622 无 Ret。
  客户端消费：RoleCenter.lua:249-353（槽位/装备）、RoleTalent.lua:27-141（升级按钮）、
    CharacterCardsData.lua:582-585/1167-1187、CfgBase.lua:72-78（GetByID 未命中返 nil）。
  卡→池权威：客户端 cfgCardData.lua 的 CardData[cfgid].subTfSkills[1]（spec §3.1）；
    本文件对 07-server/data/sub-talent.json 的 cardPools 做**全量**对账，不只抽样。
  存档语义：spec §3/§4（本地策略，无官服样本）。

三类拒绝必须分开断言，否则会把"协议层不可表达"误当成"业务拒绝"：
  * rejects()         —— 线上可表达、handler 抛 StorageError（回 SystemProto:Tips）。
  * codec_rejects()   —— 值在 byte/uint/array 编码层就不可表达（CodecError，连接会断）。
  * direct_rejects()  —— 绕过 codec 直调 handler，覆盖 bool/None/负号 等线上到不了的值。
"""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
from database import Store, StorageError
from server_core import Context, HANDLERS, LocalServer, load_dependencies
from protocol_codec import CodecError, readable
import handlers.sub_talent as sub_talent

CODEC, SEED = load_dependencies(ROOT / '05-protocol' / 'endpoints.json',
                                SERVER / 'data' / 'new_account_seed.json')
UPGRADE, UPGRADE_RET = sub_talent.UPGRADE_REQUEST, sub_talent.UPGRADE_REPLY
USE = sub_talent.USE_REQUEST
# 02-tools/scripts/protocol_codec.py:42 的 codec 上限：超过即 frame_encode_failed 断连。
MAX_FRAME = 32767

# 起始卡 cfgid=71010（新账号种子唯一卡）的池与四条链，取自 sub-talent.json 实测。
STARTER_CFGID = 71010
POOL = 71010
STARTING = [302401, 315501, 1041, 315601]
SECOND = [302402, 315502, 1042, 315602]
THIRD = [302403, 315503, 1043, 315603]
TAIL = [302405, 315505, 1045, 315605]
LV1_COSTS = {15001: 2, 14013: 1}       # 302401 -> 302402 (costId 2003)
LV2_COSTS = {15002: 3, 14023: 1, 14204: 1}   # 302402 -> 302403 (costId 3003)
# 无池卡：cfgCardData 里 subTfSkills[1] 不是 CfgSubTalentSkillPool 的 id（共 81 个）。
NO_POOL_CFGID = 10150
# 客户端有池、admin-role-templates.json 无此条（实测）：必须照常物化。
MAIN_CHARACTER_CFGID = 71020


def client_card_table():
    """Read the read-only client cfgCardData.lua with the project's safe parser."""
    sys.path.insert(0, str(ROOT / '02-tools' / 'scripts'))
    import config_codec
    from protocol_codec import lua_data
    config_codec.MAX_INPUT_BYTES = 64 * 1024 * 1024

    class Parser(config_codec.LuaDataParser):
        LONG_STRING = re.compile(r'\[(=*)\[')

        def value(self, depth=0):
            self.whitespace()
            match = self.LONG_STRING.match(self.text, self.position)
            if match:
                start = self.position + len(match[0])
                end = self.text.find(']' + match.group(1) + ']', start)
                self.position = end + len(']' + match.group(1) + ']')
                return self.text[start:end]
            return super().value(depth)

    text = (ROOT / '03-unpack' / 'lua' / 'device-luascripts' / 'cfgCardData.lua').read_text('utf-8-sig')
    body = text.split('=', 1)[1]
    body = body[:body.rfind('}') + 1]
    return lua_data(Parser(body, max_depth=64, max_nodes=8_000_000).parse())


def catalog():
    return json.loads((SERVER / 'data' / 'sub-talent.json').read_text(encoding='utf-8'))


class SubTalentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SERVER / 'tests', prefix='sub-talent-')
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.store = Store(self.path)
        self.uid = self.store.create_account('sub-talent-local', SEED)['uid']
        self.server = LocalServer(CODEC, self.store, SEED)
        self.ctx = Context(self.server, 'game', self.uid, True)
        # 2620/2622 与客户端一致地受 special20 门槛保护（CharacterCardsData.lua:1169
        # 的 MenuMgr:CheckModelOpen(OpenViewType.special,"special20")）。cfgCfgOpenConditionMore
        # 的 special20 -> rule 2006 -> 通关 1006，故测试先把该关卡记为已通关。
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['cleared_stages'] = [1006]
        self.cid = self.state()['cards'][0]['cid']

    async def asyncTearDown(self):
        self.store.close()
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to((SERVER / 'tests').resolve()))
        self.temp.cleanup()

    # ---------- helpers ----------
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

    def starter(self, key):
        return next(c for c in self.state()['cards'] if c['cid'] == self.cid)[key]

    def stored(self):
        return deepcopy(self.starter('sub_talent'))

    def patch_card(self, **fields):
        self._patch(self.cid, **fields)

    def _patch(self, cid, **fields):
        with self.store.transaction(self.uid) as tx:
            card = next(c for c in tx.state['cards'] if c['cid'] == cid)
            for key, value in fields.items():
                card[key] = deepcopy(value)

    def fund(self, costs):
        with self.store.transaction(self.uid) as tx:
            for item, amount in costs.items():
                tx.add_item(item, amount)

    def add_card(self, cfgid):
        with self.store.transaction(self.uid) as tx:
            return tx.add_card(cfgid, {'skills': {}})['cid']

    def with_slots(self, cid, **fields):
        """Materialize the card's slots, then apply overrides (break_level must be set first)."""
        self._patch(cid, **fields)
        with self.store.transaction(self.uid) as tx:
            card = next(c for c in tx.state['cards'] if c['cid'] == cid)
            sub_talent.ensure_slots(card, sub_talent_config(tx.state, card))
        return cid

    async def request(self, name, fields):
        # Every request and reply crosses the real IVProto codec, and every frame must
        # encode below the codec limit or server_core closes the connection.
        frames, tail = CODEC.decode_stream(CODEC.encode_frame(name, fields))
        self.assertFalse(tail)
        replies = await self.server.dispatch(self.ctx, frames[0])
        for reply in replies:
            inner = CODEC.encode_frame(reply.name, reply.fields)
            self.assertLess(len(inner), MAX_FRAME)
            decoded = CODEC.decode_frame(inner)
            self.assertEqual(CODEC.encode_frame(decoded.name, decoded.fields), inner)
        return replies

    async def rejects(self, name, fields):
        """Wire-expressible rejection: handler raises StorageError and the save is untouched."""
        before, revision = deepcopy(self.state()), self.revision()
        with self.assertRaises(StorageError) as raised:
            await self.request(name, fields)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.revision(), revision)
        return raised.exception

    def codec_rejects(self, name, fields):
        """Values the wire cannot carry: rejected before any handler runs."""
        with self.assertRaises(CodecError):
            CODEC.encode_frame(name, fields)

    async def direct_rejects(self, handler, fields):
        """Bypass the codec to reach values a real client cannot send."""
        before, revision = deepcopy(self.state()), self.revision()
        with self.assertRaises(StorageError):
            await handler(self.ctx, fields)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.revision(), revision)

    def payload(self, replies):
        updates = [r for r in replies if r.name == 'PlayerProto:CardUpdate']
        self.assertEqual(len(updates), 1)
        return next(c for c in updates[0].fields['cards'] if c['cid'] == self.cid)

    def update_rows(self, replies):
        updates = [r for r in replies if r.name == 'PlayerProto:ItemUpdate']
        self.assertEqual(len(updates), 1)
        return {row['id']: (row['add'], row['num']) for row in updates[0].fields['data']}

    # ================= A. ensure_slots：物化 / 幂等 / 自愈 =================
    def test_ensure_slots_materializes_four_slots_without_any_zero_in_had(self):
        # spec §4.1：break_level=B 时 had[n]=pool.ids[n].id (n=1..cnt(B))。
        self.patch_card(break_level=7, sub_talent={})
        with self.store.transaction(self.uid) as tx:
            card = next(c for c in tx.state['cards'] if c['cid'] == self.cid)
            cfg = sub_talent_config(tx.state, card)
            self.assertEqual(sub_talent.pool_id(cfg), POOL)
            self.assertTrue(sub_talent.ensure_slots(card, cfg))
            data = card['sub_talent']
        self.assertEqual(data['had'], STARTING)
        self.assertEqual(data['use'], [0, 0, 0, 0])
        # 关键回归：可见范围内绝不能出现 0 —— Lua 里 0 是真值，客户端会把该槽判成
        # "已解锁、id=0"，随后 RoleInfoTalentItem2.lua:34-35 对 nil 配置取 cfg.icon 报错。
        self.assertNotIn(0, data['had'])

    def test_ensure_slots_is_idempotent_and_only_fills_gaps(self):
        self.patch_card(break_level=7,
                        sub_talent={'had': [302404], 'use': [302404, 0, 0, 0]})
        with self.store.transaction(self.uid) as tx:
            card = next(c for c in tx.state['cards'] if c['cid'] == self.cid)
            cfg = sub_talent_config(tx.state, card)
            self.assertTrue(sub_talent.ensure_slots(card, cfg))      # 首次补齐
            first = deepcopy(card['sub_talent'])
            self.assertFalse(sub_talent.ensure_slots(card, cfg))     # 幂等：第二次无变化
            self.assertEqual(card['sub_talent'], first)
        # 已学会的进度不能被覆盖：槽 1 保留 302404，其余按池初值补。
        self.assertEqual(first['had'], [302404, STARTING[1], STARTING[2], STARTING[3]])
        self.assertEqual(first['use'], [302404, 0, 0, 0])

    def test_ensure_slots_honours_the_break_level_ladder(self):
        for level, expected in {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 4, 7: 4}.items():
            with self.subTest(break_level=level):
                self.patch_card(break_level=level, sub_talent={})
                with self.store.transaction(self.uid) as tx:
                    card = next(c for c in tx.state['cards'] if c['cid'] == self.cid)
                    sub_talent.ensure_slots(card, sub_talent_config(tx.state, card))
                    had, use = card['sub_talent']['had'], card['sub_talent']['use']
                self.assertEqual(had, STARTING[:expected])
                self.assertNotIn(0, had)
                self.assertEqual(len(use), 4)     # use 恒补足到 4（spec §4.1）

    def test_ensure_slots_drops_equipped_ids_that_are_no_longer_learned(self):
        self.patch_card(break_level=7,
                        sub_talent={'had': [302401], 'use': [302401, 999999, 0, 0]})
        with self.store.transaction(self.uid) as tx:
            card = next(c for c in tx.state['cards'] if c['cid'] == self.cid)
            sub_talent.ensure_slots(card, sub_talent_config(tx.state, card))
            use = card['sub_talent']['use']
        self.assertEqual(use, [302401, 0, 0, 0])

    def test_ensure_slots_skips_cards_whose_pool_row_does_not_exist(self):
        # 81 个卡模板的 subTfSkills[1] 不是 CfgSubTalentSkillPool 的 id（实测，见
        # sub-talent.json 的 validation.card_pools_missing_from_CfgSubTalentSkillPool）；
        # 服务端必须静默跳过：不写 sub_talent、不抛异常。
        card_id = self.add_card(NO_POOL_CFGID)
        with self.store.transaction(self.uid) as tx:
            card = next(c for c in tx.state['cards'] if c['cid'] == card_id)
            cfg = sub_talent_config(tx.state, card)
            self.assertEqual(sub_talent.pool_id(cfg), NO_POOL_CFGID)
            self.assertIsNone(sub_talent.starting_ids(sub_talent.pool_id(cfg)))
            self.assertFalse(sub_talent.ensure_slots(card, cfg))
            self.assertNotIn('sub_talent', card)
        # 该卡仍然可升级（自愈路径也不写槽位）⇒ 走"槽位尚未开放"的业务拒绝。
        self.assertIsNone(self.reopen()['cards'][1].get('sub_talent'))

    def test_ensure_slots_still_materializes_the_main_character_card_71020(self):
        # 71020 是"客户端有池、admin-role-templates.json 无此条"的卡：必须照常物化，
        # 不能被 admin 模板的缺口误判为"无池"。
        card_id = self.add_card(MAIN_CHARACTER_CFGID)
        with self.store.transaction(self.uid) as tx:
            card = next(c for c in tx.state['cards'] if c['cid'] == card_id)
            card['break_level'] = 7
            cfg = sub_talent_config(tx.state, card)
            self.assertEqual(sub_talent.pool_id(cfg), MAIN_CHARACTER_CFGID)
            self.assertTrue(sub_talent.ensure_slots(card, cfg))
            self.assertEqual(len(card['sub_talent']['had']), 4)
            self.assertNotIn(0, card['sub_talent']['had'])

    def test_ensure_slots_tolerates_junk_stored_state(self):
        # 存档被外部写坏时不得崩：非 dict、非 list、负数、布尔、字符串都要归零或重建。
        for junk in ({'had': 'oops', 'use': 5}, {'had': [-1, 'x', True], 'use': [None, 3]},
                     'not-a-dict', [1, 2, 3], {'had': {'a': 1}, 'use': []}, {'had': None}):
            with self.subTest(junk=junk):
                self.patch_card(break_level=7, sub_talent=junk)
                with self.store.transaction(self.uid) as tx:
                    card = next(c for c in tx.state['cards'] if c['cid'] == self.cid)
                    sub_talent.ensure_slots(card, sub_talent_config(tx.state, card))
                    data = card['sub_talent']
                self.assertIsInstance(data, dict)
                self.assertEqual(data['had'], STARTING)
                self.assertEqual(data['use'], [0, 0, 0, 0])

    def test_open_slots_never_raises_on_hostile_break_levels(self):
        for value, expected in ((7, 4), ('7', 4), (1, 0), (0, 0), (99, 0),
                                (None, 0), ('x', 0), (True, 0)):
            with self.subTest(break_level=value):
                self.assertEqual(sub_talent.open_slots(value), expected)

    # ================= B. 2620 升级 =================
    async def test_upgrade_debits_configured_material_and_answers_ret_and_card_update(self):
        self.patch_card(break_level=7, sub_talent={})
        self.fund(LV1_COSTS)
        replies = await self.request(UPGRADE, {'cid': self.cid, 'index': 1})
        names = [r.name for r in replies]
        self.assertEqual(names.count(UPGRADE_RET), 1)
        self.assertEqual(next(r for r in replies if r.name == UPGRADE_RET).fields,
                         {'cid': self.cid, 'index': 1})
        self.assertIn('PlayerProto:CardUpdate', names)
        # 只扣配置里的材料，且只扣一次。
        self.assertEqual(self.update_rows(replies),
                         {item: (-amount, 0) for item, amount in LV1_COSTS.items()})
        self.assertEqual(self.payload(replies)['sub_talent']['had'],
                         [SECOND[0]] + STARTING[1:])
        self.assertEqual(self.reopen()['cards'][0]['sub_talent']['had'],
                         [SECOND[0]] + STARTING[1:])

    async def test_upgrade_rewrites_the_equipped_slot_to_the_new_skill(self):
        self.patch_card(break_level=7,
                        sub_talent={'had': list(STARTING), 'use': [302401, 0, 0, 0]})
        self.fund(LV1_COSTS)
        await self.request(UPGRADE, {'cid': self.cid, 'index': 1})
        self.assertEqual(self.reopen()['cards'][0]['sub_talent']['use'], [SECOND[0], 0, 0, 0])

    async def test_upgrade_uses_each_slot_independently(self):
        # 四个槽是四条独立的链，各有自己的 costId（slots 1..4 -> costId 2003/2002/2004/2003）。
        self.with_slots(self.cid, break_level=7, sub_talent={})
        self.assertEqual(self.stored()['had'], STARTING)
        expected = list(STARTING)
        for slot in range(1, 5):
            with self.subTest(slot=slot):
                self.fund(slot_costs(slot))
                before = list(self.stored()['had'])
                await self.request(UPGRADE, {'cid': self.cid, 'index': slot})
                after = list(self.stored()['had'])
                expected[slot - 1] = SECOND[slot - 1]
                self.assertEqual(after, expected)
                # 每次请求只动目标槽，其余槽（含此前已升级的）保持原值。
                self.assertEqual([i for i, (x, y) in enumerate(zip(before, after)) if x != y],
                                 [slot - 1])

    async def test_upgrade_accepts_exactly_enough_material_and_rejects_one_short(self):
        self.patch_card(break_level=7, sub_talent={})
        short = dict(LV1_COSTS)
        item = min(short, key=lambda key: short[key])
        short[item] -= 1
        self.fund(short)
        error = await self.rejects(UPGRADE, {'cid': self.cid, 'index': 1})
        self.assertIn('不足', str(error))
        self.fund({item: 1})                       # 刚好够
        replies = await self.request(UPGRADE, {'cid': self.cid, 'index': 1})
        self.assertIn('PlayerProto:CardUpdate', [r.name for r in replies])
        self.assertEqual(self.update_rows(replies),
                         {k: (-v, 0) for k, v in LV1_COSTS.items()})

    async def test_upgrade_rejects_index_outside_the_open_slots(self):
        self.patch_card(break_level=7, sub_talent={})
        self.fund({item: amount * 4 for item, amount in LV1_COSTS.items()})
        for index in (0, 5, 6, 200, 255):
            with self.subTest(index=index):
                await self.rejects(UPGRADE, {'cid': self.cid, 'index': index})
        # 槽位未开放：break_level=2 只开放 1 个槽。
        self.patch_card(break_level=2, sub_talent={})
        error = await self.rejects(UPGRADE, {'cid': self.cid, 'index': 2})
        self.assertIn('尚未开放', str(error))

    async def test_upgrade_rejects_a_talent_at_the_end_of_its_chain(self):
        self.patch_card(break_level=7,
                        sub_talent={'had': [TAIL[0]], 'use': [TAIL[0], 0, 0, 0]})
        self.fund({item: amount * 4 for item, amount in LV1_COSTS.items()})
        error = await self.rejects(UPGRADE, {'cid': self.cid, 'index': 1})
        self.assertIn('满级', str(error))
        self.assertEqual(self.stored()['had'][0], TAIL[0])

    async def test_upgrade_rejects_a_card_the_caller_does_not_own(self):
        self.patch_card(break_level=7, sub_talent={})
        self.fund({item: amount * 4 for item, amount in LV1_COSTS.items()})
        for identifier in (999999, 12345678):
            with self.subTest(cid=identifier):
                await self.rejects(UPGRADE, {'cid': identifier, 'index': 1})
        # 真正的越权：cid 是**账号内**编号，两个账号各自都有 cid=1，所以必须换一个账号的
        # context 去申请本账号的 cid —— 它在本账号的卡包里找不到，必须被拒。
        mine = self.add_card(STARTER_CFGID)          # 本账号的第二张卡，cid=2
        other_uid = self.store.create_account('sub-talent-other', SEED)['uid']
        other_ctx = Context(self.server, 'game', other_uid, True)
        other_ctx.logged_in = True
        sub_talent.ensure_slots  # noqa: B018  (keep import used)
        before = self.store.get_player(other_uid)
        for index in (1, 2):
            with self.subTest(other_cid=mine, index=index):
                with self.assertRaises(StorageError):
                    await sub_talent.upgrade_sub_talent(other_ctx, {'cid': mine, 'index': index})
        self.assertEqual(self.store.get_player(other_uid), before)

    async def test_upgrade_self_heals_a_card_whose_sub_talent_is_missing(self):
        # spec §4.5 自愈：未跑迁移也应能直接升级。
        self.patch_card(break_level=7, sub_talent={})
        self.fund(LV1_COSTS)
        replies = await self.request(UPGRADE, {'cid': self.cid, 'index': 1})
        self.assertEqual(self.payload(replies)['sub_talent']['had'],
                         [SECOND[0]] + STARTING[1:])
        self.assertNotIn(0, self.stored()['had'])

    async def test_a_card_whose_had_is_all_zero_is_healed_rather_than_rejected(self):
        # had 全 0 不等于"已解锁 id 0"：ensure_slots 会把 0 归零并按池初值补，
        # 所以请求会成功，而不是把 0 当成一个可升级的天赋。
        self.patch_card(break_level=7, sub_talent={'had': [0, 0, 0, 0], 'use': []})
        self.fund(LV1_COSTS)
        await self.request(UPGRADE, {'cid': self.cid, 'index': 1})
        self.assertEqual(self.stored()['had'], [SECOND[0]] + STARTING[1:])

    async def test_upgrade_charges_once_per_level_and_never_double_charges(self):
        self.patch_card(break_level=7, sub_talent={})
        self.fund({**LV1_COSTS, **LV2_COSTS})
        before = dict(self.state()['inventory'])
        await self.request(UPGRADE, {'cid': self.cid, 'index': 1})
        after = dict(self.state()['inventory'])
        for item, amount in LV1_COSTS.items():
            self.assertEqual(int(before.get(str(item), 0)) - int(after.get(str(item), 0)), amount)
        # 第二次同样的请求推进到再下一级（合法），但用的是**下一级**的料，不是重复扣上一级。
        await self.request(UPGRADE, {'cid': self.cid, 'index': 1})
        final = dict(self.state()['inventory'])
        for item, amount in LV2_COSTS.items():
            self.assertEqual(int(after.get(str(item), 0)) - int(final.get(str(item), 0)), amount)
        for item, amount in LV1_COSTS.items():
            if item not in LV2_COSTS:
                self.assertEqual(int(after.get(str(item), 0)), int(final.get(str(item), 0)))
        self.assertEqual(self.stored()['had'][0], THIRD[0])

    async def test_upgrade_rolls_back_material_when_a_later_request_fails(self):
        self.patch_card(break_level=7, sub_talent={})
        self.fund(LV1_COSTS)                # 只够第一级
        await self.request(UPGRADE, {'cid': self.cid, 'index': 1})
        before, revision = deepcopy(self.state()), self.revision()
        await self.rejects(UPGRADE, {'cid': self.cid, 'index': 1})
        self.assertEqual(self.state(), before)
        self.assertEqual(self.revision(), revision)

    def test_upgrade_and_ret_wire_round_trip_and_byte_width(self):
        fields = {'cid': self.cid, 'index': 1}
        frames, tail = CODEC.decode_stream(CODEC.encode_frame(UPGRADE, fields))
        self.assertFalse(tail)
        self.assertEqual((frames[0].opcode, readable(frames[0].fields)), (2620, fields))
        self.assertEqual(CODEC.schemas[UPGRADE]['opcode'], 2620)
        self.assertEqual([(f['name'], f['type']) for f in CODEC.schemas[UPGRADE]['fields']],
                         [('cid', 'uint'), ('index', 'byte')])
        self.assertEqual([(f['name'], f['type']) for f in CODEC.schemas[UPGRADE_RET]['fields']],
                         [('cid', 'uint'), ('index', 'byte')])
        decoded, remaining = CODEC.decode_stream(CODEC.encode_frame(UPGRADE_RET, fields))
        self.assertFalse(remaining)
        self.assertEqual((decoded[0].opcode, readable(decoded[0].fields)), (2621, fields))
        # index 是 byte：256/-1 在编码层就不可表达（不是业务拒绝）。
        for bad in (256, -1, 65535):
            with self.subTest(index=bad):
                self.codec_rejects(UPGRADE, {'cid': self.cid, 'index': bad})
        # 请求只声明 2 个字段：多余字段被 codec 丢弃，永远到不了 handler。
        extra, extra_tail = CODEC.decode_stream(
            CODEC.encode_frame(UPGRADE, {**fields, 'uf': 'costNum', 'cid2': 7}))
        self.assertFalse(extra_tail)
        self.assertEqual(readable(extra[0].fields), fields)

    # ================= C. 2622 装备 =================
    async def test_use_writes_the_list_and_answers_card_update_without_a_ret(self):
        self.patch_card(break_level=7, sub_talent={})
        replies = await self.request(USE, {'cid': self.cid, 'indexs': [302401, 315501, 0, 0]})
        self.assertEqual([r.name for r in replies], ['PlayerProto:CardUpdate'])
        self.assertEqual(self.payload(replies)['sub_talent']['use'], [302401, 315501, 0, 0])
        self.assertEqual(self.stored()['use'], [302401, 315501, 0, 0])
        # 2622 在客户端与 endpoints.json 里都没有 Ret schema：绝不能发。
        self.assertNotIn('PlayerProto:SetUseSubTalentRet', CODEC.schemas)

    async def test_use_keeps_the_client_order_and_pads_a_short_list(self):
        # RoleCenter.lua:332-334 只在**卸载**分支排序，安装分支不排序 ⇒ 服务端不得重排。
        self.patch_card(break_level=7, sub_talent={})
        await self.request(USE, {'cid': self.cid, 'indexs': [1041]})
        self.assertEqual(self.stored()['use'], [1041, 0, 0, 0])
        await self.request(USE, {'cid': self.cid, 'indexs': [315501, 1041]})
        self.assertEqual(self.stored()['use'], [315501, 1041, 0, 0])
        await self.request(USE, {'cid': self.cid, 'indexs': [1041, 315501, 302401, 315601]})
        self.assertEqual(self.stored()['use'], [1041, 315501, 302401, 315601])

    async def test_use_accepts_an_empty_list_as_unequip_all(self):
        self.patch_card(break_level=7,
                        sub_talent={'had': list(STARTING), 'use': list(STARTING)})
        await self.request(USE, {'cid': self.cid, 'indexs': []})
        self.assertEqual(self.stored()['use'], [0, 0, 0, 0])

    async def test_use_rejects_duplicates_unknowns_and_oversize(self):
        self.patch_card(break_level=7, sub_talent={})
        for indexs in ([302401, 302401],
                       [302401, 302401, 0, 0],
                       [302401, 315501, 1041, 315601, 302401],   # 超长
                       [0, 0, 0, 0, 0],
                       [999999],                                 # 未学会
                       [TAIL[1]],                                # 池内但未学会
                       [302401, 999999],
                       [True]):        # bool 在 uint 上编码成 1 ⇒ 解码后是未知 id
            with self.subTest(indexs=indexs):
                await self.rejects(USE, {'cid': self.cid, 'indexs': indexs})

    async def test_use_rejects_values_the_wire_cannot_carry(self):
        # 负数/非整数/None/非数组在 array|uint 编码层就失败 ⇒ 连接按协议错误处理。
        for indexs in ([-1], [-302401], ['302401'], [302401.0], [None],
                       {'0': 302401}, '302401', 302401):
            with self.subTest(indexs=str(indexs)[:40]):
                self.codec_rejects(USE, {'cid': self.cid, 'indexs': indexs})

    async def test_use_rejects_hostile_values_when_the_codec_is_bypassed(self):
        self.patch_card(break_level=7, sub_talent={})
        for indexs in ([-1], [None], [True], [False], [302401.0], ['302401'],
                       [302401] * 10000, {0: 302401}, 'x', 7, None):
            with self.subTest(indexs=str(indexs)[:40]):
                await self.direct_rejects(sub_talent.set_use_sub_talent,
                                          {'cid': self.cid, 'indexs': indexs})

    async def test_use_rejects_more_equipped_than_open_slots(self):
        self.patch_card(break_level=2, sub_talent={})     # cnt=1
        await self.rejects(USE, {'cid': self.cid, 'indexs': [STARTING[0], STARTING[1]]})
        await self.request(USE, {'cid': self.cid, 'indexs': [STARTING[0], 0, 0, 0]})

    async def test_use_rejects_a_card_the_caller_does_not_own(self):
        self.patch_card(break_level=7, sub_talent={})
        for identifier in (999999, 12345678):
            with self.subTest(cid=identifier):
                await self.rejects(USE, {'cid': identifier, 'indexs': [0, 0, 0, 0]})
        # 跨账号：cid 是账号内编号，别的账号的 context 不能操作本账号的卡。
        mine = self.add_card(STARTER_CFGID)
        other_uid = self.store.create_account('sub-talent-other-use', SEED)['uid']
        other_ctx = Context(self.server, 'game', other_uid, True)
        other_ctx.logged_in = True
        before = self.store.get_player(other_uid)
        with self.assertRaises(StorageError):
            await sub_talent.set_use_sub_talent(other_ctx, {'cid': mine, 'indexs': [0, 0, 0, 0]})
        self.assertEqual(self.store.get_player(other_uid), before)

    async def test_use_never_touches_material_or_the_revision_on_rejection(self):
        self.patch_card(break_level=7, sub_talent={})
        self.fund(LV1_COSTS)
        await self.rejects(USE, {'cid': self.cid, 'indexs': [999999]})

    async def test_use_is_idempotent_for_the_same_payload(self):
        self.patch_card(break_level=7, sub_talent={})
        payload = {'cid': self.cid, 'indexs': [302401, 315501, 0, 0]}
        first = await self.request(USE, payload)
        second = await self.request(USE, payload)
        self.assertEqual(self.payload(first)['sub_talent']['use'],
                         self.payload(second)['sub_talent']['use'])
        self.assertEqual(self.stored()['use'], [302401, 315501, 0, 0])

    def test_use_wire_round_trip(self):
        fields = {'cid': self.cid, 'indexs': [302401, 315501, 0, 0]}
        frames, tail = CODEC.decode_stream(CODEC.encode_frame(USE, fields))
        self.assertFalse(tail)
        self.assertEqual((frames[0].opcode, readable(frames[0].fields)), (2622, fields))
        self.assertEqual(CODEC.schemas[USE]['opcode'], 2622)
        self.assertEqual([(f['name'], f['type']) for f in CODEC.schemas[USE]['fields']],
                         [('cid', 'uint'), ('indexs', 'array|uint')])

    def test_the_three_dead_protocols_have_no_opcode_and_no_schema(self):
        # GMsgNo.lua 中 0 命中、endpoints.json 无 schema、HANDLERS 未注册 ⇒ 不可达。
        text = (ROOT / '03-unpack' / 'lua' / 'device-luascripts' / 'GMsgNo.lua').read_text('utf-8-sig')
        for token in ('RandSubTalent', 'SetReplaceSubTalent', 'OpenSubTalentSlot'):
            with self.subTest(token=token):
                self.assertEqual(text.count(token), 0)
        for name in ('PlayerProto:RandSubTalent', 'PlayerProto:SetReplaceSubTalent',
                     'PlayerProto:OpenSubTalentSlot'):
            with self.subTest(name=name):
                self.assertNotIn(name, CODEC.schemas)
                self.assertNotIn(name, HANDLERS)
        self.assertIn(UPGRADE, HANDLERS)
        self.assertIn(USE, HANDLERS)
        self.assertEqual((CODEC.schemas[UPGRADE]['opcode'], CODEC.schemas[UPGRADE_RET]['opcode'],
                          CODEC.schemas[USE]['opcode']), (2620, 2621, 2622))

    # ================= D. 数据表与客户端权威的一致性 =================
    def test_generated_card_pools_match_the_client_card_table_exactly(self):
        data = catalog()
        table = client_card_table()
        expected = {str(int(key)): int(row['subTfSkills'][0])
                    for key, row in table.items() if row.get('subTfSkills')}
        self.assertEqual(len(expected), 556)
        actual = {str(key): int(value) for key, value in data['cardPools'].items()}
        # 全量对账（不是抽样）：任何漏卡都会让客户端出现"有槽位、服务端不发 had"的锁面板。
        self.assertEqual(set(actual), set(expected))
        self.assertEqual(actual, expected)
        self.assertEqual(data['counts']['card_pools'], len(expected))

    def test_card_pools_sample_of_ten_including_the_main_character_card(self):
        data = catalog()
        table = client_card_table()
        # 71020 是"客户端有池、admin-role-templates.json 无该条"的卡（实测），必须在内。
        sample = [STARTER_CFGID, MAIN_CHARACTER_CFGID, NO_POOL_CFGID, 10010, 10110,
                  60060, 70410, 40210, 10230, 30200]
        self.assertEqual(len(sample), 10)
        self.assertEqual(len(set(sample)), 10)
        for cfgid in sample:
            with self.subTest(cfgid=cfgid):
                self.assertIn(cfgid, table)
                self.assertEqual(int(data['cardPools'][str(cfgid)]),
                                 int(table[cfgid]['subTfSkills'][0]))
        # 起始卡必须能开出池：否则本文件大部分行为断言失去意义。
        self.assertEqual(int(data['cardPools'][str(STARTER_CFGID)]), POOL)

    def test_generated_tables_are_consistent_with_the_client_lua(self):
        data = catalog()
        self.assertEqual(data['openCnt'],
                         {'1': 0, '2': 1, '3': 2, '4': 3, '5': 4, '6': 4, '7': 4})
        self.assertEqual(len(data['pools']), 148)
        self.assertEqual(len(data['skills']), 2146)
        self.assertEqual(len(data['materials']), 24)
        self.assertEqual(len(data['types']), 3)
        # 每个池恒 4 项、index 恒为 1..4（RoleCenter.lua:261-282 按 ipairs 遍历）。
        for key, pool in data['pools'].items():
            with self.subTest(pool=key):
                self.assertEqual([int(row['index']) for row in pool['ids']], [1, 2, 3, 4])
        # next_id 链：目标必须存在、同组、lv+1；costId 与 next_id 同现同缺。
        tails = 0
        for key, skill in data['skills'].items():
            advanced = skill.get('next_id')
            if advanced is None:
                tails += 1
                continue
            with self.subTest(skill=key):
                target = data['skills'].get(str(int(advanced)))
                self.assertIsNotNone(target)
                self.assertEqual(int(target['group']), int(skill['group']))
                self.assertEqual(int(target['lv']), int(skill['lv']) + 1)
                self.assertIsNotNone(skill.get('costId'))
                self.assertIn(str(int(skill['costId'])), data['materials'])
        self.assertEqual(tails, 430)
        # 起始卡的链必须在表里完整存在（本文件用到的固定值来自这里）。
        self.assertEqual(data['pools'][str(POOL)]['ids'][0]['id'], STARTING[0])
        chain, current = [], STARTING[0]
        while current is not None:
            chain.append(current)
            current = data['skills'][str(current)].get('next_id')
        self.assertEqual(chain, [STARTING[0], SECOND[0], THIRD[0], 302404, TAIL[0]])

    def test_generated_table_records_the_missing_pool_ids_it_could_not_serve(self):
        data = catalog()
        missing = sorted(int(x) for x in
                         data['validation']['card_pools_missing_from_CfgSubTalentSkillPool'])
        # 81 个卡模板的池 id 不在池表里（实测），build 脚本必须显式记录而不是静默丢弃。
        self.assertEqual(len(missing), 81)
        self.assertEqual(data['counts']['missing_pool_ids'], 81)
        self.assertIn(NO_POOL_CFGID, missing)
        for pool in missing:
            self.assertNotIn(str(pool), data['pools'])
        # admin 模板只作核对：它有的必定也被客户端覆盖，反向不成立。
        crosscheck = data['crosscheck']
        self.assertEqual(crosscheck['admin_entries'], 217)
        self.assertEqual(crosscheck['conflicts'], [])
        self.assertEqual(crosscheck['admin_only'], [])

    def test_generated_table_traces_back_to_the_exact_source_bytes(self):
        data = catalog()
        self.assertTrue(data['sources'])
        for source in data['sources']:
            with self.subTest(source=source['file']):
                recorded = hashlib.sha256((ROOT / source['file']).read_bytes()).hexdigest()
                self.assertEqual(recorded, source['sha256'])

    # ================= E. 对抗性审查 =================
    async def test_handlers_reject_boolean_float_and_text_ids_when_bypassing_the_codec(self):
        # bool 是 int 的子类，float/str/None 也不是合法 uint：绕过 codec 时必须显式拒绝。
        self.patch_card(break_level=7, sub_talent={})
        self.fund({item: amount * 4 for item, amount in LV1_COSTS.items()})
        for index in (True, False, 1.0, '1', None, [1], {}):
            with self.subTest(index=str(index)):
                await self.direct_rejects(sub_talent.upgrade_sub_talent,
                                          {'cid': self.cid, 'index': index})
        for identifier in (0, -1, True, None, 'x', 1.0):
            with self.subTest(cid=str(identifier)):
                await self.direct_rejects(sub_talent.upgrade_sub_talent,
                                          {'cid': identifier, 'index': 1})

    async def test_upgrade_rejects_a_missing_data_table_instead_of_breaking_import(self):
        # 数据表缺失必须是业务拒绝（Tips），不是 import 期崩溃。
        saved = sub_talent.DATA
        try:
            sub_talent.catalog.cache_clear()
            sub_talent.DATA = Path(self.temp.name) / 'absent.json'
            self.patch_card(break_level=7, sub_talent={})
            error = await self.rejects(UPGRADE, {'cid': self.cid, 'index': 1})
            self.assertIn('数据表', str(error))
        finally:
            sub_talent.DATA = saved
            sub_talent.catalog.cache_clear()

    async def test_a_hostile_reply_is_never_encoded_above_the_frame_limit(self):
        # 最坏情况也远小于 32767：CardUpdate 只带一张卡 + 一次 2621。
        self.patch_card(break_level=7, sub_talent={})
        self.fund(LV1_COSTS)
        replies = await self.request(UPGRADE, {'cid': self.cid, 'index': 1})
        sizes = [len(CODEC.encode_frame(r.name, r.fields)) for r in replies]
        self.assertTrue(all(size < MAX_FRAME for size in sizes), max(sizes))


def slot_costs(slot):
    """The material costs of upgrading one slot of the starter card, read from the table.

    slot n's starting skill comes from pool POOL's ids[n]; the cost comes from its
    CfgSubTalentMaterial row. Reading it keeps the test honest if the table is rebuilt.
    """
    data = catalog()
    start = int(data['pools'][str(POOL)]['ids'][slot - 1]['id'])
    skill = data['skills'][str(start)]
    material = data['materials'][str(int(skill['costId']))]
    return {int(item): int(amount) for item, amount in material['costs']}


def sub_talent_config(state, card):
    """The card's client config row through the shared cfgCardData reader."""
    from handlers.cards_items import keyed_config
    return keyed_config('cfgCardData.lua', card['cfgid'])


if __name__ == '__main__':
    unittest.main()
