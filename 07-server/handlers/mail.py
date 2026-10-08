"""Persisted local mail operations; empty source map plus valid notice lists."""
from server_core import Reply, register
from database import StorageError
from mail_service import (timestamp, message_rows, active, selected_ids,
                          render_mail_pushes, claim_mail_batch, normalize_attachments)
from handlers.tasks import integer


class MailRejected(StorageError):
    def __init__(self, reason, text, **details):
        super().__init__(text)
        self.reason, self.text, self.details = reason, text, details


def rejection(ctx, uid, operation, reason, text, opcode=2906, **details):
    # GameMsg.lua:198-202 + cfgCfgTipsSimpleChinese.lua:249 GeneralTips.
    # GShowTipor.lua:5 defines OnlyParm=0; this does not promise overflow mail.
    event = getattr(ctx.server, 'event', None)
    if callable(event):
        event('mail_operation_rejected', uid=uid, operate_type=operation,
              reason=reason, **details)
    replies = [Reply('SystemProto:Tips', {'strId': 'GeneralTips', 'opId': opcode,
                     'opName': 'MailProto:MailsOperate' if opcode == 2906 else 'MailProto:GetAttachMail',
                     'args': [{'type': 0, 'param': text}]})]
    if operation in (1, 2, 3):
        # Empty valid IDs do not set any mail to claimed in MailMgr:132-156.
        replies.append(Reply('MailProto:MailsOperateRet', {'ids': [], 'operate_type': operation}))
    return replies


def check_claim_capacity(state, rows):
    from admin_resources import ITEMS, balance, maximum, resource_key
    from gift_service import gift_allowed
    totals = {}
    for mail in rows:
        if mail['is_get'] == 2:
            continue
        for reward in normalize_attachments(mail['data']['rewards']):
            if reward['type'] != 2 or gift_allowed(reward['id']):
                continue
            key, cfgid = resource_key('item:' + str(reward['id']))
            token = key, cfgid
            totals[token] = totals.get(token, 0) + reward['num']
    for (key, cfgid), count in totals.items():
        current, ceiling = balance(state, key, cfgid), maximum(state, key, cfgid)
        room = max(0, ceiling - current)
        if count > room:
            name = ITEMS.get(str(cfgid), {}).get('name', str(cfgid))
            raise MailRejected('attachment_capacity',
                f'{name}当前{current:,}，上限{ceiling:,}，最多还能领取{room:,}；本次附件{count:,}。邮件未领取，资源未改变。',
                cfgid=cfgid, current=current, maximum=ceiling, requested=count)


@register("MailProto:GetMailsData")
async def get_mails(ctx, fields):
    state = ctx.store.get_player(ctx.require_login())
    # GameMsg.sMailData lacks the id used by its map declaration; nonempty map
    # decoding would index nil. MailMgr.MailAddNotice handles complete bodies.
    return [Reply("MailProto:GetMailsDataRet", {"mails": {}})] + render_mail_pushes(state)


@register("MailProto:QueryMail")
async def query_mail(ctx, fields):
    state = ctx.store.get_player(ctx.require_login())
    return [Reply("MailProto:QueryMailRet", {})] + render_mail_pushes(state)


@register("MailProto:MailsOperate")
async def operate(ctx, fields):
    uid = ctx.require_login()
    try:
        kind = integer(fields.get("operate_type"), "mail operation", 1, 3)
        ids = selected_ids(fields.get("ids"))
    except StorageError:
        return rejection(ctx, uid, None, 'invalid_request', '邮件操作参数无效，未修改邮件或资源。')
    try:
        return operate_validated(ctx, uid, kind, ids)
    except MailRejected as error:
        return rejection(ctx, uid, kind, error.reason, error.text, **error.details)
    except StorageError:
        # The transaction has already rolled back. Return a source error channel,
        # never a claimed ID or a false "archived overflow" success notification.
        return rejection(ctx, uid, kind, 'reward_or_storage_rule',
                         '邮件操作未完成：附件容量或奖励规则校验失败。邮件和资源未改变，请处理容量后重试。')


def operate_validated(ctx, uid, kind, ids):
    replies, accepted, expired = [], [], []
    with ctx.store.transaction(uid) as tx:
        now, stored = timestamp(None, tx.state), message_rows(tx.state)
        candidates = []
        for identifier in ids:
            row = stored.get(str(identifier))
            if row is None:
                continue
            if not active(row, now):
                expired.append(identifier)
                if kind == 3:
                    row.setdefault("deleted_at", now)
                    accepted.append(identifier)
                continue
            if kind == 1:
                if row["is_read"] != 2:
                    row.update(is_read=2, read_at=now)
                accepted.append(identifier)
            elif kind == 2 and row["data"]["rewards"]:
                candidates.append(row)
                accepted.append(identifier)
            elif kind == 3 and (row["is_get"] == 2 or not row["data"]["rewards"] and row["is_read"] == 2):
                row["deleted_at"] = now
                accepted.append(identifier)
        if kind == 2:
            check_claim_capacity(tx.state, candidates)
            replies.extend(claim_mail_batch(tx, candidates, now))
        if expired and kind != 3:
            replies.append(Reply("MailProto:MailsOperateRet", {"ids": expired, "operate_type": 3}))
        replies.append(Reply("MailProto:MailsOperateRet", {"ids": accepted, "operate_type": kind}))
    return replies


@register("MailProto:GetAttachMail")
async def get_attached_template(ctx, fields):
    uid = ctx.require_login()
    # GM mail API never creates mCfgId links. A requested historical template
    # cannot grant arbitrary rewards or auto-enroll an unrelated official mail.
    return rejection(ctx, uid, None, 'unsupported_attached_template',
                     '当前邮件没有支持的附带模板，未发放奖励。', opcode=2910)
