"""Verify preserved resource archives and their paths without extracting them."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = {'Custom', 'sounds', 'il2cpp', 'videos'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    args = parser.parse_args()
    archive = args.archive.resolve(strict=True)
    if not archive.is_relative_to(ROOT / '01-device'):
        raise ValueError('Expected a preserved project resource archive')
    expected = json.loads(archive.with_suffix('.json').read_text(encoding='utf-8-sig'))
    sha = hashlib.sha256()
    with archive.open('rb') as stream:
        for block in iter(lambda: stream.read(4 << 20), b''):
            sha.update(block)
    if archive.stat().st_size != expected['bytes'] or sha.hexdigest() != expected['sha256']:
        raise ValueError('Archive size/hash differs from its preservation record')
    members = files = payload = 0
    roots, unsafe = set(), []
    with tarfile.open(archive, mode='r|') as stream:
        for member in stream:
            path = PurePosixPath(member.name)
            members += 1
            root = path.parts[0] if path.parts else ''
            roots.add(root)
            if path.is_absolute() or '..' in path.parts or '\\' in member.name or root not in ALLOWED:
                unsafe.append(member.name)
            if member.isfile():
                files += 1
                payload += member.size
            elif not member.isdir():
                unsafe.append(member.name)
    if roots != set(expected['directories']) or unsafe:
        raise ValueError('Archive contains unexpected directories or special/unsafe paths')
    report = {'archive': str(archive), 'archive_bytes': archive.stat().st_size,
              'sha256': sha.hexdigest(), 'members': members, 'files': files,
              'payload_bytes': payload, 'roots': sorted(roots), 'unsafe_paths': unsafe,
              'secret_account_dirs_included': False, 'end_headers_readable': True}
    destination = archive.with_name(archive.stem + '-audit.json')
    destination.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
