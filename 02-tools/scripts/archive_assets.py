"""Archive device resource directories locally, excluding SDK/account directories."""
import hashlib
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from adb_locator import locate_adb  # noqa: E402

SOURCE = "/sdcard/Android/data/com.megagame.crosscore/files"
DIRECTORIES = ["Custom", "sounds", "il2cpp"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--videos-only', action='store_true')
    parser.add_argument('--adb', default=None,
                        help='adb executable (default: auto-detect, $CROSSCORE_ADB then $ADB)')
    args = parser.parse_args()
    adb = locate_adb(args.adb, required=True)
    directories = ['videos'] if args.videos_only else DIRECTORIES
    name = 'assets-videos' if args.videos_only else 'assets-preservation'
    output = ROOT / ('01-device/' + name + '.tar')
    manifest = output.with_suffix(".json")
    if output.exists():
        raise RuntimeError("Archive already exists; preserve it instead of overwriting")
    partial = output.with_suffix(".tar.partial")
    digest, total, started, last_report = hashlib.sha256(), 0, time.monotonic(), 0
    command = [adb, "-s", "127.0.0.1:16384", "exec-out", "tar", "-C", SOURCE, "-cf", "-", *directories]
    errors = ROOT / ('01-device/logs/' + name + '-stderr.txt')
    with errors.open("wb") as err, partial.open("wb") as dest:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=err)
        while True:
            chunk = process.stdout.read(4 * 1024 * 1024)
            if not chunk:
                break
            dest.write(chunk)
            digest.update(chunk)
            total += len(chunk)
            now = time.monotonic()
            if now - last_report >= 20:
                print(json.dumps({"copied_bytes": total, "elapsed_seconds": round(now - started)}), flush=True)
                last_report = now
        code = process.wait()
    if code:
        raise RuntimeError(f"Device tar failed with {code}; partial preserved, inspect {errors}")
    partial.replace(output)
    result = {"source": SOURCE, "directories": directories, "archive": str(output),
              "bytes": total, "sha256": digest.hexdigest(), "duration_seconds": round(time.monotonic() - started, 2),
              "account_directories_included": False, "complete_instance_backup": False}
    manifest.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
