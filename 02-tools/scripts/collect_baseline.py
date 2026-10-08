"""Collect a local APK/device baseline without launching the game."""
import argparse
import hashlib
import importlib.util
import json
import shutil
import struct
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from adb_locator import locate_adb  # noqa: E402

DEVICE_ROOT = "/sdcard/Android/data/com.megagame.crosscore/files"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digests(path):
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            sha.update(chunk)
            md5.update(chunk)
    return {"bytes": path.stat().st_size, "sha256": sha.hexdigest(), "md5": md5.hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk", type=Path, default=ROOT.parent / "crosscore_3.3.0/base.apk")
    parser.add_argument("--serial", default="127.0.0.1:16384")
    parser.add_argument("--skip-device", action="store_true")
    parser.add_argument("--adb", default=None,
                        help="adb executable (default: auto-detect, $CROSSCORE_ADB then $ADB)")
    args = parser.parse_args()
    adb_executable = locate_adb(args.adb, required=not args.skip_device)
    for relative in ["00-official", "01-device/files", "01-device/logs", "01-device/recon-archive",
                     "02-tools/mitmproxy", "02-tools/frida", "02-tools/android-build-tools",
                     "02-tools/keystore", "03-unpack/apk", "03-unpack/packs", "03-unpack/lua",
                     "04-capture/flows", "04-capture/decoded", "04-capture/logs",
                     "05-protocol/samples", "06-client", "90-notes"]:
        (ROOT / relative).mkdir(parents=True, exist_ok=True)
    source = args.apk.resolve()
    baseline = ROOT / "00-official/base.apk"
    if not baseline.exists():
        shutil.copyfile(source, baseline)
    source_hash = digests(source)
    baseline_hash = digests(baseline)
    if source_hash != baseline_hash:
        raise RuntimeError("Source APK and baseline differ; refusing to replace baseline")
    if baseline_hash["md5"] != "ba6dc1bb1624cbf73aa9b0418c051912":
        raise RuntimeError("APK differs from supplied baseline MD5")
    baseline.chmod(0o444)
    report = {"observed_local_time": datetime.now().isoformat(timespec="seconds"),
              "apk_source": str(source), "apk_baseline": str(baseline), "apk": baseline_hash,
              "serial": args.serial, "game_launched_by_collector": False,
              "network_settings_modified": False, "official_server_requests": False}
    print("APK copied and hashes verified", flush=True)
    with zipfile.ZipFile(baseline) as archive:
        inventory = [{"name": info.filename, "bytes": info.file_size, "compressed_bytes": info.compress_size,
                      "crc32": f"{info.CRC:08x}"} for info in archive.infolist()]
        write_json(ROOT / "00-official/apk-entries.json", inventory)
        targets = ["AndroidManifest.xml", "assets/MJEnv.txt", "assets/com.megagame.crosscore.cert.pem",
                   "lib/arm64-v8a/libil2cpp.so", "lib/arm64-v8a/libxlua.so"]
        targets.extend(i.filename for i in archive.infolist() if i.filename.endswith("global-metadata.dat"))
        packs = sorted((i for i in archive.infolist() if i.filename.startswith("assets/packs/")
                        and i.filename.endswith(".bs")), key=lambda i: i.file_size)
        targets.extend(i.filename for i in packs[:2])
        report["pack_count"] = len(packs)
        report["zip_entries"] = len(inventory)
        for name in dict.fromkeys(targets):
            output = ROOT / "03-unpack/apk" / name
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as src, output.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    for item in [ROOT.parent / "_recon", ROOT.parent / "_recon_apk.py", ROOT.parent / "_recon_extract.py"]:
        if item.is_dir():
            shutil.copytree(item, ROOT / "01-device/recon-archive" / item.name, dirs_exist_ok=True)
        elif item.is_file():
            shutil.copyfile(item, ROOT / "01-device/recon-archive" / item.name)
    if not args.skip_device:
        def adb(*arguments, timeout=120):
            return subprocess.run([adb_executable, "-s", args.serial, *arguments], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=timeout)
        commands = {
            "identity": "id; getprop ro.product.cpu.abi; getprop ro.build.version.release",
            "package": "dumpsys package com.megagame.crosscore",
            "idle-network": "ss -tnp",
            "proxy": "settings get global http_proxy",
            "pid": "pidof com.megagame.crosscore",
            "root-files": f"find {DEVICE_ROOT} -maxdepth 1 -type f",
            "cache-files": "find /data/data/com.megagame.crosscore/cache -maxdepth 2 -type f",
        }
        logs = {}
        for label, command in commands.items():
            result = adb("shell", command)
            (ROOT / "01-device/logs" / f"{label}.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
            logs[label] = {"command": command, "exit_code": result.returncode}
        report["device_commands"] = logs
        # SDK backup may contain account state; keep it local, never print contents.
        paths = ["Custom/luascripts", "Custom/fixluascripts"]
        for directory in ["il2cpp", "CN1001"]:
            result = adb("shell", f"find {DEVICE_ROOT}/{directory} -type f")
            if result.returncode != 0:
                raise RuntimeError(f"Cannot inventory device directory {directory}")
            paths.extend(p[len(DEVICE_ROOT) + 1:] for p in result.stdout.splitlines()
                         if p.startswith(DEVICE_ROOT + "/"))
        paths.extend(Path(p).name for p in (ROOT / "01-device/logs/root-files.txt").read_text(encoding="utf-8").splitlines()
                     if Path(p).name.startswith("0_") or Path(p).name.startswith("internation"))
        pulls = []
        for relative in dict.fromkeys(paths):
            destination = ROOT / "01-device/files" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            result = adb("pull", f"{DEVICE_ROOT}/{relative}", str(destination), timeout=300)
            pulls.append({"source": f"{DEVICE_ROOT}/{relative}", "destination": str(destination),
                          "exit_code": result.returncode, "output": result.stdout + result.stderr})
        write_json(ROOT / "01-device/pull-results.json", pulls)
        report["pulls_succeeded"] = sum(p["exit_code"] == 0 for p in pulls)
        report["pulls_failed"] = sum(p["exit_code"] != 0 for p in pulls)
        metadata = ROOT / "01-device/files/il2cpp/Metadata/global-metadata.dat"
        if metadata.exists():
            magic, version = struct.unpack("<II", metadata.read_bytes()[:8])
            report["device_metadata"] = {**digests(metadata), "magic": f"0x{magic:08X}",
                                         "version": version, "standard_magic": magic == 0xFAB11BAF}
        hashes = [{"path": str(p.relative_to(ROOT / "01-device/files")), **digests(p)}
                  for p in sorted((ROOT / "01-device/files").rglob("*")) if p.is_file()]
        write_json(ROOT / "01-device/files-manifest.json", hashes)
    modules = {}
    for name in ["UnityPy", "Crypto", "androguard", "google.protobuf", "frida", "mitmproxy", "requests"]:
        try:
            modules[name] = importlib.util.find_spec(name) is not None
        except ModuleNotFoundError:
            modules[name] = False
    report["python"] = {"executable": sys.executable, "version": sys.version, "available_modules": modules}
    write_json(ROOT / "00-official/baseline.json", report)
    print(json.dumps({key: report[key] for key in ["apk", "zip_entries", "pack_count", "device_metadata",
                      "pulls_succeeded", "pulls_failed"] if key in report}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
