"""Locate a usable adb executable without hard-coding a machine-specific path.

Resolution order, highest priority first:

1. the path the caller passes explicitly (--adb on the command line),
2. the CROSSCORE_ADB environment variable,
3. the ADB environment variable,
4. adb on PATH (shutil.which),
5. well-known SDK and emulator install locations, matched with glob so that no
   emulator version number is hard-coded (Android SDK, MuMu, LDPlayer,
   BlueStacks, Nox, MEmu, Genymotion, MiPhoneAssistant, ...).

An explicit value (argument or environment variable) that does not resolve to a
usable executable is an error instead of a silent fallback: someone who named an
adb expects that adb to be used. When nothing at all is found, required=True
raises AdbNotFoundError carrying the --adb / CROSSCORE_ADB guidance, while
required=False returns the string 'adb' so a caller can still build a command
line.

The discovered path is never written back to any file: a machine-specific path
must not be committed again.

Standard library only.
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
from pathlib import Path

__all__ = ["AdbNotFoundError", "locate_adb", "search_patterns", "candidate_paths"]


class AdbNotFoundError(RuntimeError):
    """No usable adb executable was found, or an explicit one was unusable."""


_GUIDANCE = (
    "\n"
    "Point the script at a working adb, highest precedence first:\n"
    '  --adb "<path>/adb.exe"    for example: --adb "C:\\Android\\Sdk\\platform-tools\\adb.exe"\n'
    "  CROSSCORE_ADB=<path>      project environment variable\n"
    "  ADB=<path>                generic fallback environment variable\n"
    "or install Android platform-tools so that adb is on PATH."
)


def _failure(reason):
    return AdbNotFoundError(reason + _GUIDANCE)


def _resolve_value(value):
    """Return a usable adb path for a user-supplied value, or None.

    An existing file is returned exactly as the user wrote it; a bare command
    name such as 'adb' is resolved through PATH.
    """
    text = os.path.expandvars(str(value).strip().strip('"'))
    if not text:
        return None
    if Path(text).is_file():
        return text
    expanded = Path(text).expanduser()
    if expanded.is_file():
        return str(expanded)
    return shutil.which(text)


def search_patterns():
    """Glob patterns for known adb locations, most likely first.

    Every emulator path keeps its version as a wildcard (MuMu's nx_device/*,
    LDPlayer's LDPlayer*), so an emulator update does not invalidate it.
    """
    home = os.environ.get("USERPROFILE") or str(Path.home())
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    program_dirs = []
    for key, fallback in (("ProgramFiles", r"C:\Program Files"),
                          ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                          ("ProgramW6432", r"C:\Program Files")):
        directory = os.environ.get(key) or fallback
        if directory not in program_dirs:
            program_dirs.append(directory)

    patterns = []

    def add(*parts):
        pattern = os.path.join(*parts)
        if pattern not in patterns:
            patterns.append(pattern)

    sdk_roots = [os.environ.get("ANDROID_SDK_ROOT"), os.environ.get("ANDROID_HOME"),
                 os.path.join(local, "Android", "Sdk"),
                 os.path.join(home, "AppData", "Local", "Android", "Sdk"),
                 r"C:\Android\Sdk", r"C:\Android\android-sdk",
                 os.path.join(home, "Android", "Sdk"), "/opt/android-sdk"]
    for root in sdk_roots:
        if not root:
            continue
        add(root, "platform-tools", "adb.exe")
        add(root, "platform-tools", "adb")

    for directory in program_dirs:
        # NetEase MuMu keeps adb under nx_device/<version>/shell; glob the version.
        add(directory, "NetEase", "MuMu*", "nx_device", "*", "shell", "adb.exe")
        add(directory, "NetEase", "MuMu*", "shell", "adb.exe")
        add(directory, "MuMu*", "nx_device", "*", "shell", "adb.exe")
        add(directory, "MuMu*", "shell", "adb.exe")
        add(directory, "LDPlayer*", "adb.exe")
        add(directory, "BlueStacks_nxt", "HD-Adb.exe")
        add(directory, "BlueStacks", "HD-Adb.exe")
        add(directory, "Nox", "bin", "adb.exe")
        add(directory, "Nox*", "bin", "adb.exe")
        add(directory, "Microvirt", "MEmu", "adb.exe")
        add(directory, "Genymobile", "Genymotion", "tools", "adb.exe")
        add(directory, "MiPhoneAssistant*", "platform-tools", "adb.exe")

    add(os.path.join(local, "Android", "Sdk", "platform-tools", "adb.exe"))
    add(os.path.join(local, "MiPhoneAssistant*", "platform-tools", "adb.exe"))
    add(os.path.join(home, "scoop", "apps", "adb", "current", "adb.exe"))
    add(r"C:\ProgramData\chocolatey\bin\adb.exe")
    add(os.path.join(home, "LDPlayer*", "adb.exe"))
    add(os.path.join(local, "Android", "Sdk", "platform-tools", "adb"))
    add(os.path.join(home, "Android", "Sdk", "platform-tools", "adb"))
    for root in ("/usr/local/bin", "/usr/bin", "/opt/homebrew/bin", "/snap/bin"):
        add(root, "adb")
    return patterns


def candidate_paths():
    """Yield existing adb files from the known locations, best guess first."""
    seen = set()
    for pattern in search_patterns():
        try:
            matches = glob.glob(pattern)
        except (OSError, ValueError):
            continue
        for match in sorted(matches):
            key = os.path.normcase(os.path.abspath(match))
            if key in seen:
                continue
            seen.add(key)
            path = Path(match)
            if path.is_file():
                yield path


def locate_adb(explicit=None, required=False):
    """Return the adb executable to use for this run.

    explicit is the --adb value, or None to fall back to the environment
    (CROSSCORE_ADB then ADB), PATH and the known install locations. A
    named-but-unusable explicit value raises regardless of required. With
    required=True a genuinely missing adb raises AdbNotFoundError whose text
    names --adb and CROSSCORE_ADB; with required=False the string 'adb' is
    returned as a last resort.
    """
    if explicit:
        resolved = _resolve_value(explicit)
        if resolved:
            return resolved
        raise _failure(f"The --adb value does not point at a usable adb executable: {explicit!r}")
    for name in ("CROSSCORE_ADB", "ADB"):
        value = os.environ.get(name)
        if value:
            resolved = _resolve_value(value)
            if resolved:
                return resolved
            raise _failure(f"Environment variable {name} is set to {value!r}, "
                           "which is not a usable adb executable.")
    found = shutil.which("adb")
    if found:
        return found
    for path in candidate_paths():
        return str(path)
    if required:
        raise _failure("No adb executable was found on PATH or in any known SDK/emulator location.")
    return "adb"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Print the adb executable this project would use.")
    parser.add_argument("--adb", help="explicit adb path (highest precedence)")
    parser.add_argument("--required", action="store_true",
                        help="exit non-zero when no adb is found instead of printing 'adb'")
    args = parser.parse_args(argv)
    try:
        print(locate_adb(args.adb, required=args.required))
    except AdbNotFoundError as error:
        parser.exit(2, str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
