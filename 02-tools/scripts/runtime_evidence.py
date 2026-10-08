"""Record and audit real-device runtime verification evidence (plan item D2).

Why this exists
---------------
90-notes/feature-coverage.json and parity-report.json are static registration counts: they
say a handler exists, not that the client ever received and consumed its reply.  D2 requires
one record per request that was actually exercised on the emulator, each tying together the
client-side log line and the server-side event line.  That file is the single source of
truth for the runtime_verified counter.

Evidence contract (all fields mandatory for an accepted record):
  request      protocol name, e.g. 'AbilityProto:AddAbility'
  uid          local account uid that was exercised
  client_log   the client-side proof: logcat line / screenshot path / Lua stack excerpt
  server_event the matching event from 07-server/logs/server.jsonl
  outcome      'ok' | 'rejected' | 'loading_blocked' - 'ok' means the client consumed the
               reply and the UI advanced
  note         what the operator saw; free text, never a guess presented as fact

Usage:
  python -B 02-tools/scripts/runtime_evidence.py list
  python -B 02-tools/scripts/runtime_evidence.py add --request AbilityProto:AddAbility \
      --uid 900000002 --client-log "logcat:PlayerAbility_Add ok" \
      --server-event '{"event":"response","name":"AbilityProto:GetAbilityRet"}' --outcome ok
  python -B 02-tools/scripts/runtime_evidence.py verify
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
TARGET = os.path.join(ROOT, "90-notes", "runtime-evidence.json")
SCHEMA_VERSION = 1
OUTCOMES = ("ok", "rejected", "loading_blocked")


def load():
    if not os.path.isfile(TARGET):
        return {"schema_version": SCHEMA_VERSION, "evidence": []}
    with open(TARGET, "r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data.get("evidence"), list):
        raise SystemExit("runtime-evidence.json has no evidence list")
    return data


def save(data):
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with open(TARGET, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)


def fingerprint(data):
    """Recomputable digest of the evidence list, independent of file formatting."""
    canonical = json.dumps(data["evidence"], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def command_add(args):
    data = load()
    for name, value in (("request", args.request), ("client_log", args.client_log),
                        ("server_event", args.server_event)):
        if not value or not str(value).strip():
            raise SystemExit("refusing to record evidence without " + name)
    if args.outcome not in OUTCOMES:
        raise SystemExit("outcome must be one of " + ", ".join(OUTCOMES))
    local = datetime.now(timezone(timedelta(hours=8))).isoformat()
    record = {"request": args.request, "uid": int(args.uid),
              "client_log": args.client_log.strip(), "server_event": args.server_event.strip(),
              "outcome": args.outcome, "note": (args.note or "").strip(), "recorded_at": local}
    replaced = [row for row in data["evidence"]
                if not (row.get("request") == record["request"] and row.get("uid") == record["uid"])]
    replaced.append(record)
    data["evidence"] = sorted(replaced, key=lambda row: (str(row.get("request")), int(row.get("uid", 0))))
    data["schema_version"] = SCHEMA_VERSION
    data["updated_at"] = local
    data["evidence_sha256"] = fingerprint(data)
    data["method"] = ("每条的 client_log 与 server_event 必须来自同一次实机操作；本文件只记录"
                      "被真实消费过的请求，未验证的请求不写入。")
    save(data)
    print(json.dumps({"recorded": record["request"], "uid": record["uid"],
                      "evidence_count": len(data["evidence"])}, ensure_ascii=False))


def command_list(args):
    data = load()
    for row in data["evidence"]:
        print("%-42s uid=%-10s %-14s %s" % (row.get("request"), row.get("uid"),
                                            row.get("outcome"), row.get("note", "")))


def command_verify(args):
    if not os.path.isfile(TARGET):
        # Materialize the sealed empty ledger so parity_report.py always sees a real source
        # file and "0 verified" reads as "nothing recorded yet", not "evidence system absent".
        empty = {"schema_version": SCHEMA_VERSION, "evidence": [],
                 "updated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
                 "method": ("每条的 client_log 与 server_event 必须来自同一次实机操作；本文件只记录"
                            "被真实消费过的请求，未验证的请求不写入。")}
        empty["evidence_sha256"] = fingerprint(empty)
        save(empty)
    data = load()
    expected = fingerprint(data)
    actual = data.get("evidence_sha256")
    ok = expected == actual
    print(json.dumps({"evidence_count": len(data["evidence"]),
                      "distinct_requests": len({row.get("request") for row in data["evidence"]}),
                      "digest_matches": ok,
                      "recomputed_sha256": expected,
                      "stored_sha256": actual}, ensure_ascii=False))
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    add = sub.add_parser("add", help="append or replace one verified record")
    add.add_argument("--request", required=True)
    add.add_argument("--uid", required=True)
    add.add_argument("--client-log", required=True, dest="client_log")
    add.add_argument("--server-event", required=True, dest="server_event")
    add.add_argument("--outcome", default="ok")
    add.add_argument("--note", default="")
    add.set_defaults(func=command_add)
    listing = sub.add_parser("list", help="print recorded evidence")
    listing.set_defaults(func=command_list)
    verify = sub.add_parser("verify", help="recompute the digest and counts")
    verify.set_defaults(func=command_verify)
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
