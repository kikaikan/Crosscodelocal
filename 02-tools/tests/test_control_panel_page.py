"""Control-page checks for the per-account bag view and item risk warnings.

These call control_panel directly through a fake handler: no HTTP server, no
game server, and no real save is opened.
"""
import io
import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import control_panel  # noqa: E402


class Handler:
    command = "GET"

    def __init__(self):
        self.wfile, self.headers = io.BytesIO(), {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


def make_database(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE accounts (uid INTEGER, account_name TEXT,"
                           " created_at INTEGER, revision INTEGER, state_json TEXT)")
        connection.execute("INSERT INTO accounts VALUES (1, 'local', 0, 1, ?)",
                           (json.dumps(state),))


def test_page_carries_the_bag_entry_and_warning_area():
    page = control_panel.PAGE
    assert "当前账号背包" in page
    assert 'id="inventory-rows"' in page
    assert 'id="resource-warning"' in page
    assert "riskLabels" in page


def test_state_route_lists_the_selected_account_bag(tmp_path, monkeypatch):
    root = tmp_path / "app"
    make_database(root / "07-server/data/players.sqlite3",
                  {"player": {"level": 2, "name": "测试"},
                   "inventory": {"60101": 7, "10004": 3, "not-a-cfgid": 9, "10002": 0}})
    monkeypatch.setattr(control_panel, "ROOT", root)
    monkeypatch.setattr(control_panel, "probe_port",
                        lambda port: {"listening": False, "error": None})
    handler = Handler()
    assert control_panel.serve_control(handler, "/control/state")
    database = json.loads(handler.wfile.getvalue())["database"]
    assert database["available"] is True
    assert database["total"] == 1
    # The snapshot keeps configured rows (including a zero balance) and drops
    # non-numeric keys; the page itself filters to quantities above zero.
    assert database["accounts"][0]["inventory"] == {"60101": 7, "10004": 3, "10002": 0}


def test_page_script_parses_with_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable; page JavaScript check was not run")
    script = control_panel.PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
    result = subprocess.run([node, "--check"], input=script, text=True,
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
