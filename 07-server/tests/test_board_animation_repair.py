"""Restored model selection and bounded, idempotent existing-board migration."""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repair_board_animation import repair, migrate, RESTORED_MODELS
from panel_service import new_random_row, supports_animation
from skins_service import catalog


class BoardAnimationRepairTests(unittest.TestCase):
    def state(self):
        return {'other': {'currency': 123}, 'panels': {
            'using': 1, 'random': 0, 'setting': 3, 'update_time': 10,
            'panels': {'1': {'ids': [6031004], 'detail1': {
                'live2d': False, 'x': 55, 'y': -5, 'scale': 1.4, 'top': True}},
                '2': {'ids': [6015005], 'detail1': {'live2d': False}}},
            'random_panels': {'422': {'ids': [7805003, 6031003], 'bg': 7,
                'detail1': {'live2d': False}, 'detail2': {'live2d': False}},
                '501': {'ids': [4002005, 7102303],
                        'detail1': {'live2d': False}, 'detail2': {'live2d': False}}}}}

    def test_restored_models_can_select_dynamic_and_new_boards_default_to_it(self):
        for model in RESTORED_MODELS:
            self.assertTrue(catalog()['models'][str(model)]['has_l2d'])
            self.assertTrue(catalog()['base_characters'][str(model)]['has_l2d'])
            self.assertTrue(supports_animation(model))
            self.assertTrue(new_random_row({}, 2, model, 7)['detail1']['live2d'])

    def test_repair_preserves_layout_and_unrelated_disabled_skin_and_is_idempotent(self):
        state = self.state()
        expected = deepcopy(state)
        expected['panels']['panels']['1']['detail1']['live2d'] = True
        expected['panels']['random_panels']['422']['detail1']['live2d'] = True
        expected['panels']['panels']['2']['detail1']['live2d'] = True
        expected['panels']['random_panels']['501']['detail1']['live2d'] = True
        expected['panels']['random_panels']['501']['detail2']['live2d'] = True
        expected['panels']['update_time'] = 20
        self.assertEqual(len(repair(state, 20)), 5)
        self.assertEqual(state, expected)
        self.assertEqual(repair(state, 30), [])
        self.assertEqual(state, expected)

    def test_sqlite_backup_and_only_changed_accounts_increment_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            database, backup = Path(temp) / 'players.db', Path(temp) / 'before.db'
            old = self.state()
            with closing(sqlite3.connect(database, isolation_level=None)) as c:
                c.execute('CREATE TABLE accounts(uid INTEGER PRIMARY KEY,state_json TEXT,revision INTEGER)')
                c.executemany('INSERT INTO accounts VALUES(?,?,?)', [(1, json.dumps(old), 4), (2, '{}', 9)])
            result = migrate(database, backup)
            self.assertEqual([r['uid'] for r in result['accounts']], [1])
            with closing(sqlite3.connect(backup)) as c:
                self.assertEqual(json.loads(c.execute('SELECT state_json FROM accounts WHERE uid=1').fetchone()[0]), old)
            with closing(sqlite3.connect(database)) as c:
                self.assertEqual(c.execute('SELECT uid,revision FROM accounts ORDER BY uid').fetchall(), [(1, 5), (2, 9)])
            second = migrate(database, Path(temp) / 'second.db')
            self.assertEqual(second['accounts'], [])
            with closing(sqlite3.connect(database)) as c:
                self.assertEqual(c.execute('SELECT uid,revision FROM accounts ORDER BY uid').fetchall(), [(1, 5), (2, 9)])
