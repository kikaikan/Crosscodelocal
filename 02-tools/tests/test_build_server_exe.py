"""Tests for the single-file server build script.

The build must verify the artifact it produced, must fail loudly when that
verification cannot run at all, and must never delete anything in dist/ other
than the artifact it is about to replace. Nothing here invokes PyInstaller or
the real frozen executable; the probes are faked so the whole file runs in
under a second.
"""
import builtins
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_server_exe as build  # noqa: E402

VERDICT = '{"missing": [], "checked": 49}'


def _tree(root, name, filename):
    tree = root / name
    tree.mkdir(parents=True)
    (tree / filename).write_text("{}", encoding="utf-8")
    return tree


@pytest.fixture
def staging_inputs(tmp_path, monkeypatch):
    """Small stand-ins for data/, 05-protocol/ and 03-unpack/."""
    inputs = (
        ("data", _tree(tmp_path, "data", "table.json")),
        ("05-protocol", _tree(tmp_path, "05-protocol", "endpoints.json")),
        ("03-unpack", _tree(tmp_path, "03-unpack", "GameMsg.lua")),
    )
    monkeypatch.setattr(build, "STAGING_INPUTS", inputs)
    return inputs


def test_selfcheck_links_inputs_without_touching_the_mother_checkout(tmp_path, staging_inputs):
    artifact = tmp_path / "CrossCorePS-Server.exe"
    artifact.write_bytes(b"MZ fake")
    staging = build.prepare_selfcheck_tree(artifact)
    try:
        assert (staging["root"] / artifact.name).read_bytes() == b"MZ fake"
        assert (staging["root"] / "data" / "table.json").is_file()
        assert (staging["root"] / "05-protocol" / "endpoints.json").is_file()
        if not staging["attached"]:
            pytest.skip("this host cannot create directory links")
        assert staging["unavailable"] == []
    finally:
        build.cleanup_selfcheck_tree(staging)
    assert not staging["root"].exists()
    for _name, source in staging_inputs:
        assert source.is_dir() and any(source.iterdir())


def test_selfcheck_copies_the_small_trees_when_linking_is_unavailable(tmp_path, staging_inputs, monkeypatch):
    monkeypatch.setattr(build, "link_directory", lambda source, target: False)
    artifact = tmp_path / "CrossCorePS-Server.exe"
    artifact.write_bytes(b"MZ fake")
    staging = build.prepare_selfcheck_tree(artifact)
    try:
        assert staging["copied"] == ["data", "05-protocol"]
        assert staging["unavailable"] == ["03-unpack"]
        assert (staging["root"] / "data" / "table.json").is_file()
        assert (staging["root"] / "05-protocol" / "endpoints.json").is_file()
        assert not (staging["root"] / "03-unpack").exists()
    finally:
        build.cleanup_selfcheck_tree(staging)
    assert not staging["root"].exists()


def test_archive_read_failure_aborts_instead_of_reporting_success(monkeypatch):
    """D2: a skipped check must never be reported as a passed check."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "PyInstaller.archive.readers":
            raise ImportError("simulated removal of ZlibArchiveReader")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="cannot inspect the frozen archive"):
        build.bundle_contents(Path("no-such-artifact.exe"))


def test_empty_archive_is_rejected(monkeypatch):
    from PyInstaller.archive import readers

    monkeypatch.setattr(readers, "CArchiveReader",
                        lambda path: SimpleNamespace(toc={}, _start_offset=0))
    with pytest.raises(RuntimeError, match="0 Python modules"):
        build.bundle_contents(Path("whatever.exe"))


def test_main_returns_nonzero_when_the_self_check_raises(tmp_path, staging_inputs, monkeypatch, capsys):
    dist = tmp_path / "dist"
    dist.mkdir()
    artifact = dist / "Fake.exe"

    def fake_run(command, **kwargs):
        artifact.write_bytes(b"MZ")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build.subprocess, "run", fake_run)

    def exploding_verify(artifact, staging, timeout=300):
        raise RuntimeError("simulated removal of ZlibArchiveReader")

    monkeypatch.setattr(build, "verify_bundle", exploding_verify)
    monkeypatch.setattr(sys, "argv", ["build_server_exe.py", "--dist", str(dist), "--name", "Fake"])
    assert build.main() == 1
    assert "bundle self-check FAILED" in capsys.readouterr().err
    assert not list(tmp_path.glob("Fake-selfcheck-*"))


def test_unpack_gap_is_unverified_but_a_real_import_error_is_a_defect(tmp_path, monkeypatch):
    contents = {module.replace(".", "/") for module in build.critical_modules()}
    monkeypatch.setattr(build, "bundle_contents", lambda artifact: contents)
    monkeypatch.setattr(build, "optional_modules", lambda: [])
    failures = [
        "handlers.player_state -> FileNotFoundError: ... 03-unpack\\lua\\device-luascripts\\cfgglobal_setting.lua",
        "handlers.sub_talent -> ValueError: Duplicate handler: PlayerProto:SetCardInfo",
    ]
    monkeypatch.setattr(build, "run_import_check",
                        lambda artifact, imports, timeout=300: (len(imports), list(failures), VERDICT))
    exe = tmp_path / "Fake.exe"
    without_unpack = build.verify_bundle(exe, {"exe": exe, "unavailable": ["03-unpack"]})
    assert without_unpack["missing"] == []
    assert len(without_unpack["unverified"]) == 2
    monkeypatch.setattr(build, "run_import_check",
                        lambda artifact, imports, timeout=300:
                        (len(imports), ["handlers.player_state -> ModuleNotFoundError: broken"], VERDICT))
    with_unpack = build.verify_bundle(exe, {"exe": exe, "unavailable": []})
    assert with_unpack["unverified"] == []
    assert with_unpack["missing"] == ["handlers.player_state -> ModuleNotFoundError: broken"]


def test_main_leaves_dist_alone_and_removes_its_own_work_directory(tmp_path, staging_inputs, monkeypatch, capsys):
    """D3: a build deletes its own artifact and work directory, nothing else."""
    dist = tmp_path / "dist"
    dist.mkdir()
    sentinel = dist / "someone-elses-file.txt"
    sentinel.write_text("keep", encoding="utf-8")
    artifact = dist / "Fake.exe"

    def fake_run(command, **kwargs):
        artifact.write_bytes(b"MZ")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build.subprocess, "run", fake_run)
    monkeypatch.setattr(build, "verify_bundle",
                        lambda a, s, timeout=300: {"checked": 1, "archived": 1, "missing": [],
                                                   "unverified": [], "verdict": VERDICT})
    removed = []
    real_rmtree = shutil.rmtree

    def recording_rmtree(path, *args, **kwargs):
        removed.append(Path(path))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(build.shutil, "rmtree", recording_rmtree)
    workdirs = []
    for _ in range(2):
        monkeypatch.setattr(sys, "argv",
                            ["build_server_exe.py", "--dist", str(dist), "--name", "Fake"])
        assert build.main() == 0
        line = next(row for row in capsys.readouterr().out.splitlines()
                    if row.startswith("[build] work directory: "))
        workdirs.append(Path(line.split(": ", 1)[1]))
    assert workdirs[0] != workdirs[1]
    assert all(not workdir.exists() for workdir in workdirs)
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert artifact.is_file()
    assert dist not in removed


def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    dist = tmp_path / "dist"
    monkeypatch.setattr(sys, "argv", ["build_server_exe.py", "--dry-run", "--dist", str(dist)])
    assert build.main() == 0
    assert "PyInstaller" in capsys.readouterr().out
    assert not dist.exists()
