import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from database import Store, StorageError
from server_core import Context, LocalServer, heartbeat, load_dependencies
from admin_control import execute

CODEC, SEED = load_dependencies(SERVER.parent / '05-protocol/endpoints.json',
                                SERVER / 'data/new_account_seed.json')


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SERVER / 'tests', prefix='admin-')
        self.path = Path(self.temp.name) / 'players.sqlite3'
        self.store = Store(self.path)
        self.uid = self.store.create_account('admin-test', SEED)['uid']

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def payload(self, **fields):
        return dict(uid=self.uid, request_id=str(uuid.uuid4()), **fields)

    def revision(self):
        return self.store.connection.execute('SELECT revision FROM accounts WHERE uid=?', (self.uid,)).fetchone()[0]

    def test_resource_retry_is_exactly_once_and_reopen_persists(self):
        payload = self.payload(key='gold', mode='add', amount=345)
        result = execute(self.store, 'resource', payload, CODEC)
        revision = self.revision()
        again = execute(self.store, 'resource', payload, CODEC)
        self.assertEqual(result['after'], 1345)
        self.assertTrue(again['replayed'])
        self.assertEqual(self.revision(), revision)
        with self.assertRaises(StorageError):
            execute(self.store, 'resource', {**payload, 'amount': 346}, CODEC)
        self.store.close()
        self.store = Store(self.path)
        state = self.store.get_player(self.uid)
        self.assertEqual(state['player']['gold'], 1345)
        self.assertEqual(state['inventory']['10001'], 1345)
        self.assertIn(payload['request_id'], state['control_receipts'])
        self.assertIn('gold', state['control_pending']['resources'])

    def test_clear_mail_preserves_assets_receipts_and_retries(self):
        from mail_service import send_mail
        with self.store.transaction(self.uid) as tx:
            first = send_mail(tx, '未领', '', [{'id': 10001, 'num': 50, 'type': 2}])
            second = send_mail(tx, '已领', '', [])
            tx.state['mailbox']['messages'][str(second)].update(is_get=2, claim_receipt={'kept': True})
        before = self.store.get_player(self.uid)
        payload = self.payload()
        result = execute(self.store, 'clear_mail', payload, CODEC)
        revision = self.revision()
        self.assertEqual(result['removed_ids'], [first, second])
        self.assertTrue(execute(self.store, 'clear_mail', payload, CODEC)['replayed'])
        self.assertEqual(self.revision(), revision)
        state = self.store.get_player(self.uid)
        self.assertEqual(state['inventory'], before['inventory'])
        self.assertEqual(state['player'], before['player'])
        self.assertEqual(state['mailbox']['messages'][str(second)]['claim_receipt'], {'kept': True})
        self.assertTrue(all('deleted_at' in row for row in state['mailbox']['messages'].values()))
        ctx = Context(LocalServer(CODEC, self.store, SEED), 'game', self.uid, True)
        replies = asyncio.run(heartbeat(ctx, {}))
        cleared = next(reply for reply in replies if reply.name == 'MailProto:MailsOperateRet')
        self.assertEqual(cleared.fields, {'ids': [first, second], 'operate_type': 3})
        CODEC.encode_frame(cleared.name, cleared.fields)

    def test_bad_amount_unknown_uid_and_fields_rollback_receipt_assets_outbox(self):
        before, revision = self.store.get_player(self.uid), self.revision()
        for payload in (self.payload(key='gold', mode='add', amount=-99999),
                        self.payload(key='gold', mode='set', amount=True),
                        self.payload(key='gold', mode='set', amount=2147483648),
                        self.payload(key='gold', mode='set', amount=5, extra='x'),
                        self.payload(key='item:99999999', mode='add', amount=1)):
            with self.subTest(payload=payload):
                with self.assertRaises(StorageError):
                    execute(self.store, 'resource', payload, CODEC)
                self.assertEqual(self.store.get_player(self.uid), before)
                self.assertEqual(self.revision(), revision)
        with self.assertRaises(StorageError):
            execute(self.store, 'resource', {**self.payload(key='gold', mode='add', amount=1), 'uid': self.uid + 1}, CODEC)

    def test_heartbeat_push_reads_latest_balance_and_consumes_durable_notification(self):
        execute(self.store, 'resource', self.payload(key='gold', mode='add', amount=50), CODEC)
        with self.store.transaction(self.uid) as tx:
            tx.add_currency('gold', -20)
        ctx = Context(LocalServer(CODEC, self.store, SEED), 'game', self.uid, True)
        replies = asyncio.run(heartbeat(ctx, {}))
        self.assertGreater(len(replies), 1)
        for reply in replies:
            decoded = CODEC.decode_frame(CODEC.encode_frame(reply.name, reply.fields))
            self.assertEqual(decoded.name, reply.name)
        self.assertNotIn('control_pending', self.store.get_player(self.uid))
        self.assertEqual(len(asyncio.run(heartbeat(ctx, {}))), 1)
        self.assertEqual(self.store.get_player(self.uid)['player']['gold'], 1030)

    def test_mail_send_retry_does_not_grant_attachment_or_duplicate_mail(self):
        payload = self.payload(title='测试邮件', content='本地附件测试',
                               attachments=[{'cfgid': 10001, 'num': 25}], expires_days=7)
        result = execute(self.store, 'mail', payload, CODEC)
        revision = self.revision()
        repeat = execute(self.store, 'mail', payload, CODEC)
        self.assertEqual(repeat['mail_id'], result['mail_id'])
        self.assertEqual(self.revision(), revision)
        self.assertEqual(self.store.get_player(self.uid)['player']['gold'], 1000)
        self.assertEqual(self.store.get_player(self.uid)['control_pending']['mail_ids'], [result['mail_id']])

    def test_invalid_mail_attachment_and_deadline_are_atomic(self):
        before = deepcopy(self.store.get_player(self.uid))
        for rows, days in (([{'cfgid': 10001, 'num': -1}], 7),
                           ([{'cfgid': 99999999, 'num': 1}], 7),
                           ([{'cfgid': 10059, 'num': 100}, {'cfgid': 10059, 'num': 1}], 7),
                           ([{'cfgid': 10001, 'num': 1}], 0)):
            with self.assertRaises(StorageError):
                execute(self.store, 'mail', self.payload(title='测试', content='正文',
                        attachments=rows, expires_days=days), CODEC)
            self.assertEqual(self.store.get_player(self.uid), before)

    def test_role_grant_retry_and_wire_notification(self):
        import admin_roles
        roles = admin_roles.catalog()['roles']
        selected = next(row for row in roles if row['cfgid'] not in (71010, 71020))
        payload = self.payload(cfgid=selected['cfgid'])
        result = execute(self.store, 'role', payload, CODEC)
        count = len(self.store.get_player(self.uid)['cards'])
        repeat = execute(self.store, 'role', payload, CODEC)
        self.assertEqual(result['cid'], repeat['cid'])
        self.assertEqual(len(self.store.get_player(self.uid)['cards']), count)
        ctx = Context(LocalServer(CODEC, self.store, SEED), 'game', self.uid, True)
        for reply in asyncio.run(heartbeat(ctx, {})):
            CODEC.encode_frame(reply.name, reply.fields)

    def test_all_content_access_changes_no_clear_star_or_asset_history(self):
        before = self.store.get_player(self.uid)
        payload = self.payload(enabled=True)
        execute(self.store, 'access', payload, CODEC)
        state = self.store.get_player(self.uid)
        self.assertFalse(state['offline_unlock_all'])
        self.assertEqual(state['offline_access'], dict.fromkeys(('pools','activities','illustrations'),True))
        self.assertEqual(state['progress'], before['progress'])
        self.assertEqual(state['cards'], before['cards'])
        self.assertEqual(state['inventory'], before['inventory'])
        ctx = Context(LocalServer(CODEC, self.store, SEED), 'game', self.uid, True)
        replies = asyncio.run(heartbeat(ctx, {}))
        self.assertEqual(replies[0].fields['key'], 'crosscore_ps_access_v2')
        self.assertEqual(json.loads(replies[0].fields['data']), state['offline_access'])
        for reply in replies:
            CODEC.encode_frame(reply.name, reply.fields)

    def test_archive_limited_pools_show_and_retry_close_without_rewards(self):
        import admin_roles
        pool_ids = [row['id'] for row in admin_roles.catalog()['pools'] if row['limited']]
        before = self.store.get_player(self.uid)
        payload = self.payload(pool_ids=pool_ids, enabled=True)
        result = execute(self.store, 'pools', payload, CODEC)
        self.assertEqual(sorted(result['offline_archive_pools']), sorted(pool_ids))
        self.assertTrue(execute(self.store, 'pools', payload, CODEC)['replayed'])
        state = self.store.get_player(self.uid)
        self.assertEqual(state['inventory'], before['inventory'])
        self.assertEqual(state['cards'], before['cards'])
        execute(self.store, 'pools', self.payload(pool_ids=pool_ids, enabled=False), CODEC)
        self.assertEqual(self.store.get_player(self.uid)['offline_archive_pools'], [])


if __name__ == '__main__':
    unittest.main()
