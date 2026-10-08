"""Loopback-only JSON controls; game changes use one durable transaction."""
import hmac
import json
from pathlib import Path
import secrets
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
TOKEN = secrets.token_urlsafe(32)
MAX_BODY = 65536
_codec = None


def send_json(handler, status, value):
    body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('Connection', 'close')
    handler.end_headers()
    handler.close_connection = True
    handler.wfile.write(body)


def dependencies():
    global _codec
    server_dir = str(ROOT / '07-server')
    if server_dir not in sys.path:
        sys.path.insert(0, server_dir)
    from admin_control import catalog, execute
    from database import Store, StorageError
    from protocol_codec import IVProtoCodec, WireConfig
    if _codec is None:
        _codec = IVProtoCodec(json.loads((ROOT / '05-protocol/endpoints.json').read_text(encoding='utf-8')),
                              WireConfig('little'))
    return catalog, execute, Store, StorageError


def serve_gateway(handler, path):
    route = urlsplit(path).path
    if route not in ('/control/session', '/control/catalog') and not route.startswith('/control/api/'):
        return False
    # Host checking also prevents rebinding a remote website onto the local API.
    expected_host = '127.0.0.1:' + str(handler.server.server_port)
    if handler.client_address[0] != '127.0.0.1' or handler.headers.get('Host') != expected_host:
        send_json(handler, 403, {'ok': False, 'message': '请使用127.0.0.1的本地控制页'})
        return True
    if handler.command == 'GET' and route == '/control/session':
        send_json(handler, 200, {'csrf_token': TOKEN})
        return True
    if handler.command == 'GET' and route == '/control/catalog':
        try:
            get_catalog, unused, unused_store, unused_error = dependencies()
            send_json(handler, 200, get_catalog())
        except (ValueError, OSError, ImportError):
            send_json(handler, 503, {'ok': False, 'message': '本地配置尚未加载，请稍后刷新'})
        return True
    if handler.command != 'POST' or route not in tuple('/control/api/' + action for action in ('resource', 'mail', 'clear_mail', 'role', 'pools', 'access')):
        send_json(handler, 405, {'ok': False, 'message': '此接口不支持该请求'})
        return True
    if (handler.headers.get('Origin') != 'http://' + expected_host or
            not hmac.compare_digest(handler.headers.get('X-Control-Token', '').encode('utf-8'), TOKEN.encode('utf-8')) or
            handler.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json' or
            handler.headers.get('Transfer-Encoding')):
        send_json(handler, 403, {'ok': False, 'message': '控制页会话已失效，请刷新后重试'})
        return True
    try:
        length = int(handler.headers.get('Content-Length', '0'))
        if not 0 < length <= MAX_BODY:
            raise ValueError()
        handler.connection.settimeout(5)
        body = handler.rfile.read(length)
        if len(body) != length:
            raise ValueError()
        payload = json.loads(body, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, TimeoutError):
        send_json(handler, 400, {'ok': False, 'message': '请求格式无效或内容过长'})
        return True
    # A frozen build keeps the save beside the executable (server_core resolves it
    # through app_path), which is not below ROOT; a source checkout keeps it inside
    # the checkout that ROOT already points at.
    if getattr(sys, 'frozen', False):
        from config_codec import app_path
        database = app_path('data', 'players.sqlite3')
    else:
        database = ROOT / '07-server/data/players.sqlite3'
    if not database.is_file():
        send_json(handler, 409, {'ok': False, 'message': '请先在游戏中创建本地账号'})
        return True
    store = None
    try:
        get_catalog, execute, Store, StorageError = dependencies()
        store = Store(database)
        result = execute(store, route.rsplit('/', 1)[-1], payload, _codec)
        send_json(handler, 200, {'ok': True, 'message': '已保存。在线角色约5秒内同步，离线角色下次登录生效。', 'result': result})
    except (ValueError, TypeError, KeyError, ImportError) as error:
        send_json(handler, 400, {'ok': False, 'message': str(error) or '操作内容不正确'})
    except Exception:
        send_json(handler, 503, {'ok': False, 'message': '本地存档暂时不可写，请稍后重试'})
    finally:
        if store is not None:
            store.close()
    return True
