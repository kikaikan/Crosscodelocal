"""Local control operations, committed with their receipt and game notification."""
from copy import deepcopy
import hashlib
import json
import time
import uuid

from database import StorageError


class Replay(Exception):
    def __init__(self, result):
        self.result = result


def integer(value, label, minimum=0, maximum=2147483647):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise StorageError(label + '必须是范围内的整数')
    return value


def catalog():
    import admin_resources
    result = admin_resources.catalog()
    try:
        import admin_roles
    except ImportError:
        result.setdefault('roles', [])
    else:
        result.update(admin_roles.catalog())
    return result


def queue(state, kind, values):
    pending = state.setdefault('control_pending', {})
    target = pending.setdefault(kind, [])
    for value in values:
        if value not in target:
            target.append(value)


def render_pending(state):
    pending = state.get('control_pending', {})
    replies = []
    if pending.get('access'):
        replies.append(access_reply(state))
    if pending.get('resources'):
        import admin_resources
        # Only local control operations queue here, so render every item row the
        # administrator wrote, including domain/auto-use/expiring ones.
        replies.extend(admin_resources.resource_pushes(state, pending['resources'], allow_any=True))
    if pending.get('mail_ids'):
        from mail_service import render_mail_pushes
        replies.extend(render_mail_pushes(state, pending['mail_ids']))
    if pending.get('card_ids'):
        import admin_roles
        replies.extend(admin_roles.role_pushes(state, pending['card_ids']))
    if pending.get('pools'):
        from handlers.gacha import factory
        from server_core import Reply
        replies.append(Reply('PlayerProto:CardFactoryInfoRet', factory(deepcopy(state))))
    if pending.get('member_infos'):
        from gift_service import member_pushes
        replies.extend(member_pushes(state))
    return replies


def refresh_daily_gifts(store, uid, codec):
    from gift_service import entitlement_rows, gift_time, business_day, refresh_gifts
    state = store.get_player(uid)
    today = business_day(gift_time(state))
    if not any(row.get('remaining_days', 0) > 0 and
               (row.get('last_issue_day') is None or today > row['last_issue_day'])
               for row in entitlement_rows(state).values()):
        return
    class Unchanged(Exception):
        pass
    try:
        with store.transaction(uid) as tx:
            refreshed = refresh_gifts(tx)
            if not refreshed['changed']:
                raise Unchanged()
            queue(tx.state, 'mail_ids', refreshed['mail_ids'])
            queue(tx.state, 'member_infos', [True])
            for reply in render_pending(tx.state):
                codec.encode_frame(reply.name, reply.fields)
    except Unchanged:
        pass


def access_reply(state):
    from server_core import Reply
    from access_policy import POLICY_KEY, options
    return Reply('PlayerProto:GetClientDataRet', {'key': POLICY_KEY, 'type': 3,
                'data': json.dumps(options(state), separators=(',', ':'))})


def consume_notifications(store, uid, codec):
    if not store.get_player(uid).get('control_pending'):
        return []
    with store.transaction(uid) as tx:
        replies = render_pending(tx.state)
        # A notification must be encodable before its durable outbox is removed.
        for reply in replies:
            codec.encode_frame(reply.name, reply.fields)
        tx.state.pop('control_pending', None)
    return replies


def execute(store, action, payload, codec):
    if not isinstance(payload, dict):
        raise StorageError('请求内容必须是对象')
    uid = integer(payload.get('uid'), 'UID', 1)
    request_id = payload.get('request_id')
    try:
        if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id.lower():
            raise ValueError()
    except (ValueError, AttributeError):
        raise StorageError('缺少有效的操作编号') from None
    fingerprint = hashlib.sha256(json.dumps({'action': action, 'payload': payload},
                                sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    store.get_player(uid)
    try:
        with store.transaction(uid) as tx:
            receipts = tx.state.setdefault('control_receipts', {})
            existing = receipts.get(request_id)
            if existing:
                if existing['fingerprint'] != fingerprint:
                    raise StorageError('操作编号已用于不同内容，请重新提交')
                raise Replay(deepcopy(existing['result']))
            if action == 'resource':
                if set(payload) != {'uid', 'key', 'mode', 'amount', 'request_id'}:
                    raise StorageError('资源请求字段不正确')
                import admin_resources
                result = admin_resources.apply_resource(tx, payload['key'], payload['mode'],
                                                        payload['amount'], allow_any=True)
                queue(tx.state, 'resources', [payload['key']])
            elif action == 'mail':
                if set(payload) != {'uid', 'title', 'content', 'attachments', 'expires_days', 'request_id'}:
                    raise StorageError('邮件请求字段不正确')
                rows = payload['attachments']
                if not isinstance(rows, list) or len(rows) > 20:
                    raise StorageError('邮件最多包含20种附件')
                attachments = []
                for row in rows:
                    if not isinstance(row, dict) or not set(row) <= {'cfgid', 'num', 'type'} or not {'cfgid', 'num'} <= set(row):
                        raise StorageError('附件字段不正确')
                    attachments.append({'id': integer(row['cfgid'], '附件ID', 1),
                                        'num': integer(row['num'], '附件数量', 1),
                                        'type': integer(row.get('type', 2), '附件类型', 2, 4)})
                days = integer(payload['expires_days'], '有效天数', 1, 3650)
                from mail_service import send_mail
                stamp = int(time.time())
                mail_id = send_mail(tx, payload['title'], payload['content'], attachments,
                                    now=stamp, expires_at=stamp + days * 86400)
                queue(tx.state, 'mail_ids', [mail_id])
                result = {'mail_id': mail_id, 'attachment_count': len(attachments)}
            elif action == 'clear_mail':
                if set(payload) != {'uid', 'request_id'}:
                    raise StorageError('清空邮箱请求字段不正确')
                from mail_service import message_rows
                stamp = int(time.time())
                removed = []
                for key, row in message_rows(tx.state).items():
                    if 'deleted_at' not in row:
                        row['deleted_at'] = stamp
                        removed.append(int(key))
                queue(tx.state, 'mail_ids', removed)
                result = {'removed_ids': sorted(removed), 'removed_count': len(removed)}
            elif action == 'role':
                if set(payload) != {'uid', 'cfgid', 'request_id'}:
                    raise StorageError('角色请求字段不正确')
                import admin_roles
                result = admin_roles.grant_role(tx, integer(payload['cfgid'], '角色ID', 1))
                queue(tx.state, 'card_ids', [result['cid']])
                # Duplicate compensation changes the item bag too.
                queue(tx.state, 'resources', ['inventory'])
            elif action == 'pools':
                if set(payload) != {'uid', 'request_id', 'pool_ids', 'enabled'}:
                    raise StorageError('卡池请求字段不正确')
                import admin_roles
                result = admin_roles.set_archive_pools(tx, payload)
                queue(tx.state, 'pools', [True])
            elif action == 'access':
                if set(payload) not in ({'uid', 'request_id', 'policy'}, {'uid', 'request_id', 'enabled'}):
                    raise StorageError('开放模式请求字段不正确')
                import admin_roles
                result = admin_roles.set_content_access(tx, payload.get('policy', payload.get('enabled')))
                queue(tx.state, 'access', [True])
                queue(tx.state, 'pools', [True])
            else:
                raise StorageError('未知控制操作')
            for reply in render_pending(tx.state):
                codec.encode_frame(reply.name, reply.fields)
            receipts[request_id] = {'fingerprint': fingerprint, 'result': deepcopy(result)}
            audit = tx.state.setdefault('control_audit', [])
            audit.append({'time': int(time.time()), 'action': action, 'request_id': request_id,
                          'result': deepcopy(result)})
            del audit[:-500]
    except Replay as replay:
        return {**replay.result, 'replayed': True}
    return {**result, 'replayed': False}
