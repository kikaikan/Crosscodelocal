"""Build the single-file, no-install CrossCore PS server executable.

One command hides every PyInstaller flag, so the packaging contract lives here
and nowhere else:

    python 02-tools/scripts/build_server_exe.py

The result is dist/CrossCorePS-Server.exe: a console, --onefile build that
listens on the resource/control HTTP port plus the query and game TCP ports.
Data tables (07-server/data/*.json) and the protocol definition
(05-protocol/endpoints.json) are intentionally NOT collected; the executable
reads them from its own directory at run time.

Because those inputs are deliberately absent from the archive, the artifact
cannot be import-checked where it is built: a frozen server resolves data/,
05-protocol/ and 03-unpack/ next to its own executable, and dist/ has none of
them. Every build therefore stages the artifact in a private temporary
directory, attaches the three runtime inputs (a directory junction when the
host allows it, a copy for the two small ones otherwise) and runs the probe
there. The staging directory is removed again afterwards.

Two builds never share state: the PyInstaller work directory comes from
tempfile.mkdtemp, and nothing outside this run's own artifact and work
directory is ever deleted.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / '07-server'
SCRIPTS = REPO / '02-tools' / 'scripts'
DEFAULT_ENTRY = SERVER / 'server_core.py'
DEFAULT_NAME = 'CrossCorePS-Server'

# Modules reached through importlib/monkeypatched sys.path that static analysis
# cannot see, plus the modules a bare run of the entry point registers.
HIDDEN_IMPORTS = (
    'control_http', 'control_panel', 'control_gateway', 'config_codec', 'protocol_codec',
    'admin_control', 'admin_local', 'admin_resources', 'admin_roles', 'access_policy',
    'card_roles_service', 'collection_unlock', 'database', 'equip_service', 'equipment_stats',
    'error_policy', 'formation_halo', 'gift_service', 'mail_service', 'panel_service',
    'reply_chunks', 'seed_generator', 'skins_service',
)
# Whole packages collected recursively so dynamically imported submodules ship.
COLLECT_SUBMODULES = ('handlers',)
EXCLUDES = ('pytest', 'unittest', 'tkinter', 'test', 'tests')

# Runtime inputs the frozen server reads from beside its executable. They are
# kept out of the archive by design, so the import self-check attaches them to a
# throwaway copy of the artifact instead of running it inside dist/.
STAGING_INPUTS = (
    ('data', SERVER / 'data'),
    ('05-protocol', REPO / '05-protocol'),
    ('03-unpack', REPO / '03-unpack'),
)
# A link that cannot be created falls back to a copy for these two small trees.
# 03-unpack holds tens of thousands of client scripts and is never copied; when
# it cannot be attached the affected modules are reported as unverifiable here.
COPIED_WHEN_UNLINKABLE = ('data', '05-protocol')
UNPACK_RELATIVE = '03-unpack/lua/device-luascripts'
# A runtime failure naming this tree is caused by the host, not by the package.
UNPACK_MARKER = '03-unpack'


def python_files(directory: Path):
    return sorted(path for path in directory.glob('*.py') if path.name != '__init__.py')


def module_names(directory: Path, package: str = ''):
    names = []
    for path in python_files(directory):
        names.append(f'{package}.{path.stem}' if package else path.stem)
    return names


def build_command(python: str, entry: Path, name: str, work: Path, out: Path, specdir: Path):
    command = [
        python, '-m', 'PyInstaller',
        '--noconfirm', '--clean', '--onefile', '--console',
        '--name', name,
        '--distpath', str(out),
        '--workpath', str(work),
        '--specpath', str(specdir),
        '--paths', str(SERVER),
        '--paths', str(SCRIPTS),
    ]
    for package in COLLECT_SUBMODULES:
        command += ['--collect-submodules', package]
    imports = dict.fromkeys(module_names(SERVER) + module_names(SERVER / 'handlers', 'handlers')
                            + list(HIDDEN_IMPORTS))
    for module in imports:
        command += ['--hidden-import', module]
    for module in EXCLUDES:
        command += ['--exclude-module', module]
    command.append(str(entry))
    return command


def default_handlers():
    """Read the entry point's own default handler list instead of copying it."""
    tree = ast.parse(DEFAULT_ENTRY.read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == 'DEFAULT_HANDLERS' for target in node.targets):
            return [element.value for element in node.value.elts]
    raise RuntimeError('DEFAULT_HANDLERS not found in ' + str(DEFAULT_ENTRY))


def critical_modules():
    """Modules a bare double-click run must be able to import."""
    required = set(HIDDEN_IMPORTS)
    required |= set(module_names(SERVER))
    required |= set(default_handlers())
    return required


def optional_modules():
    """Handler modules that are opt-in and may share a message name with a default."""
    defaults = set(default_handlers())
    return [f'handlers.{path.stem}' for path in python_files(SERVER / 'handlers')
            if f'handlers.{path.stem}' not in defaults]


def is_reparse_point(path: Path) -> bool:
    """True for junctions and directory symlinks, without following either."""
    try:
        attributes = getattr(path.lstat(), 'st_file_attributes', 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def link_directory(source: Path, target: Path) -> bool:
    """Attach source at target without copying it; False when unsupported.

    A Windows junction needs no privilege, unlike a symlink, so it is tried
    first and os.symlink is the portable fallback.
    """
    if os.name == 'nt':
        try:
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(target), str(source)],
                                    capture_output=True, text=True)
        except OSError:
            result = None
        if result is not None and result.returncode == 0 and target.is_dir():
            return True
        # A rejected mklink can leave an empty directory where the link belongs.
        if target.exists() and not is_reparse_point(target):
            shutil.rmtree(target, ignore_errors=True)
    try:
        os.symlink(str(source), str(target), target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    return target.is_dir()


def prepare_selfcheck_tree(artifact: Path) -> dict:
    """Copy the artifact somewhere writable and attach its runtime inputs."""
    root = Path(tempfile.mkdtemp(prefix=DEFAULT_NAME + '-selfcheck-'))
    shutil.copy2(artifact, root / artifact.name)
    attached, copied, unavailable = [], [], []
    for name, source in STAGING_INPUTS:
        target = root / name
        if not source.is_dir():
            unavailable.append(name)
        elif link_directory(source, target):
            attached.append(name)
        elif name in COPIED_WHEN_UNLINKABLE:
            shutil.copytree(source, target)
            copied.append(name)
        else:
            unavailable.append(name)
    return {'root': root, 'exe': root / artifact.name, 'attached': attached,
            'copied': copied, 'unavailable': unavailable}


def cleanup_selfcheck_tree(staging: dict) -> None:
    """Remove the staging directory, never the trees it links to."""
    root = staging.get('root')
    if root is None:
        return
    for name, _source in STAGING_INPUTS:
        child = root / name
        if is_reparse_point(child):
            # rmdir drops the link itself and leaves the target untouched.
            try:
                os.rmdir(child)
            except OSError:
                pass
    shutil.rmtree(root, ignore_errors=True)


def describe_staging(staging: dict) -> str:
    rows = []
    for name, _source in STAGING_INPUTS:
        if name in staging.get('present', ()):
            rows.append(f'{name}=kept')
        elif name in staging['attached']:
            rows.append(f'{name}=linked')
        elif name in staging['copied']:
            rows.append(f'{name}=copied')
        else:
            rows.append(f'{name}=ABSENT')
    return ', '.join(rows)


def stage_runtime(artifact: Path, target: Path) -> dict:
    """Leave a ready-to-run directory: the exe with its runtime inputs beside it.

    The frozen exe resolves data/, 05-protocol/ and 03-unpack/ from the folder that
    holds it, so a bare copy of the artifact cannot start on its own. This stages that
    folder instead of asking someone to assemble it by hand. Anything already present
    in the target is kept as it is and never replaced or removed.
    """
    target.mkdir(parents=True, exist_ok=True)
    exe = target / artifact.name
    shutil.copy2(artifact, exe)
    attached, copied, present, unavailable = [], [], [], []
    for name, source in STAGING_INPUTS:
        link = target / name
        if link.exists() or is_reparse_point(link):
            present.append(name)
        elif not source.is_dir():
            unavailable.append(name)
        elif link_directory(source, link):
            attached.append(name)
        elif name in COPIED_WHEN_UNLINKABLE:
            shutil.copytree(source, link)
            copied.append(name)
        else:
            unavailable.append(name)
    return {'root': target, 'exe': exe, 'attached': attached, 'copied': copied,
            'present': present, 'unavailable': unavailable}


def run_import_check(artifact: Path, imports, timeout: int = 300):
    """Ask the frozen exe to import a module list; return (checked, failures, verdict)."""
    result = subprocess.run([str(artifact), '--check-imports', '-'], input=json.dumps(sorted(imports)),
                            capture_output=True, text=True, timeout=timeout,
                            cwd=str(artifact.parent))
    output = (result.stdout or "") + (result.stderr or "")
    line = next((row for row in reversed(output.splitlines()) if row.startswith('{"missing"')), None)
    if line is None:
        raise RuntimeError('frozen import check produced no verdict:\n' + output[-2000:])
    verdict = json.loads(line)
    return verdict['checked'], verdict['missing'], line


def bundle_contents(artifact: Path):
    """List the module names recorded in the frozen executable PYZ archive.

    Failing to read the archive means the build cannot be verified at all, so it
    aborts the build: reporting success while quietly skipping the check is the
    exact failure this function must never allow.
    """
    try:
        from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    except ImportError as error:
        raise RuntimeError('cannot inspect the frozen archive; this PyInstaller build no '
                           f'longer exposes its archive readers ({error})') from error
    try:
        reader = CArchiveReader(str(artifact))
        # TOC offsets are relative to the start of the CArchive, not the file.
        base = getattr(reader, '_start_offset', 0)
        names = set()
        for name, (position, compressed, uncompressed, compress_flag, typecode) in reader.toc.items():
            if name.endswith('.pyz') and typecode == 'z':
                archive = ZlibArchiveReader(str(artifact), base + position)
                for module, (_, _, module_type) in archive.toc.items():
                    names.add(module.replace('.', '/'))
    except Exception as error:
        raise RuntimeError(f'cannot read the frozen archive {artifact.name}: '
                           f'{type(error).__name__}: {error}') from error
    if not names:
        raise RuntimeError('the frozen archive records 0 Python modules, which cannot be '
                           'correct; refusing to report an unverifiable artifact as good')
    return names


def verify_bundle(artifact: Path, staging: dict, timeout: int = 300):
    """Prove that every dynamic module really shipped and can be imported.

    The PKG/PYZ archive is inspected directly, because PyInstaller static
    analysis cannot see names passed to importlib. The produced exe is then
    asked to import the dynamic modules itself from the staging directory;
    optional modules are probed one process at a time because two handlers can
    register the same message and must never be imported together.

    A host without 03-unpack cannot import the handlers that read the unpacked
    client tables, and the first such failure also makes every later handler
    that registers the same message look duplicated. Those failures are
    reported as unverified (a local data gap) only when the run really named
    the missing tree; anything else stays a build defect.
    """
    contents = bundle_contents(artifact)
    critical = sorted(critical_modules())
    missing = [module + ' -> absent from frozen archive' for module in critical
               if module.replace('.', '/') not in contents]
    checked, verdict = 0, ''
    failures = []
    if critical:
        checked, batch_failures, verdict = run_import_check(staging['exe'], critical, timeout)
        failures += batch_failures
    for module in optional_modules():
        checked += 1
        _, module_failures, _ = run_import_check(staging['exe'], [module], timeout)
        failures += module_failures
    defective, unverified = [], []
    data_gap = '03-unpack' in staging['unavailable'] and any(UNPACK_MARKER in row for row in failures)
    for row in failures:
        if data_gap and (UNPACK_MARKER in row or 'Duplicate handler' in row):
            unverified.append(row)
        else:
            defective.append(row)
    return {'checked': checked, 'archived': len(contents), 'missing': missing + defective,
            'unverified': unverified, 'verdict': verdict}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--entry', type=Path, default=DEFAULT_ENTRY, help='Entry script to freeze')
    parser.add_argument('--name', default=DEFAULT_NAME, help='Output base name (no .exe)')
    parser.add_argument('--dist', type=Path, default=REPO / 'dist', help='Output directory')
    parser.add_argument('--work', type=Path, default=None,
                        help='PyInstaller work directory (default: a unique temporary directory)')
    parser.add_argument('--stage', type=Path, default=None,
                        help='Also leave a ready-to-run directory here: the exe plus data/, '
                             '05-protocol/ and 03-unpack/ beside it (linked when the filesystem '
                             'allows it, otherwise copied)')
    parser.add_argument('--dry-run', action='store_true', help='Print the PyInstaller command and exit')
    args = parser.parse_args()

    entry = args.entry.resolve()
    if not entry.is_file():
        parser.error(f'entry script not found: {entry}')
    out = args.dist.resolve()
    if args.dry_run:
        work = args.work.resolve() if args.work else Path(tempfile.gettempdir()) / (args.name + '-build')
        print(' '.join(str(part) for part in
                       build_command(sys.executable, entry, args.name, work, out, work / 'spec')))
        return 0

    # A private work directory per run: two builds must never clean each other's
    # intermediate files. Only a caller-supplied --work is reused as given.
    own_work = args.work is None
    work = Path(tempfile.mkdtemp(prefix=args.name + '-build-')) if own_work else args.work.resolve()
    specdir = work / 'spec'
    staging = None
    try:
        specdir.mkdir(parents=True, exist_ok=True)
        out.mkdir(parents=True, exist_ok=True)
        # Remove only the artifact this run is about to replace; dist/ may hold
        # files other runs put there and none of them are ours to delete.
        stale = out / (args.name + '.exe')
        if stale.is_file():
            stale.unlink()
        command = build_command(sys.executable, entry, args.name, work, out, specdir)
        print(f'[build] entry={entry}')
        print(f'[build] work directory: {work}')
        print(f'[build] handlers collected recursively: {", ".join(COLLECT_SUBMODULES)}')
        print('[build] data tables and 05-protocol/endpoints.json are NOT bundled (by design)')
        result = subprocess.run(command, cwd=str(REPO))
        if result.returncode != 0:
            print('[build] PyInstaller failed', file=sys.stderr)
            return result.returncode
        artifact = out / (args.name + '.exe')
        if not artifact.is_file():
            print(f'[build] expected artifact missing: {artifact}', file=sys.stderr)
            return 1
        try:
            staging = prepare_selfcheck_tree(artifact)
        except OSError as error:
            print(f'[build] self-check staging failed: {type(error).__name__}: {error}', file=sys.stderr)
            return 1
        print(f'[build] self-check directory: {staging["root"]} ({describe_staging(staging)})')
        missing_inputs = [name for name in COPIED_WHEN_UNLINKABLE if name in staging['unavailable']]
        if missing_inputs:
            print('[build] this checkout does not provide ' + ', '.join(missing_inputs)
                  + '; the frozen import check cannot run and the build is NOT verified',
                  file=sys.stderr)
            return 1
        try:
            report = verify_bundle(artifact, staging)
        except Exception as error:
            print(f'[build] bundle self-check FAILED: {type(error).__name__}: {error}', file=sys.stderr)
            return 1
        if report['missing']:
            print('[build] missing modules in bundle:', file=sys.stderr)
            for row in report['missing']:
                print('  ' + row, file=sys.stderr)
            return 1
        if report['unverified']:
            print(f'[build] NOT verified here: {len(report["unverified"])} module(s) read '
                  f'{UNPACK_RELATIVE}, which this machine does not have; add 03-unpack to the '
                  'checkout to import-check them. This is a local data gap, not a packaging failure:',
                  file=sys.stderr)
            for row in report['unverified']:
                print('  ' + row, file=sys.stderr)
        if report['verdict']:
            print(f'[build] frozen import verdict: {report["verdict"]}')
        print(f'[build] frozen self-check imported {report["checked"]} dynamic modules; none missing')
        print(f'[build] archive lists {report["archived"]} frozen modules')
        size = artifact.stat().st_size
        print(f'[build] artifact: {artifact}')
        print(f'[build] size: {size} bytes ({size / 1024 / 1024:.1f} MiB)')
        print(f'[build] sha256: {hashlib.sha256(artifact.read_bytes()).hexdigest()}')
        if args.stage is not None:
            staged = stage_runtime(artifact, args.stage.resolve())
            print(f'[build] staged run directory: {staged["root"]} ({describe_staging(staged)})')
            print(f'[build] start it with: {staged["exe"]}')
            if staged['unavailable']:
                print('[build] not staged because this checkout lacks: '
                      + ', '.join(staged['unavailable']), file=sys.stderr)
        return 0
    finally:
        if staging is not None:
            cleanup_selfcheck_tree(staging)
        if own_work:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())