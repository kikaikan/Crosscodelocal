"""副天赋槽位迁移：把存档里已有的卡按 break_level 物化 sub_talent 槽位。

默认 dry-run（只读、不写任何文件）；--apply 才写，且写前先把 sqlite3 存档文件
（含 -wal/-shm）复制到备份目录。逐卡调用 handlers.sub_talent.ensure_slots，
与运行时 handler 走同一条代码路径。

用法：
  python -B 02-tools/scripts/sub-talent-migrate.py                     # 预览
  python -B 02-tools/scripts/sub-talent-migrate.py --uid 900000002     # 只看一个账号
  python -B 02-tools/scripts/sub-talent-migrate.py --apply             # 备份后写入

物化后的守卫口径（task-27 已收敛）：
  * use 全 0（未装备）→ 无属性影响：CardUpgrade / CardBreak / 出战全部放行。
  * use 有非零（玩家自己装备了）→ equipment_stats 按 CardCalculator.lua:410-443 精确计入
    属性与战斗载荷，出战放行；但 cards_items.recalculate_bare_hp 的裸 HP 公式不含该乘区，
    这类卡的升级/跃升仍会被拒（先卸载即可，或等第三阶段把 card['hp'] 换成 equipped_stats 口径）。
本脚本只物化 had、use 保持全 0，因此 --apply 后不会影响升级/跃升。
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
import sys

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / '07-server'))
sys.path.insert(0, str(PROJECT / '02-tools/scripts'))

from database import Store, StorageError                    # noqa: E402
from handlers import sub_talent                             # noqa: E402
from handlers.cards_items import keyed_config               # noqa: E402


class _NoChange(Exception):
    """Roll the transaction back untouched when a card needs no materialization."""


def read_accounts(path):
    """Read-only snapshot: [(uid, state)] without taking the database write lock."""
    uri = 'file:%s?mode=ro' % path.as_posix()
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        connection = sqlite3.connect(path)
    try:
        rows = connection.execute('SELECT uid, state_json FROM accounts ORDER BY uid').fetchall()
    finally:
        connection.close()
    return [(int(uid), json.loads(state)) for uid, state in rows]


def plan(state):
    """Return [(cid, cfgid, before_had, after_had, after_use)] for cards that would change."""
    result = []
    for card in state.get('cards', []):
        try:
            config = keyed_config('cfgCardData.lua', card['cfgid'])
        except (StorageError, KeyError):
            continue
        before = card.get('sub_talent') if isinstance(card.get('sub_talent'), dict) else {}
        probe = {'sub_talent': {'had': list(before.get('had', [])), 'use': list(before.get('use', []))},
                 'break_level': card.get('break_level', 1)}
        if not sub_talent.ensure_slots(probe, config):
            continue
        result.append((int(card['cid']), int(card['cfgid']),
                       list(before.get('had', [])),
                       probe['sub_talent']['had'], probe['sub_talent']['use']))
    return result


def backup(path, directory):
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    saved = []
    for suffix in ('', '-wal', '-shm'):
        source = Path(str(path) + suffix)
        if source.exists():
            target = directory / (source.name + '.' + stamp + '.bak')
            shutil.copy2(source, target)
            saved.append(target)
    return saved


def apply_accounts(path, uids):
    """Materialize inside Store.transaction so revision/format stay identical to runtime."""
    store = Store(path)
    changed = 0
    try:
        for uid in uids:
            try:
                with store.transaction(uid) as tx:
                    touched = 0
                    for card in tx.state.get('cards', []):
                        try:
                            config = keyed_config('cfgCardData.lua', card['cfgid'])
                        except (StorageError, KeyError):
                            continue
                        if sub_talent.ensure_slots(card, config):
                            touched += 1
                    if not touched:
                        raise _NoChange()
            except _NoChange:
                continue
            changed += 1
    finally:
        store.close()
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--database', type=Path,
                        default=PROJECT / '07-server/data/players.sqlite3')
    parser.add_argument('--backup-dir', type=Path,
                        default=PROJECT / '07-server/data/backups')
    parser.add_argument('--uid', action='append', type=int, default=None,
                        help='只处理该 uid（可重复）；默认全部账号')
    parser.add_argument('--apply', action='store_true', help='真正写入（默认 dry-run）')
    parser.add_argument('--limit', type=int, default=20, help='每个账号最多打印多少张卡')
    args = parser.parse_args()
    if not args.database.is_file():
        raise SystemExit('存档不存在：%s' % args.database)
    accounts = read_accounts(args.database)
    if args.uid:
        wanted = set(args.uid)
        accounts = [row for row in accounts if row[0] in wanted]
    total = 0
    pending = []
    for uid, state in accounts:
        rows = plan(state)
        total += len(rows)
        if rows:
            pending.append(uid)
        print('uid=%s cards=%s 需要物化=%s' % (uid, len(state.get('cards', [])), len(rows)))
        for cid, cfgid, before, had, use in rows[:args.limit]:
            print('    cid=%s cfgid=%s had %s -> %s use -> %s' % (cid, cfgid, before, had, use))
        if len(rows) > args.limit:
            print('    …（其余 %s 张省略）' % (len(rows) - args.limit))
    print(json.dumps({'mode': 'apply' if args.apply else 'dry-run',
                      'accounts': len(accounts), 'cards_to_materialize': total,
                      'accounts_with_changes': len(pending)},
                     ensure_ascii=False))
    print('提醒：本脚本只物化 had、use 保持全 0；按 task-27 收敛后的口径，'
          '这些卡的 CardUpgrade / CardBreak / 出战都不会被副天赋守卫拦住。')
    if not args.apply:
        print('dry-run 结束：未写入任何文件。加 --apply 才会备份并写入。')
        return
    if not total:
        print('无需写入。')
        return
    saved = backup(args.database, args.backup_dir)
    print('已备份：%s' % ', '.join(str(path) for path in saved))
    written = apply_accounts(args.database, pending)
    print(json.dumps({'applied_accounts': written}, ensure_ascii=False))


if __name__ == '__main__':
    main()
