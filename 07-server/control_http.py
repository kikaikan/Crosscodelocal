"""HTTP resource/control listener bundled into the single-file server executable.

This is a deliberately small listener. It reuses the existing read-only control
page and control gateway (02-tools/scripts) and, when a plain-file resource root
is present, serves those files under their /cross/release/... paths. It never
reads the preserved TAR archives, so it works with nothing but the executable
and the files the user placed next to it.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


def _router_factory(gateway, control, static, app_dir: Path):
    class ControlHandler(BaseHTTPRequestHandler):
        # The control gateway checks Host against this attribute; the real HTTP
        # server fills it in, but a bare class assignment keeps it explicit.
        server_port = 0
        protocol_version = 'HTTP/1.1'

        def _json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == '/health':
                self._json(200, {'mode': 'crosscore-ps-server', 'business_server': True,
                                 'app_dir': str(app_dir), 'handlers': True})
                return
            if gateway is not None and gateway(self, self.path):
                return
            if control is not None and control(self, self.path):
                return
            self._static()

        def do_HEAD(self):
            self._static(head=True)

        def do_POST(self):
            if gateway is None or not gateway(self, self.path):
                self.send_error(404)

        def _static(self, head=False):
            if static is None:
                self.send_error(404)
                return
            route = urlsplit(self.path).path
            if not route.startswith('/cross/release/'):
                self.send_error(404)
                return
            relative = PurePosixPath(unquote(route[len('/cross/release/'):]))
            if (not relative.parts or relative.is_absolute() or '..' in relative.parts
                    or any(':' in part or '\\' in part for part in relative.parts)):
                self.send_error(404)
                return
            target = (static / Path(*relative.parts)).resolve()
            if not target.is_relative_to(static) or not target.is_file():
                self.send_error(404)
                return
            content = target.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            if not head:
                self.wfile.write(content)

        def log_message(self, *unused):
            pass

    return ControlHandler


def build_server(port, bind, app_dir, static_dir=None):
    """Return a bound ThreadingHTTPServer serving the control page on `port`."""
    import control_gateway
    import control_panel
    # The control modules resolve their workspace root from their own __file__.
    # Frozen builds keep the layout below the executable, so both roots are
    # pointed at the application directory before the first request.
    app_dir = Path(app_dir).resolve()
    control_panel.ROOT = app_dir
    control_gateway.ROOT = app_dir
    static = Path(static_dir).resolve() if static_dir else None
    handler = _router_factory(control_gateway.serve_gateway, control_panel.serve_control, static, app_dir)
    return ThreadingHTTPServer((bind, port), handler)
