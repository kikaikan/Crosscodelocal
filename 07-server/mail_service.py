"""Local GM mail storage and exact client notification shapes.

Sources: GameMsg.lua:2933-2987, MailInfo.lua, MailMgr.lua, MailView.lua.
All mutations require the caller's existing PlayerTxn; never await inside it.
Sending creates a mailbox entry only. Assets are granted by claim_mail_batch.
"""
from copy import deepcopy
from functools import lru_cache

from database import StorageError
from server_core import Reply
from seed_generator import selected_record
from handlers.initialization import local_time
from handlers.tasks import integer, advance_tasks

MAIL_FIELDS = ("id", "cfgid", "is_read", "is_get", "from_uid", "start_time", "end_time", "data", "create_time")
NOTICE_BUDGET = 30000
ATTACHMENT_MAX = 100


@lru_cache(maxsize=1)
def mailbox_limit():
    row, _ = selected_record("cfgglobal_setting.lua", "g_MailMaxSize")
    return integer(int(row["value"]), "mailbox capacity", 1, 1000)


def text(value, label, maximum, allow_empty=False):
    if not isinstance(value, str) or not allow_empty and not value.strip():
        raise StorageError(label + " must be nonempty text")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError as error:
        raise StorageError(label + " is not valid UTF-8 text") from error
    if len(encoded) > maximum or any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise StorageError(label + " exceeds the supported text limits")
    return value


def role_allowed(identifier):
    from admin_roles import role_allowed as supported_role
    return supported_role(identifier)


def validate_attachment(row):
    if not isinstance(row, dict) or set(row) != {"id", "num", "type"}:
        raise StorageError("Mail attachments require exactly id, num and type")
    identifier = integer(row["id"], "attachment configuration id", 1)
    kind = integer(row["type"], "attachment reward type", 2, 4)
    count = integer(row["num"], "attachment quantity", 1, 100 if kind in (3, 4) else 2147483647)
    if kind == 2:
        from admin_resources import item_allowed
        from gift_service import gift_allowed, MAX_GRANT_QUANTITY
        if gift_allowed(identifier):
            count = integer(count, "礼包数量", 1, MAX_GRANT_QUANTITY)
        elif not item_allowed(identifier):
            raise StorageError("附件" + str(identifier) + "需要尚未实现的专用奖励处理，或属于充值资源")
    elif kind == 3:
        if not role_allowed(identifier):
            raise StorageError("Mail card reward template is not currently supported")
    else:
        from handlers.progression import EQUIPS
        if str(identifier) not in EQUIPS:
            raise StorageError("Mail equipment reward template is not currently supported")
    return {"id": identifier, "num": count, "type": kind}


def normalize_attachments(attachments):
    if not isinstance(attachments, list) or len(attachments) > ATTACHMENT_MAX:
        raise StorageError("Mail attachments must be a bounded list")
    combined = {}
    for source in attachments:
        row = validate_attachment(source)
        key = (row["type"], row["id"])
        previous = combined.setdefault(key, dict(row, num=0))
        previous["num"] = integer(previous["num"] + row["num"], "combined attachment count", 1,
                                  100 if row["type"] in (3, 4) else 2147483647)
    # Revalidate totals, including gifts whose daily entitlement grant is capped.
    return [validate_attachment(row) for row in combined.values()]


def timestamp(value, state):
    return integer(local_time(state) if value is None else value, "mail time", 0, 9223372036854775807)


def message_rows(state):
    return state.get("mailbox", {}).get("messages", {})


def active(row, now):
    return "deleted_at" not in row and row["start_time"] <= now and (not row["end_time"] or now < row["end_time"])


def send_mail(tx, title, content, attachments, now=None, expires_at=None, sender="本地控制台"):
    """Create one local mail; caller commits and queues its returned ID.

    attachments=[{'id': cfgid, 'num': positive_int, 'type': 2/3/4}].
    expires_at=None is permanent; an explicit absolute expiry must exceed now.
    No official template, credentials, enrollment or automatic reward is used.
    """
    title, content = text(title, "mail title", 256), text(content, "mail body", 16000, True)
    sender = text(sender, "mail sender", 128)
    rows = normalize_attachments(attachments)
    now = timestamp(now, tx.state)
    expiry = 0 if expires_at is None else timestamp(expires_at, tx.state)
    if expires_at is not None and expiry <= now:
        raise StorageError("Mail expiry must be later than its creation time")
    if sum(active(row, now) for row in message_rows(tx.state).values()) >= mailbox_limit():
        raise StorageError("Local mailbox is full; no mail was discarded")
    mailbox = tx.state.setdefault("mailbox", {"version": 1, "next_id": 1, "messages": {}})
    if mailbox.get("version") != 1:
        raise StorageError("Unsupported local mailbox version")
    identifier = integer(mailbox["next_id"], "next mail id", 1, 4294967295)
    if str(identifier) in mailbox["messages"]:
        raise StorageError("Duplicate persisted mail id")
    mailbox["messages"][str(identifier)] = {
        "id": identifier, "cfgid": 0, "is_read": 1, "is_get": 1, "from_uid": 0,
        "start_time": now, "end_time": expiry, "create_time": now,
        "data": {"name": title, "from": sender, "desc": content, "rewards": rows},
    }
    mailbox["next_id"] = identifier + 1
    return identifier


def selected_ids(ids, maximum=200):
    if not isinstance(ids, list) or len(ids) > maximum:
        raise StorageError("Mail IDs must be a bounded array")
    return list(dict.fromkeys(integer(value, "mail id", 1, 4294967295) for value in ids))


def mail_info(row):
    return {key: deepcopy(row[key]) for key in MAIL_FIELDS}


def render_mail_pushes(state, ids=None, now=None):
    """Pure read: refresh current entries and remove known deleted/expired IDs.

    ids=None renders the complete mailbox; a selection is useful for admin
    heartbeat pushes. Only MailAddNotice / MailsOperateRet source protocols are
    used; no invalid nonempty map|sMailData|id is encoded. Each notice stays
    below 30KB so long mail bodies cannot overflow an inner packet.
    """
    now, rows = timestamp(now, state), message_rows(state)
    wanted = [int(value) for value in rows] if ids is None else selected_ids(ids, 2000)
    removed, notices, batch, size = [], [], [], 0
    for identifier in wanted:
        row = rows.get(str(identifier))
        if row is None or not active(row, now):
            removed.append(identifier)
            continue
        info = mail_info(row)
        estimate = 200 + sum(len(info["data"][key].encode("utf-8")) for key in ("name", "from", "desc")) + 32 * len(info["data"]["rewards"])
        if batch and size + estimate > NOTICE_BUDGET:
            notices.append(Reply("MailProto:MailAddNotice", {"adds": batch}))
            batch, size = [], 0
        batch.append(info)
        size += estimate
    if batch:
        notices.append(Reply("MailProto:MailAddNotice", {"adds": batch}))
    return [Reply("MailProto:MailsOperateRet", {"ids": removed[i:i + 200], "operate_type": 3})
            for i in range(0, len(removed), 200)] + notices


def claim_mail_batch(tx, rows, now):
    """Claim a validated active selection atomically with the caller's store."""
    from admin_resources import award_items, resource_pushes
    from handlers.tasks import grant_rewards
    from gift_service import gift_allowed, grant_gift, member_pushes
    fresh = [row for row in rows if row["is_get"] != 2 and row["data"]["rewards"]]
    rewards = [reward for row in fresh for reward in normalize_attachments(row["data"]["rewards"])]
    rendered, replies = award_items(tx, [row for row in rewards if row["type"] == 2 and not gift_allowed(row["id"])])
    gift_mail_ids = []
    gifts = [row for row in rewards if row["type"] == 2 and gift_allowed(row["id"])]
    for row in gifts:
        result = grant_gift(tx, row["id"], row["num"], now=now)
        gift_mail_ids.extend(result["mail_ids"])
    if gifts:
        replies.extend(member_pushes(tx.state))
        replies.extend(render_mail_pushes(tx.state, gift_mail_ids, now=now))
    objects = [row for row in rewards if row["type"] in (3, 4)]
    if objects:
        from admin_roles import grant_role, role_pushes
        cids = []
        for row in objects:
            if row["type"] == 3:
                for _ in range(row["num"]):
                    result = grant_role(tx, row["id"])
                    cids.append(result["cid"])
                    rendered.append({"id": row["id"], "num": 1, "type": 3, "c_id": result["cid"]})
            else:
                more, changes = grant_rewards(tx, [row])
                rendered.extend(more)
                replies.extend(changes)
        if cids:
            replies.extend(role_pushes(tx.state, cids))
            replies.extend(resource_pushes(tx.state, ["inventory"]))
    for row in fresh:
        row.update(is_read=2, is_get=2, read_at=row.get("read_at", now), claimed_at=now)
        row["claim_receipt"] = {"time": now, "attachments": deepcopy(row["data"]["rewards"])}
    if fresh:
        replies.extend(advance_tasks(tx.state, "state_changed"))
    return replies
