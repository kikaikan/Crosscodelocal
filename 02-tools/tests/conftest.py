"""Keep pytest's temporary directory usable when the host default is not.

pytest derives its session base temp from the system temp directory. If that
directory was left behind by another tool with ownership this user cannot even
list, the built-in tmp_path fixture fails before any test body runs. Probe the
host default and fall back to a fresh writable base temp only when it really is
unusable; an explicit --basetemp always wins, and hosts with a healthy temp
directory keep pytest's own default.
"""
import os
import tempfile
from pathlib import Path


def host_default(config):
    """The directory pytest would use without help: <temp>/pytest-of-<user>."""
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return Path(tempfile.gettempdir()) / ("pytest-of-" + user)


def usable(directory):
    try:
        directory.mkdir(parents=True, exist_ok=True)
        next(directory.iterdir(), None)
    except OSError:
        return False
    return True


def pytest_configure(config):
    if config.option.basetemp is not None:
        return
    if not usable(host_default(config)):
        config.option.basetemp = Path(tempfile.mkdtemp(prefix="pytest-of-"))
