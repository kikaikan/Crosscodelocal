"""One-time resource-download reward, using the original local configuration.

SilentDownloadView.lua gates the claim button on client-side download progress.
Neither request carries a completion percentage or even a UID on the wire:
GameMsg.lua:6343-6362 defines empty 5201/5204 bodies. A claim is therefore a
trusted original UI event from an authenticated local game connection, not a
server-side assertion that every resource exists on the device.
"""
from copy import deepcopy
import json
import sys
import time

from admin_resources import award_items, integer
from config_codec import app_path
from database import StorageError
from server_core import Reply, register

SETTINGS = json.loads(app_path("data", "progression-settings.json").read_text(encoding="utf-8"))
SOURCE_KEY = "g_DownloadReward"
STATE_KEY = "download_reward"


class _AlreadyClaimed(Exception):
    """Leave the read-only transaction without creating a new save revision."""


def reward_rows():
    """g_DownloadReward=[id,quantity,reward-type]; never invent a card award."""
    source = SETTINGS.get(SOURCE_KEY)
    if not isinstance(source, dict) or source.get("type") != "json":
        raise StorageError("Missing local download reward configuration")
    try:
        values = json.loads(source["value"])
    except (ValueError, TypeError, KeyError) as error:
        raise StorageError("Invalid local download reward configuration") from error
    if not isinstance(values, list) or not 1 <= len(values) <= 20:
        raise StorageError("Download rewards require a bounded configured list")
    rows = []
    for value in values:
        if not isinstance(value, list) or len(value) != 3:
            raise StorageError("Invalid configured download reward row")
        identifier, quantity, kind = (integer(value[0], 1), integer(value[1], 1),
                                      integer(value[2], 2, 2))
        rows.append({"id": identifier, "num": quantity, "type": kind})
    return rows


def claimed(state):
    receipt = state.get(STATE_KEY)
    if receipt is None:
        return False
    if (not isinstance(receipt, dict) or receipt.get("version") != 1 or
            not isinstance(receipt.get("claimed"), bool)):
        raise StorageError("Unsupported persisted download reward receipt")
    return receipt["claimed"]


def initial_pushes(state):
    """The capture contains this push before LoginGame; use local history only."""
    return [Reply("DownloadProto:CheckDownloadRewardRet", {"isGet": claimed(state)})]


def request_uid(ctx, fields):
    uid = ctx.require_login()
    if ctx.role != "game" or not isinstance(fields, dict) or fields:
        raise StorageError("Download reward requires an empty authenticated game request")
    return uid


def checked(ctx, replies):
    # Validate actual reward/status wire bodies before committing any inventory
    # or claim marker. Encoding failure must roll back the whole transaction.
    for reply in replies:
        ctx.server.codec.encode_frame(reply.name, reply.fields)
    return replies


@register("DownloadProto:CheckDownloadReward")
async def check_download_reward(ctx, fields):
    uid = request_uid(ctx, fields)
    return checked(ctx, initial_pushes(ctx.store.get_player(uid)))


@register("DownloadProto:GetDownloadReward")
async def get_download_reward(ctx, fields):
    uid = request_uid(ctx, fields)
    try:
        with ctx.store.transaction(uid) as tx:
            if claimed(tx.state):
                raise _AlreadyClaimed()
            rows = reward_rows()
            rendered, replies = award_items(tx, rows)
            tasks = sys.modules.get("handlers.tasks")
            if tasks is not None:
                replies.extend(tasks.advance_tasks(tx.state, "state_changed", 0))
            stamp = int(tx.state.get("offline_clock", time.time()))
            tx.state[STATE_KEY] = {"version": 1, "claimed": True,
                                   "claimed_at": stamp, "source_key": SOURCE_KEY,
                                   "rewards": deepcopy(rendered)}
            # Release the claim button's callback before showing the normal
            # source reward popup. ItemUpdate already contains the real balance.
            replies.append(Reply("DownloadProto:GetDownloadRewardRet", {"result": True}))
            replies.append(Reply("ClientProto:RewardNotice", {"rewards": rendered, "is_finish": True}))
            checked(ctx, replies)
        return replies
    except _AlreadyClaimed:
        # Success means the one-time reward is already recorded. Repeated
        # clicks/reconnects do not produce another award or mutate that receipt.
        return checked(ctx, [Reply("DownloadProto:GetDownloadRewardRet", {"result": True})])
    except StorageError:
        # A configured award that cannot be applied closes the waiting view with
        # the source failure result, after SQLite has rolled back every change.
        return checked(ctx, [Reply("DownloadProto:GetDownloadRewardRet", {"result": False})])
