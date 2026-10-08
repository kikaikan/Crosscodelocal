#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wait_contracts.py -- 生成"客户端等待语义表"（静态机械推导，仅标准库）。

只读输入：
  05-protocol/endpoints.json                (1304 条 schema)
  90-notes/feature-coverage.json            (493 条 client-sendable 请求，已算好)
  03-unpack/lua/device-luascripts/*.lua     (客户端 Lua 源码)

只写：
  07-server/data/wait-contracts.json
  90-notes/wait-contracts.md

不启动服务、不接触模拟器、不修改 07-server 下任何源码。
输出除 generated_at 外均为确定性结果（排序稳定）。

用法：
  python -B 02-tools/scripts/wait_contracts.py                 # 生成下面两个默认输出
  python -B 02-tools/scripts/wait_contracts.py --help          # 查看参数
  python -B 02-tools/scripts/wait_contracts.py --json-output <path> --md-output <path>

参数由 argparse 解析：--help 正常生效，未知参数直接报错退出（退出码 2），
不会再被静默忽略后仍然全量写入。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

SCHEMA_VERSION = 1
EXPECTED_REQUESTS = 493

ROOT = Path(__file__).resolve().parents[2]
ENDPOINTS = ROOT / "05-protocol" / "endpoints.json"
FEATURE = ROOT / "90-notes" / "feature-coverage.json"
LUA_DIR = ROOT / "03-unpack" / "lua" / "device-luascripts"
OUT_JSON = ROOT / "07-server" / "data" / "wait-contracts.json"
OUT_MD = ROOT / "90-notes" / "wait-contracts.md"

METHOD = (
    "静态机械推导：以 90-notes/feature-coverage.json 的 493 条 client-sendable 请求为行集，"
    "在 05-protocol/endpoints.json 的全部 1304 条 schema 名上做前缀枚举得到 response_candidates，"
    "再在 03-unpack/lua/device-luascripts/*.lua 全文里精确匹配响应本地名的 Lua 函数定义"
    "（function <Proto>:<LocalName>( 或 function <LocalName>(），据此给出 wait_semantics 三值标签。"
    "本文件是源码文本层面的机械统计，不是运行期证据；不含服务端实际回包行为，"
    "也不含 NetWait / Loading_Weight / 超时的解除时机。"
)

# Lua 函数定义精确匹配：只接受标识符整体匹配，不做模糊/子串匹配。
METHOD_RE = re.compile(r"\bfunction\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")
FUNC_RE = re.compile(r"\bfunction\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")

SEMANTICS_RET_HANDLER = "ret_schema_client_handler"
SEMANTICS_RET_NO_HANDLER = "ret_schema_no_client_handler"
SEMANTICS_NO_RET = "no_ret_schema"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rel_posix(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def lua_manifest_digest(files):
    """对 Lua 源集合的确定性摘要：按文件名排序，逐个 (relpath, sha256) 喂入。"""
    h = hashlib.sha256()
    for rel in files:
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(sha256_file(LUA_DIR / rel).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def scan_lua(files):
    """扫描全部 Lua 文件，建立函数定义索引。

    返回：
      method_index[(proto, local)] -> [(rel, line), ...]  来自 function <proto>:<local>(
      func_index[local]          -> [(rel, line), ...]  来自 function <local>(
    """
    method_index = {}
    func_index = {}
    for rel in files:
        path = LUA_DIR / rel
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    if "function" not in line:
                        continue
                    for m in METHOD_RE.finditer(line):
                        method_index.setdefault((m.group(1), m.group(2)), []).append((rel, lineno))
                    for m in FUNC_RE.finditer(line):
                        func_index.setdefault(m.group(1), []).append((rel, lineno))
        except OSError as exc:
            print("warning: cannot read %s: %s" % (rel, exc), file=sys.stderr)
    return method_index, func_index


def find_handlers(candidate, method_index, func_index):
    if ":" in candidate:
        proto, local = candidate.split(":", 1)
    else:
        proto, local = None, candidate
    hits = []
    if proto is not None:
        for rel, line in method_index.get((proto, local), []):
            hits.append({"response": candidate, "file": rel, "line": line,
                         "match": "proto_method"})
    for rel, line in func_index.get(local, []):
        hits.append({"response": candidate, "file": rel, "line": line,
                     "match": "bare_function"})
    return hits


def build_requests(fc_requests, ep_by_name, names_sorted, method_index, func_index):
    records = []
    for req in fc_requests:
        name = req["name"]
        schema = ep_by_name.get(name, {})
        candidates = sorted(
            n for n in names_sorted
            if len(n) > len(name) and n.startswith(name)
        )
        handlers = []
        for cand in candidates:
            handlers.extend(find_handlers(cand, method_index, func_index))
        seen = set()
        uniq = []
        for h in handlers:
            key = (h["response"], h["file"], h["line"], h["match"])
            if key not in seen:
                seen.add(key)
                uniq.append(h)
        handlers = sorted(uniq, key=lambda h: (h["response"], h["file"], h["line"], h["match"]))

        if not candidates:
            semantics = SEMANTICS_NO_RET
        elif handlers:
            semantics = SEMANTICS_RET_HANDLER
        else:
            semantics = SEMANTICS_RET_NO_HANDLER

        send_sources = sorted(
            ({"file": s.get("file"), "line": s.get("line")}
             for s in schema.get("send_sources", [])),
            key=lambda s: (str(s["file"]), s["line"] if s["line"] is not None else -1),
        )
        implementation = req.get("implementation")
        records.append({
            "name": name,
            "opcode": req.get("opcode"),
            "implemented": implementation is not None,
            "implementation": implementation,
            "observed_request": bool(req.get("observed_request")),
            "send_sources": send_sources,
            "response_candidates": candidates,
            "client_handlers": handlers,
            "wait_semantics": semantics,
        })
    records.sort(key=lambda r: (r["opcode"] if r["opcode"] is not None else -1, r["name"]))
    return records


def compute_counts(records, known_client_requests):
    return {
        "requests": len(records),
        "known_client_requests_declared": known_client_requests,
        "implemented": sum(1 for r in records if r["implemented"]),
        "unimplemented": sum(1 for r in records if not r["implemented"]),
        "observed_request": sum(1 for r in records if r["observed_request"]),
        "with_ret_schema": sum(1 for r in records if r["response_candidates"]),
        "with_client_handler": sum(1 for r in records if r["client_handlers"]),
        "no_ret_schema": sum(1 for r in records if r["wait_semantics"] == SEMANTICS_NO_RET),
        "ret_schema_client_handler": sum(
            1 for r in records if r["wait_semantics"] == SEMANTICS_RET_HANDLER),
        "ret_schema_no_client_handler": sum(
            1 for r in records if r["wait_semantics"] == SEMANTICS_RET_NO_HANDLER),
        "response_candidates_total": sum(len(r["response_candidates"]) for r in records),
        "client_handlers_total": sum(len(r["client_handlers"]) for r in records),
    }


def human_examples(records):
    out = {}
    for key in (SEMANTICS_NO_RET, SEMANTICS_RET_NO_HANDLER, SEMANTICS_RET_HANDLER):
        out[key] = [r for r in records if r["wait_semantics"] == key][:3]
    return out


def render_md(generated_at, inputs, counts, records):
    ex = human_examples(records)
    lines = []
    add = lines.append
    add("# 客户端等待语义表（wait-contracts）")
    add("")
    add("> 静态机械推导，**不是运行期证据**。复算：`python -B 02-tools/scripts/wait_contracts.py`")
    add("")
    add("- 生成时间（UTC）：`%s`" % generated_at)
    add("- 数据文件：`07-server/data/wait-contracts.json`（schema_version=%d）" % SCHEMA_VERSION)
    add("- 行集：`90-notes/feature-coverage.json` 的 493 条 client-sendable 请求")
    add("")
    add("## 1. 方法与输入")
    add("")
    add(METHOD)
    add("")
    add("输入及其 sha256（Lua 为按文件名排序后的清单摘要）：")
    add("")
    add("| 输入 | sha256 | 备注 |")
    add("| --- | --- | --- |")
    for item in inputs:
        add("| `%s` | `%s` | %s |" % (item["path"], item["sha256"], item.get("note", "")))
    add("")
    add("## 2. 字段含义")
    add("")
    add("| 字段 | 含义 |")
    add("| --- | --- |")
    add("| `name` | 请求的协议名（与 endpoints.json / feature-coverage.json 一致） |")
    add("| `opcode` | 该请求的消息号 |")
    add("| `implemented` / `implementation` | 直接取自 feature-coverage.json；非空表示 07-server 已注册处理函数 |")
    add("| `observed_request` | 直接取自 feature-coverage.json；是否为抓包观测到的 c2s 请求 |")
    add("| `send_sources` | endpoints.json 记录的客户端 Lua 调用点（file+line） |")
    add("| `response_candidates` | endpoints.json 中所有以本条请求名为前缀且更长的 schema 名（排序去重） |")
    add("| `client_handlers` | 在 device-luascripts/*.lua 全文精确命中 `function <Proto>:<LocalName>(` 或 `function <LocalName>(` 的记录（response/file/line/match） |")
    add("| `wait_semantics` | 三值标签，仅表示“客户端是否存在该 *Ret 的 Lua 处理函数”，见下 |")
    add("")
    add("`wait_semantics` 判定规则（机械，无自由发挥）：")
    add("")
    add("- `no_ret_schema`：`response_candidates` 为空（endpoints.json 里没有任何更长前缀 schema）。")
    add("- `ret_schema_client_handler`：有候选名，且其中至少一个命中客户端 Lua 处理函数。")
    add("- `ret_schema_no_client_handler`：有候选名，但没有任何命中。")
    add("")
    add("## 3. 统计")
    add("")
    add("| 计数 | 数值 |")
    add("| --- | --- |")
    for k in ("requests", "known_client_requests_declared",
              "rowset_client_send_literal_true", "rowset_observed_c2s",
              "implemented", "unimplemented",
              "observed_request", "with_ret_schema", "with_client_handler",
              "no_ret_schema", "ret_schema_client_handler", "ret_schema_no_client_handler",
              "response_candidates_total", "client_handlers_total"):
        add("| `%s` | %d |" % (k, counts[k]))
    add("")
    add("对账：`requests == known_client_requests_declared == 493`；"
        "行集 == endpoints.json 中（`client_send_literal == true` 或 `observed.directions` 含 "
        "`c2s`）的并集（%d 条），并与 feature-coverage.json 的 493 行逐名相等（脚本内断言）；"
        "其中 `client_send_literal == true` 有 %d 条、`observed.directions` 含 `c2s` 有 %d 条。"
        % (counts["requests"], counts["rowset_client_send_literal_true"],
           counts["rowset_observed_c2s"]))
    add("")
    add("## 4. 代表性示例（每类取排序后前 3 条）")
    add("")
    for key in (SEMANTICS_NO_RET, SEMANTICS_RET_NO_HANDLER, SEMANTICS_RET_HANDLER):
        add("### `%s`" % key)
        add("")
        picks = ex[key]
        if not picks:
            add("（无）")
            add("")
            continue
        for r in picks:
            cand = "、".join("`%s`" % c for c in r["response_candidates"]) or "（无）"
            if r["client_handlers"]:
                h = r["client_handlers"][0]
                htxt = "命中 `%s` @ `%s:%d` (%s)" % (
                    h["response"], h["file"], h["line"], h["match"])
            else:
                htxt = "无命中"
            add("- `%s` (opcode %s)：候选 = %s；%s" % (r["name"], r["opcode"], cand, htxt))
        add("")
    add("## 5. 边界与未验证项（必读）")
    add("")
    add("1. **`wait_semantics` 不是“卡不卡”的结论。** 它只表示“客户端是否存在该 *Ret 的 Lua 处理函数”。"
        "命中处理函数 **不等于** 服务端回 SystemProto:Tips 时该界面一定不会卡（处理函数可能只更新部分状态，"
        "等待可能由别处解除）；没有处理函数 **也不等于** 一定卡（请求可能是 fire-and-forget，"
        "或状态由后续推送消息驱动）。")
    add("2. **NetWait / Loading_Weight 的真实解除时机没有运行期证据。** 在 `03-unpack/lua/device-luascripts` 中，"
        "`NetWait` 只出现在 `cfglauncher.lua`、`cfgview.lua`、`EquipRefining.lua`；"
        "`Loading_Weight_Apply/Update` 由 `BattleMgr.lua`、`FightClient.lua`、`GuideMgr.lua`、"
        "`LoadingView.lua`、`HideLoading.lua`、`MatrixView.lua`、`RogueMapBattleMgr.lua` "
        "以字符串 key（如 `loading_weight_key`、`matrix_scene_enter`）驱动。"
        "二者都**不以协议请求名索引**，因此无法从本数据机械地推出“某条请求的等待由哪个 *Ret 解除”。")
    add("3. **本表不含任何超时/权重字段**，也不含服务端实际是否回 *Ret 的行为；`implemented` 只说明 07-server "
        "是否注册了处理函数（来自 feature-coverage.json），不代表该处理函数一定回 *Ret。")
    add("4. **裸函数名匹配可能跨 Proto。** 按规则 `function <LocalName>(` 也会记录，"
        "故 `client_handlers` 中 `match == \"bare_function\"` 的条目可能来自与请求不同模块的同名函数，"
        "人工核对时优先看 `proto_method` 条目。")
    add("5. **前缀候选可能命中另一条请求。** 部分 `response_candidates` 本身就是另一个 client-sendable 请求"
        "（例如 `FightProtocol:Quit` -> `FightProtocol:QuitDuplicate`），按机械规则保留。")
    add("6. **待人工核对：当前静态证据不足以列出“客户端确实有等待”的请求清单，故该清单为空（有意留空）。** "
        "要确认某条请求的等待语义，需要运行期证据（抓包/断点：请求发出后客户端是否重试、是否阻塞输入、"
        "界面是否停留在 loading，以及 *Ret 到达前后 NetWait/Loading_Weight 的变化）。"
        "在拿到这类证据前，任何按请求名书写的“等待”字段都是臆造，故不写入 JSON。")
    add("7. 机械上最值得人工核对的一组是 `no_ret_schema`（%d 条）：客户端在 endpoints.json 中"
        "根本没有可对应的 *Ret schema。完整名单见 JSON 中 `wait_semantics == \"no_ret_schema\"` 的条目。"
        % counts["no_ret_schema"])
    add("")
    add("## 6. 复算")
    add("")
    add("```")
    add("python -B 02-tools/scripts/wait_contracts.py")
    add("```")
    add("")
    return "\n".join(lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="wait_contracts.py",
        description="生成客户端等待语义表：只读 endpoints.json / feature-coverage.json / Lua，"
                    "写 JSON 与 Markdown 两个输出。",
        epilog="无参数时写入默认路径；未知参数会报错退出（退出码 2）。")
    parser.add_argument("--json-output", type=Path, default=OUT_JSON,
                        help="JSON 输出路径（默认：%(default)s）")
    parser.add_argument("--md-output", type=Path, default=OUT_MD,
                        help="Markdown 输出路径（默认：%(default)s）")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    for path in (ENDPOINTS, FEATURE):
        if not path.is_file():
            print("ERROR: missing input %s" % path, file=sys.stderr)
            return 2
    if not LUA_DIR.is_dir():
        print("ERROR: missing Lua dir %s" % LUA_DIR, file=sys.stderr)
        return 2

    ep = json.loads(ENDPOINTS.read_text(encoding="utf-8"))
    fc = json.loads(FEATURE.read_text(encoding="utf-8"))

    schemas = ep["schemas"]
    ep_by_name = {s["name"]: s for s in schemas}
    names_sorted = sorted(ep_by_name)
    fc_requests = fc["requests"]
    known = fc.get("known_client_requests")

    if len(fc_requests) != EXPECTED_REQUESTS or known != EXPECTED_REQUESTS:
        print("ERROR: request row set mismatch: len=%d known_client_requests=%s expected=%d"
              % (len(fc_requests), known, EXPECTED_REQUESTS), file=sys.stderr)
        return 1
    missing = [r["name"] for r in fc_requests if r["name"] not in ep_by_name]
    if missing:
        print("ERROR: %d request names missing from endpoints.json: %s"
              % (len(missing), ", ".join(missing[:5])), file=sys.stderr)
        return 1

    # 行集筛选规则（与 feature-coverage.json 的产出规则一致）：
    #   client_send_literal == true 或 observed.directions 含 c2s
    csl = {s["name"] for s in schemas if s.get("client_send_literal") is True}
    observed_c2s = {
        s["name"] for s in schemas
        if isinstance(s.get("observed"), dict)
        and "c2s" in (s["observed"].get("directions") or [])
    }
    fc_names = {r["name"] for r in fc_requests}
    union = csl | observed_c2s
    if union != fc_names:
        print("ERROR: row set != (client_send_literal OR observed c2s) "
              "(only_union=%d only_fc=%d)" % (len(union - fc_names), len(fc_names - union)),
              file=sys.stderr)
        return 1

    lua_files = sorted(p.name for p in LUA_DIR.iterdir()
                       if p.is_file() and p.suffix == ".lua")
    print("scanning %d Lua files ..." % len(lua_files))
    method_index, func_index = scan_lua(lua_files)

    records = build_requests(fc_requests, ep_by_name, names_sorted, method_index, func_index)
    counts = compute_counts(records, known)
    counts["rowset_client_send_literal_true"] = len(csl)
    counts["rowset_observed_c2s"] = len(observed_c2s)

    generated_at = datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat()
    inputs = [
        {"path": rel_posix(ENDPOINTS), "sha256": sha256_file(ENDPOINTS),
         "note": "%d schemas" % len(schemas)},
        {"path": rel_posix(FEATURE), "sha256": sha256_file(FEATURE),
         "note": "%d request rows (known_client_requests=%s)" % (len(fc_requests), known)},
        {"path": "03-unpack/lua/device-luascripts/*.lua",
         "sha256": lua_manifest_digest(lua_files),
         "note": "%d files, sorted-name manifest digest" % len(lua_files)},
    ]

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "method": METHOD,
        "inputs": inputs,
        "counts": counts,
        "requests": records,
    }

    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
    args.md_output.parent.mkdir(parents=True, exist_ok=True)
    args.md_output.write_text(render_md(generated_at, inputs, counts, records), encoding="utf-8")

    print("== wait-contracts summary ==")
    print("generated_at: %s" % generated_at)
    print("requests: %d (declared known_client_requests=%s)" % (counts["requests"], known))
    print("  implemented=%d unimplemented=%d observed_request=%d"
          % (counts["implemented"], counts["unimplemented"], counts["observed_request"]))
    print("  with_ret_schema=%d with_client_handler=%d"
          % (counts["with_ret_schema"], counts["with_client_handler"]))
    print("  wait_semantics: %s=%d  %s=%d  %s=%d"
          % (SEMANTICS_RET_HANDLER, counts["ret_schema_client_handler"],
             SEMANTICS_RET_NO_HANDLER, counts["ret_schema_no_client_handler"],
             SEMANTICS_NO_RET, counts["no_ret_schema"]))
    print("  response_candidates_total=%d client_handlers_total=%d"
          % (counts["response_candidates_total"], counts["client_handlers_total"]))
    print("outputs:")
    for p in (args.json_output, args.md_output):
        try:
            label = rel_posix(p)
        except ValueError:
            label = str(p)
        print("  %s (%d bytes)" % (label, p.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
