"""Account-item writes for every admin-items.json row, with risk warnings.

The game-handler gate (admin_resources.item_allowed) must stay narrow; only the
local control path (allow_any=True) may write domain/auto-use/expiring/typed rows.
"""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from database import Store, StorageError
from server_core import load_dependencies
import admin_control
import admin_resources as resources

CODEC, SEED = load_dependencies(SERVER.parent / '05-protocol/endpoints.json',
                                SERVER / 'data/new_account_seed.json')

# One representative of each class the old whitelist refused outright.
SAMPLES = (
    (10004, 'domain'),      # 探索经验, DOMAIN_ITEMS, type 1
    (17227, 'auto_use'),    # R3光幕芯片箱, stackable but auto_use
    (10408, 'expiry'),      # 扭蛋币, nExpiry/sExpiry
    (913027, 'type'),       # 加长围栏, type 12 outside STACK_TYPES
)


class AdminItemWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SERVER / 'tests', prefix='admin-items-')
        self.store = Store(Path(self.temp.name) / 'state.sqlite3')
        self.uid = self.store.create_account('item-write', SEED)['uid']

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def execute(self, key, mode, amount):
        payload = {'uid': self.uid, 'request_id': str(uuid.uuid4()),
                   'key': key, 'mode': mode, 'amount': amount}
        return admin_control.execute(self.store, 'resource', payload, CODEC), payload

    def revision(self):
        return self.store.connection.execute(
            'SELECT revision FROM accounts WHERE uid=?', (self.uid,)).fetchone()[0]

    def test_refused_classes_are_writable_but_warn(self):
        for cfgid, code in SAMPLES:
            with self.subTest(cfgid=cfgid):
                # The game-side gate is deliberately untouched.
                self.assertFalse(resources.item_allowed(cfgid))
                self.assertTrue(resources.item_present(cfgid))
                self.assertIn(code, resources.item_risks(cfgid))
                self.assertIn(code, [row['code'] for row in resources.item_warnings(cfgid)])
                result, unused = self.execute('item:' + str(cfgid), 'set', 4)
                self.assertEqual(result['after'], 4)
                self.assertEqual([row['code'] for row in result['warnings']],
                                 list(resources.item_risks(cfgid)))
                self.assertEqual(self.store.get_player(self.uid)['inventory'][str(cfgid)], 4)
                # add may be negative and lands on the requested total
                result, unused = self.execute('item:' + str(cfgid), 'add', -1)
                self.assertEqual(result['after'], 3)

    def test_every_table_row_is_present_and_unknown_ids_stay_refused(self):
        self.assertTrue(all(resources.item_present(int(key)) for key in resources.ITEMS))
        for cfgid in (10004, 10998, 12005, 60101, 37024004, 913027):
            self.assertTrue(resources.item_present(cfgid))
        before, revision = self.store.get_player(self.uid), self.revision()
        for key in ('item:999999999', 'item:0', 'item:01', 'item:abc', 'level', 'item:1e9'):
            with self.subTest(key=key):
                with self.assertRaises(StorageError):
                    self.execute(key, 'set', 1)
                self.assertEqual(self.store.get_player(self.uid), before)
                self.assertEqual(self.revision(), revision)

    def test_resource_keys_keep_exact_shape_and_strict_refusal(self):
        with self.store.transaction(self.uid) as tx:
            strict = resources.apply_resource(tx, 'gold', 'set', 7)
            self.assertEqual(set(strict), {'key', 'requested_key', 'cfgid', 'before', 'after', 'delta', 'max'})
            loose = resources.apply_resource(tx, 'item:60101', 'set', 3, allow_any=True)
            self.assertEqual(loose['warnings'], [])
            self.assertEqual(loose['key'], 'item:60101')
            with self.assertRaises(StorageError):
                resources.apply_resource(tx, 'item:10004', 'set', 1)

    def test_upper_limit_negative_add_and_atomic_rollback(self):
        self.assertEqual(resources.ITEMS['913027']['upperLimit'], 4)
        result, unused = self.execute('item:913027', 'set', 4)
        self.assertEqual((result['before'], result['after'], result['max']), (0, 4, 4))
        result, unused = self.execute('item:913027', 'add', -4)
        self.assertEqual(result['after'], 0)
        before, revision = self.store.get_player(self.uid), self.revision()
        for mode, amount in (('set', 5), ('add', -1), ('set', -1)):
            with self.subTest(mode=mode, amount=amount):
                with self.assertRaises(StorageError):
                    self.execute('item:913027', mode, amount)
                self.assertEqual(self.store.get_player(self.uid), before)
                self.assertEqual(self.revision(), revision)

    def test_receipt_idempotency_fingerprint_audit_and_outbox(self):
        payload = {'uid': self.uid, 'request_id': str(uuid.uuid4()),
                   'key': 'item:10004', 'mode': 'set', 'amount': 9}
        first = admin_control.execute(self.store, 'resource', payload, CODEC)
        revision = self.revision()
        again = admin_control.execute(self.store, 'resource', payload, CODEC)
        self.assertTrue(again['replayed'])
        self.assertEqual(again['warnings'], first['warnings'])
        self.assertEqual(self.revision(), revision)
        self.assertEqual(self.store.get_player(self.uid)['inventory']['10004'], 9)
        with self.assertRaises(StorageError):
            admin_control.execute(self.store, 'resource', {**payload, 'amount': 10}, CODEC)
        state = self.store.get_player(self.uid)
        self.assertIn(payload['request_id'], state['control_receipts'])
        self.assertEqual(state['control_pending']['resources'], ['item:10004'])
        self.assertEqual(len(state['control_audit']), 1)
        self.assertEqual(state['control_audit'][-1]['result']['after'], 9)
        self.assertEqual(state['control_audit'][-1]['action'], 'resource')

    def test_item_update_frames_wire_valid_and_outbox_consumed(self):
        self.execute('item:10004', 'set', 5)
        replies = admin_control.consume_notifications(self.store, self.uid, CODEC)
        self.assertIn('PlayerProto:ItemUpdate', [reply.name for reply in replies])
        rows = [row for reply in replies if reply.name == 'PlayerProto:ItemUpdate'
                for row in reply.fields['data']]
        self.assertEqual(next(row for row in rows if row['id'] == 10004)['num'], 5)
        for reply in replies:
            decoded = CODEC.decode_frame(CODEC.encode_frame(reply.name, reply.fields))
            self.assertEqual(decoded.name, reply.name)
        self.assertNotIn('control_pending', self.store.get_player(self.uid))
        self.assertEqual(admin_control.consume_notifications(self.store, self.uid, CODEC), [])

    def test_inventory_refresh_stays_chunked_at_128_rows(self):
        with self.store.transaction(self.uid) as tx:
            for key in list(resources.ITEMS)[:200]:
                tx.state['inventory'].setdefault(key, 1)
            expected = len(tx.state['inventory'])
            replies = resources.resource_pushes(tx.state, ['inventory'], allow_any=True)
        self.assertGreater(expected, 128)
        chunks = [reply for reply in replies if reply.name == 'PlayerProto:ItemUpdate']
        self.assertGreater(len(chunks), 1)
        self.assertEqual(sum(len(reply.fields['data']) for reply in chunks), expected)
        self.assertTrue(all(len(reply.fields['data']) <= 128 for reply in chunks))
        for reply in replies:
            self.assertEqual(CODEC.decode_frame(CODEC.encode_frame(reply.name, reply.fields)).name,
                             reply.name)

    def test_catalog_exposes_risks_and_keeps_the_mail_gate(self):
        value = resources.catalog()
        self.assertEqual(len(value['items']), len(resources.ITEMS))
        self.assertTrue(all(row['supported'] for row in value['items']))
        self.assertEqual(set(value['risk_labels']), set(resources.ITEM_RISKS))
        rows = {row['cfgid']: row for row in value['items']}
        self.assertEqual(rows[10004]['risks'], ['domain'])
        self.assertEqual(rows[10004]['max'], 2147483647)
        self.assertEqual(rows[17227]['risks'], ['auto_use'])
        self.assertEqual(rows[10408]['risks'], ['expiry'])
        self.assertEqual(rows[37024004]['risks'], ['type'])
        self.assertEqual(rows[60101]['risks'], [])
        # Mail attachment eligibility still follows the game-handler gate.
        self.assertFalse(rows[10004]['mail_supported'])
        self.assertFalse(rows[10408]['mail_supported'])
        from gift_service import gift_allowed
        for cfgid in (60101, 10004, 17227, 37024004):
            self.assertEqual(rows[cfgid]['mail_supported'],
                             resources.item_allowed(cfgid) or gift_allowed(cfgid))


if __name__ == '__main__':
    unittest.main()
