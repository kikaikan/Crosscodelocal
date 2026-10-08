"""档案回忆/插画 + 心间私语 全开放（本地策略，无官服样本）。

用户的诉求是「所有档案的回忆和插画都开启」+「所有心间私语开放」。三者的解锁判定全部落在
服务端存档上，因此不需要任何客户端 Lua 补丁、资源覆盖或设备部署：

  * 回忆：PlotMgr:IsPlayed(story_id) 为真 ⇔ story_id <= plot_data['line_'..(line or storyType)]
    （PlotMgr.lua:36-47 读、PlotMgr.lua:122-132 写）。plot_data 是 client_data 里的
    'plot_data'（type=3、data=JSON 字符串），由 handlers/player_state.py 的 get_client_data 下发。
  * 插画：MulPicInfo:IsHad() = BagMgr:GetCount(cfg.itemId) > 0，
    itemId 取自 cfgCfgArchiveMultiPicture.lua（144 条，cfgItemInfo type=16）。
  * 心间私语：ASMRData:IsBuy() = BagMgr:GetCount(cfg.item) > 0，
    item 取自 cfgCfgASMR.lua（7 条，cfgItemInfo type=27）。

数据表由 02-tools/scripts/collection-unlock-build.py 生成（151 件道具、20 条被 CfgArchiveStory
引用的剧情线）。apply() 是幂等 + 单调的：只补不降，任何已有进度都不会被调低。

本地策略（无官服样本）与副作用：把一条线标记为已看之后，PlotMgr:TryPlay 在该线首次触发时
会直接跳过自动播放（PlotMgr.lua:43-46）。但客户端仍可在档案「回忆」里点开重看
（ArchiveStoryItemTH.OnClick → CSAPI.OpenView("Plot",{storyID=...})），并且这让 CfgArchiveStory
的 607/607 条回忆、28/28 个分组全部可点（ArchiveStoryItemT.OnClick 要求 cur>0）。
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
import sqlite3
from functools import lru_cache
from pathlib import Path
import time

from config_codec import app_path
from database import StorageError

DATA = app_path('data', 'collection-unlock.json')
PLOT_KEY = 'plot_data'
LINE_PREFIX = 'line_'          # PlotMgr.lua:8
PLOT_TYPE = 3                  # PlotMgr:Save → PlayerProto:SetClientData(key, dict)


@lru_cache(maxsize=1)
def catalog() -> dict:
    """Read and validate the generated table once per process (config boundary)."""
    try:
        payload = json.loads(DATA.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise StorageError('Missing local collection-unlock catalog') from error
    items, lines = payload.get('items'), payload.get('plot_lines')
    if not isinstance(items, list) or not items or not isinstance(lines, dict) or not lines:
        raise StorageError('Malformed local collection-unlock catalog')
    return {'items': [int(value) for value in items],
            'lines': {int(key): int(value) for key, value in lines.items()}}


def plot_values(state) -> dict:
    """The stored plot_data mapping, or an empty one when the entry is unusable.

    The only legitimate writer is the client's PlotMgr (type 3 + JSON text; PlotMgr.lua:122-137),
    so anything else is treated as absent and rebuilt instead of parsed optimistically.
    """
    entry = (state.get('client_data') or {}).get(PLOT_KEY)
    if not isinstance(entry, dict) or int(entry.get('type', 0) or 0) != PLOT_TYPE:
        return {}
    raw = entry.get('data')
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw or '{}')
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def apply(state) -> bool:
    """Unlock every configured collection item and plot line. Returns True when changed.

    Idempotent and monotonic: the 151 items are only added when missing or non-positive,
    and each plot line only ever moves up to the largest story id that CfgArchiveStory
    references. An unchanged state is left byte-identical.
    """
    data = catalog()
    changed = False
    inventory = state.setdefault('inventory', {})
    for identifier in data['items']:
        key = str(identifier)
        if int(inventory.get(key, 0) or 0) <= 0:
            inventory[key] = 1
            changed = True
    merged = dict(plot_values(state))
    for line, story in data['lines'].items():
        key = LINE_PREFIX + str(line)
        current = merged.get(key)
        if not isinstance(current, int) or isinstance(current, bool) or current < story:
            merged[key] = story
            changed = True
    if not changed:
        return False
    state.setdefault('client_data', {})[PLOT_KEY] = {
        'type': PLOT_TYPE,
        'data': json.dumps(merged, separators=(',', ':'), sort_keys=True)}
    return True


def backup(store):
    """Copy the whole database next to it before a migration writes anything.

    Same layout as access_policy.migrate: 07-server/data/backups/ for the real save and a
    temporary directory when a test drives its own database.
    """
    directory = Path(store.path).parent / 'backups'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ('collection-unlock-%d.sqlite3' % time.time_ns())
    with closing(sqlite3.connect(target)) as destination:
        store.connection.backup(destination)
    return target


def migrate(store) -> int:
    """Apply the unlock to every existing local account; returns how many changed.

    Same shape as access_policy.migrate: accounts are first probed on a copy of their
    read-only state, so only accounts that actually need a change are written, and the
    backup is taken once and only when at least one account needs it.
    """
    uids = [int(row[0]) for row in store.connection.execute('SELECT uid FROM accounts').fetchall()]
    pending = [uid for uid in uids if apply(deepcopy(store.get_player(uid)))]
    if not pending:
        return 0
    backup(store)
    for uid in pending:
        with store.transaction(uid) as tx:
            apply(tx.state)
    return len(pending)
