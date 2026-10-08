"""Actual HTTP request checks, using only an isolated temporary save."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

import control_gateway as gateway
SERVER = Path(__file__).resolve().parents[2] / '07-server'
sys.path.insert(0, str(SERVER))
from admin_control import catalog, execute
from database import Store, StorageError
from server_core import load_dependencies

CODEC, SEED = load_dependencies(SERVER.parent / '05-protocol/endpoints.json', SERVER / 'data/new_account_seed.json')


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='control-http-')
        self.root = Path(self.temp.name)
        database = self.root / '07-server/data/players.sqlite3'
        store = Store(database)
        self.uid = store.create_account('control-test', SEED)['uid']
        store.close()
        self.patches = [patch.object(gateway, 'ROOT', self.root),
                        patch.object(gateway, '_codec', CODEC),
                        patch.object(gateway, 'dependencies', lambda: (catalog, execute, Store, StorageError))]
        for replacement in self.patches:
            replacement.start()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                gateway.serve_gateway(self, self.path)
            def do_POST(self):
                gateway.serve_gateway(self, self.path)
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        for replacement in reversed(self.patches):
            replacement.stop()
        self.temp.cleanup()

    def request(self, method, path, data=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        body = json.dumps(data).encode() if data is not None else None
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_valid_form_and_duplicate_post_persist_once(self):
        status, session = self.request('GET', '/control/session')
        self.assertEqual(status, 200)
        headers = {'Origin': self.origin, 'Content-Type': 'application/json', 'X-Control-Token': session['csrf_token']}
        data = {'uid': self.uid, 'key': 'gold', 'mode': 'add', 'amount': 17, 'request_id': str(uuid.uuid4())}
        status, result = self.request('POST', '/control/api/resource', data, headers)
        self.assertEqual(status, 200)
        self.assertEqual(result['result']['after'], 1017)
        status, repeated = self.request('POST', '/control/api/resource', data, headers)
        self.assertEqual(status, 200)
        self.assertTrue(repeated['result']['replayed'])

    def test_wrong_origin_token_content_type_and_host_cannot_write(self):
        data = {'uid': self.uid, 'key': 'gold', 'mode': 'add', 'amount': 1, 'request_id': str(uuid.uuid4())}
        good = {'Origin': self.origin, 'Content-Type': 'application/json', 'X-Control-Token': gateway.TOKEN}
        for headers in ({**good, 'Origin': 'https://example.invalid'},
                        {**good, 'X-Control-Token': 'wrong'},
                        {**good, 'Content-Type': 'text/plain'},
                        {**good, 'Host': 'example.invalid'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request('POST', '/control/api/resource', data, headers)[0], 403)
        store = Store(self.root / '07-server/data/players.sqlite3')
        self.assertEqual(store.get_player(self.uid)['player']['gold'], 1000)
        store.close()

    def test_get_mutation_route_and_missing_account_rejected(self):
        self.assertEqual(self.request('GET', '/control/api/resource')[0], 405)
        headers = {'Origin': self.origin, 'Content-Type': 'application/json', 'X-Control-Token': gateway.TOKEN}
        data = {'uid': self.uid + 1, 'key': 'gold', 'mode': 'add', 'amount': 1, 'request_id': str(uuid.uuid4())}
        self.assertEqual(self.request('POST', '/control/api/resource', data, headers)[0], 400)


if __name__ == '__main__':
    unittest.main()
