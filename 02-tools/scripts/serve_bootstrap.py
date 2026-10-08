"""Serve preserved bootstrap, bundles, sounds and videos locally, without CDN fallback."""
import argparse
from dataclasses import dataclass
import hashlib
import json
import os
import tempfile
import re
import tarfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from config_codec import encode_payload
from control_panel import serve_control
from control_gateway import serve_gateway

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ArchivedFile:
    archive: Path
    offset: int
    size: int


ARCHIVE_NAMES = ('assets-preservation', 'assets-videos')
ARCHIVE_ROOTS = {'Custom', 'sounds', 'il2cpp', 'videos'}
INDEX_VERSION = 1
MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_INDEX_ENTRIES = 1_000_000


def _integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 9223372036854775807:
        raise ValueError('Invalid archive index integer')
    return value


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate archive cache JSON key')
        result[key] = value
    return result


def _progress(callback, **fields):
    if callback is not None:
        callback(fields)


def _archive_specs(root):
    archives, identity = {}, []
    directory = (root / '01-device').resolve()
    for name in ARCHIVE_NAMES:
        archive = (directory / (name + '.tar')).resolve(strict=True)
        if not archive.is_relative_to(directory) or archive.name != name + '.tar':
            raise ValueError('Unexpected preserved archive path')
        metadata = archive.with_suffix('.json').read_bytes()
        if len(metadata) > 1024 * 1024:
            raise ValueError('Oversized preserved archive manifest')
        record = json.loads(metadata.decode('utf-8-sig'), object_pairs_hook=_unique_object)
        expected = _integer(record['bytes'], 1)
        stat = archive.stat()
        if stat.st_size != expected:
            raise ValueError('Preserved archive size mismatch: ' + name)
        dirs = record['directories']
        if (not isinstance(dirs, list) or not dirs or
                any(not isinstance(value, str) for value in dirs) or
                len(set(dirs)) != len(dirs) or not set(dirs) <= ARCHIVE_ROOTS):
            raise ValueError('Unexpected archive root')
        digest = record['sha256']
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('Invalid preserved archive manifest SHA256')
        archives[archive.name] = (archive, set(dirs), expected)
        identity.append({'name': archive.name, 'path': str(archive),
                         'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                         'manifest_sha256': digest, 'manifest_bytes': expected,
                         'manifest_directories': dirs,
                         'manifest_file_sha256': hashlib.sha256(metadata).hexdigest(),
                         'manifest_file_bytes': len(metadata)})
    return archives, identity


def _member_key(name, allowed, canonical=False):
    if (not isinstance(name, str) or not name or len(name) > 4096 or
            '\\' in name or any(ord(char) < 32 for char in name)):
        raise ValueError('Unsafe preserved archive path')
    path = PurePosixPath(name)
    if (not path.parts or path.is_absolute() or '..' in path.parts or
            path.parts[0] not in allowed or canonical and path.as_posix() != name):
        raise ValueError('Unsafe preserved archive path')
    return path.as_posix()


def _entry(archive, offset, size, limit):
    offset, size = _integer(offset, 512), _integer(size)
    if offset % 512 or offset + size > limit or offset + ((size + 511) // 512) * 512 > limit:
        raise ValueError('Unexpected archive entry')
    return ArchivedFile(archive, offset, size)


def _check_overlap(entries):
    ranges = {}
    for entry in entries.values():
        ranges.setdefault(entry.archive, []).append((entry.offset, entry.size))
    for group in ranges.values():
        previous_end = 0
        for offset, size in sorted(group):
            # Every regular TAR entry needs its own preceding 512-byte header.
            if offset < previous_end + 512:
                raise ValueError('Overlapping preserved archive entries')
            previous_end = offset + ((size + 511) // 512) * 512


def _cache_rows(entries):
    return [{'key': key, 'archive': row.archive.name, 'offset': row.offset, 'size': row.size}
            for key, row in sorted(entries.items())]


def _read_index(cache_path, archives, identity):
    try:
        if cache_path.stat().st_size > MAX_INDEX_BYTES:
            return None
        raw = cache_path.read_bytes()
        if len(raw) > MAX_INDEX_BYTES:
            return None
        saved = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
        if not isinstance(saved, dict):
            return None
        if (type(saved.get('version')) is not int or saved['version'] != INDEX_VERSION or
                _json_bytes(saved.get('archives')) != _json_bytes(identity)):
            return None
        rows = saved['entries']
        if (not isinstance(rows, list) or type(saved.get('entry_count')) is not int or
                saved['entry_count'] != len(rows) or len(rows) > MAX_INDEX_ENTRIES or
                len(rows) > sum(value[2] // 512 for value in archives.values()) or
                hashlib.sha256(_json_bytes(rows)).hexdigest() != saved['entries_sha256']):
            return None
        entries = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'key', 'archive', 'offset', 'size'}:
                raise ValueError('Invalid preserved archive cache entry')
            archive, allowed, limit = archives[row['archive']]
            key = _member_key(row['key'], allowed, canonical=True)
            if key in entries:
                raise ValueError('Duplicate preserved resource')
            entries[key] = _entry(archive, row['offset'], row['size'], limit)
        _check_overlap(entries)
        return entries
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError):
        return None


def _atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name + '-',
                                         suffix='.tmp', delete=False) as stream:
            pending = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def index_archives(root=None, cache_path=None, progress=None, force_rebuild=False):
    """Reuse a bounded local index only for unchanged, checked archive metadata.

    The small manifest bytes are hashed; full TAR hashing is never added here.
    Fingerprints retain each manifest's recorded archive hash, size and roots.
    An invalid cache falls back to the original regular-member TAR checks.
    """
    root = Path(ROOT if root is None else root).resolve()
    cache_path = Path(cache_path) if cache_path is not None else root / '07-server/run/archive-index.json'
    archives, identity = _archive_specs(root)
    entries = None if force_rebuild else _read_index(cache_path, archives, identity)
    if entries is not None:
        if _archive_specs(root)[1] != identity:
            raise ValueError('Preserved archive changed while loading index')
        _progress(progress, phase='archive-index', cache='hit', entries=len(entries))
        return entries
    _progress(progress, phase='archive-index', cache='rebuild', path=str(cache_path))
    entries = {}
    for name, (archive, allowed, limit) in archives.items():
        _progress(progress, phase='indexing-archive', archive=name, bytes=limit)
        with tarfile.open(archive, 'r:') as stream:
            for member in stream:
                key = _member_key(member.name, allowed)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError('Unexpected archive entry')
                if key in entries:
                    raise ValueError('Duplicate preserved resource')
                entries[key] = _entry(archive, member.offset_data, member.size, limit)
                if len(entries) > MAX_INDEX_ENTRIES:
                    raise ValueError('Oversized preserved archive index')
    _check_overlap(entries)
    if _archive_specs(root)[1] != identity:
        raise ValueError('Preserved archive changed while indexing')
    rows = _cache_rows(entries)
    encoded = _json_bytes({'version': INDEX_VERSION, 'archives': identity,
                           'entry_count': len(rows), 'entries': rows,
                           'entries_sha256': hashlib.sha256(_json_bytes(rows)).hexdigest()})
    if len(encoded) > MAX_INDEX_BYTES:
        raise ValueError('Oversized preserved archive index')
    try:
        _atomic_write(cache_path, encoded)
    except OSError:
        # Read-only cache storage should not make a valid in-memory index unusable.
        _progress(progress, phase='archive-index', cache='write-failed', entries=len(entries))
    else:
        _progress(progress, phase='archive-index', cache='written', entries=len(entries))
    return entries


def export_static(resources, plaintext, directory=None):
    """Write only changed bytes to checked, exact local overlay destinations."""
    base = Path(ROOT / '06-client/local-static' if directory is None else directory).resolve()
    outputs = [(base / 'server-list.local.json', plaintext)]
    for url, content in resources.items():
        if (not isinstance(url, str) or not url.startswith('/') or url.startswith('//') or
                '\\' in url or any(ord(char) < 32 for char in url)):
            raise ValueError('Unsafe local static resource path')
        relative = PurePosixPath(url[1:])
        if (not relative.parts or relative.is_absolute() or '..' in relative.parts or
                relative.as_posix() != url[1:] or any(':' in part for part in relative.parts)):
            raise ValueError('Unsafe local static resource path')
        destination = base.joinpath(*relative.parts).resolve()
        if not destination.is_relative_to(base):
            raise ValueError('Local static resource escapes overlay')
        outputs.append((destination, content))
    # Validate all destinations before the first write, including the fixed file.
    reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {prefix + str(index)
                for prefix in ('COM', 'LPT') for index in range(1, 10)}
    if any(part.rstrip(' .') != part or part.split('.')[0].upper() in reserved or
           any(char in part for char in '<>"|?*')
           for path, _ in outputs for part in path.relative_to(base).parts):
        raise ValueError('Unsafe local static filename')
    if len({path.resolve() for path, _ in outputs}) != len(outputs):
        raise ValueError('Duplicate local static output')
    if any(not path.resolve().is_relative_to(base) or not isinstance(content, bytes)
           for path, content in outputs):
        raise ValueError('Unsafe local static output')
    changed = 0
    for path, content in outputs:
        if path.is_file() and path.stat().st_size == len(content) and path.read_bytes() == content:
            continue
        _atomic_write(path, content)
        changed += 1
    return changed


def resource_key(path):
    match = re.fullmatch(r'/cross/release/android/(?:curr|curr_1)/(.+)', path)
    if not match:
        return None
    value = unquote(match[1])
    parts = PurePosixPath(value).parts
    if (not parts or parts[0] not in {'Custom', 'sounds', 'il2cpp', 'videos'} or
            '..' in parts or '\\' in value or value.startswith('/')):
        return None
    return PurePosixPath(value).as_posix()


def requested_range(header, size):
    if header is None:
        return 0, size, False
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header.strip())
    if not match or not any(match.groups()) or size <= 0:
        raise ValueError('Unsupported byte range')
    if not match[1]:
        count = int(match[2])
        if count <= 0:
            raise ValueError('Invalid suffix length')
        start, end = max(0, size - count), size
    else:
        start = int(match[1])
        end = min(size, int(match[2]) + 1) if match[2] else size
        if start >= size or end <= start:
            raise ValueError('Unsatisfiable byte range')
    return start, end, True


def load_offline_overlay(resources):
    report_path = ROOT / '06-client/reports/offline-version-report.json'
    if not report_path.exists():
        return
    report = json.loads(report_path.read_text(encoding='utf-8-sig'))
    directory = (ROOT / '06-client/offline-resources').resolve()
    lua_path = Path(report['resourcePath']).resolve(strict=True)
    if not lua_path.is_relative_to(directory):
        raise ValueError('Offline Lua must belong to the project')
    lua = lua_path.read_bytes()
    if hashlib.sha256(lua).hexdigest() != report['resourceSha256']:
        raise ValueError('Offline Lua differs from version metadata')
    for output in report['outputs']:
        path = Path(output['outputPath']).resolve(strict=True)
        if not path.is_relative_to(directory / 'bootstrap'):
            raise ValueError('Offline version metadata must belong to bootstrap overlay')
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != output['outputSha256']:
            raise ValueError('Offline version metadata hash mismatch')
        resources['/cross/release/' + path.relative_to(directory / 'bootstrap').as_posix()] = content
    for environment in ('curr', 'curr_1'):
        resources[f'/cross/release/android/{environment}/Custom/luascripts'] = lua


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--client-host", default="10.0.2.2")
    parser.add_argument("--index-only", action="store_true",
                        help="Warm the archive index cache and exit without serving or exporting static files")
    parser.add_argument("--rebuild-index", action="store_true", help="Ignore the saved archive index")
    args = parser.parse_args()
    report_progress = lambda fields: print(json.dumps(fields, ensure_ascii=False), flush=True)
    report_progress({"phase": "indexing"})
    archived = index_archives(progress=report_progress, force_rebuild=args.rebuild_index)
    if args.index_only:
        report_progress({"phase": "index-ready", "archived_files": len(archived)})
        return
    base_url = f"http://{args.client_host}:{args.port}"
    resources = {}
    for metadata_file in sorted((ROOT / "04-capture/decoded/http").glob("*/metadata.json")):
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        if metadata["host"].startswith(("cdn.", "cdn2.")) and metadata["status_code"] == 200:
            resources[metadata["path"]] = (metadata_file.parent / "response.body").read_bytes()
    sl = json.loads((ROOT / "05-protocol/samples/server-list/sl.decoded.json").read_text(encoding="utf-8"))
    for record in sl["data"]:
        record.update({"description": "CrossCore PS local", "serverName": "CrossCore PS local",
                       "serverIp": [f"{args.client_host}:19001"], "port": 19001,
                       "gmSvrIp": args.client_host, "gmSvrPort": 19002,
                       "webIp": base_url, "webPort": args.port,
                       "SDK_URL": base_url + "/php/sdk/"})
    plaintext = json.dumps(sl, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    resources["/cross/release/sl.json"] = encode_payload(plaintext).encode("ascii")
    load_offline_overlay(resources)
    changed_static = export_static(resources, plaintext)
    report_progress({"phase": "static-ready", "changed_files": changed_static})
    logfile = ROOT / "04-capture/logs/local-bootstrap.jsonl"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_HEAD(self):
            self.serve_content(False)

        def do_GET(self):
            if serve_gateway(self, self.path):
                return
            if serve_control(self, self.path):
                return
            self.serve_content(True)

        def do_POST(self):
            if not serve_gateway(self, self.path):
                self.send_error(404)

        def serve_content(self, send_body):
            path = urlsplit(self.path).path
            content = resources.get(path)
            entry = archived.get(resource_key(path)) if content is None else None
            status = 200 if content is not None or entry is not None else 404
            if path == "/health":
                content, status = b'{"mode":"static-bootstrap","business_server":false}', 200
            size = len(content) if content is not None else entry.size if entry is not None else 0
            start, end, partial = 0, size, False
            if status == 200:
                try:
                    start, end, partial = requested_range(self.headers.get('Range'), size)
                    status = 206 if partial else 200
                except ValueError:
                    status, start, end = 416, 0, 0
            with logfile.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"time_utc": datetime.now(timezone.utc).isoformat(),
                                         "path": path, "status": status, "bytes": end - start,
                                         'range': self.headers.get('Range'),
                                         "client": self.client_address[0]}) + "\n")
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(end - start))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Connection', 'close')
            if status == 206:
                self.send_header('Content-Range', f'bytes {start}-{end - 1}/{size}')
            elif status == 416:
                self.send_header('Content-Range', f'bytes */{size}')
            self.end_headers()
            self.close_connection = True
            if not send_body or status not in (200, 206):
                return
            try:
                if content is not None:
                    self.wfile.write(content[start:end])
                elif entry is not None:
                    with entry.archive.open('rb') as stream:
                        stream.seek(entry.offset + start)
                        remaining = end - start
                        while remaining:
                            block = stream.read(min(1 << 20, remaining))
                            if not block:
                                raise ValueError('Truncated preserved resource')
                            self.wfile.write(block)
                            remaining -= len(block)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *unused):
            pass

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(json.dumps({"phase": "listening", "bind": args.bind, "port": args.port,
                      "resources": len(resources), 'archived_files': len(archived),
                      "log": str(logfile)}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
