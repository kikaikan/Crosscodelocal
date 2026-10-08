"""Mail persistence, exact wire notifications and atomic attachment lifecycle."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from database import Store, StorageError
from server_core import Context, HANDLERS
from handlers import mail
from mail_service import send_mail, render_mail_pushes, normalize_attachments, mailbox_limit
from protocol_codec import IVProtoCodec, WireConfig, CodecError


class MailTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="crosscore-mail-")
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path)
        seed = json.loads((HERE / "data" / "new_account_seed.json").read_text("utf-8"))
        self.uid = self.store.create_account("new-local-mail-account", seed)["uid"]
        with self.store.transaction(self.uid) as tx:
            tx.state['progress']['cleared_stages'] = [1001,1002,1003]
        self.ctx = Context(SimpleNamespace(store=self.store), "game", self.uid, True)
        self.now = 1791072000
        with self.store.transaction(self.uid) as tx:
            tx.state["offline_clock"] = self.now
        self.codec = IVProtoCodec(json.loads((HERE.parent / "05-protocol" / "endpoints.json").read_text("utf-8")),
                                  WireConfig("little", max_frame_size=65535))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def state(self):
        return self.store.get_player(self.uid)

    def send(self, attachments=None, **kw):
        with self.store.transaction(self.uid) as tx:
            return send_mail(tx, "本地邮件 \"α\"", "中文正文\n下一行", attachments or [], **kw)

    def row(self, identifier):
        return self.state()["mailbox"]["messages"][str(identifier)]

    async def operate(self, ids, kind):
        result = await mail.operate(self.ctx, {"ids": ids, "operate_type": kind})
        self.wire(result)
        return result

    def wire(self, replies):
        for reply in replies:
            raw = self.codec.encode_frame(reply.name, reply.fields)
            decoded = self.codec.decode_frame(raw)
            self.assertEqual(raw, self.codec.encode_frame(decoded.name, decoded.fields))
            self.assertLess(len(raw), 32767)

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.ctx.server.store = self.store

    async def test_create_does_not_grant_and_body_survives_reopen_and_query(self):
        before = self.state()
        identifier = self.send([{"id": 10001, "num": 50, "type": 2}])
        self.assertEqual(self.state()["player"], before["player"])
        self.assertEqual(self.state()["inventory"], before["inventory"])
        self.reopen()
        state = self.state()
        notices = await mail.get_mails(self.ctx, {})
        self.wire(notices)
        self.assertEqual(notices[0].fields, {"mails": {}})
        info = notices[1].fields["adds"][0]
        self.assertEqual(info["id"], identifier)
        self.assertEqual(info["cfgid"], 0)
        self.assertEqual(info["is_get"], 1)
        self.assertEqual(info["data"]["desc"], "中文正文\n下一行")
        self.assertNotIn("mCfgId", info["data"])
        self.assertEqual(self.state(), state)
        queried = await mail.query_mail(self.ctx, {})
        self.wire(queried)
        self.assertEqual(queried[0].name, "MailProto:QueryMailRet")
        self.assertEqual(queried[1].fields["adds"][0], info)

    async def test_read_claim_and_retry_are_persistent_and_award_once(self):
        identifier = self.send([{"id": 10001, "num": 50, "type": 2}])
        self.assertEqual((await self.operate([identifier], 1))[-1].fields["ids"], [identifier])
        read = self.state()
        await self.operate([identifier], 1)
        self.assertEqual(self.state(), read)
        before = self.state()["player"]["gold"]
        result = await self.operate([identifier, identifier], 2)
        self.assertEqual(result[-1].fields["ids"], [identifier])
        self.assertEqual(self.state()["player"]["gold"], before + 50)
        self.assertEqual((self.row(identifier)["is_read"], self.row(identifier)["is_get"]), (2, 2))
        claimed = self.state()
        self.reopen()
        repeated = await self.operate([identifier], 2)
        self.assertEqual([reply.name for reply in repeated], ["MailProto:MailsOperateRet"])
        self.assertEqual(self.state(), claimed)
        self.assertNotIn("claim_receipt", render_mail_pushes(claimed)[0].fields["adds"][0])

    async def test_bulk_claim_combines_real_canonical_resources(self):
        first = self.send([{"id": 10002, "num": 3, "type": 2}, {"id": 10020, "num": 4, "type": 2}])
        second = self.send([{"id": 10003, "num": 70, "type": 2}, {"id": 10035, "num": 10, "type": 2}])
        before = self.state()
        result = await self.operate([first, second, 999999], 2)
        self.assertEqual(result[-1].fields["ids"], [first, second])
        after = self.state()
        self.assertEqual(after["player"]["diamond"], before["player"]["diamond"] + 3)
        self.assertEqual(after["login"]["ability_num"], before["login"]["ability_num"] + 4)
        self.assertEqual(after["store_exp"], before["store_exp"] + 70)
        self.assertEqual(after["player"]["hot"], before["player"]["hot"] + 10)
        self.assertNotIn("10003", after["inventory"])
        self.assertNotIn("10035", after["inventory"])
        self.assertGreater(after["login"]["t_hot"], self.now)

    async def test_later_overflow_rolls_back_entire_bulk_assets_and_receipts(self):
        first = self.send([{"id": 10001, "num": 50, "type": 2}])
        second = self.send([{"id": 10002, "num": 2147483647, "type": 2}])
        before = self.state()
        result = await self.operate([first, second], 2)
        self.assertEqual(result[0].name, 'SystemProto:Tips')
        self.assertEqual(result[-1].fields, {'ids': [], 'operate_type': 2})
        self.assertEqual(self.state(), before)


    async def test_real_source_star_dust_limit_is_honest_tip_and_context_survives(self):
        from admin_resources import ITEMS
        self.assertEqual(ITEMS['100011']['upperLimit'], 9999)
        star_dust = self.send([{'id': 100011, 'num': 22222, 'type': 2}])
        diamond = self.send([{'id': 10002, 'num': 22222, 'type': 2}])
        tech = [self.send([{'id': 10003, 'num': 99999999, 'type': 2}]) for _ in range(2)]
        before = self.state()
        events = []
        self.ctx.server.event = lambda kind, **fields: events.append((kind, fields))
        result = await self.operate([star_dust, diamond, *tech], 2)
        self.assertEqual(self.state(), before)
        tip = result[0]
        self.assertEqual(tip.name, 'SystemProto:Tips')
        self.assertEqual((tip.fields['strId'], tip.fields['opId'], tip.fields['opName']),
                         ('GeneralTips', 2906, 'MailProto:MailsOperate'))
        self.assertEqual(tip.fields['args'][0]['type'], 0)
        text = tip.fields['args'][0]['param']
        self.assertIn('9,999', text)
        self.assertIn('22,222', text)
        self.assertIn('邮件未领取', text)
        self.assertNotIn('邮件发送', text)
        self.assertEqual(events[0][0], 'mail_operation_rejected')
        self.assertEqual(events[0][1]['reason'], 'attachment_capacity')
        self.assertEqual(events[0][1]['cfgid'], 100011)
        self.assertNotIn('title', events[0][1])
        self.assertNotIn('content', events[0][1])
        # A rejected authenticated request returns normally; reads and a valid
        # subset remain usable in the same context without fake claim status.
        self.wire(await mail.query_mail(self.ctx, {}))
        valid = await self.operate([diamond], 2)
        self.assertEqual(valid[-1].fields['ids'], [diamond])
        self.assertEqual(self.state()['player']['diamond'], before['player']['diamond'] + 22222)
        self.assertEqual(self.row(star_dust)['is_get'], 1)

    async def test_source_technology_points_have_no_invented_99_million_ceiling(self):
        from admin_resources import ITEMS
        self.assertNotIn('upperLimit', ITEMS['10003'])
        ids = [self.send([{'id': 10003, 'num': 99999999, 'type': 2}]) for _ in range(2)]
        before = self.state()['store_exp']
        result = await self.operate(ids, 2)
        self.assertEqual(self.state()['store_exp'], before + 199999998)
        self.assertEqual(result[-1].fields['ids'], ids)
        self.assertNotIn('SystemProto:Tips', [reply.name for reply in result])
        self.assertNotIn('10003', self.state()['inventory'])

    async def test_aggregate_current_balance_boundary_rejects_then_claims_without_clamping(self):
        with self.store.transaction(self.uid) as tx:
            tx.add_item(100011, 9990)
        first = self.send([{'id': 100011, 'num': 5, 'type': 2}])
        second = self.send([{'id': 100011, 'num': 5, 'type': 2}])
        before = self.state()
        result = await self.operate([first, second], 2)
        self.assertEqual(result[-1].fields['ids'], [])
        self.assertEqual(self.state(), before)
        self.assertIn('最多还能领取9', result[0].fields['args'][0]['param'])
        await self.operate([first], 2)
        self.assertEqual(self.state()['inventory']['100011'], 9995)
        self.assertEqual(self.row(first)['is_get'], 2)
        before = self.state()
        result = await self.operate([first, second], 2)
        self.assertEqual(self.state(), before)
        self.assertEqual(result[-1].fields['ids'], [])
        self.assertEqual(self.row(second)['is_get'], 1)

    async def test_expiry_exact_boundary_never_grants_and_removes_visible_entry(self):
        identifier = self.send([{"id": 10001, "num": 50, "type": 2}], expires_at=self.now + 1)
        self.wire(render_mail_pushes(self.state(), [identifier], self.now))
        with self.store.transaction(self.uid) as tx:
            tx.state["offline_clock"] = self.now + 1
        before = self.state()
        result = await self.operate([identifier], 2)
        self.assertEqual(result[-1].fields["ids"], [])
        self.assertEqual(result[0].fields, {"ids": [identifier], "operate_type": 3})
        self.assertEqual(self.state(), before)
        notices = render_mail_pushes(self.state())
        self.wire(notices)
        self.assertFalse(any(reply.name == "MailProto:MailAddNotice" for reply in notices))

    async def test_delete_protects_unclaimed_attachment_and_unread_text(self):
        attached = self.send([{"id": 60101, "num": 1, "type": 2}])
        plain = self.send()
        self.assertEqual((await self.operate([attached, plain], 3))[-1].fields["ids"], [])
        await self.operate([attached, plain], 1)
        self.assertEqual((await self.operate([attached, plain], 3))[-1].fields["ids"], [plain])
        self.assertNotIn("deleted_at", self.row(attached))
        await self.operate([attached], 2)
        self.assertEqual((await self.operate([attached], 3))[-1].fields["ids"], [attached])
        before = self.state()
        self.reopen()
        await self.operate([attached], 3)
        self.assertEqual(self.state(), before)
        self.assertTrue(all(reply.name == "MailProto:MailsOperateRet" for reply in render_mail_pushes(self.state())))

    async def test_plain_mail_claim_does_not_invent_an_attachment(self):
        identifier = self.send()
        before = self.state()
        result = await self.operate([identifier], 2)
        self.assertEqual(result[-1].fields["ids"], [])
        self.assertEqual(self.state(), before)

    async def test_role_and_equipment_instances_and_duplicates_are_real_and_once(self):
        from admin_roles import role_allowed
        from handlers.progression import EQUIPS
        self.assertTrue(role_allowed(10010))
        equip = int(next(iter(EQUIPS)))
        identifier = self.send([{"id": 10010, "num": 2, "type": 3}, {"id": equip, "num": 1, "type": 4}])
        before = self.state()
        result = await self.operate([identifier], 2)
        after = self.state()
        card = next(card for card in after["cards"] if card["cfgid"] == 10010)
        self.assertEqual(card["get_cnt"], 2)
        self.assertEqual(len(after["cards"]), len(before["cards"]) + 1)
        self.assertEqual(len(after["equips"]), 1)
        self.assertEqual(after["equips"][0]["cfgid"], equip)
        self.assertIn("PlayerProto:CardAdd", [reply.name for reply in result])
        self.assertIn("EquipProto:EquipAdd", [reply.name for reply in result])
        self.assertNotIn(str(equip), after["inventory"])
        # Role cfg10010 and army-coin item10010 share a number but different
        # reward types; granting the fighter must not increase the coin balance.
        self.assertEqual(after["inventory"]["10010"], before["inventory"]["10010"])
        self.reopen()
        await self.operate([identifier], 2)
        self.assertEqual(self.state(), after)

    async def test_object_capacity_failure_rolls_back_items_card_and_mail(self):
        from handlers.progression import EQUIPS
        equip = int(next(iter(EQUIPS)))
        identifier = self.send([{"id": 10001, "num": 50, "type": 2},
                                {"id": 10010, "num": 1, "type": 3}, {"id": equip, "num": 1, "type": 4}])
        with self.store.transaction(self.uid) as tx:
            tx.state["max_equip_size"] = 0
        before = self.state()
        result = await self.operate([identifier], 2)
        self.assertEqual(result[0].name, 'SystemProto:Tips')
        self.assertEqual(result[-1].fields, {'ids': [], 'operate_type': 2})
        self.assertEqual(self.state(), before)

    def test_strict_send_validation_rejects_payment_object_confusion_and_bad_inputs(self):
        cases = ([{"id": True, "num": 1, "type": 2}], [{"id": 10001, "num": 0, "type": 2}],
                 [{"id": 10001, "num": 1, "type": 2, "c_id": 999}],
                 [{"id": 10998, "num": 1, "type": 2}], [{"id": 10004, "num": 1, "type": 2}],
                 [{"id": 43020, "num": 1, "type": 2}], [{"id": 99999999, "num": 1, "type": 2}],
                 [{"id": 10001, "num": 1, "type": 5}])
        before = self.state()
        for attachments in cases:
            with self.subTest(attachments=attachments), self.assertRaises(StorageError):
                self.send(attachments)
            self.assertEqual(self.state(), before)
        for title, content, opts in (("", "body", {}), ("x" * 257, "body", {}),
                                     ("title", "x\0body", {}), ("title", "body", {"expires_at": self.now}),
                                     ("title", "body", {"now": True})):
            with self.assertRaises(StorageError):
                with self.store.transaction(self.uid) as tx:
                    send_mail(tx, title, content, [], **opts)
            self.assertEqual(self.state(), before)

    def test_capacity_never_discards_mail_and_ids_never_reuse(self):
        with self.store.transaction(self.uid) as tx:
            ids = [send_mail(tx, "title", "body", []) for _ in range(mailbox_limit())]
        before = self.state()
        with self.assertRaises(StorageError):
            self.send()
        self.assertEqual(self.state(), before)
        with self.store.transaction(self.uid) as tx:
            tx.state["mailbox"]["messages"][str(ids[0])]["deleted_at"] = self.now
        self.assertEqual(self.send(), ids[-1] + 1)

    def test_notice_chunks_long_utf8_bodies_below_native_frame_limit(self):
        with self.store.transaction(self.uid) as tx:
            for _ in range(5):
                send_mail(tx, "中文", "中文内容" * 1200, [])
        before = self.state()
        replies = render_mail_pushes(before)
        self.wire(replies)
        self.assertEqual(sum(len(reply.fields["adds"]) for reply in replies), 5)
        self.assertGreater(len(replies), 1)
        self.assertEqual(self.state(), before)

    def test_source_mail_map_cannot_encode_its_missing_id_but_notice_can(self):
        identifier = self.send()
        info = render_mail_pushes(self.state())[0].fields["adds"][0]
        raw = self.codec.encode_frame("MailProto:GetMailsDataRet", {"mails": {
            str(identifier): {"id": identifier, "data": info, "bIsChange": False}}})
        with self.assertRaises(CodecError):
            self.codec.decode_frame(raw)
        self.wire(render_mail_pushes(self.state()))

    async def test_unknown_ids_bad_operation_and_unrelated_templates_cannot_grant(self):
        identifier = self.send([{"id": 10001, "num": 50, "type": 2}])
        before = self.state()
        self.assertEqual((await self.operate([99999999], 2))[-1].fields["ids"], [])
        for fields in ({"ids": [True], "operate_type": 2}, {"ids": [identifier], "operate_type": True},
                       {"ids": [identifier], "operate_type": 4}, {"ids": "1", "operate_type": 1}):
            result = await mail.operate(self.ctx, fields)
            self.wire(result)
            self.assertEqual(result[0].name, 'SystemProto:Tips')
            self.assertEqual(result[0].fields['args'][0]['type'], 0)
        result = await mail.get_attached_template(self.ctx, {"id": identifier, "mCfgId": 1})
        self.wire(result)
        self.assertEqual(result[0].name, 'SystemProto:Tips')
        self.assertEqual(result[0].fields['opId'], 2910)
        self.assertEqual(self.state(), before)

    async def test_all_handlers_require_login_and_get_registration_is_unique(self):
        self.ctx.logged_in = False
        for function, fields in ((mail.get_mails, {}), (mail.query_mail, {}),
                                 (mail.operate, {"ids": [], "operate_type": 1}),
                                 (mail.get_attached_template, {"id": 1, "mCfgId": 1})):
            with self.assertRaises(StorageError):
                await function(self.ctx, fields)
        self.assertIs(HANDLERS["MailProto:GetMailsData"], mail.get_mails)


if __name__ == "__main__":
    unittest.main()
