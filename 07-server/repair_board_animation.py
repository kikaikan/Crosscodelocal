"""Enable existing boards whose restored source model paths were previously absent.

Only the five source-backed restored models are migrated. A SQLite backup is mandatory;
layouts, selected boards, other skins and resource ownership stay unchanged.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

RESTORED_MODELS = frozenset({6031004, 7805003, 6015005, 7102303, 4002005})


def repair(state, stamp):
    section = state.get('panels', {})
    changes = []
    for group in ('panels', 'random_panels'):
        for key, row in section.get(group, {}).items():
            for slot, model in enumerate(row.get('ids', [])[:2], 1):
                detail = row.get('detail' + str(slot))
                if model in RESTORED_MODELS and isinstance(detail, dict) and detail.get('live2d') is False:
                    detail['live2d'] = True
                    changes.append({'group': group, 'idx': key, 'slot': slot, 'model': model})
    if changes:
        section['update_time'] = max(section.get('update_time', 0), stamp)
    return changes


def migrate(database, backup):
    from skins_service import catalog
    for model in RESTORED_MODELS:
        if not catalog()['models'][str(model)]['has_l2d']:
            raise ValueError('Regenerate the skin catalog before migration')
    database, backup = Path(database).resolve(), Path(backup).resolve()
    if not database.is_file() or backup.exists():
        raise ValueError('Database must exist and backup must be a new file')
    backup.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database, isolation_level=None)) as connection:
        with closing(sqlite3.connect(backup)) as destination:
            connection.backup(destination)
        connection.execute('BEGIN IMMEDIATE')
        result = []
        try:
            for uid, raw in connection.execute('SELECT uid,state_json FROM accounts').fetchall():
                state = json.loads(raw)
                changes = repair(state, int(time.time()))
                if not changes:
                    continue
                connection.execute('UPDATE accounts SET state_json=?,revision=revision+1 WHERE uid=?',
                                   (json.dumps(state, ensure_ascii=False, separators=(',', ':')), uid))
                result.append({'uid': uid, 'changes': changes})
            connection.execute('COMMIT')
        except BaseException:
            connection.execute('ROLLBACK')
            raise
    return {'database': str(database), 'backup': str(backup), 'accounts': result}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default=str(Path(__file__).parent / 'data/players.sqlite3'))
    parser.add_argument('--backup', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(migrate(args.database, args.backup), ensure_ascii=False, indent=2))
