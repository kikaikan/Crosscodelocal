"""Small isolated TAR cache regressions; no real archive or service operation."""
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import serve_bootstrap as bootstrap


class ArchiveIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='crosscore-index-')
        self.root = Path(self.temp.name)
        (self.root / '01-device').mkdir()
        self.preservation = self.make_tar('assets-preservation', ['Custom', 'sounds', 'il2cpp'],
                            [('Custom/one', b'first'), ('sounds/two', b'second'), ('il2cpp/empty', b'')])
        self.videos = self.make_tar('assets-videos', ['videos'], [('videos/three', b'third')])
        self.cache = self.root / '07-server/run/archive-index.json'

    def tearDown(self):
        self.temp.cleanup()

    def make_tar(self, name, directories, files, kind=None):
        path = self.root / '01-device' / (name + '.tar')
        with tarfile.open(path, 'w', format=tarfile.PAX_FORMAT) as archive:
            for key, content in files:
                member = tarfile.TarInfo(key)
                if kind is not None:
                    member.type = kind
                    member.linkname = 'Custom/one'
                    archive.addfile(member)
                else:
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
        payload = path.read_bytes()
        record = {'archive': str(path), 'bytes': len(payload),
                  'sha256': hashlib.sha256(payload).hexdigest(), 'directories': directories}
        path.with_suffix('.json').write_text(json.dumps(record), encoding='utf-8')
        return path

    def index(self, **fields):
        return bootstrap.index_archives(root=self.root, **fields)

    def payload(self, entry):
        with entry.archive.open('rb') as archive:
            archive.seek(entry.offset)
            return archive.read(entry.size)

    def corrupt(self, modify):
        saved = json.loads(self.cache.read_text('utf-8'))
        modify(saved)
        if isinstance(saved.get('entries'), list):
            saved['entry_count'] = len(saved['entries'])
            saved['entries_sha256'] = hashlib.sha256(bootstrap._json_bytes(saved['entries'])).hexdigest()
        self.cache.write_text(json.dumps(saved), encoding='utf-8')

    def test_first_build_then_cache_hit_never_opens_tar_again(self):
        events = []
        real_open = tarfile.open
        with patch.object(bootstrap.tarfile, 'open', wraps=real_open) as walked:
            entries = self.index(progress=events.append)
            self.assertEqual(walked.call_count, 2)
        self.assertEqual(set(entries), {'Custom/one', 'sounds/two', 'il2cpp/empty', 'videos/three'})
        self.assertEqual(self.payload(entries['Custom/one']), b'first')
        self.assertEqual(self.payload(entries['videos/three']), b'third')
        saved = json.loads(self.cache.read_text('utf-8'))
        self.assertEqual(saved['archives'][0]['name'], self.preservation.name)
        self.assertEqual(saved['archives'][0]['path'], str(self.preservation.resolve()))
        self.assertEqual(saved['entry_count'], 4)
        before = self.cache.stat().st_mtime_ns
        with patch.object(bootstrap.tarfile, 'open', side_effect=AssertionError('Warm cache traversed TAR')):
            reused = self.index(progress=events.append)
        self.assertEqual(reused, entries)
        self.assertEqual(self.cache.stat().st_mtime_ns, before)
        self.assertTrue(any(event.get('cache') == 'hit' for event in events))

    def test_mtime_change_invalidates_then_reuses_new_fingerprint(self):
        self.index()
        current = self.preservation.stat()
        os.utime(self.preservation, ns=(current.st_atime_ns, current.st_mtime_ns + 2_000_000))
        with patch.object(bootstrap.tarfile, 'open', wraps=tarfile.open) as walked:
            entries = self.index()
            self.assertEqual(walked.call_count, 2)
        identity = json.loads(self.cache.read_text('utf-8'))['archives'][0]
        self.assertEqual(identity['mtime_ns'], self.preservation.stat().st_mtime_ns)
        with patch.object(bootstrap.tarfile, 'open', side_effect=AssertionError('Reused stale index')):
            self.assertEqual(self.index(), entries)

    def test_manifest_hash_bytes_or_roots_cannot_reuse_old_cache(self):
        self.index()
        original = self.preservation.with_suffix('.json').read_text('utf-8')
        record = json.loads(original)
        record['sha256'] = '0' * 64
        self.preservation.with_suffix('.json').write_text(json.dumps(record), encoding='utf-8')
        with patch.object(bootstrap.tarfile, 'open', wraps=tarfile.open) as walked:
            self.index()
            self.assertEqual(walked.call_count, 2)
        record['directories'] = ['Custom', 'sounds']
        self.preservation.with_suffix('.json').write_text(json.dumps(record), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.index()  # il2cpp member is genuinely outside updated source manifest.
        self.preservation.with_suffix('.json').write_text(original, encoding='utf-8')
        record = json.loads(original)
        record['bytes'] += 512
        self.preservation.with_suffix('.json').write_text(json.dumps(record), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'size mismatch'):
            self.index()

    def test_cache_cannot_redirect_archive_path_or_name(self):
        expected = self.index()
        pristine = self.cache.read_text('utf-8')
        variants = [
            lambda saved: saved['archives'][0].update(path='C:/outside/secret.tar'),
            lambda saved: saved['archives'][0].update(name='other.tar'),
            lambda saved: saved['entries'][0].update(archive='C:/outside/secret.tar'),
        ]
        for modify in variants:
            with self.subTest(modify=modify):
                self.cache.write_text(pristine, encoding='utf-8')
                self.corrupt(modify)
                with patch.object(bootstrap.tarfile, 'open', wraps=tarfile.open) as walked:
                    self.assertEqual(self.index(), expected)
                    self.assertEqual(walked.call_count, 2)

    def test_dangerous_entries_rebuild_even_with_recomputed_entry_digest(self):
        expected = self.index()
        pristine = self.cache.read_text('utf-8')
        variants = [
            lambda saved: saved['entries'][0].update(key='../secret'),
            lambda saved: saved['entries'][0].update(key='/Custom/one'),
            lambda saved: saved['entries'][0].update(key='Custom\\secret'),
            lambda saved: saved['entries'][0].update(key='Custom//one'),
            lambda saved: saved['entries'][0].update(offset=-512),
            lambda saved: saved['entries'][0].update(offset=1),
            lambda saved: saved['entries'][0].update(offset=True),
            lambda saved: saved['entries'][0].update(size=-1),
            lambda saved: saved['entries'][0].update(size=1.5),
            lambda saved: saved['entries'][0].update(size=self.preservation.stat().st_size),
            lambda saved: saved['entries'].append(deepcopy(saved['entries'][0])),
        ]
        for modify in variants:
            with self.subTest(modify=modify):
                self.cache.write_text(pristine, encoding='utf-8')
                self.corrupt(modify)
                with patch.object(bootstrap.tarfile, 'open', wraps=tarfile.open) as walked:
                    rebuilt = self.index()
                    self.assertEqual(walked.call_count, 2)
                self.assertEqual(rebuilt, expected)
                self.assertEqual(self.payload(rebuilt['Custom/one']), b'first')

    def test_overlapping_entry_offsets_and_json_duplicates_rebuild(self):
        expected = self.index()
        pristine = self.cache.read_text('utf-8')
        def overlap(saved):
            first = next(row for row in saved['entries'] if row['key'] == 'Custom/one')
            second = next(row for row in saved['entries'] if row['key'] == 'sounds/two')
            second['offset'] = first['offset']
        self.corrupt(overlap)
        with patch.object(bootstrap.tarfile, 'open', wraps=tarfile.open) as walked:
            self.assertEqual(self.index(), expected)
            self.assertEqual(walked.call_count, 2)
        self.cache.write_text(pristine.replace('"version":1', '"version":1,"version":1'), encoding='utf-8')
        with patch.object(bootstrap.tarfile, 'open', wraps=tarfile.open) as walked:
            self.assertEqual(self.index(), expected)
            self.assertEqual(walked.call_count, 2)

    def test_corrupt_root_and_integrity_mismatch_rebuild(self):
        expected = self.index()
        pristine = self.cache.read_text('utf-8')
        for value in ('[]', 'null', '{"bad"', pristine.replace('"entries_sha256":"', '"entries_sha256":"a')):
            with self.subTest(value=value[:30]):
                self.cache.write_text(value, encoding='utf-8')
                with patch.object(bootstrap.tarfile, 'open', wraps=tarfile.open) as walked:
                    self.assertEqual(self.index(), expected)
                    self.assertEqual(walked.call_count, 2)

    def test_original_unsafe_tar_members_are_still_rejected(self):
        for name, kind in (('../outside', None), ('/Custom/one', None),
                           ('Custom\\one', None), ('Custom/link', tarfile.SYMTYPE),
                           ('Custom/link', tarfile.LNKTYPE)):
            with self.subTest(name=name, kind=kind):
                self.make_tar('assets-preservation', ['Custom'], [(name, b'x')], kind)
                with self.assertRaises(ValueError):
                    self.index(force_rebuild=True)

    def test_original_duplicate_and_truncated_archive_checks_remain(self):
        self.make_tar('assets-videos', ['Custom'], [('Custom/one', b'other')])
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.index()
        self.make_tar('assets-videos', ['videos'], [('videos/three', b'third')])
        self.index()
        with self.preservation.open('ab') as archive:
            archive.write(b'x')
        with self.assertRaisesRegex(ValueError, 'size mismatch'):
            self.index()

    def test_unwritable_cache_keeps_checked_in_memory_index_and_reports_failure(self):
        events = []
        with patch.object(bootstrap, '_atomic_write', side_effect=PermissionError('test')):
            entries = self.index(progress=events.append)
        self.assertEqual(self.payload(entries['Custom/one']), b'first')
        self.assertFalse(self.cache.exists())
        self.assertTrue(any(event.get('cache') == 'write-failed' for event in events))

    def test_index_only_cli_does_not_listen_or_export_static_files(self):
        output = io.StringIO()
        with patch.object(bootstrap, 'ROOT', self.root), \
             patch.object(sys, 'argv', ['serve_bootstrap.py', '--index-only']), \
             patch.object(bootstrap, 'ThreadingHTTPServer', side_effect=AssertionError('Server started')), \
             patch.object(bootstrap, 'export_static', side_effect=AssertionError('Static files exported')), \
             redirect_stdout(output):
            bootstrap.main()
        self.assertTrue(self.cache.is_file())
        self.assertFalse((self.root / '06-client').exists())
        phases = [json.loads(line)['phase'] for line in output.getvalue().splitlines()]
        self.assertIn('index-ready', phases)

    def test_static_export_only_changed_exact_files_without_rewriting_other_outputs(self):
        target = self.root / 'static'
        resources = {'/cross/release/one': b'one', '/cross/release/two': b'two'}
        self.assertEqual(bootstrap.export_static(resources, b'{}', target), 3)
        one = target / 'cross/release/one'
        two = target / 'cross/release/two'
        first = {path: path.stat().st_mtime_ns for path in (one, two, target / 'server-list.local.json')}
        self.assertEqual(bootstrap.export_static(resources, b'{}', target), 0)
        self.assertEqual(first, {path: path.stat().st_mtime_ns for path in first})
        resources['/cross/release/one'] = b'changed'
        self.assertEqual(bootstrap.export_static(resources, b'{}', target), 1)
        self.assertEqual(one.read_bytes(), b'changed')
        self.assertEqual(two.stat().st_mtime_ns, first[two])

    def test_unsafe_static_paths_are_rejected_before_any_write(self):
        for value in ('/../outside', '//server/share', '/cross\\outside',
                      '/cross/C:/outside', '/cross/CON', '/cross/file.', '/server-list.local.json'):
            with self.subTest(path=value):
                target = self.root / 'safe-static'
                with self.assertRaises(ValueError):
                    bootstrap.export_static({value: b'bad'}, b'{}', target)
                self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
