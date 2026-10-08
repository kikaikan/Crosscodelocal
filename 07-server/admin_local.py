"""Explicit local-account operations through the same transactional control API."""
import argparse
import json
from pathlib import Path
import uuid

from admin_control import execute
from database import Store
from server_core import IVProtoCodec, WireConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['clear_mail'])
    parser.add_argument('--uid', required=True, type=int)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    codec = IVProtoCodec(json.loads((root / '05-protocol/endpoints.json').read_text('utf-8')),
                         WireConfig('little'))
    store = Store(root / '07-server/data/players.sqlite3')
    try:
        result = execute(store, args.action, {'uid': args.uid, 'request_id': str(uuid.uuid4())}, codec)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        store.close()


if __name__ == '__main__':
    main()
