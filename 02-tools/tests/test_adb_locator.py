"""Tests for the shared adb locator.

The executable must come from the caller, the environment, PATH or a known
install location, never from a machine-specific path committed to the repo.
Every test fakes PATH and the install tree, so no real emulator is needed.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import adb_locator  # noqa: E402


def fake_adb(directory, name="adb.exe"):
    """Create an empty stand-in for an adb executable and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture
def clean_env(monkeypatch):
    """Remove the ambient adb sources, leaving the install-location glob active."""
    for name in ("CROSSCORE_ADB", "ADB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(adb_locator.shutil, "which", lambda name: None)
    return monkeypatch


def test_explicit_existing_file_is_returned_verbatim(tmp_path, clean_env):
    executable = fake_adb(tmp_path / "sdk" / "platform-tools")
    assert adb_locator.locate_adb(explicit=str(executable)) == str(executable)


def test_explicit_beats_the_environment(tmp_path, clean_env):
    explicit = fake_adb(tmp_path / "explicit")
    clean_env.setenv("CROSSCORE_ADB", str(fake_adb(tmp_path / "from-env")))
    assert adb_locator.locate_adb(explicit=str(explicit)) == str(explicit)


def test_crosscore_adb_beats_adb_and_auto_detection(tmp_path, clean_env):
    crosscore = fake_adb(tmp_path / "crosscore")
    generic = fake_adb(tmp_path / "generic")
    on_path = fake_adb(tmp_path / "on-path")
    clean_env.setenv("ADB", str(generic))
    clean_env.setenv("CROSSCORE_ADB", str(crosscore))
    clean_env.setattr(adb_locator.shutil, "which", lambda name: str(on_path))

    assert adb_locator.locate_adb() == str(crosscore)
    clean_env.delenv("CROSSCORE_ADB")
    assert adb_locator.locate_adb() == str(generic)
    clean_env.delenv("ADB")
    assert adb_locator.locate_adb() == str(on_path)


def test_sdk_location_glob_hits(tmp_path, clean_env):
    executable = fake_adb(tmp_path / "Android" / "Sdk" / "platform-tools")
    clean_env.setenv("LOCALAPPDATA", str(tmp_path))
    clean_env.setenv("USERPROFILE", str(tmp_path / "home"))
    for name in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        clean_env.delenv(name, raising=False)
    clean_env.setenv("ProgramFiles", str(tmp_path / "nope"))
    clean_env.setenv("ProgramFiles(x86)", str(tmp_path / "nope-x86"))
    clean_env.setenv("ProgramW6432", str(tmp_path / "nope-w64"))
    assert adb_locator.locate_adb() == str(executable)


def test_emulator_layout_glob_keeps_the_version_a_wildcard(tmp_path, clean_env):
    executable = fake_adb(tmp_path / "NetEase" / "MuMu Player 12" / "nx_device" / "12.0" / "shell")
    clean_env.setenv("ProgramFiles", str(tmp_path))
    clean_env.setenv("ProgramFiles(x86)", str(tmp_path / "nope-x86"))
    clean_env.setenv("ProgramW6432", str(tmp_path / "nope-w64"))
    clean_env.setenv("LOCALAPPDATA", str(tmp_path / "nope-local"))
    clean_env.setenv("USERPROFILE", str(tmp_path / "home"))
    real_patterns = adb_locator.search_patterns
    clean_env.setattr(adb_locator, "search_patterns",
                      lambda: [p for p in real_patterns() if str(p).startswith(str(tmp_path))])
    assert adb_locator.locate_adb() == str(executable)


def test_nothing_found_and_required_raises_with_guidance(clean_env):
    clean_env.setattr(adb_locator, "search_patterns", lambda: [])
    with pytest.raises(adb_locator.AdbNotFoundError) as error:
        adb_locator.locate_adb(required=True)
    text = str(error.value)
    assert "--adb" in text
    assert "CROSSCORE_ADB" in text


def test_nothing_found_and_optional_returns_bare_adb(clean_env):
    clean_env.setattr(adb_locator, "search_patterns", lambda: [])
    assert adb_locator.locate_adb() == "adb"


def test_stale_explicit_value_is_reported_not_ignored(tmp_path, clean_env):
    with pytest.raises(adb_locator.AdbNotFoundError, match="--adb"):
        adb_locator.locate_adb(explicit=str(tmp_path / "gone" / "adb.exe"))


def test_stale_environment_value_is_reported_not_ignored(tmp_path, clean_env):
    clean_env.setenv("CROSSCORE_ADB", str(tmp_path / "gone" / "adb.exe"))
    with pytest.raises(adb_locator.AdbNotFoundError, match="CROSSCORE_ADB"):
        adb_locator.locate_adb()
