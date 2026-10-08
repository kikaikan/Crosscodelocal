"""Offline disconnect and maintainability audit.

Default writes only the selected JSON and Markdown outputs. --check writes
nothing and compares stable P0/P1 signatures with a versioned baseline.
No client, listener, network request or writable live Store is created.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import sys
from types import SimpleNamespace

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / '07-server'
JSON_OUTPUT = ROOT / '90-notes/disconnect-audit.json'
MD_OUTPUT = ROOT / '90-notes/disconnect-audit.md'
VERSION = 2
TICK = chr(96)


class AuditError(RuntimeError):
    """An input cannot be audited without guessing."""


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def relative(path):
    return Path(path).resolve().relative_to(ROOT).as_posix()


class Sources:
    """Freeze inputs; evidence must resolve to an actual unique source location."""

    def __init__(self):
        self.raw = {}
        self.evidence = {}

    def read(self, path):
        key = relative(ROOT / path)
        if key not in self.raw:
            self.raw[key] = (ROOT / key).read_bytes()
        return self.raw[key].decode('utf-8-sig')

    def tree(self, path):
        return ast.parse(self.read(path), filename=path)

    def site(self, path, line):
        lines = self.read(path).splitlines()
        if type(line) is not int or not 1 <= line <= len(lines):
            raise AuditError(f'Invalid evidence location: {path}:{line}')
        return {'file': path, 'line': line, 'text': lines[line - 1].strip()}

    def anchor(self, key, path, needle):
        matches = [n for n, line in enumerate(self.read(path).splitlines(), 1) if needle in line]
        if len(matches) != 1:
            raise AuditError(f'{path}: {key!r} matched {len(matches)} lines')
        self.evidence[key] = self.site(path, matches[0])

    def fingerprint(self):
        return {path: {'sha256': sha(raw), 'bytes': len(raw)} for path, raw in sorted(self.raw.items())}

    def verify_unchanged(self):
        changed = [p for p, raw in self.raw.items() if (ROOT / p).read_bytes() != raw]
        if changed:
            raise AuditError('Source/config inputs changed during audit: ' + ', '.join(changed))


# Each anchor is unique by design. A change is an analysis error, not evidence
# silently attached to an outdated hard-coded line number.
ANCHORS = {
    'launcher': ('start_local.ps1', '[string[]]$Handlers ='),
    # 2026-10-05 error policy: unknown requests answer a tip instead of closing.
    'unsupported': ('07-server/server_core.py', 'raise UnknownRequest(frame.name,frame.opcode)'),
    'unsupported_close': ('07-server/server_core.py', 'close_reason = reason'),
    'tips_continue': ('07-server/server_core.py', '_,tip = tips_fields(request.name,request.opcode,client_message(error,request.name))'),
    'error_policy': ('07-server/error_policy.py', 'def classify(error, frames_sent=False):'),
    'encode_reply': ('07-server/server_core.py', 'outbound = [self.codec.encode_frame(reply.name,reply.fields) for reply in replies]'),
    'rejected': ('07-server/server_core.py', 'disposition,reason = classify(error)'),
    'handler_failure': ('07-server/server_core.py', "self.event(failure.pop('event'),**failure)"),
    'frame_encode_failed': ('07-server/server_core.py', "self.event('frame_encode_failed'"),
    'chunk_bytes': ('07-server/reply_chunks.py', 'cost = len(codec.encode_struct(row_struct, row))'),
    'busy_timeout': ('07-server/database.py', 'SQLITE_BUSY_TIMEOUT_MS = 1000'),
    'storage_busy': ('07-server/database.py', 'class StorageBusy(StorageError):'),
    'limits': ('07-server/server_core.py', 'log_path=None,idle_timeout='),
    'connection_limit': ('07-server/server_core.py', 'if self.connections>=self.max_connections:'),
    'partial_timeout': ('07-server/server_core.py', "raise TimeoutError('Partial assembly timeout')"),
    'idle_read': ('07-server/server_core.py', 'await asyncio.wait_for(reader.read(8192),timeout)'),
    'decode': ('07-server/server_core.py', 'requests,tail = self.codec.decode_stream(payload)'),
    'malformed': ('07-server/server_core.py', "raise CodecError('Malformed inner frame sequence')"),
    'outer_length': ('07-server/server_core.py', "raise CodecError('Client outer length out of range')"),
    'outer_flag': ('07-server/server_core.py', "raise CodecError('Unobserved client flag')"),
    'coalesced': ('07-server/server_core.py', "raise CodecError('Too many coalesced packets')"),
    'pending': ('07-server/server_core.py', "raise CodecError('Pending frame limit exceeded')"),
    'partial_eof': ('07-server/server_core.py', "raise CodecError('Client closed with incomplete packet')"),
    'reply_missing': ('02-tools/scripts/protocol_codec.py', 'raise CodecError(f"Unknown command'),
    'frame_limit': ('02-tools/scripts/protocol_codec.py', 'if length > self.config.max_frame_size:'),
    'unknown_opcode': ('02-tools/scripts/protocol_codec.py', 'raise CodecError(f"Unknown opcode'),
    'gate': ('07-server/access_policy.py', "raise StorageError('Feature is locked"),
    'gate_wrapper': ('07-server/server_core.py', 'require_feature(ctx.store.get_player(uid)'),
    'unknown_gate': ('07-server/handlers/initialization.py', 'if cfg is None:'),
    'database_connect': ('07-server/database.py', 'self.connection = sqlite3.connect('),
    'transaction': ('07-server/database.py', 'Synchronous BEGIN IMMEDIATE'),
    'control_store': ('02-tools/scripts/control_gateway.py', 'store = Store(database)'),
    'server_process': ('start_local.ps1', '$taskServer=Start-Process'),
    'control_process': ('start_local.ps1', '$taskStatic=Start-Process'),
    'birthday': ('07-server/handlers/player_state.py', 'date(2000,*birthday)'),
    'commander': ('07-server/handlers/player_state.py', 'commander=next(c for c'),
    'task_id': ('07-server/handlers/tasks.py', 'ids.add(integer(fields["id"]'),
    'client_finish': ('03-unpack/lua/device-luascripts/RoleMgr.lua', 'if (proto.finish) then'),
    'unknown_test': ('07-server/tests/test_server_core.py', 'def test_invalid_ticket_closes_but_unsupported_request_keeps_session'),
    'unknown_contract': ('07-server/error_policy.py', 'SystemProto:Tips so the client keeps a usable connection'),
    'equip_chunk': ('07-server/equip_service.py', 'batches=[equips[n:n+200]'),
    'shop_chunk': ('07-server/handlers/shop.py', 'rows[i:i + 25]'),
    'tasks_chunk': ('07-server/handlers/tasks.py', 'rows[i:i + 80]'),
    'mail_tip': ('07-server/handlers/mail.py', "replies = [Reply('SystemProto:Tips'"),
    'equip_tip': ('07-server/equip_service.py', "return [Reply('SystemProto:Tips'"),
    'client_pool': ('06-client/patch_offline_lua.py', 'if CrossCorePSAccess.pools == true then'),
    'client_activity': ('06-client/patch_offline_lua.py', 'CrossCorePSAccess.activities == true and cfg and cfg.group == 3'),
    'client_normal_gates': ('06-client/patch_offline_lua.py', 'CrossCore PS recomputes gates from saved progression'),
}


# request_failed is the 2026-10-05 event; the other three are kept so the
# historical log window still classifies correctly.
FAILURE_KINDS = ('request_failed', 'unsupported_handler', 'connection_rejected', 'handler_failure')


def launcher_modules(sources):
    text = sources.read('start_local.ps1')
    matches = list(re.finditer(r'\[string\[\]\]\s*\$Handlers\s*=\s*@\(([^)]*)\)', text))
    if len(matches) != 1:
        raise AuditError('Expected one literal launcher $Handlers default')
    body = matches[0].group(1)
    modules = re.findall(r"'([^']+)'", body)
    if re.sub(r"'[^']+'|[\s,]", '', body) or not modules or len(set(modules)) != len(modules):
        raise AuditError('Launcher modules must be unique literals')
    if any(not re.fullmatch(r'handlers\.[A-Za-z][A-Za-z0-9_]*', m) for m in modules):
        raise AuditError('Invalid launcher handler module')
    return modules


def registry_audit(modules, sources):
    if 'server_core' in sys.modules or any(m.startswith('handlers.') for m in sys.modules):
        raise AuditError('Registry analysis requires a fresh Python process')
    for path in (SERVER, ROOT / '02-tools/scripts'):
        sys.path.insert(0, str(path))
    core = importlib.import_module('server_core')
    failures = []
    for module in modules:
        try:
            importlib.import_module(module)
        except Exception as error:
            failures.append({'module': module, 'error': type(error).__name__, 'detail': str(error)})
    actual = dict(core.HANDLERS)
    loaded = sorted(m for m in sys.modules if m.startswith('handlers.'))
    before_modules = set(sys.modules)
    dormant = []
    try:
        for path in sorted((SERVER / 'handlers').glob('*.py')):
            module = 'handlers.' + path.stem
            if module in loaded or path.stem.startswith('_'):
                continue
            before = set(core.HANDLERS)
            error_text = None
            try:
                importlib.import_module(module)
            except Exception as error:
                error_text = f'{type(error).__name__}: {error}'
            dormant.append({'module': module, 'source': relative(path),
                            'handlers': sorted(set(core.HANDLERS) - before), 'import_error': error_text})
        potential = sorted(core.HANDLERS)
    finally:
        core.HANDLERS.clear()
        core.HANDLERS.update(actual)
        # Restore module presence because production code consults sys.modules.
        for module in set(sys.modules) - before_modules:
            sys.modules.pop(module, None)
        package = sys.modules.get('handlers')
        for item in dormant:
            name = item['module'].split('.')[-1]
            if package and hasattr(package, name):
                delattr(package, name)
    ownership = {}
    for name, handler in sorted(actual.items()):
        fn = inspect.unwrap(handler)
        path = relative(inspect.getsourcefile(fn))
        ownership[name] = {'module': fn.__module__, 'source': sources.site(path, inspect.getsourcelines(fn)[1])}
    return core, {'handler_count': len(actual), 'handlers': sorted(actual),
                  'handler_count_if_all_modules_loaded': len(potential), 'potential_handlers': potential,
                  'launcher_modules': modules, 'loaded_handler_modules': loaded,
                  'unloaded_modules': dormant, 'ownership': ownership, 'import_failures': failures}


def source_audit(sources, schema):
    for key, (path, needle) in ANCHORS.items():
        sources.anchor(key, path, needle)
    fn = next(n for n in ast.walk(sources.tree('07-server/server_core.py'))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == 'handle_connection')
    closes = [n for block in fn.body if isinstance(block, ast.Try) for n in block.finalbody
              if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
              and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == 'close']
    if len(closes) != 1:
        raise AuditError('Expected one finally writer.close()')
    sources.evidence['finally_close'] = sources.site('07-server/server_core.py', closes[0].lineno)
    names_seen, opcodes_seen = set(), set()
    for entry in schema['schemas']:
        if entry['name'] in names_seen or entry['opcode'] in opcodes_seen:
            raise AuditError('Duplicate schema name/opcode: ' + entry['name'])
        names_seen.add(entry['name'])
        opcodes_seen.add(entry['opcode'])
        for key in ('source', 'opcode_source'):
            site = entry.get(key)
            if not site:
                raise AuditError('Schema lacks source evidence: ' + entry['name'])
            text = sources.site(site['file'], site['line'])['text']
            if entry['name'] not in text:
                raise AuditError('Schema source mismatch: ' + entry['name'])
            if key == 'opcode_source' and not re.search(r'=\s*' + str(entry['opcode']) + r'\b', text):
                raise AuditError('Opcode source mismatch: ' + entry['name'])
    replies, categories = defaultdict(list), defaultdict(set)
    paths = sorted(p for p in SERVER.rglob('*.py') if 'tests' not in p.parts and '__pycache__' not in p.parts)
    paths.append(ROOT / '02-tools/scripts/control_gateway.py')
    for path in paths:
        rel = relative(path)
        for node in ast.walk(sources.tree(rel)):
            if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)) and node.args:
                kind = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
                first = node.args[0]
                if kind in ('Reply', 'ReadSpec') and isinstance(first, ast.Constant) and isinstance(first.value, str):
                    replies[first.value].append(sources.site(rel, node.lineno))
                    categories[kind].add(first.value)
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Attribute) and t.attr == 'name' for t in targets):
                    replies[node.value.value].append(sources.site(rel, node.lineno))
                    categories['name_assignment'].add(node.value.value)
    names = {e['name'] for e in schema['schemas']}
    return {'distinct_reply_names': len(replies), 'validated_schema_entries': len(names_seen),
            'category_counts': {k: len(v) for k, v in sorted(categories.items())},
            'names': dict(sorted(replies.items())),
            'missing_from_schema': {k: v for k, v in sorted(replies.items()) if k not in names},
            'scope': '静态 Reply/ReadSpec/.name 字面量；不声称涵盖任意动态字符串'}


def read_log(cutoff):
    path = SERVER / 'logs/server.jsonl'
    raw = path.read_bytes() if path.exists() else b''
    lines = raw.splitlines(keepends=True)
    if lines and not lines[-1].endswith(b'\n'):
        lines.pop()
    if cutoff is not None and cutoff > len(lines):
        raise AuditError(f'Cutoff {cutoff} exceeds {len(lines)} complete log lines')
    lines = lines[:cutoff] if cutoff is not None else lines
    prefix = b''.join(lines)
    events, invalid = [], []
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or 'event' not in row:
                raise ValueError('Missing event')
            row['_line'] = n
            events.append(row)
        except (ValueError, UnicodeDecodeError) as error:
            invalid.append({'line': n, 'error': type(error).__name__})
    return events, {'file': relative(path), 'cutoff_line': len(lines), 'bytes': len(prefix),
                    'sha256': sha(prefix), 'invalid_lines': invalid}


def summarize_log(events, registry, limits):
    active, previous, segments = Counter(), {}, Counter()
    failures, unsupported, intervals, excluded = [], defaultdict(list), [], 0
    handled, potential = set(registry['handlers']), set(registry['potential_handlers'])
    for row in events:
        kind, role, uid = row['event'], row.get('role'), row.get('uid')
        if kind == 'started':
            active.clear()
            previous.clear()
        elif kind == 'connect':
            active[role] += 1
            segments[role] += 1
            previous.pop(role, None)
        elif kind == 'disconnect':
            active[role] = max(0, active[role] - 1)
            previous.pop(role, None)
        elif kind == 'request':
            old = previous.get(role)
            if old and active[role] == 1 and old.get('uid') == uid:
                delta = row.get('time_ms', 0) - old.get('time_ms', 0)
                if delta >= 0:
                    intervals.append({'milliseconds': delta, 'before_line': old['_line'], 'after_line': row['_line']})
            elif old:
                excluded += 1
            previous[role] = row
        elif kind in FAILURE_KINDS:
            old = previous.get(role)
            if old and uid is not None and old.get('uid') not in (None, uid):
                old = None
            association = 'nearest_request_inferred' if old and active[role] == 1 else 'ambiguous_or_missing'
            # request_failed and unsupported_handler carry the exact request name;
            # the older connection_rejected/handler_failure events only have a class.
            name = row.get('name') if kind in ('request_failed', 'unsupported_handler') else None
            if name:
                association = 'event_name_exact'
            if name and (kind == 'unsupported_handler' or row.get('reason') == 'unsupported_request'):
                unsupported[name].append(row['_line'])
            if name is None:
                name = old.get('name') if old else None
            if kind in ('unsupported_handler', 'request_failed'):
                status = ('historical_handler_now_registered' if name in handled else
                          'implementation_not_loaded' if name in potential else 'currently_unhandled')
            else:
                status = 'historical_fix_unproven'
            # Never use an unrelated previous opcode as "exact" for a named event.
            opcode = row.get('opcode')
            if opcode is None and old and old.get('name') == name:
                opcode = old.get('opcode')
            failures.append({'event': kind, 'line': row['_line'], 'time_ms': row.get('time_ms'),
                             'role': role, 'uid': uid, 'error': row.get('error'), 'name': name, 'opcode': opcode,
                             'reason': row.get('reason'), 'disposition': row.get('disposition'),
                             'preceding_request_line': old['_line'] if old else None,
                             'attribution': association, 'segment': segments[role], 'current_status': status})
    classifications = {}
    for kind in FAILURE_KINDS:
        classifications[kind] = dict(sorted(Counter(r['name'] if kind in ('unsupported_handler', 'request_failed') else r['error']
                                                   for r in failures if r['event'] == kind).items()))
    return {'total_events': len(events), 'event_counts': dict(sorted(Counter(r['event'] for r in events).items())),
            'classifications': classifications, 'failure_records': failures,
            'unsupported_handlers': {k: {'count': len(v), 'lines': v} for k, v in sorted(unsupported.items())},
            'request_intervals': {'samples': len(intervals), 'excluded_ambiguous': excluded,
                                 'maximum': max(intervals, key=lambda r: r['milliseconds'], default=None),
                                 'over_idle_timeout': sum(r['milliseconds'] > limits['idle_timeout'] * 1000 for r in intervals)},
            'limitations': ['旧窗口无连接 ID；request_failed 与 unsupported_handler 的 name 是事件精确证据，其余关联为邻近推断。',
                            '统计不包含末尾空闲；旧窗口的类名不能区分 idle/assembly 超时，也不能证明门槛拒绝。']}


def wal_checksum(raw, endian, seed=(0, 0)):
    first, second = seed
    for left, right in struct.iter_unpack(endian + 'II', raw):
        first = (first + left + second) & 0xffffffff
        second = (second + right + first) & 0xffffffff
    return first, second


def committed_image(database, wal):
    """Apply only validated committed WAL frames to an in-memory image.

    Format/checksums: https://www.sqlite.org/fileformat2.html#walformat
    Raw file reads never open or modify the live -shm. Reused/stale or incomplete
    WAL tails terminate the valid prefix; only its last commit is visible.
    """
    if len(database) < 100 or database[:16] != b'SQLite format 3\x00':
        raise AuditError('Invalid SQLite header')
    page_size = int.from_bytes(database[16:18], 'big')
    page_size = 65536 if page_size == 1 else page_size
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1) or len(database) % page_size:
        raise AuditError('Invalid SQLite page size/image length')
    image, pending, committed, valid, tail = bytearray(database), {}, 0, 0, None
    if wal:
        if len(wal) < 32:
            raise AuditError('Incomplete WAL header; retry after writer completes')
        magic, version, size, _, salt1, salt2, sum1, sum2 = struct.unpack('>8I', wal[:32])
        if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or size != page_size:
            raise AuditError('Unsupported WAL header or mismatched page size')
        endian = '<' if magic == 0x377f0682 else '>'
        checksum = wal_checksum(wal[:24], endian)
        if checksum != (sum1, sum2):
            raise AuditError('Invalid WAL header checksum')
        frame_size = 24 + page_size
        for offset in range(32, len(wal), frame_size):
            frame = wal[offset:offset + frame_size]
            if len(frame) < frame_size:
                tail = 'incomplete_frame'
                break
            page, db_pages, s1, s2, c1, c2 = struct.unpack('>6I', frame[:24])
            if (s1, s2) != (salt1, salt2):
                tail = 'stale_salt'
                break
            checksum = wal_checksum(frame[:8] + frame[24:], endian, checksum)
            if checksum != (c1, c2):
                tail = 'invalid_checksum'
                break
            if not page or page > 0xfffffffe:
                raise AuditError('Invalid WAL page number')
            pending[page] = frame[24:]
            valid += 1
            if db_pages:
                # Cap allocation to observed source size plus validated pages.
                if db_pages * page_size > len(database) + len(wal):
                    raise AuditError('WAL commit size exceeds captured source bounds')
                wanted = db_pages * page_size
                if len(image) < wanted:
                    image.extend(b'\x00' * (wanted - len(image)))
                del image[wanted:]
                for number, content in pending.items():
                    if number <= db_pages:
                        image[(number - 1) * page_size:number * page_size] = content
                pending.clear()
                committed = valid
    # deserialize cannot read a WAL image without a filesystem; change ONLY
    # the memory copy's journal flags, not any persisted source bytes.
    image[18:20] = b'\x01\x01'
    return bytes(image), {'page_size': page_size, 'valid_frames': valid,
                         'last_committed_frame': committed, 'tail_status': tail,
                         'uncommitted_pages_ignored': len(pending), 'shm_opened': False}


def load_states(path=None):
    path = path or SERVER / 'data/players.sqlite3'
    wal_path = path.with_name(path.name + '-wal')
    journal = path.with_name(path.name + '-journal')
    if journal.exists() and journal.stat().st_size:
        raise AuditError('Rollback journal present; this reader supports WAL/quiescent databases only')
    def capture():
        return path.read_bytes(), wal_path.read_bytes() if wal_path.exists() else b''
    # Fail closed if a concurrent write/checkpoint prevents a stable capture.
    for _ in range(3):
        first, second = capture(), capture()
        if first == second:
            break
    else:
        raise AuditError('Database/WAL changed during snapshot; rerun')
    raw, wal = second
    image, metadata = committed_image(raw, wal)
    # mode=ro + immutable never touches WAL/SHM. Deserialize immediately detaches
    # the file handle, after which all SQL runs on the reconstructed RAM image.
    connection = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        if not hasattr(connection, 'deserialize'):
            raise AuditError('SQLite deserialize API required (Python >=3.11); no writable fallback')
        connection.deserialize(image)
        connection.execute('PRAGMA query_only=ON')
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise AuditError('Captured database image failed quick_check')
        rows = connection.execute('SELECT uid,revision,state_json FROM accounts ORDER BY uid').fetchall()
    finally:
        connection.close()
    players = [{'uid': uid, 'revision': revision, 'state': json.loads(raw)} for uid, revision, raw in rows]
    return players, {'file': relative(path) if path.is_relative_to(ROOT) else path.name,
                     'mode': 'ro', 'immutable': True, 'query_only': True, 'wal_snapshot': metadata,
                     'database_sha256': sha(raw), 'wal_sha256': sha(wal),
                     'snapshot_sha256': sha(canonical(players).encode('utf-8')),
                     'players': [{'uid': p['uid'], 'revision': p['revision'],
                                  'state_sha256': sha(canonical(p['state']).encode('utf-8'))} for p in players]}


def gate_audit(players, sources, at_seconds):
    from access_policy import OPERATION_GATES
    from handlers.initialization import READS, config_table, feature_open
    conditions = {**config_table('cfgCfgOpenCondition.lua'), **config_table('cfgCfgOpenConditionMore.lua')}
    rules = config_table('cfgCfgOpenRules.lua')
    for filename in ('cfgCfgOpenCondition.lua', 'cfgCfgOpenConditionMore.lua', 'cfgCfgOpenRules.lua'):
        sources.read('03-unpack/lua/device-luascripts/' + filename)
    known = {name: spec.gate for name, spec in READS.items() if spec.gate}
    known.update(OPERATION_GATES)
    evaluated = {}
    for name, gate in sorted(known.items()):
        opens = {}
        for player in players:
            state = deepcopy(player['state'])
            state.setdefault('offline_clock', at_seconds)
            opens[str(player['uid'])] = feature_open(state, gate)
        evaluated[name] = {'gate': gate, 'open_per_player': opens}
    rows = [{'handler': name, **evaluated[name], 'gate_in_config': gate in conditions,
             'conditions': [rules.get(k, {'missing_rule': k}) for k in conditions.get(gate, {}).get('conditions', [])],
             'ui_offer_proven': False, 'needs_device_confirmation': True}
            for name, gate in sorted(OPERATION_GATES.items())]
    return {'operation_count': len(rows), 'distinct_gate_count': len(set(OPERATION_GATES.values())),
            'unknown_gate_names': [r for r in rows if not r['gate_in_config']], 'gates': rows,
            'known_request_gates': evaluated, 'evaluation_time_seconds': at_seconds}


def request_audit(schema, registry, log, gates, sources):
    handled, potential = set(registry['handlers']), set(registry['potential_handlers'])
    rows, checked = [], 0
    for entry in schema['schemas']:
        if entry.get('client_send_literal') is not True:
            continue
        send_sites = []
        for site in entry.get('send_sources', []):
            value = sources.site(site['file'], site['line'])
            if entry['name'].split(':')[-1] not in value['text']:
                raise AuditError(f"Send site does not mention {entry['name']}: {site}")
            send_sites.append(value)
            checked += 1
        if not send_sites:
            raise AuditError('Sendable message lacks a source: ' + entry['name'])
        for key in ('source', 'opcode_source'):
            if key in entry:
                sources.site(entry[key]['file'], entry[key]['line'])
        for sample in entry.get('observed_samples', []):
            for key in ('raw', 'outer', 'decoded'):
                if sample.get(key):
                    path = (ROOT / sample[key]).resolve()
                    if not path.is_relative_to(ROOT) or not path.is_file():
                        raise AuditError('Missing capture evidence: ' + sample[key])
                    sources.raw[relative(path)] = path.read_bytes()
        if entry['name'] in handled:
            continue
        past = log['unsupported_handlers'].get(entry['name'], {'count': 0, 'lines': []})
        gate = gates['known_request_gates'].get(entry['name'])
        closed = bool(gate and gate['open_per_player'] and not any(gate['open_per_player'].values()))
        reachability = 'log_proven' if past['count'] else 'known_gate_locked' if closed else 'code_path'
        rows.append({'name': entry['name'], 'opcode': entry['opcode'], 'domain': entry['name'].split(':')[0],
                     'send_sources': send_sites, 'send_site_count': len(send_sites),
                     'observed_in_official_capture': bool(entry.get('observed_samples')),
                     'observed_samples': entry.get('observed_samples', []),
                     'historical_disconnect_count': past['count'], 'log_lines': past['lines'],
                     'reachability': reachability, 'known_gate': gate, 'needs_device_confirmation': True,
                     'implementation_not_loaded': entry['name'] in potential})
    total = sum(e.get('client_send_literal') is True for e in schema['schemas'])
    return {'client_sendable_count': total, 'handled_count': total - len(rows), 'unhandled_count': len(rows),
            'unhandled_count_if_all_modules_loaded': sum(e.get('client_send_literal') is True and e['name'] not in potential
                                                        for e in schema['schemas']),
            'unhandled_by_domain': dict(sorted(Counter(r['domain'] for r in rows).items())),
            'unhandled': sorted(rows, key=lambda r: (r['domain'], r['name'])), 'validated_send_sites': checked}


class MemoryStore:
    """Run read/legacy repair handlers on deep-copied state; no disk commits."""

    def __init__(self, player):
        self.uid, self.state = player['uid'], deepcopy(player['state'])
        self.repairs = 0

    def get_player(self, uid):
        from database import PlayerTxn
        if uid != self.uid:
            raise AuditError('Unexpected UID')
        state = deepcopy(self.state)
        PlayerTxn.sync_currency_items(state)
        return state

    @contextmanager
    def transaction(self, uid):
        from database import PlayerTxn
        tx = PlayerTxn(self.get_player(uid))
        yield tx
        self.state = deepcopy(tx.state)
        self.repairs += 1


def measure(codec, name, fields):
    from protocol_codec import CodecError
    total = len(codec.encode_struct(name, fields)) + 4
    try:
        raw = codec.encode_frame(name, fields)
        if total != len(raw):
            raise AuditError('Codec size mismatch')
        return {'bytes': total, 'encodable': True, 'error': None}
    except CodecError as error:
        if str(error) != 'Frame exceeds configured size limit':
            raise AuditError(f'{name}: unexpected encoding failure: {error}') from error
        return {'bytes': total, 'encodable': False, 'error': str(error)}


def threshold(codec, name, fields, key):
    samples = fields[key]
    if not samples:
        return {'status': 'no_sample', 'sample_rows': 0}
    def expanded(count):
        result = deepcopy(fields)
        result[key] = [deepcopy(samples[n % len(samples)]) for n in range(count)]
        return result
    def fits(count):
        return measure(codec, name, expanded(count))['encodable']
    low, high = 0, max(1, len(samples))
    while fits(high):
        low, high = high, min(65535, high * 2)
        if high == low:
            return {'status': 'no_overflow_before_list_limit', 'sample_rows': len(samples)}
    while high - low > 1:
        mid = (low + high) // 2
        if fits(mid):
            low = mid
        else:
            high = mid
    before, after = measure(codec, name, expanded(low)), measure(codec, name, expanded(high))
    if not before['encodable'] or after['encodable'] or high != low + 1:
        raise AuditError('Invalid encoded-size boundary')
    empty = measure(codec, name, expanded(0))['bytes']
    payload = (measure(codec, name, fields)['bytes'] - empty) / len(samples)
    return {'status': 'verified', 'model': 'cycle_current_rows_preserve_fields', 'sample_rows': len(samples),
            'empty_bytes': empty, 'average_payload_bytes_per_row': round(payload, 4),
            'linear_max_rows_estimate': int((codec.config.max_frame_size - empty) // payload) if payload else None,
            'largest_encodable_rows': low, 'first_oversized_rows': high,
            'last_valid_bytes': before['bytes'], 'first_invalid_bytes': after['bytes'],
            'safe_additional_rows': max(0, low - len(samples)),
            'additional_rows_until_failure': max(0, high - len(samples)),
            'warning': '循环真实样本仅测大小，重复 ID 不构成有效存档；强化/技能/字段变化会改变边界。'}


def frame_audit(core, codec, players):
    list_keys = {'PlayerProto:CardAdd': 'cards', 'PlayerProto:AddCardRole': 'roles',
                 'PlayerProto:UpdateCardRole': 'roles', 'PlayerProto:ItemBag': 'item',
                 'EquipProto:GetEquipsRet': 'equips', 'PlayerProto:DuplicateData': 'mainLine'}
    async def read_routes(player):
        store = MemoryStore(player)
        ctx = core.Context(SimpleNamespace(store=store, codec=codec), 'game', uid=player['uid'], logged_in=True)
        results = [('initial_pushes', core.initial_pushes(store.get_player(player['uid']), codec))]
        for name in ('ClientProto:InitFinish', 'EquipProto:GetEquips', 'PlayerProto:CardsData', 'PlayerProto:GetCardRole'):
            results.append((name, await core.HANDLERS[name](ctx, {})))
        return results, store.repairs
    output = []
    for player in players:
        # Current read handlers are async signatures around synchronous memory
        # work. Drive them without an event loop: Windows asyncio creates a
        # loopback self-pipe, contrary to this tool's no-listener constraint.
        # A future handler that actually suspends must be audited separately.
        operation = read_routes(player)
        try:
            try:
                operation.send(None)
            except StopIteration as finished:
                routes, repairs = finished.value
            else:
                raise AuditError('Read handler suspended; offline audit cannot perform asynchronous I/O')
        finally:
            operation.close()
        rows, cache = [], {}
        for route, replies in routes:
            for index, reply in enumerate(replies):
                row = {'route': route, 'reply': reply.name, 'index': index, **measure(codec, reply.name, reply.fields)}
                key = list_keys.get(reply.name)
                if key and isinstance(reply.fields.get(key), list):
                    row['rows'] = len(reply.fields[key])
                    signature = reply.name, canonical(reply.fields)
                    if signature not in cache:
                        cache[signature] = threshold(codec, reply.name, reply.fields, key)
                    row['threshold'] = cache[signature]
                rows.append(row)
        state = player['state']
        output.append({'uid': player['uid'], 'revision': player['revision'], 'cards': len(state.get('cards', [])),
                       'card_roles': len(state.get('card_roles', [])), 'equips': len(state.get('equips', [])),
                       'max_card_size': state.get('max_card_size'), 'max_equip_size': state.get('max_equip_size'),
                       'in_memory_role_repairs': repairs, 'replies': rows})
    return {'limit_bytes': codec.config.max_frame_size, 'header_bytes': 4, 'players': output,
            'method': '真实读取 handler + 内存 Store + encode_struct/encode_frame；循环样本倍增、二分并验证边界两侧。'}


def maintainability_audit(sources):
    paths = sorted(p for p in SERVER.rglob('*.py') if 'tests' not in p.parts and '__pycache__' not in p.parts)
    names = {relative(p): '.'.join(p.relative_to(SERVER).with_suffix('').parts) for p in paths}
    available = set(names.values())
    files, graph, hotspots = [], {}, defaultdict(list)
    for path in paths:
        rel = relative(path)
        text, tree = sources.read(rel), sources.tree(rel)
        imports, counts = [], Counter()
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.ImportFrom):
                targets = [node.module + '.' + a.name for a in node.names] if node.module == 'handlers' else [node.module]
            elif isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            for target in targets:
                if target in available:
                    site = sources.site(rel, node.lineno)
                    imports.append({'module': target, 'source': site})
                    if target.startswith('handlers.'):
                        hotspots['handler_dependencies'].append(site)
                        counts['imports_handler'] += 1
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr in ('transaction', 'get_player'):
                    counts['storage_calls'] += 1
                    hotspots['storage_calls'].append(sources.site(rel, node.lineno))
                if isinstance(node.func, ast.Name) and node.func.id == 'register':
                    counts['register_calls'] += 1
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'sys' and node.attr == 'modules':
                counts['sys_modules_probes'] += 1
                hotspots['sys_modules'].append(sources.site(rel, node.lineno))
        graph[names[rel]] = sorted({r['module'] for r in imports})
        functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        largest = max(functions, key=lambda n: n.end_lineno - n.lineno, default=None)
        files.append({'file': rel, 'module': names[rel], 'lines': len(text.splitlines()), 'functions': len(functions),
                      'metrics': dict(sorted(counts.items())), 'imports': imports,
                      'largest_function': {'name': largest.name, 'lines': largest.end_lineno - largest.lineno + 1,
                                           'source': sources.site(rel, largest.lineno)} if largest else None})
    # Tarjan SCC. Include delayed imports as source coupling, not runtime failures.
    serial, stack, on_stack, ids, low, cycles = 0, [], set(), {}, {}, []
    def visit(node):
        nonlocal serial
        ids[node] = low[node] = serial
        serial += 1
        stack.append(node)
        on_stack.add(node)
        for target in graph[node]:
            if target not in ids:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], ids[target])
        if ids[node] == low[node]:
            group = []
            while True:
                target = stack.pop()
                on_stack.remove(target)
                group.append(target)
                if target == node:
                    break
            if len(group) > 1:
                cycles.append(sorted(group))
    for node in sorted(graph):
        if node not in ids:
            visit(node)
    tests = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith('test_')
                for p in sorted((SERVER / 'tests').glob('test_*.py')) for n in ast.walk(sources.tree(relative(p))))
    return {'source_file_count': len(files), 'source_lines': sum(r['lines'] for r in files),
            'static_test_method_count': tests, 'files': sorted(files, key=lambda r: (-r['lines'], r['file'])),
            'dependency_graph': graph, 'dependency_cycles': sorted(cycles), 'hotspots': dict(sorted(hotspots.items())),
            'metrics': {'large_files_400_lines': sum(r['lines'] >= 400 for r in files),
                        'storage_calls': len(hotspots['storage_calls']),
                        'handler_import_sites': len(hotspots['handler_dependencies']),
                        'sys_modules_probes': len(hotspots['sys_modules'])}}


ROADMAP = [
    {'phase': 1, 'title': '行为基线与契约',
     'changes': '集中临时 Store、codec、seed、时钟/随机源夹具；固定实际启动注册表、回包/schema/gate、初始化順序及错误行为。先建立各域 wire/存档特征测试。',
     'interfaces': '暂保留 register/Reply/Context/Store；测试单独和全量运行均采用显式统一 bootstrap。',
     'acceptance': '现有全部测试通过；登录、心跳、命名、抽卡/养成、战斗、商店、任务、邮件、后台与重开存档均有行为基线。单独/全量测试注册表一致。',
     'rollback': '新增测试与夹具可独立撤销。'},
    {'phase': 2, 'title': '显式组装与注册表',
     'changes': '建立 bootstrap、HandlerRegistry/HandlerSpec 和唯一启用模块清单；handler 导出声明后显式注册，传递导入业务工具不再启用协议。启动器与审计读取同一清单。equipment 默认禁用，另经业务验收启用。',
     'interfaces': 'HandlerSpec(name, handler, gate, owner, reply_names)；Registry.register/resolve/snapshot；bootstrap.build_app(config, store, codec, clock, rng)。server_core 过渡再导出旧入口。',
     'acceptance': '实际 handler 能力及所有权保持；重复协议/缺 schema/缺 gate 启动失败；两个应用实例互不污染；导入顺序不改变能力。',
     'rollback': '旧 bootstrap/兼容导出可切回，无存档迁移。'},
    {'phase': 3, 'title': '传输、异常策略与日志',
     'changes': '分离 transport/session、dispatcher、error_policy、event_sink。进入请求后保存 connection_id/request_id/opcode/name。业务拒绝/未实现请求回明确 Tips；损坏/未知帧、无效认证关闭；锁繁忙拒绝可重试；程序缺陷先回滚，记录堆栈，状态不确定时关闭。',
     'interfaces': 'RequestContext；ProtocolFatal/BusinessRejected/ResourceBusy/UnexpectedFailure；ErrorDisposition(action, reason, replies)。增量日志字段包括关联 ID、失败阶段、close_reason，禁止密码/ticket。',
     'acceptance': '业务拒绝后同连接心跳成功；无效 ticket/坏帧仍关；并发日志精确关联；连接上限有事件；同步修订关闭策略测试与文档。',
     'rollback': 'error_policy 保留 legacy_close 过渡配置；日志只增字段。'},
    {'phase': 4, 'title': '统一回包与按字节分片',
     'changes': '集中 reply_builder/sender，按真实 codec 大小分片。卡牌、角色、背包、装备、主线、商店、任务统一构建；显式声明各协议结束标记。事务提交前校验回包；无 finish 字段须先验证客户端合并能力；禁止静默截断。',
     'interfaces': 'ReplySpec(name, collection_field, finish_field, metadata_builder)；build_replies(state, purpose)；sender.send(replies)。单行超限是明确编码错误。',
     'acceptance': '每帧 <=32767；跨阈值存档登录/读取/重连可用；集合与存档一致且结束标记正确；单行超限可定位、资产不部分提交。',
     'rollback': '逐协议迁移，可切回对应 builder；wire/schema 与存档不变。'},
    {'phase': 5, 'title': '存储端口与工作单元',
     'changes': 'PlayerRepository/UnitOfWork 封装 SQLite，显式 busy_timeout 默认5000ms、统一 ResourceBusy。完整同步业务操作在专用 worker 执行，SQLite 连接由所属 worker 创建关闭；事件循环不执行 SQL，事务内无 await。后台使用同一存储工厂和应用服务。',
     'interfaces': 'Repository.get(uid)；UnitOfWork.run(uid, operation)；StorageConfig(path, busy_timeout_ms)。业务操作接收 tx/clock/rng/catalog，不持有 socket；禁止跨线程共享 SQLite 连接。',
     'acceptance': '双进程故意持锁实验、幂等重放/回滚；繁忙无资源消耗、无重复奖励；锁等待期间其他连接心跳继续；state_json/UID/ticket/revision 和重开兼容。',
     'rollback': '保留同步存储适配器，可整块切回；不改变表结构。'},
    {'phase': 6, 'title': '全业务域与生成工具分层',
     'changes': '拆账号/玩家、角色/养成/装备、背包/奖励、抽卡、进度/战斗、任务/签到、商店/外观、邮件/礼物应用服务。catalog/clock/validation/rewards/task_events 从 handlers 移出；handler 仅输入与协议映射。admin/panel/generate/data-build 复用纯 catalog/应用服务。',
     'interfaces': 'domain operation(tx,input)->result；应用服务掌管工作单元；mapper 转 Reply。CatalogLoader 只读/显式路径/缓存；生成器 parse/build/render 分离，仅 CLI 入口写输出。',
     'acceptance': 'service/admin/catalog 无反向 handler 依赖；消除当前依赖环/sys.modules 业务探测；各域真实 codec、回滚、重放、重开测试通过；缺失玩法保持明确缺口。',
     'rollback': '先共享奖励/任务，再玩家/养成、商店/邮件、抽卡/战斗；各域兼容转发入口保留到验收后，逐域独立撤销。'},
]


def findings_audit(report):
    e = report['evidence']
    def row(code, level, title, trigger, keys):
        return {'id': code, 'severity': level, 'title': title, 'trigger': trigger,
                'status': 'current_code', 'evidence': [e[k] for k in keys]}
    findings = [
        row('P0-1', 'P0', '未注册请求回 Tips 并保持连接（2026-10-05 修复）', 'schema 已知但无 handler 的请求回 SystemProto:Tips；只有帧/opcode 损坏与认证失败才关闭。',
            ['unsupported', 'tips_continue', 'error_policy', 'finally_close']),
        row('P0-2', 'P0', '全量快照回包按字节分片（2026-10-05 修复）', '初始推送/卡牌/角色/背包按真实编码字节分片；装备等其他单帧路径仍是余留风险。',
            ['frame_limit', 'chunk_bytes', 'encode_reply', 'frame_encode_failed', 'finally_close']),
        row('P1-3', 'P1', '业务与程序异常回 Tips（2026-10-05 更新）', 'StorageError 与未预期异常记 request_failed 并回 Tips；仅认证失败或已写出部分帧时关闭。',
            ['rejected', 'handler_failure', 'error_policy', 'birthday', 'commander', 'task_id']),
        row('P1-4', 'P1', '功能门槛拒绝回 Tips（2026-10-05 更新）', 'gate 关闭抛 StorageError，现由 error_policy 转成 Tips，不再关闭连接。',
            ['gate_wrapper', 'gate', 'rejected', 'tips_continue']),
        row('P1-5', 'P1', '协议解析拒绝关闭连接', '未知 opcode、长度/flag/结构/残包异常发生在 dispatch 前，按设计关闭。',
            ['unknown_opcode', 'outer_length', 'outer_flag', 'coalesced', 'pending', 'malformed', 'partial_eof', 'decode']),
        row('P2-6a', 'P2', '空闲/组包超时', '超时仍关闭；日志现在区分 idle_timeout 与 assembly_timeout，历史行无法区分。',
            ['limits', 'partial_timeout', 'idle_read']),
        row('P2-6b', 'P2', '连接上限', '达到 max_connections 后关闭，现在记录 connection_limit_rejected 事件。',
            ['connection_limit']),
        row('P2-7', 'P2', 'SQLite 写竞争与同步阻塞', '两进程同库；已显式 busy_timeout=1000ms 且 locked 转成可重试 StorageBusy；同步 SQL 仍在事件循环内执行。',
            ['database_connect', 'busy_timeout', 'storage_busy', 'transaction', 'control_store', 'server_process', 'control_process']),
        row('P2-8', 'P2', '失败事件缺少关联与堆栈（2026-10-05 更新）', 'request_failed 现在带 connection_id/request_id/opcode/name/stage/close_reason，未预期异常带 traceback；历史事件仍缺这些字段。',
            ['rejected', 'handler_failure', 'connection_limit']),
    ]
    signatures = [f"P0:unhandled:{r['opcode']}:{r['name']}" for r in report['requests']['unhandled']]
    signatures += [f"P0:oversized:{p['uid']}:{r['route']}:{r['reply']}" for p in report['frames']['players']
                   for r in p['replies'] if not r['encodable']]
    signatures += ['P1:missing_reply:' + name for name in report['replies']['missing_from_schema']]
    signatures += ['P1:unknown_gate:' + r['handler'] + ':' + r['gate'] for r in report['gates']['unknown_gate_names']]
    signatures += [f"P1:closed_gate:{uid}:{r['handler']}:{r['gate']}" for r in report['gates']['gates']
                   if r['handler'] in report['registry']['handlers']
                   for uid, opened in r['open_per_player'].items() if not opened]
    signatures += ['P1:import_failure:' + r['module'] + ':' + r['error'] for r in report['registry']['import_failures']]
    signatures += ['P1:dormant_import_failure:' + r['module'] for r in report['registry']['unloaded_modules'] if r['import_error']]
    signatures += [f"{r['severity']}:path:{r['id']}" for r in findings if r['severity'] in ('P0', 'P1')]
    signatures += [f"P1:historical_exception:{r['event']}:{r['error']}:{r['name'] or '?'}"
                   for r in report['log']['failure_records'] if r['event'] != 'unsupported_handler']
    return findings, sorted(set(signatures))


def build_report(cutoff):
    sources = Sources()
    modules = launcher_modules(sources)
    # Snapshot all executable server inputs before importing. Do not fingerprint
    # output files or the growing tail after the selected log cutoff.
    # 'tests' is excluded for the same reason as in the module walks above: it is
    # not a server input, and its scratch subdirectories hold transient copies of
    # data tables that can disappear mid-walk and break an otherwise clean run.
    for path in sorted(SERVER.rglob('*')):
        if path.is_file() and path.suffix in ('.py', '.json') and not {'run', 'backups', '__pycache__', 'tests'} & set(path.parts):
            sources.read(relative(path))
    for path in sorted((ROOT / '03-unpack/lua/device-luascripts').glob('*.lua')):
        sources.read(relative(path))
    # Executable inputs only: the repository keeps one README and no per-folder
    # notes, so documentation must never be a required audit input.
    for path in ('02-tools/scripts/audit_disconnect_risks.py', '02-tools/scripts/protocol_codec.py',
                 '02-tools/scripts/config_codec.py', '02-tools/scripts/control_gateway.py',
                 '06-client/patch_offline_lua.py'):
        if (ROOT / path).is_file():
            sources.read(path)
    schema = json.loads(sources.read('05-protocol/endpoints.json'))
    events, log_input = read_log(cutoff)
    at_seconds = max((r.get('time_ms', 0) for r in events), default=0) // 1000
    core, registry = registry_audit(modules, sources)
    defaults = inspect.signature(core.LocalServer.__init__).parameters
    limits = {name: defaults[name].default for name in ('idle_timeout', 'assembly_timeout', 'max_connections')}
    replies = source_audit(sources, schema)
    log = summarize_log(events, registry, limits)
    players, db_input = load_states()
    gates = gate_audit(players, sources, at_seconds)
    requests = request_audit(schema, registry, log, gates, sources)
    from protocol_codec import IVProtoCodec, WireConfig
    codec = IVProtoCodec(schema, WireConfig('little'))
    frames = frame_audit(core, codec, players)
    maintainability = maintainability_audit(sources)
    report = {'schema_version': VERSION, 'tool': 'audit_disconnect_risks.py',
              'inputs': {'log': log_input, 'database': db_input}, 'registry': registry,
              'requests': requests, 'replies': replies, 'gates': gates, 'frames': frames, 'log': log,
              'connection_limits': limits, 'evidence': dict(sorted(sources.evidence.items())),
              'maintainability': maintainability, 'roadmap': ROADMAP,
              'assumptions': [
                  '以 start_local.ps1 默认模块为准；自定义 -Handlers 与现有进程可不同。',
                  '历史 handler 现已注册不证明旧异常全已修复；历史因果只能按日志证据强度说明。',
                  '固定日志窗口不冻结存档与代码；活跃存档改变会导致结论变化，须检查输入指纹。',
                  '所有 handler 读/修复只在内存中执行，无网络或服务/客户端启动。',
                  '静态检测涵盖列明语法和锚点，不构成其他缺陷不存在的证明。',
                  '无 .git 时按受保护目录哈希验收；三个现有交付物原地修订。',
              ]}
    report['findings'], report['risk_signatures'] = findings_audit(report)
    report['inputs']['files'] = sources.fingerprint()
    sources.verify_unchanged()
    report['self_checks'] = {
        'source_anchors_unique': True, 'source_locations_valid': True,
        'validated_schema_entries': replies['validated_schema_entries'],
        'validated_send_sites': requests['validated_send_sites'],
        'registry_restored_after_potential_probe': sorted(core.HANDLERS) == registry['handlers'],
        'frame_boundaries_verified': sum(r.get('threshold', {}).get('status') == 'verified'
                                        for p in frames['players'] for r in p['replies']),
        'log_failure_count_matches': len(log['failure_records']) == sum(log['event_counts'].get(k, 0)
                                                                      for k in log['classifications']),
        'invalid_log_lines': len(log_input['invalid_lines']), 'disk_database_open_mode': 'ro',
        'shm_opened': False, 'is_git_workspace': (ROOT / '.git').exists(),
        'bytecode_writes_disabled': sys.dont_write_bytecode, 'source_inputs_unchanged_during_run': True,
    }
    return report


REACHABILITY = {'log_proven': '已实测触发', 'code_path': '静态代码路径可达', 'known_gate_locked': '已知门槛未解锁'}
STATUSES = {'historical_handler_now_registered': '仅历史证据：现已注册',
            'implementation_not_loaded': '实现存在但启动未加载',
            'currently_unhandled': '当前仍未处理', 'historical_fix_unproven': '历史异常，是否修复不明'}


def refs(values):
    return '、'.join(TICK + f"{v['file']}:{v['line']}" + TICK for v in values)


def render_markdown(report):
    lines = []
    def add(text=''):
        lines.append(text)
    def table(headers, rows):
        add('| ' + ' | '.join(headers) + ' |')
        add('| ' + ' | '.join('---' for _ in headers) + ' |')
        for row in rows:
            add('| ' + ' | '.join(str(v).replace('|', r'\|').replace('\n', ' ') for v in row) + ' |')
        add()
    e = report['evidence']
    def ref(*keys):
        return refs([e[k] for k in keys])
    registry, requests, log = report['registry'], report['requests'], report['log']
    add('# CrossCore 掉线风险与服务端可维护性审计')
    add()
    add('报告和 JSON 由同一份分析结果自动生成，数值可按输入快照复算。服务端重构是后续蓝图。')
    add()
    inp = report['inputs']['log']
    add(f"日志窗口：{TICK}{inp['file']}{TICK} 第 1–{inp['cutoff_line']} 行，{inp['bytes']} 字节，SHA-256 {TICK}{inp['sha256']}{TICK}。")
    add(f"存档语义快照 SHA-256：{TICK}{report['inputs']['database']['snapshot_sha256']}{TICK}；所有源码与配置指纹见 JSON inputs.files。")
    add()
    add('## 1. 结论、风险与排期')
    add()
    triggered = [r for r in requests['unhandled'] if r['historical_disconnect_count']]
    add(f"启动默认 {len(registry['launcher_modules'])} 模块，注册 {registry['handler_count']} handler；{requests['client_sendable_count']} 条可发送请求中，"
        f"{requests['handled_count']} 已处理，{requests['unhandled_count']} 未处理。handler 总数含非 client_send_literal 入口，不能直接相减。")
    add(f"最有直接证据的当前掉线源是仍未注册且历史触发过的 {len(triggered)} 条请求："
        + '、'.join(TICK + r['name'] + TICK for r in triggered) + '。当前 UI 可达性仍需实机核对。')
    add()
    table(['风险', '触发条件与用户症状', '证据', '建议'], [
        (r['id'] + ' ' + r['title'], r['trigger'], refs(r['evidence']),
         {'P0-1': '明确未实现请求策略', 'P0-2': '按真实编码字节分片', 'P1-4': '拒绝回 Tips',
          'P2-7': '短事务、忙等待、存储隔离'}.get(r['id'], '见重构阶段验收')) for r in report['findings']])
    add('建议顺序：行为基线 → 全量推送分片 → 明确异常/未实现/门槛策略 → 关联日志与分类计数 → SQLite 等待与循环隔离。')
    add()
    add('## 2. 关闭连接路径与证据边界')
    add()
    add('历史八类触发：未实现请求、超限回包、handler 异常、业务门槛、协议拒绝、超时、连接上限、SQLite 异常。可观测性缺口不另算 close 路径，未加载模块属于未实现请求的子类。')
    add('异常处理最终 close：' + ref('finally_close') + '。正常 EOF/服务停机属于生命周期，不能计为异常掉线。')
    add('2026-10-05 起未实现请求、业务拒绝、门槛拒绝与未预期 handler 缺陷都回 SystemProto:Tips 并保持会话：' + ref('tips_continue', 'error_policy') + '；契约见 ' + ref('unknown_contract') + '，回归见 ' + ref('unknown_test') + '。')
    add('旧日志里 ValueError/TypeError/StorageError 记 connection_rejected，KeyError/StopIteration 记 handler_failure；新日志统一记 request_failed，附 name/opcode/stage/reason 与（未预期异常的）traceback。两类都要按窗口分别读。')
    add()
    add('静态输入风险：非法生日抛 ValueError ' + ref('birthday') + '；存档缺初始角色抛 StopIteration '
        + ref('commander') + '；缺任务 id 抛 KeyError ' + ref('task_id') + '。是否能由当前客户端正常 UI 构造，未实机验证。')
    add()
    add('## 3. 实际注册、潜在能力与全部未实现请求')
    add()
    table(['口径', 'handler 数', '未实现请求数'], [
        ('实际启动及传递导入', registry['handler_count'], requests['unhandled_count']),
        ('假设全部 dormant 模块均导入', registry['handler_count_if_all_modules_loaded'], requests['unhandled_count_if_all_modules_loaded'])])
    add('启动列表证据：' + ref('launcher') + '；动态 READS 通过真实导入计入，不用正则模拟 register。')
    add()
    table(['未加载模块', '请求', '导入错误'], [
        (r['module'], '、'.join(r['handlers']) or '无新增注册', r['import_error'] or '无') for r in registry['unloaded_modules']])
    add('2026-10-05 起未加载或未注册请求进入 dispatch 会回 Tips 并保持连接（不再关闭）；清单仍记录所有发送点、抓包观测和本地历史失败。')
    add()
    table(['域', '未实现数'], requests['unhandled_by_domain'].items())
    add('档位优先级：实测日志 → 明确 gate 关闭 → 静态发送点。官服观测独立列出，不代表本地 UI 可点；没有证据时不从域名猜 gate。')
    add()
    for domain in sorted(requests['unhandled_by_domain']):
        add(f"### {domain}（{requests['unhandled_by_domain'][domain]}）")
        add()
        table(['请求 / opcode', '档位', '证据与待确认'], [
            (TICK + r['name'] + TICK + ' / ' + str(r['opcode']), REACHABILITY[r['reachability']],
             refs(r['send_sources']) + (f"；失败日志行 {r['log_lines']}" if r['log_lines'] else '')
             + ('；官服已观测' if r['observed_in_official_capture'] else '')
             + ('；实现未加载' if r['implementation_not_loaded'] else '') + '；UI 待实机')
            for r in requests['unhandled'] if r['domain'] == domain])
    add('## 4. 回包实测与可验证越界点')
    add()
    add(f"内部帧上限 {report['frames']['limit_bytes']} B，包含 4 B 内部头，外层封装不计。证据：" + ref('frame_limit', 'encode_reply') + '；分片实现见 ' + ref('chunk_bytes') + '。')
    add(report['frames']['method'])
    add('外推循环当前真实行样本，字段和 ID 保留；重复 ID 仅用于大小测量，不是有效业务存档。强化/技能/字段增长会改变边界；无行样本时不外推。')
    add()
    for player in report['frames']['players']:
        add(f"### UID {player['uid']}（revision {player['revision']}）")
        add()
        add(f"{player['cards']} 卡 / 容量 {player['max_card_size']}；{player['card_roles']} 角色；{player['equips']} 芯片 / 容量 {player['max_equip_size']}；"
            f"内存读取发生 {player['in_memory_role_repairs']} 次修复事务。")
        add()
        table(['路径 / 回包', '当前行', '当前字节', '最大可编码行 / 字节', '首个越界行 / 字节', '距失败新增行'], [
            (r['route'] + ' / ' + r['reply'], r.get('rows', '—'), r['bytes'],
             f"{r['threshold']['largest_encodable_rows']} / {r['threshold']['last_valid_bytes']}" if r.get('threshold', {}).get('status') == 'verified' else '—',
             f"{r['threshold']['first_oversized_rows']} / {r['threshold']['first_invalid_bytes']}" if r.get('threshold', {}).get('status') == 'verified' else '—',
             r.get('threshold', {}).get('additional_rows_until_failure', '—')) for r in player['replies']])
        add('当前超限回包 ' + str(sum(not r['encodable'] for r in player['replies'])) + ' 个；潜在边界不等于今天已超限。容量限制或字段变化会影响实际能否触发。')
        add()
    add('分片实现：' + ref('chunk_bytes') + '（全量快照与卡牌/角色/背包）；既有分片范式：' + ref('equip_chunk', 'shop_chunk', 'tasks_chunk') + '；CardAdd 客户端 finish 消费者：'
        + ref('client_finish') + '。其他回包的合并/终止语义必须逐协议验证。')
    add()
    add('## 5. 业务 gate 与 UI 对照待验证')
    add()
    gates = report['gates']
    add(f"受门槛保护的操作 {gates['operation_count']} 条、独立 gate {gates['distinct_gate_count']} 个；缺配置 {len(gates['unknown_gate_names'])} 个。")
    add('按源规则评估；时钟固定为日志窗口末尾时间，存档 offline_clock 优先。服务端闭门槛会拒绝（2026-10-05 起回 Tips 并保持连接），当前 UI 是否仍给按钮没有足够证据。')
    add('拒绝机制：' + ref('gate_wrapper', 'gate', 'unknown_gate') + '；项目已有提示回包：' + ref('mail_tip', 'equip_tip') + '。')
    add()
    table(['操作', 'gate / 原规则', '各 UID 状态', 'UI 对照'], [
        (r['handler'], r['gate'] + ' / ' + canonical(r['conditions']),
         '、'.join(uid + (' 开' if state else ' 闭') for uid, state in r['open_per_player'].items()), '需实机；拒绝回 Tips 并保持连接')
        for r in gates['gates']])
    add('## 6. 历史失败与当前状态')
    add()
    table(['事件', '数量'], log['event_counts'].items())
    table(['失败事件', '请求/异常', '数量'], [(kind, key, count) for kind, values in log['classifications'].items() for key, count in values.items()])
    add('每条失败均保留事件行；request_failed 与 unsupported_handler 的 name 为事件精确证据，旧事件的关联仅同 role/UID 邻近推断，历史窗口没有连接 ID 时不能确证因果。')
    add()
    table(['日志行', '事件/异常', '候选请求 / opcode / 前序行', '强度', '当前判定'], [
        (r['line'], r['event'] + ' / ' + str(r['error'] or '—'),
         f"{r['name'] or '未知'} / {r['opcode']} / {r['preceding_request_line']}", r['attribution'], STATUSES[r['current_status']])
        for r in log['failure_records']])
    add('StorageError 可由认证、业务校验或门槛等抛出；UseItemList 邻近请求不能单独证明当时因 Bag 关闭而失败。当前有 handler 也不证明所有旧异常修复；新日志的 reason/stage/traceback 才能定位当前失败。')
    add()
    add('## 7. 超时、连接上限与 SQLite')
    add()
    limits, intervals = report['connection_limits'], log['request_intervals']
    add(f"idle={limits['idle_timeout']}s、assembly={limits['assembly_timeout']}s、max_connections={limits['max_connections']}。证据：" + ref('limits', 'connection_limit') + '。')
    maximum = intervals['maximum']
    add(f"近似分段相邻请求 {intervals['samples']} 个，排除歧义 {intervals['excluded_ambiguous']} 个；"
        + (f"最大 {maximum['milliseconds']/1000:.3f}s（日志 {maximum['before_line']}→{maximum['after_line']} 行）；" if maximum else '无最大值；')
        + f"超过 idle {intervals['over_idle_timeout']} 个。统计不含末尾空闲，不证明前台/后台不会超时。TimeoutError 类名也不能区分两种超时。")
    add('两进程同库：' + ref('server_process', 'control_process', 'control_store') + '；SQLite 已显式设置 busy_timeout=1000ms：'
        + ref('database_connect', 'busy_timeout') + '。锁等待仍会阻塞同步执行的事件循环，但封顶 1 s 并转成可重试 StorageBusy（' + ref('storage_busy') + '）。')
    add('同步事务：' + ref('transaction') + '；锁繁忙经 begin_immediate/commit 转 StorageBusy，其它 OperationalError 仍走通用异常路径：' + ref('handler_failure') + '。锁竞争为实验证据，历史日志未观测到。')
    add()
    add('## 8. 掉线取证手册与回归校验')
    add()
    for text in [
        '保留掉线时刻的日志窗口、源码指纹、存档 revision 与最后用户操作、前后台状态。',
        'request_failed 的 name/opcode/stage/reason 是精确证据；旧 unsupported_handler.name 对照实际启动注册表，区分当前未实现、未加载和历史现已注册。',
        '旧窗口的 connection_rejected 需结合请求/响应事件判断编码、解码、认证、门槛或业务校验；类名不足以唯一定位。',
        '旧 handler_failure 用同 role/UID 前序 request 提供候选，并发时标歧义；不能把相邻位置写成确定 handler。',
        '无失败事件先核对 EOF/进程关闭/日志缺失；连接上限无日志，不能凭缺日志断言就是它。',
        '固定窗口运行 --check；它只覆盖已实现的检测器，输入指纹漂移需要人工复核。',
    ]:
        add('- ' + text)
    add()
    table(['event', '字段及含义'], [
        ('connect/disconnect', 'role + connection_id；disconnect 含 uid 与 close_reason'),
        ('request/response', 'connection_id/request_id/role/uid/opcode/name；response 另含 bytes=内部帧'),
        ('request_failed（当前）', 'connection_id/request_id/name/opcode/stage/disposition/reason/error；未预期异常附 traceback'),
        ('connection_closed / frame_encode_failed（当前）', 'close_reason 与 error；分片前的编码失败单独记录 reply 名列表'),
        ('unsupported_handler（历史）', 'role/uid/name/action=close_connection'),
        ('connection_rejected/handler_failure（历史）', 'role/uid/error 类名，无堆栈/阶段')])
    add(f"静态回包不同名字 {report['replies']['distinct_reply_names']}，分类 {canonical(report['replies']['category_counts'])}，schema 缺失 {len(report['replies']['missing_from_schema'])}。"
        + report['replies']['scope'] + '；类别可能相交，不相加。')
    add('已有 load_dependencies 校验 handler 名和 seed 初始回包，不覆盖全部业务回包；后续补完整契约。')
    add('客户端只对卡池和活动章节应用独立例外，普通门槛从存档进度重算：' + ref('client_pool', 'client_activity', 'client_normal_gates') + '。不由可发送清单推断 UI 全解锁。')
    add()
    add('## 9. 全服务端可维护性')
    add()
    m = report['maintainability']
    add(f"生产/生成 Python {m['source_file_count']} 文件 / {m['source_lines']} 行；维护指标 {canonical(m['metrics'])}；"
        f"AST 测试方法 {m['static_test_method_count']} 个（实际执行另做交付验收）。大小是维护信号，不单独用来判质量。")
    add()
    table(['模块', '行数', '存储调用 / 注册调用 / 模块探测', '最大函数 / 行数 / 证据'], [
        (r['module'], r['lines'], f"{r['metrics'].get('storage_calls',0)} / {r['metrics'].get('register_calls',0)} / {r['metrics'].get('sys_modules_probes',0)}",
         r['largest_function']['name'] + ' / ' + str(r['largest_function']['lines']) + ' / ' + refs([r['largest_function']['source']]) if r['largest_function'] else '—')
        for r in m['files']])
    add('源码依赖环（含函数内延迟导入；环不等于运行时导入失败）：')
    add()
    for cycle in m['dependency_cycles']:
        add('- ' + '、'.join(TICK + name + TICK for name in cycle))
    add()
    for key, title in [('handler_dependencies', 'handler 反向依赖与跨域导入'), ('sys_modules', '用导入状态决定业务')]:
        add('### ' + title)
        add()
        for site in m['hotspots'][key]:
            add('- ' + refs([site]) + ' — ' + TICK + site['text'].replace(TICK, "'") + TICK)
        add()
    add('核心混合登录/业务与传输；service/admin/生成器反向引用 handler 工具；导入控制注册和推送；事务与回包校验散在业务函数。分层按实际依赖迁移，不以机械拆文件作为验收。')
    add()
    add('## 10. 全服务端六阶段重构蓝图（本轮仅建议）')
    add()
    add('兼容：wire/schema、SQLite state_json/UID/revision、离线无官方回退、已实现业务/幂等回执保留。错误策略属于显式行为改变，更新契约；未实现玩法不回伪成功。')
    add()
    for stage in report['roadmap']:
        add(f"### 阶段 {stage['phase']}：{stage['title']}")
        add()
        for key, label in [('changes', ''), ('interfaces', '接口：'), ('acceptance', '验收：'), ('rollback', '回滚：')]:
            add(label + stage[key])
            add()
    add('## 11. 复现与未验证项')
    add()
    add(TICK * 3 + 'powershell')
    add('python -B 02-tools/scripts/audit_disconnect_risks.py')
    add(f"python -B 02-tools/scripts/audit_disconnect_risks.py --log-cutoff-line {inp['cutoff_line']}")
    add(f"python -B 02-tools/scripts/audit_disconnect_risks.py --check --log-cutoff-line {inp['cutoff_line']}")
    add(TICK * 3)
    add()
    add('默认只写 JSON/Markdown 两文件；--output 保留 JSON 参数，--markdown-output 指定 Markdown。--check 不写文件，--baseline 默认当前 JSON。')
    add('退出码：0=成功/无新增 P0/P1；1=新增 P0/P1；2=输入/锚点/基线错误。基线需 schema_version=2。')
    add('稳定风险签名不含次数/字节/行号；新增异常种类+候选请求组合计新证据风险；input/P2 漂移独立显示。此检查不能证明不存在新的未知语义缺陷。')
    add('存档先双读 DB/WAL，按盐值、校验和和提交帧重建内存快照；mode=ro&immutable=1 连接立即 deserialize 到 RAM，不打开 SHM，不写临时数据库。依据：[SQLite WAL 格式](https://www.sqlite.org/fileformat2.html#walformat)、[Python deserialize](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.deserialize)。')
    add()
    for assumption in report['assumptions']:
        add('- ' + assumption)
    add('- 待实机：所有未实现请求 UI 可达性、门槛按钮、息屏/后台心跳、各列表分片消费者。')
    add('- 待隔离环境实验：持锁/连接上限、单行超限、长期存档增长。')
    add()
    add('自检：' + TICK + canonical(report['self_checks']) + TICK)
    add()
    return '\n'.join(lines)


def summary(report):
    r, q = report['registry'], report['requests']
    return '\n'.join([
        f"启动模块 {len(r['launcher_modules'])}；实际 handler {r['handler_count']}；潜在 handler {r['handler_count_if_all_modules_loaded']}",
        f"可发送 {q['client_sendable_count']}；已处理 {q['handled_count']}；未处理 {q['unhandled_count']}（潜在全部导入后 {q['unhandled_count_if_all_modules_loaded']}）",
        f"回包缺 schema {len(report['replies']['missing_from_schema'])}；gate 缺配置 {len(report['gates']['unknown_gate_names'])}",
        f"日志截止行 {report['inputs']['log']['cutoff_line']}；分类 {canonical(report['log']['event_counts'])}",
        f"当前超限回包 {sum(not r['encodable'] for p in report['frames']['players'] for r in p['replies'])}；验证边界 {report['self_checks']['frame_boundaries_verified']}",
        f"维护指标 {canonical(report['maintainability']['metrics'])}；测试方法 {report['maintainability']['static_test_method_count']}",
    ])


def check_report(report, baseline):
    if (not isinstance(baseline, dict) or baseline.get('schema_version') != VERSION
            or not isinstance(baseline.get('risk_signatures'), list)
            or any(not isinstance(r, str) or not r.startswith(('P0:', 'P1:')) for r in baseline['risk_signatures'])):
        raise AuditError('Baseline must have schema_version=2 and risk_signatures; generate it first')
    added = sorted(set(report['risk_signatures']) - set(baseline['risk_signatures']))
    removed = sorted(set(baseline['risk_signatures']) - set(report['risk_signatures']))
    changes = [key for key in ('log', 'database', 'files') if report['inputs'][key] != baseline.get('inputs', {}).get(key)]
    low = {'limits': report['connection_limits'], 'metrics': report['maintainability']['metrics'],
           'log_classifications': report['log']['classifications'], 'request_intervals': report['log']['request_intervals']}
    old_low = {'limits': baseline.get('connection_limits'), 'metrics': baseline.get('maintainability', {}).get('metrics'),
               'log_classifications': baseline.get('log', {}).get('classifications'),
               'request_intervals': baseline.get('log', {}).get('request_intervals')}
    print('新增 P0/P1 签名：' + canonical(added))
    print('消失签名：' + canonical(removed))
    print('输入变化：' + canonical(changes))
    print('低级别/维护指标漂移：' + str(low != old_low))
    return 1 if added else 0


def destinations(json_path, md_path):
    json_path, md_path = json_path.resolve(), md_path.resolve()
    if json_path == md_path:
        raise AuditError('JSON and Markdown destinations must differ')
    for path, suffix in ((json_path, '.json'), (md_path, '.md')):
        if path.suffix.lower() != suffix:
            raise AuditError('Output extension must be ' + suffix)
        for name in ('05-protocol', '06-client', '07-server', '03-unpack'):
            if path.is_relative_to((ROOT / name).resolve()):
                raise AuditError('Output is under protected input directory: ' + name)
        if path.exists() and path not in (JSON_OUTPUT.resolve(), MD_OUTPUT.resolve()):
            old = path.read_text('utf-8')
            owned = ('"tool": "audit_disconnect_risks.py"' in old if suffix == '.json'
                     else old.startswith('# CrossCore 掉线风险与服务端可维护性审计'))
            if not owned:
                raise AuditError('Refusing to overwrite unrelated output: ' + str(path))
    return json_path, md_path


@contextmanager
def readonly_guard(outputs=()):
    """Constrain imported modules too; hooks are inactive after this scope."""
    allowed = {p.resolve() for p in outputs}
    parents = {p.parent for p in allowed}
    active = [True]
    def hook(event, args):
        if not active[0]:
            return
        if event == 'open':
            path, mode, flags = args
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing and (isinstance(path, int) or Path(path).resolve() not in allowed):
                raise AuditError('Write blocked by audit read-only guard: ' + str(path))
        elif event == 'os.mkdir':
            if Path(args[0]).resolve() not in parents:
                raise AuditError('Directory creation blocked by audit guard')
        elif event in ('os.remove', 'os.rmdir', 'os.rename', 'os.chmod', 'os.chown', 'os.link', 'os.symlink',
                       'os.truncate', 'subprocess.Popen', 'os.system', 'os.exec', 'socket.connect',
                       'socket.bind', 'socket.getaddrinfo'):
            raise AuditError('Side effect blocked by audit guard: ' + event)
        elif event == 'sqlite3.connect' and 'mode=ro' not in str(args[0]):
            raise AuditError('SQLite connections must use mode=ro')
    sys.addaudithook(hook)
    try:
        yield
    finally:
        active[0] = False


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=JSON_OUTPUT, help='JSON output')
    parser.add_argument('--markdown-output', type=Path, default=MD_OUTPUT)
    parser.add_argument('--baseline', type=Path, default=JSON_OUTPUT)
    parser.add_argument('--log-cutoff-line', type=int, help='inclusive last physical log line')
    parser.add_argument('--check', action='store_true', help='read-only baseline comparison')
    args = parser.parse_args(argv)
    try:
        if args.log_cutoff_line is not None and args.log_cutoff_line < 0:
            raise AuditError('Cutoff must be nonnegative')
        if args.check:
            baseline = json.loads(args.baseline.read_text('utf-8'))
            if not isinstance(baseline, dict) or baseline.get('schema_version') != VERSION:
                raise AuditError('Baseline schema_version must be 2; generate a fresh baseline')
        else:
            paths = destinations(args.output, args.markdown_output)
        with readonly_guard(() if args.check else paths):
            report = build_report(args.log_cutoff_line)
            print(summary(report))
            if args.check:
                return check_report(report, baseline)
            payloads = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n', render_markdown(report))
            for path, payload in zip(paths, payloads):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload.encode('utf-8'))
        print('已生成 JSON 与 Markdown。')
        return 0
    except Exception as error:  # All analysis failures have a distinct exit code.
        print(f'AUDIT ERROR: {type(error).__name__}: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
