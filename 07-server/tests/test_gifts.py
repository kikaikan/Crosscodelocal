"""Source daily gifts: exact wire, restart, cadence and atomic rollback."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from database import Store, StorageError
from gift_service import (gift_allowed, catalog, grant_gift, refresh_gifts,
    render_member_infos, member_pushes, MAX_GRANT_QUANTITY, INT_MAX)
from mail_service import send_mail, message_rows, render_mail_pushes, claim_mail_batch, mailbox_limit
from server_core import Context, heartbeat
from handlers import gifts, mail
from protocol_codec import IVProtoCodec, WireConfig


def moment(day, hour=3, minute=0, second=0):
    return int(datetime(2026, 10, day, hour, minute, second,
        tzinfo=timezone(timedelta(hours=8))).timestamp())


class GiftTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='crosscore-gifts-')
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.store = Store(self.path)
        seed = json.loads((HERE / 'data/new_account_seed.json').read_text('utf-8'))
        self.uid = self.store.create_account('local-gift-test', seed)['uid']
        self.ctx = Context(SimpleNamespace(store=self.store), 'game', self.uid, True)
        self.now = moment(4, 13)
        with self.store.transaction(self.uid) as tx:
            tx.state['offline_clock'] = self.now
            tx.state['progress']['cleared_stages'] = [1001,1002,1003]
        self.codec = IVProtoCodec(json.loads((HERE.parent / '05-protocol/endpoints.json').read_text('utf-8')),
            WireConfig('little', max_frame_size=65535))
        self.ctx.server.codec = self.codec

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def state(self):
        return self.store.get_player(self.uid)

    def revision(self):
        return self.store.connection.execute('SELECT revision FROM accounts WHERE uid=?',
                                             (self.uid,)).fetchone()[0]

    def grant(self, cfgid=10059, num=1, now=None):
        with self.store.transaction(self.uid) as tx:
            return grant_gift(tx, cfgid, num, now)

    def refresh(self, now):
        with self.store.transaction(self.uid) as tx:
            return refresh_gifts(tx, now)

    def wire(self, replies):
        for reply in replies:
            raw = self.codec.encode_frame(reply.name, reply.fields)
            parsed = self.codec.decode_frame(raw)
            self.assertEqual(raw, self.codec.encode_frame(parsed.name, parsed.fields))
            self.assertLess(len(raw), 32768)

    async def test_activation_one_source_mail_no_direct_assets(self):
        before = self.state()
        result = self.grant()
        state = self.state()
        self.assertEqual((result['added_days'], result['remaining_days']), (5, 4))
        self.assertEqual(state['inventory'], before['inventory'])
        self.assertEqual(state['player'], before['player'])
        self.assertNotIn('10059', state['inventory'])
        row = message_rows(state)[str(result['mail_ids'][0])]
        self.assertEqual(row['data']['name'], '复归五日构建礼包奖励（剩余：4天）')
        self.assertEqual(row['data']['rewards'], [{'id':11002, 'num':1, 'type':2}])
        self.assertEqual(row['gift_receipt']['mail_cfgid'], 17001)
        self.assertEqual(render_member_infos(state), [{'c_time':self.now, 'item_id':10059, 'l_cnt':4}])
        self.wire(member_pushes(state) + render_mail_pushes(state, result['mail_ids']))

    async def test_daily_mail_claim_retry_one_blueprint(self):
        result = self.grant()
        identifier = str(result['mail_ids'][0])
        before = self.state()['inventory'].get('11002', 0)
        for _ in range(2):
            with self.store.transaction(self.uid) as tx:
                self.wire(claim_mail_batch(tx, [message_rows(tx.state)[identifier]], self.now))
        self.assertEqual(self.state()['inventory']['11002'], before + 1)
        self.assertEqual(render_member_infos(self.state())[0]['l_cnt'], 4)

    async def test_same_day_copies_extend_not_parallel_awards(self):
        self.grant()
        result = self.grant(num=2)
        self.assertEqual(result['remaining_days'], 14)
        self.assertEqual(result['mail_ids'], [])
        self.assertEqual(len(message_rows(self.state())), 1)
        self.assertEqual(self.refresh(self.now + 3600)['mail_ids'], [])

    async def test_three_am_boundary(self):
        self.grant(now=moment(5, 2, 59, 59))
        self.assertEqual(self.refresh(moment(5, 2, 59, 59))['mail_ids'], [])
        result = self.refresh(moment(5))
        self.assertEqual((len(result['mail_ids']), result['infos'][0]['l_cnt']), (1, 3))
        self.assertEqual(self.refresh(moment(5, 22))['mail_ids'], [])

    async def test_offline_gap_does_not_bulk_issue(self):
        self.grant()
        future = self.now + 365 * 86400
        result = self.refresh(future)
        self.assertEqual((len(result['mail_ids']), result['infos'][0]['l_cnt']), (1, 3))
        self.assertEqual(self.refresh(future)['mail_ids'], [])
        self.assertEqual(len(message_rows(self.state())), 2)

    async def test_restart_clock_rollback_do_not_reissue(self):
        self.grant()
        before = self.state()
        self.store.close()
        self.store = Store(self.path)
        self.ctx.server.store = self.store
        self.assertEqual(self.refresh(self.now)['mail_ids'], [])
        self.assertEqual(self.refresh(self.now - 86400)['mail_ids'], [])
        self.assertEqual(self.state(), before)

    async def test_full_mailbox_retains_entitlement(self):
        with self.store.transaction(self.uid) as tx:
            for _ in range(mailbox_limit()):
                send_mail(tx, '已有邮件', '', [], now=self.now)
        result = self.grant()
        self.assertEqual((result['remaining_days'], result['deferred']), (5, [10059]))
        self.assertEqual(result['mail_ids'], [])
        with self.store.transaction(self.uid) as tx:
            message_rows(tx.state)['1']['deleted_at'] = self.now
        result = self.refresh(self.now)
        self.assertEqual((len(result['mail_ids']), result['infos'][0]['l_cnt']), (1, 4))

    async def test_catalog_truth_and_four_source_recipes(self):
        rows = catalog()['gifts']
        self.assertEqual(len(rows), 10)
        self.assertEqual({r['cfgid'] for r in rows if r['supported']}, {10031,10050,10051,10059})
        for value in (True, '10059', 10001, 10030, 10032, 10056, 10058, 999999):
            self.assertFalse(gift_allowed(value))
        for cfgid, days, reward in ((10031,7,10037),(10050,7,11002),(10051,14,10037)):
            result = self.grant(cfgid)
            self.assertEqual(result['remaining_days'], days-1)
            row = message_rows(self.state())[str(result['mail_ids'][0])]
            self.assertTrue(any(r['id']==reward for r in row['data']['rewards']))
            self.wire(render_mail_pushes(self.state(), result['mail_ids']))

    async def test_invalid_quantity_recipe_and_time_roll_back(self):
        before = self.state()
        for cfgid, num, now in ((10059,True,self.now),(10059,0,self.now),
            (10059,MAX_GRANT_QUANTITY+1,self.now),(10059,1.0,self.now),
            (True,1,self.now),(10030,1,self.now),(10059,1,-1),(10059,1,True)):
            with self.assertRaises(StorageError):
                self.grant(cfgid, num, now)
            self.assertEqual(self.state(), before)

    async def test_signed_day_overflow_rolls_back(self):
        self.grant()
        with self.store.transaction(self.uid) as tx:
            tx.state['member_gifts']['entitlements']['10059'].update(
                remaining_days=0, issued_days=INT_MAX, total_days=INT_MAX)
        before = self.state()
        with self.assertRaises(StorageError):
            self.grant()
        self.assertEqual(self.state(), before)

    async def test_five_days_completion_and_same_day_renewal(self):
        self.grant()
        for day in range(5,9):
            self.assertEqual(len(self.refresh(moment(day))['mail_ids']), 1)
        self.assertEqual(render_member_infos(self.state())[0]['l_cnt'], 0)
        self.assertEqual(len(message_rows(self.state())), 5)
        self.assertEqual(self.refresh(moment(9))['mail_ids'], [])
        renewal = self.grant(now=moment(8,23))
        self.assertEqual((renewal['mail_ids'],renewal['remaining_days']), ([],5))
        self.assertEqual(len(self.refresh(moment(9))['mail_ids']), 1)

    async def test_read_handler_wire_and_login(self):
        self.grant()
        with self.store.transaction(self.uid) as tx:
            tx.state['offline_clock'] = moment(5)
        replies = await gifts.member_info(self.ctx,{})
        self.assertEqual([r.name for r in replies], ['ClientProto:GetMemberRewardInfoRet','MailProto:MailAddNotice'])
        self.assertEqual(replies[0].fields['infos'][0]['l_cnt'],3)
        self.wire(replies)
        before = deepcopy(self.state())
        revision = self.revision()
        self.assertEqual(len(await gifts.member_info(self.ctx,{})),1)
        self.assertEqual(self.state(),before)
        self.assertEqual(self.revision(), revision)
        self.ctx.logged_in=False
        with self.assertRaises(StorageError):
            await gifts.member_info(self.ctx,{})

    async def test_original_gift_mail_claim_twice_and_restart_activates_once(self):
        before = self.state()
        with self.store.transaction(self.uid) as tx:
            identifier = send_mail(tx, '复归礼包', '', [{'id':10059,'num':1,'type':2}], now=self.now)
        self.assertNotIn('member_gifts', self.state())
        first = await mail.operate(self.ctx, {'ids':[identifier], 'operate_type':2})
        self.wire(first)
        after = self.state()
        self.assertEqual(after['inventory'], before['inventory'])
        self.assertEqual(after['member_gifts']['entitlements']['10059']['total_days'],5)
        self.assertEqual(render_member_infos(after)[0]['l_cnt'],4)
        self.assertEqual(len(message_rows(after)),2)
        self.assertEqual(message_rows(after)[str(identifier)]['is_get'],2)
        self.assertIn('claim_receipt', message_rows(after)[str(identifier)])
        self.assertTrue(any(r.name=='MailProto:MailAddNotice' for r in first))
        self.assertTrue(any(r.name=='ClientProto:GetMemberRewardInfoRet' for r in first))
        self.store.close()
        self.store = Store(self.path)
        self.ctx.server.store = self.store
        repeated = await mail.operate(self.ctx, {'ids':[identifier], 'operate_type':2})
        self.wire(repeated)
        self.assertEqual(self.state(),after)

    async def test_heartbeat_no_entitlements_and_same_day_do_not_change_revision(self):
        before, revision = self.state(), self.revision()
        for _ in range(3):
            replies = await heartbeat(self.ctx, {})
            self.assertEqual([row.name for row in replies], ['LoginProto:Heartbeat'])
            self.wire(replies)
        self.assertEqual((await gifts.member_info(self.ctx, {}))[0].fields, {'infos': []})
        self.assertEqual(self.state(), before)
        self.assertEqual(self.revision(), revision)
        self.grant()
        before, revision = self.state(), self.revision()
        for _ in range(3):
            self.assertEqual(len(await heartbeat(self.ctx, {})), 1)
        self.assertEqual((await gifts.member_info(self.ctx, {}))[0].fields['infos'][0]['l_cnt'], 4)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.revision(), revision)

    async def test_heartbeat_three_am_issues_and_consumes_durable_daily_notice_once(self):
        self.grant(now=moment(5, 2, 59, 59))
        with self.store.transaction(self.uid) as tx:
            tx.state['offline_clock'] = moment(5, 2, 59, 59)
        revision = self.revision()
        self.assertEqual(len(await heartbeat(self.ctx, {})), 1)
        self.assertEqual(self.revision(), revision)
        with self.store.transaction(self.uid) as tx:
            tx.state['offline_clock'] = moment(5)
        replies = await heartbeat(self.ctx, {})
        self.assertEqual([row.name for row in replies],
                         ['MailProto:MailAddNotice', 'ClientProto:GetMemberRewardInfoRet', 'LoginProto:Heartbeat'])
        self.wire(replies)
        self.assertEqual(replies[1].fields['infos'][0]['l_cnt'], 3)
        self.assertEqual(len(message_rows(self.state())), 2)
        self.assertNotIn('control_pending', self.state())
        before, revision = self.state(), self.revision()
        self.store.close()
        self.store = Store(self.path)
        self.ctx.server.store = self.store
        self.assertEqual(len(await heartbeat(self.ctx, {})), 1)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.revision(), revision)

    async def test_full_mailbox_heartbeat_preserves_days_and_revision_until_space(self):
        self.grant()
        with self.store.transaction(self.uid) as tx:
            for _ in range(mailbox_limit() - 1):
                send_mail(tx, '已有邮件', '', [], now=self.now)
            tx.state['offline_clock'] = moment(5)
        before, revision = self.state(), self.revision()
        for _ in range(3):
            self.assertEqual(len(await heartbeat(self.ctx, {})), 1)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.revision(), revision)
        self.assertEqual(render_member_infos(self.state())[0]['l_cnt'], 4)
        with self.store.transaction(self.uid) as tx:
            message_rows(tx.state)['2']['deleted_at'] = moment(5)
        replies = await heartbeat(self.ctx, {})
        self.wire(replies)
        self.assertEqual(render_member_infos(self.state())[0]['l_cnt'], 3)
        self.assertEqual(len([row for row in message_rows(self.state()).values() if 'gift_receipt' in row]), 2)
        revision = self.revision()
        self.assertEqual(len(await heartbeat(self.ctx, {})), 1)
        self.assertEqual(self.revision(), revision)

    async def test_heartbeat_codec_failure_rolls_back_issuance_and_outbox(self):
        self.grant()
        with self.store.transaction(self.uid) as tx:
            tx.state['offline_clock'] = moment(5)
        before, revision = self.state(), self.revision()
        original = self.codec
        def reject_member(name, fields):
            if name == 'ClientProto:GetMemberRewardInfoRet':
                raise ValueError('intentional gift wire rejection')
            return original.encode_frame(name, fields)
        self.ctx.server.codec = SimpleNamespace(encode_frame=reject_member)
        with self.assertRaisesRegex(ValueError, 'intentional gift wire rejection'):
            await heartbeat(self.ctx, {})
        self.assertEqual(self.state(), before)
        self.assertEqual(self.revision(), revision)
        self.assertNotIn('control_pending', self.state())
        self.ctx.server.codec = original
        replies = await heartbeat(self.ctx, {})
        self.wire(replies)
        self.assertEqual(render_member_infos(self.state())[0]['l_cnt'], 3)
        self.assertEqual(len(message_rows(self.state())), 2)
