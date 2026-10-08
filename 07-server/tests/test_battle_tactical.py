"""Synthetic source-map regressions, not observed official gameplay traffic."""
from collections import deque
from copy import deepcopy
import unittest
import test_battle as battle_tests
from database import StorageError
from database import Store
from handlers import battle_tactical as tactical, battle, gacha, progression

class TacticalTests(unittest.TestCase):
    def setUp(self):
        battle_tests.BattleTests.setUp(self)
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['cleared_stages'].append(1090)
    tearDown = battle_tests.BattleTests.tearDown
    get = battle_tests.BattleTests.get
    call = battle_tests.BattleTests.call

    def enter(self, teams=None):
        fields = {'index': 1, 'nDuplicateID': 12301}
        if teams:
            fields['data'] = teams
        replies = self.call('FightProtocol:EnterDuplicate', fields)
        self.assertIn('FightProto:DuplicateData', [row.name for row in replies])
        return replies

    def walk(self, target, oid=1):
        data = self.get()['active_duplicate']
        actor = tactical.unit(data, oid, 1)
        graph = tactical.geometry(data['stage_id'])
        blocked = {row['pos'] for row in data['units'] if row['oid'] != oid and row['state'] != 2 and row['type'] in [1, 3] and row['pos'] != target}
        queue = deque([(actor['pos'], [])])
        seen = {actor['pos']}
        route = None
        while queue:
            current, path = queue.popleft()
            if current == target:
                route = path
                break
            for dest in graph[current]['edges']:
                if dest not in seen and dest not in blocked and abs(graph[current]['height'] - graph[dest]['height']) <= actor['nJump']:
                    seen.add(dest)
                    queue.append((dest, path + [dest]))
        self.assertIsNotNone(route, 'Source map should provide a route')
        replies = []
        for offset in range(0, len(route), actor['nStep']):
            replies.extend(self.call('FightProtocol:MoveTo', {'index': 1, 'oid': oid, 'path': route[offset:offset + actor['nStep']]}))
        return replies

    def fight(self, enemy, oid=1, won=True, ratio=1):
        self.walk(enemy['pos'], oid)
        replies = self.call('FightProtocol:EnterFight', {'index': 1, 'myOID': oid, 'monsterOID': enemy['oid']})
        self.fight_data = next(row.fields for row in replies if row.name == 'FightProto:SingleFight')
        cards = self.fight_data['data']['data']
        hp = {str(row['data']['cuid']): {'hp': int(row['data']['maxhp'] * ratio), 'maxhp': row['data']['maxhp'], 'sp': 9} for row in cards} if won else {}
        report = {'winer': 1 if won else 2, 'myOID': oid, 'monsterOID': enemy['oid'], 'data': hp,
                  'nGrade': [1, 1, 1] if won else [0, 0, 0],
                  'exdata': {'turnNum': 5, 'deathCnt': 0, 'cardCnt': len(cards), 'nMinHpPercent': int(100 * ratio) if won else 0}}
        return self.call('FightProtocol:OnFightOver', report)

    def enemy(self, category):
        data = self.get()['active_duplicate']
        return next(row for row in data['units'] if row['type'] == 3 and row['state'] != 2 and
                    battle.MONSTERS[str(row['nMonsterGroupID'])]['type'] == category)

    def test_geometry_spawn_restore_and_entry_guards(self):
        self.enter()
        state = self.get()
        data = state['active_duplicate']
        self.assertEqual(len([row for row in data['units'] if row['type'] == 3]), 5)
        actor = tactical.unit(data, 1, 1)
        self.assertIn(actor['pos'], [10102, 10104])
        self.assertEqual((actor['nStep'], actor['nJump']), (2, 2))
        self.assertNotIn(10501, tactical.geometry(12301))
        self.assertEqual(self.call('FightProtocol:ReqDuplicateData', {'index': 1, 'nDuplicateID': 12301})[0].fields,
                         tactical.payload(data))
        self.assertEqual(state, self.get())
        before = self.get()
        with self.assertRaises(StorageError):
            self.call('FightProtocol:EnterDuplicate', {'nDuplicateID': 12301})
        self.assertEqual(before, self.get())
        self.call('FightProtocol:QuitDuplicate', {'nDuplicateID': 12301})
        before = self.get()
        with self.assertRaises(StorageError):
            self.call('FightProtocol:EnterDuplicate', {'nDuplicateID': 12301, 'data': [1, 1]})
        self.assertEqual(before, self.get())
        with self.assertRaises(StorageError):
            self.call('FightProtocol:EnterDuplicate', {'nDuplicateID': 10001})
        self.assertEqual(before, self.get())

    def test_invalid_path_and_unencountered_combat_are_atomic(self):
        self.enter()
        before = self.get()
        for path in [[10603], [10501], [10101, 10201, 10301]]:
            with self.assertRaises(StorageError):
                self.call('FightProtocol:MoveTo', {'oid': 1, 'path': path})
            self.assertEqual(before, self.get())
        with self.assertRaises(StorageError):
            self.call('FightProtocol:EnterFight', {'myOID': 1, 'monsterOID': self.enemy(1)['oid']})
        self.assertEqual(before, self.get())

    def test_overkill_hp_persists_as_zero_and_counts_death(self):
        with self.store.transaction(self.uid) as tx:
            _, card, _, _ = gacha.award(tx, 30200, {'nType': 1})
            tx.state['teams'][0]['data'].append({'cid': card['cid'], 'index': 2, 'row': 1, 'col': 1})
        self.enter()
        enemy = self.enemy(1)
        self.walk(enemy['pos'])
        replies = self.call('FightProtocol:EnterFight', {'index': 1, 'myOID': 1, 'monsterOID': enemy['oid']})
        entry = next(row.fields for row in replies if row.name == 'FightProto:SingleFight')
        cards = entry['data']['data']
        hp = {str(row['data']['cuid']): {'hp': row['data']['maxhp'], 'maxhp': row['data']['maxhp'], 'sp': 0}
              for row in cards}
        hp[str(cards[0]['data']['cuid'])]['hp'] = -10
        self.call('FightProtocol:OnFightOver', {'winer': 1, 'myOID': 1, 'monsterOID': enemy['oid'],
                  'data': hp, 'nGrade': [1, 0, 1],
                  'exdata': {'turnNum': 5, 'deathCnt': 1, 'cardCnt': 2, 'nMinHpPercent': 0}})
        data = self.get()['active_duplicate']
        actor = tactical.unit(data, 1, 1)
        self.assertEqual(actor['team'][0]['hp'], 0)
        self.assertEqual(actor['_pve']['data'][0]['data']['hp'], 0)
        self.assertEqual(data['deaths'], 1)
        self.assertNotIn('active_battle', self.get())

    def test_encounter_hp_props_first_chest_and_boss_completion(self):
        self.enter()
        initial = self.get()
        replies = self.fight(self.enemy(1), ratio=0.5)
        self.assertNotIn('FightProto:DuplicateOver', [row.name for row in replies])
        data = self.get()['active_duplicate']
        actor = tactical.unit(data, 1, 1)
        maximum = actor['_pve']['data'][0]['data']['maxhp']
        self.assertEqual(actor['team'][0]['hp'], int(maximum * 0.5))
        self.assertEqual(actor['team'][0]['sp'], 9)
        self.assertEqual(data['normal_kills'], 1)
        self.assertNotIn(12301, self.get()['progress']['cleared_stages'])
        self.store.close()
        self.store = Store(self.temp.name + '/state.sqlite3')
        self.server.store = self.store
        restored = self.call('FightProtocol:ReqDuplicateData', {'nDuplicateID': 12301})[0].fields
        self.assertEqual(restored['nKillCount'], 1)
        self.assertEqual(next(row for row in restored['arrChar'] if row['oid'] == 1)['team'][0]['hp'], int(maximum * 0.5))
        heal = next(row for row in data['units'] if row['type'] == 4)
        self.walk(heal['pos'])
        actor = tactical.unit(self.get()['active_duplicate'], 1, 1)
        self.assertEqual(actor['team'][0]['hp'], int(maximum * 0.5) + int(maximum * 0.3))
        self.fight(self.enemy(1), ratio=0.8)
        self.assertEqual(self.fight_data['data']['data'][0]['data']['hp'], int(maximum * 0.5) + int(maximum * 0.3))
        data = self.get()['active_duplicate']
        chest = next(row for row in data['units'] if row['type'] == 4 and row['_prop'] == 2)
        replies = self.walk(chest['pos'])
        after_chest = self.get()
        self.assertEqual(after_chest['player']['gold'], initial['player']['gold'] + 20000)
        self.assertEqual(after_chest['store_exp'], initial['store_exp'] + 20000)
        self.assertEqual(after_chest['inventory']['14209'], 1)
        self.assertIn('FightProto:UseProp', [row.name for row in replies])
        self.assertNotIn('10003', after_chest['inventory'])
        replies = self.fight(self.enemy(3))
        completed = self.get()
        self.assertNotIn('active_duplicate', completed)
        self.assertIn(12301, completed['progress']['cleared_stages'])
        self.assertEqual(completed['progress']['mainLine'][-1]['data'], [1, 1, 1])
        self.assertIn('FightProto:DuplicateOver', [row.name for row in replies])
        self.enter()
        self.fight(self.enemy(1))
        self.fight(self.enemy(1))
        chest = next(row for row in self.get()['active_duplicate']['units'] if row['type'] == 4 and row['_prop'] == 2)
        before = self.get()
        self.walk(chest['pos'])
        after = self.get()
        self.assertEqual(after['inventory'], before['inventory'])
        self.assertEqual(after['store_exp'], before['store_exp'])
        self.assertEqual(after['player']['gold'], before['player']['gold'])

    def test_lost_party_keeps_second_team_and_quit_never_completes(self):
        with self.store.transaction(self.uid) as tx:
            card = gacha.award(tx, 30200, gacha.POOLS['1001'])[1]
            tx.state['teams'][1]['data'] = [{'cid': card['cid'], 'index': 1, 'row': 2, 'col': 2}]
            tx.state['teams'][1]['leader'] = card['cid']
            tx.state['teams'][1]['skill_group_id'] = 0
        self.enter([1, 2])
        replies = self.fight(self.enemy(1), won=False)
        self.assertNotIn('FightProto:DuplicateOver', [row.name for row in replies])
        data = self.get()['active_duplicate']
        self.assertEqual(tactical.unit(data, 1, 1)['state'], 2)
        self.assertEqual(tactical.unit(data, 2, 1)['state'], 1)
        self.assertNotIn(12301, self.get()['progress']['cleared_stages'])
        self.call('FightProtocol:QuitDuplicate', {'nDuplicateID': 12301})
        self.assertNotIn('active_duplicate', self.get())
        self.assertNotIn(12301, self.get()['progress']['cleared_stages'])

if __name__ == '__main__':
    unittest.main()
