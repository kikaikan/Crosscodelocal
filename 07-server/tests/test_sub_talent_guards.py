"""task-27：副天赋三处守卫收敛 + CardCalculator 属性移植 + 战斗载荷。

权威：CardCalculator.lua:410-443（nFightSkillId → ret.skills；jPropertys 经
CfgCardPropertyEnum.sFieldName 累加）、TakePropertyAdd(CardCalculator.lua:280-320)、
CharacterCardsData.lua:1169（special20 门槛）、GameMsg.lua:21（CardData.use_sub_talent
= array|int）、FightRoleInfo.lua:431-455（助战卡读 use_sub_talent）。
决策口径：未装备(use 全 0)=无属性影响必须放行；
已装备=有影响，只有精确移植后才放行。
"""
import asyncio
from copy import deepcopy
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
from database import Store, StorageError
from server_core import Context, LocalServer, load_dependencies
from handlers import battle, cards_items, sub_talent
import equipment_stats

CODEC, SEED = load_dependencies(ROOT / '05-protocol' / 'endpoints.json',
                                SERVER / 'data/new_account_seed.json')
CATALOG = json.loads((SERVER / 'data' / 'sub-talent.json').read_text(encoding='utf-8'))
# special20 = 「跃升天赋」，条件 rules[2006] = 通关关卡 1006（cfgCfgOpenConditionMore.lua）。
UNLOCK_STAGE = 1006
MAX_FRAME = 32767


class SubTalentGuardTests(unittest.TestCase):
    # 同步 TestCase（同 tests/test_battle.py）：call() 里用 asyncio.run 驱动真实 dispatcher。
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SERVER / 'tests', prefix='sub-guard-')
        self.store = Store(Path(self.temp.name) / 'state.sqlite3')
        self.uid = self.store.create_account('sub-talent-guard', SEED)['uid']
        self.server = LocalServer(CODEC, self.store, SEED)
        self.ctx = Context(self.server, 'game', self.uid, True)
        with self.store.transaction(self.uid) as tx:
            progress = tx.state['progress']
            progress['cleared_stages'] = sorted(set(progress.get('cleared_stages', [])) | {UNLOCK_STAGE})
            tx.state['store_exp'] = 100000

    def tearDown(self):
        self.store.close()
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to((SERVER / 'tests').resolve()))
        self.temp.cleanup()

    def state(self):
        return self.store.get_player(self.uid)

    def own(self, cid=1):
        return next(card for card in self.state()['cards'] if card['cid'] == cid)

    def config(self, cid=1):
        return cards_items.keyed_config('cfgCardData.lua', self.own(cid)['cfgid'])

    def edit(self, function):
        with self.store.transaction(self.uid) as tx:
            return function(tx)

    def materialize(self, break_level=7):
        def apply(tx):
            card = tx.state['cards'][0]
            card['break_level'] = break_level
            return sub_talent.ensure_slots(card, cards_items.keyed_config('cfgCardData.lua', card['cfgid']))
        return self.edit(apply)

    def equip(self, ids):
        def apply(tx):
            tx.state['cards'][0]['sub_talent']['use'] = list(ids)
        self.edit(apply)

    def fund(self, costs):
        def apply(tx):
            for cfgid, amount in costs.items():
                tx.add_item(int(cfgid), int(amount))
        self.edit(apply)

    def call(self, name, fields):
        frame = CODEC.decode_frame(CODEC.encode_frame(name, fields))
        replies = asyncio.run(self.server.dispatch(self.ctx, frame))
        for reply in replies:
            raw = CODEC.encode_frame(reply.name, reply.fields)
            self.assertLess(len(raw), MAX_FRAME)
            self.assertEqual(CODEC.decode_frame(raw).name, reply.name)
        return replies

    def start_fight(self, stage=1001):
        replies = self.call('FightProtocol:EnterFightDuplicate', {'nDuplicateID': stage, 'nTeamIndex': 1})
        return next(reply.fields for reply in replies if reply.name == 'FightProto:SingleFight')

    # ── 守卫收敛（spec §7.1） ────────────────────────────────────────────────
    def test_idle_materialized_card_can_level_up_and_enter_battle(self):
        self.assertEqual(self.materialize(7), True)
        card = self.own()
        self.assertEqual(len(card['sub_talent']['had']), 4)
        self.assertEqual(card['sub_talent']['use'], [0, 0, 0, 0])
        self.assertEqual(sub_talent.active_ids(card), [])
        # 物化但未装备 → 升级必须放行（修复前这里会被 recalculate_bare_hp 拒绝）。
        replies = self.call('PlayerProto:CardUpgrade', {'cid': 1, 'use_store_exp': 100})
        self.assertEqual(replies[-1].name, 'PlayerProto:CardUpgradeRet')
        # 出战同样放行，且 use_sub_talent 为空数组（GameMsg.lua:21 的 array|int）。
        fight = self.start_fight()
        own = fight['data']['data'][0]['data']
        self.assertEqual(own['use_sub_talent'], [])
        self.assertEqual(own['cuid'], 1)

    def test_active_talent_blocks_the_bare_hp_path_only(self):
        self.materialize(7)
        learned = self.own()['sub_talent']['had']
        self.equip([learned[0], 0, 0, 0])
        self.assertEqual(sub_talent.active_ids(self.own()), [learned[0]])
        before = self.state()
        with self.assertRaises(StorageError) as raised:
            self.call('PlayerProto:CardUpgrade', {'cid': 1, 'use_store_exp': 100})
        self.assertIn('active secondary talent', str(raised.exception))
        self.assertEqual(self.state(), before)          # 事务回滚，存档未变
        # 出战不再被拒：属性由 equipment_stats 精确计入载荷（spec §7.3）。
        fight = self.start_fight()
        own = fight['data']['data'][0]['data']
        self.assertEqual(own['use_sub_talent'], [learned[0]])

    def test_break_materializes_slots_and_never_emits_zero(self):
        material = self.break_material(1)
        card = self.own()
        self.fund(self.break_costs(material))
        self.edit(lambda tx: tx.state['cards'][0].update(level=60, break_level=1))
        self.assertEqual(self.break_material(1)['materials'], material['materials'])
        replies = self.call('PlayerProto:CardBreak', {'cid': 1})
        names = [reply.name for reply in replies]
        self.assertIn('PlayerProto:CardBreakRet', names)     # 后面还可能跟 UpdateCardRole
        card = self.own()
        self.assertEqual(card['break_level'], 2)
        # 跃升挂钩：cnt(2)=1 → had 恰好一个非零 id，use 补足 4（含 0）。
        self.assertEqual(len(card['sub_talent']['had']), 1)
        self.assertTrue(all(value > 0 for value in card['sub_talent']['had']))
        self.assertEqual(len(card['sub_talent']['use']), 4)

    def break_material(self, jump):
        cfg = self.config()
        if jump < 5:
            return cards_items.keyed_config('cfgCardBreakMaterial.lua', cfg['break_id'] + jump - 1)
        return cards_items.indexed(cards_items.row('cfgCardBreakMaterial2.lua', cfg['quality'])['infos'], jump)

    def break_costs(self, material):
        costs = cards_items.costs_from_rows(material.get('materials', []))
        costs[10001] = costs.get(10001, 0) + int(material.get('gold', 0))
        return costs

    def test_percent_talent_survives_the_fight_frame_with_exact_attack(self):
        # 1015「武器精通」攻击 +10%：载荷必须能被 int 列编码，且数值与 CardCalculator 一致。
        self.materialize(7)
        self.equip([1015, 0, 0, 0])
        bare = self.bare_card()
        base = equipment_stats.card_base_stats(deepcopy(bare))
        fight = self.start_fight()
        own = fight['data']['data'][0]['data']
        self.assertEqual(own['use_sub_talent'], [1015])
        self.assertEqual(own['attack'], math.floor(base['attack'] * 1.1))
        self.assertIsInstance(own['attack'], int)
        self.assertEqual(own['maxhp'], math.floor(base['maxhp']))

    # ── 属性移植（CardCalculator.lua:410-443） ───────────────────────────────
    def test_property_port_matches_cardcalculator_percent(self):
        # 1015「武器精通」jPropertys = {{1, 0.1}} → 攻击 +10%（乘法区 cal1）。
        skill = CATALOG['skills']['1015']
        self.assertEqual(skill['jPropertys'], [[1, 0.1]])
        bare = self.bare_card()
        base = equipment_stats.card_base_stats(deepcopy(bare))
        active = deepcopy(bare)
        active['sub_talent'] = {'had': [1015], 'use': [1015]}
        result = equipment_stats.equipped_stats(self.state(), active)
        self.assertEqual(result['attack'], math.floor(base['attack'] * 1.1))
        self.assertLess(result['attack'], math.floor(base['attack'] * 1.1) + 1)
        # refresh_card_hp（装备/出战路径复用的 HP 刷新）也必须吃下该乘区：1015 只加攻击，
        # 所以 hp 与未装备时一致（正好验证乘区没有被错误地施加到 maxhp 上）。
        equipment_stats.refresh_card_hp(self.state(), active)
        self.assertEqual(active['hp'], result['maxhp'])
        self.assertEqual(active['hp'], math.floor(base['maxhp']))

    def test_talent_fight_skill_joins_the_skill_list(self):
        # 302401 只有 nFightSkillId（无 jPropertys）→ 进 skills，属性不变。
        record = CATALOG['skills']['302401']
        self.assertIn('nFightSkillId', record)
        self.assertNotIn('jPropertys', record)
        card = self.bare_card()
        idle = equipment_stats.equipped_stats(self.state(), deepcopy(card))
        card['sub_talent'] = {'had': [302401], 'use': [302401]}
        active = equipment_stats.equipped_stats(self.state(), card)
        self.assertIn(int(record['nFightSkillId']), active['skills'])
        self.assertNotIn(int(record['nFightSkillId']), idle['skills'])
        self.assertEqual(active['attack'], idle['attack'])

    def test_unknown_talent_id_is_skipped_like_the_client_warning(self):
        card = self.bare_card()
        card['sub_talent'] = {'had': [99999999], 'use': [99999999]}
        idle = equipment_stats.equipped_stats(self.state(), self.bare_card())
        result = equipment_stats.equipped_stats(self.state(), card)
        self.assertEqual(result['attack'], idle['attack'])
        self.assertEqual(result['skills'], idle['skills'])
        self.assertEqual(equipment_stats.validate_card_modifiers(card), None)

    def bare_card(self):
        card = deepcopy(self.own())
        card['equips'] = []
        card.pop('equip_ids', None)
        card.pop('sub_talent', None)
        card.pop('mix_data', None)
        return card

    # ── 数据表与口径一致性 ────────────────────────────────────────────────
    def test_catalog_carries_the_property_fields(self):
        skills = CATALOG['skills']
        with_props = [key for key, row in skills.items() if 'jPropertys' in row]
        with_fight = [key for key, row in skills.items() if 'nFightSkillId' in row]
        self.assertEqual(len(with_props), 95)
        self.assertEqual(len(with_fight), 2051)
        for key in with_props:
            for entry in skills[key]['jPropertys']:
                self.assertEqual(len(entry), 2)
                self.assertIsInstance(entry[0], int)
                self.assertIsInstance(entry[1], (int, float))

    def test_every_guard_site_reads_the_same_predicate(self):
        idle = self.own()
        idle['sub_talent'] = {'had': [1015], 'use': [0, 0, 0, 0]}
        active = self.own()
        active['sub_talent'] = {'had': [1015], 'use': [0, 1015, 0, 0]}
        for card, expected in ((idle, []), (active, [1015])):
            with self.subTest(use=card['sub_talent']['use']):
                self.assertEqual(sub_talent.active_ids(card), expected)
                self.assertEqual(equipment_stats.equipped_talents(card), expected)

    def test_ensure_slots_never_emits_zero_and_skips_unknown_pools(self):
        cfg = self.config()
        pool = sub_talent.starting_ids(sub_talent.pool_id(cfg))
        limit = sub_talent.open_slots(7)
        dirty = {'sub_talent': {'had': [0, 0, 'x', -5, pool[0]], 'use': [0, 0, 0, 0]}, 'break_level': 7}
        self.assertTrue(sub_talent.ensure_slots(dirty, cfg))
        self.assertEqual(dirty['sub_talent']['had'], [pool[0], pool[1], pool[2], pool[3]][:limit])
        self.assertNotIn(0, dirty['sub_talent']['had'])
        self.assertTrue(all(isinstance(value, int) and value > 0 for value in dirty['sub_talent']['had']))
        self.assertEqual(dirty['sub_talent']['use'], [0, 0, 0, 0])
        self.assertFalse(sub_talent.ensure_slots(dirty, cfg))     # 幂等
        unknown = {'sub_talent': {}, 'break_level': 7}
        self.assertFalse(sub_talent.ensure_slots(unknown, {'subTfSkills': [999999]}))
        self.assertEqual(unknown['sub_talent'], {})               # 不写任何东西（审计 F5）
