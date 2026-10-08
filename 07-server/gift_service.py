"""Persisted source daily gifts, using the caller's existing SQLite transaction.

Source recipes determine duration, daily reward and mail text. Local policy:
activation day is eligible, one mail per recipe per UTC+8 03:00 business day;
unissued days survive offline gaps/full mailbox. No Lua execution or payment.
"""
from copy import deepcopy
import json
from pathlib import Path

from config_codec import app_path
from database import StorageError
from server_core import Reply

SOURCE = json.loads(app_path('data', 'gifts-source.json').read_text('utf-8'))
GIFTS = SOURCE['items']
DAY_SECONDS = 86400
RESET_SHIFT = (8 - SOURCE['reset_hour_utc_plus_8']) * 3600
UINT_MAX = 4294967295
INT_MAX = 2147483647
MAX_GRANT_QUANTITY = 100


def integer(value, label, minimum=0, maximum=INT_MAX):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise StorageError('Invalid gift ' + label)
    return value


def gift_allowed(cfgid):
    return (not isinstance(cfgid, bool) and isinstance(cfgid, int)
            and bool(GIFTS.get(str(cfgid), {}).get('supported')))


def catalog():
    return {'gifts': [deepcopy(row) for _, row in sorted(GIFTS.items(), key=lambda pair: int(pair[0]))],
            'gift_max_quantity': MAX_GRANT_QUANTITY,
            'gift_policy': 'activation day; UTC+8 03:00; unissued days retained; one daily mail per recipe'}


def gift_time(state, now=None):
    if now is None:
        from handlers.initialization import local_time
        now = local_time(state)
    return integer(now, 'time', 0, UINT_MAX)


def business_day(now):
    return (now + RESET_SHIFT) // DAY_SECONDS


def entitlement_rows(state):
    saved = state.get('member_gifts')
    if saved is None:
        return {}
    if not isinstance(saved, dict) or saved.get('version') != 1 or not isinstance(saved.get('entitlements'), dict):
        raise StorageError('Unsupported persisted gift state')
    return saved['entitlements']


def validate_entitlement(key, row):
    if key not in GIFTS or not GIFTS[key]['supported'] or not isinstance(row, dict):
        raise StorageError('Unknown persisted gift recipe')
    integer(row.get('c_time'), 'creation time', 0, UINT_MAX)
    for field in ('remaining_days', 'total_days', 'issued_days'):
        integer(row.get(field), field)
    if row['remaining_days'] + row['issued_days'] != row['total_days']:
        raise StorageError('Inconsistent persisted gift day count')
    if row.get('last_issue_day') is not None:
        integer(row['last_issue_day'], 'issue day', 0, UINT_MAX)
    return row


def render_member_infos(state):
    """Pure exact sMemberReward rows; completed entries retain l_cnt=0."""
    return [{'c_time': validate_entitlement(key, row)['c_time'], 'item_id': int(key),
             'l_cnt': row['remaining_days']}
            for key, row in sorted(entitlement_rows(state).items(), key=lambda pair: int(pair[0]))]


def member_pushes(state):
    return [Reply('ClientProto:GetMemberRewardInfoRet', {'infos': render_member_infos(state)})]


def refresh_gifts(tx, now=None):
    """Issue at most one currently eligible daily mail per recipe, atomically.

    No remaining-day decrement happens until send_mail succeeds. A full mailbox
    defers issuance without deleting mail or losing an entitlement. Caller must
    push returned mail_ids after commit. No await is permitted in this function.
    """
    from mail_service import send_mail, active, message_rows, mailbox_limit
    now = gift_time(tx.state, now)
    day = business_day(now)
    available = mailbox_limit() - sum(active(row, now) for row in message_rows(tx.state).values())
    mail_ids, changed, deferred = [], [], []
    for key, row in sorted(entitlement_rows(tx.state).items(), key=lambda pair: int(pair[0])):
        validate_entitlement(key, row)
        if row['remaining_days'] == 0 or row.get('last_issue_day') is not None and day <= row['last_issue_day']:
            continue
        if available <= 0:
            deferred.append(int(key))
            continue
        recipe = GIFTS[key]
        after = row['remaining_days'] - 1
        template = SOURCE['mail_templates'][str(recipe['mail_cfgid'])]
        identifier = send_mail(tx, template['name'].replace('{day}', str(after)), template.get('desc', ''),
                               deepcopy(recipe['daily_rewards']), now=now, sender=template['from'])
        mail_row = message_rows(tx.state)[str(identifier)]
        # Use cfgid=0 with source text expanded: MailInfo's template path cannot
        # replace {day}, since its named args only replace numeric {1} keys.
        mail_row['gift_receipt'] = {'item_id': int(key), 'business_day': day,
                                   'mail_cfgid': recipe['mail_cfgid'], 'remaining_days': after}
        row.update(remaining_days=after, issued_days=row['issued_days'] + 1,
                   last_issue_day=day, last_issue_time=now, last_mail_id=identifier)
        mail_ids.append(identifier)
        changed.append(int(key))
        available -= 1
    return {'mail_ids': mail_ids, 'changed': changed, 'deferred': deferred,
            'infos': render_member_infos(tx.state)}


def grant_gift(tx, cfgid, num=1, now=None):
    """Consume an auto-use grant into source daily entitlements, never bag items.

    Repeated copies extend one recipe, matching source member continuation
    semantics. Same-day copies cannot create a second daily mail. Mail claiming
    remains responsible for its outer receipt/idempotence in the same txn.
    """
    cfgid = integer(cfgid, 'configuration id', 1)
    num = integer(num, 'quantity', 1, MAX_GRANT_QUANTITY)
    if not gift_allowed(cfgid):
        raise StorageError('Gift recipe requires an unsupported domain or payment benefit')
    now = gift_time(tx.state, now)
    duration = integer(GIFTS[str(cfgid)]['days'], 'configured duration', 1, 365)
    added = integer(duration * num, 'added days', 1)
    saved = tx.state.get('member_gifts')
    if saved is None:
        saved = {'version': 1, 'entitlements': {}}
        tx.state['member_gifts'] = saved
    rows = entitlement_rows(tx.state)
    key = str(cfgid)
    if key not in rows:
        rows[key] = {'c_time': now, 'remaining_days': 0, 'total_days': 0,
                     'issued_days': 0, 'last_issue_day': None}
    row = validate_entitlement(key, rows[key])
    before = row['remaining_days']
    remaining = integer(before + added, 'remaining days')
    total = integer(row['total_days'] + added, 'total days')
    row.update(remaining_days=remaining, total_days=total)
    refreshed = refresh_gifts(tx, now)
    return dict(refreshed, cfgid=cfgid, num=num, added_days=added, before_days=before,
                remaining_days=row['remaining_days'])
