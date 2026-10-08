"""Install this project's user CA and save/restore the emulator's proxy state."""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from cryptography import x509

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from adb_locator import locate_adb  # noqa: E402

# Resolved from --adb / CROSSCORE_ADB / auto-detection in main(); never hard-coded.
ADB = None
SERIAL = "127.0.0.1:16384"
STATE = ROOT / "04-capture/logs/proxy-state.json"
PROXY_KEYS = ['global_http_proxy_host', 'global_http_proxy_port',
              'global_http_proxy_exclusion_list', 'global_proxy_pac_url']


def adb(*args, check=True):
    return subprocess.run([ADB, "-s", SERIAL, *args], check=check, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=30)


def main():
    global ADB
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["apply", "restore"])
    parser.add_argument("--adb", default=None,
                        help="adb executable (default: auto-detect, $CROSSCORE_ADB then $ADB)")
    args = parser.parse_args()
    ADB = locate_adb(args.adb, required=True)
    if args.action == "restore":
        state = json.loads(STATE.read_text(encoding="utf-8"))
        previous = state["previous_proxy"]
        if previous == "null":
            # Deleting http_proxy alone leaves Android's derived global proxy
            # active. :0 sends the observed reset before removing the legacy key.
            adb("shell", "settings put global http_proxy :0")
            adb("shell", "settings delete global http_proxy")
        else:
            adb("shell", "settings", "put", "global", "http_proxy", previous)
        for key, value in state.get('previous_proxy_settings', {}).items():
            if value == 'null':
                adb('shell', 'settings', 'delete', 'global', key)
            else:
                adb('shell', 'settings', 'put', 'global', key, value)
        adb("reverse", "--remove", "tcp:8080", check=False)
        if state["certificate_created"]:
            adb("shell", "rm", state["certificate_device_path"])
        state["restored"] = True
        STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        print("Previous proxy restored; project CA removed")
        return
    if STATE.exists() and not json.loads(STATE.read_text(encoding="utf-8")).get("restored"):
        raise RuntimeError("Proxy state already exists; restore before applying again")
    pem = ROOT / "02-tools/mitmproxy/conf/mitmproxy-ca-cert.pem"
    cert = x509.load_pem_x509_certificate(pem.read_bytes())
    subject_hash = f"{int.from_bytes(hashlib.md5(cert.subject.public_bytes()).digest()[:4], 'little'):08x}"
    device_path = f"/data/misc/user/0/cacerts-added/{subject_hash}.0"
    existing = adb("shell", "test", "-e", device_path, check=False).returncode == 0
    if existing:
        raise RuntimeError("Certificate path already exists; refusing to overwrite it")
    reverse = adb("reverse", "--list").stdout
    if "tcp:8080" in reverse:
        raise RuntimeError("An existing ADB reverse rule uses tcp:8080")
    state = {"previous_proxy": adb("shell", "settings get global http_proxy").stdout.strip(),
             'previous_proxy_settings': {key: adb('shell', 'settings', 'get', 'global', key).stdout.strip()
                                         for key in PROXY_KEYS},
             "previous_reverse_rules": reverse, "certificate_device_path": device_path,
             "certificate_created": False, "restored": False}
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    adb("shell", "mkdir -p /data/misc/user/0/cacerts-added")
    adb("push", str(pem), device_path)
    state["certificate_created"] = True
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    adb("shell", f"chown system:system {device_path}; chmod 644 {device_path}; chcon u:object_r:system_data_file:s0 {device_path}")
    adb("reverse", "tcp:8080", "tcp:8080")
    adb("shell", "settings put global http_proxy 127.0.0.1:8080")
    print(json.dumps({"proxy": "127.0.0.1:8080", "ca_hash": subject_hash, "saved_state": str(STATE)}))


if __name__ == "__main__":
    main()
