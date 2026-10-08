#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CrossCore 本地部署 parity 报表生成器（P0.7 门禁 + P0.10 可复算报表）。

本脚本只读既有审计产物，生成两张可复算报表：

    90-notes/parity-report.json
    90-notes/parity-report.md

权威来源约定（写进本文档，报表里也会再次声明）
----------------------------------------------
1. 静态实现登记：90-notes/feature-coverage.json（由 feature_coverage.py 产出）。
   requests[i].implementation 非空 = 已实现。这是**静态登记**，只说明源码里
   注册了 handler，不等于客户端真机调通，也不等于业务正确。
2. 运行期验证 runtime_verified：唯一权威来源是 90-notes/runtime-evidence.json，
   由设备抓包 / server.jsonl 关联的工作人员维护。2026-10-05 实际落地的格式：

       {
         "schema_version": 1,
         "updated_at": "2026-10-05T10:23:07+08:00",
         "method": "每条的 client_log 与 server_event 必须来自同一次实机操作……",
         "evidence_sha256": "…",
         "evidence": [
           "PlayerProto:GetClientData",
           {"request": "FightProtocol:StartMainLineFight",
            "client_log": "…", "server_event": "…"}
         ]
       }

   读取时依次识别 evidence / verified / requests 三个键（evidence 是落地键，
   另两个是历史别名）；条目可以是字符串，也可以是带 request 或 name 的字典。
   该文件不存在、列表为空或条目无法识别时，runtime_verified 一律记 0，
   报表显式写「未建立 / 未验证」。**绝不**从 feature-coverage.json 里的
   runtime_verified 布尔字段或静态登记反推运行期验证；两者的差异只作为
   cross_check 记录，不作为验证证据。
3. 教程组：90-notes/tutorial-loading-audit.json（配置候选请求的静态扫描，
   不是实机调用图）。
4. 审计风险：90-notes/disconnect-audit.json（重跑审计后覆盖）。
5. 测试数 / 编码违规数：run_all_checks.ps1 产出的 steps JSON（临时文件），
   通过 --steps-json 传入；不传时对应栏目标注「未运行」，不猜数字。

用法
----
    python -B -X utf8 02-tools/scripts/parity_report.py
    python -B -X utf8 02-tools/scripts/parity_report.py --steps-json <临时文件>
    python -B -X utf8 02-tools/scripts/parity_report.py --generated-at 2026-10-05T12:00:00+08:00

退出码：0 = 报表成功生成（不代表 parity 达标）；2 = 必需输入缺失或损坏。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NOTES = ROOT / "90-notes"

FEATURE_COVERAGE = NOTES / "feature-coverage.json"
TUTORIAL_AUDIT = NOTES / "tutorial-loading-audit.json"
DISCONNECT_AUDIT = NOTES / "disconnect-audit.json"
RUNTIME_EVIDENCE = NOTES / "runtime-evidence.json"

DEFAULT_JSON = NOTES / "parity-report.json"
DEFAULT_MD = NOTES / "parity-report.md"

TICK = chr(96)
STATIC_NOTE = "静态登记只说明源码注册了 handler，不等于客户端实机调通，也不等于业务正确。"
RUNTIME_NOTE = "运行期验证只认 90-notes/runtime-evidence.json；该文件不存在时记 0 并标注未建立。"


class ReportError(RuntimeError):
    """必需输入缺失或损坏，报表无法复算。"""


def q(text):
    return TICK + str(text) + TICK


def read_json(path: Path):
    """返回 (对象, 原始字节)；容忍 BOM。"""
    if not path.is_file():
        raise ReportError("缺少必需输入文件：" + str(path.relative_to(ROOT)).replace("\\", "/"))
    raw = path.read_bytes()
    try:
        return json.loads(raw.decode("utf-8-sig")), raw
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReportError("输入不是合法 JSON：%s（%s）" % (path, error)) from error


def fingerprint(path: Path, raw=None, present=True):
    record = {
        "path": str(path.relative_to(ROOT)).replace("\\", "/"),
        "present": present,
        "bytes": None,
        "sha256": None,
        "mtime": None,
    }
    if present and path.is_file():
        data = path.read_bytes() if raw is None else raw
        record["bytes"] = len(data)
        record["sha256"] = hashlib.sha256(data).hexdigest()
        record["mtime"] = dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
        record["present"] = True
    return record


def display_path(path: Path) -> str:
    """仓库内文件用相对路径；仓库外只保留文件名，绝不输出本机绝对路径。"""
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return "<outside>/" + resolved.name


def family_of(request_name: str) -> str:
    return request_name.split(":", 1)[0] if ":" in request_name else "(未分族)"


def request_face(coverage: dict) -> dict:
    requests = coverage.get("requests")
    if not isinstance(requests, list):
        raise ReportError("feature-coverage.json 缺少 requests 列表")
    families = defaultdict(lambda: {"total": 0, "implemented": 0, "gap": 0, "observed_requests": 0})
    missing_all = []
    for entry in requests:
        name = entry.get("name") or "(未命名)"
        bucket = families[family_of(name)]
        bucket["total"] += 1
        if entry.get("observed_request"):
            bucket["observed_requests"] += 1
        if entry.get("implementation"):
            bucket["implemented"] += 1
        else:
            bucket["gap"] += 1
            missing_all.append(name)
    total = len(requests)
    implemented = sum(1 for entry in requests if entry.get("implementation"))
    observed = sum(1 for entry in requests if entry.get("observed_request"))
    by_family = []
    for family, bucket in families.items():
        by_family.append(
            {
                "family": family,
                "total": bucket["total"],
                "implemented": bucket["implemented"],
                "gap": bucket["gap"],
                "observed_requests": bucket["observed_requests"],
                "coverage_percent": round(bucket["implemented"] * 100.0 / bucket["total"], 1) if bucket["total"] else 0.0,
            }
        )
    by_family.sort(key=lambda item: (-item["gap"], -item["total"], item["family"]))
    missing_all.sort()
    return {
        "source": "90-notes/feature-coverage.json",
        "definition": "implementation 非空 = 已实现（静态）",
        "total": total,
        "implemented": implemented,
        "gap": total - implemented,
        "observed_requests": observed,
        "registered_handlers_in_source": coverage.get("registered_handlers"),
        "coverage_percent": round(implemented * 100.0 / total, 1) if total else 0.0,
        "by_family": by_family,
        "top_gap_families": [item["family"] for item in by_family if item["gap"] > 0][:10],
        "missing_requests_sample": missing_all[:50],
        "note": STATIC_NOTE,
    }


def runtime_verification(coverage: dict) -> dict:
    static_flagged = sorted(
        entry["name"] for entry in coverage.get("requests", []) if entry.get("runtime_verified")
    )
    section = {
        "authoritative_source": "90-notes/runtime-evidence.json",
        "source_present": RUNTIME_EVIDENCE.is_file(),
        "status": "未建立",
        "verified_count": 0,
        "verified_requests": [],
        "unknown_requests": [],
        "static_flag_count": len(static_flagged),
        "static_flag_mismatch": [],
        "note": RUNTIME_NOTE,
    }
    if section["source_present"]:
        try:
            evidence, _ = read_json(RUNTIME_EVIDENCE)
        except ReportError as error:
            section["status"] = "证据文件损坏：" + str(error)
            return section
        raw_entries = None
        evidence_field = None
        if isinstance(evidence, dict):
            for field in ("evidence", "verified", "requests"):
                if isinstance(evidence.get(field), list):
                    raw_entries = evidence[field]
                    evidence_field = field
                    break
        names = []
        unrecognized = 0
        if isinstance(raw_entries, list):
            for item in raw_entries:
                value = None
                if isinstance(item, str):
                    value = item
                elif isinstance(item, dict):
                    for field in ("request", "name", "proto", "protocol"):
                        if isinstance(item.get(field), str):
                            value = item[field]
                            break
                if isinstance(value, str) and value.strip():
                    names.append(value.strip())
                else:
                    unrecognized += 1
        names = sorted(set(names))
        section["evidence_field"] = evidence_field
        section["unrecognized_entries"] = unrecognized
        known = {entry.get("name") for entry in coverage.get("requests", [])}
        section["verified_count"] = len(names)
        section["verified_requests"] = names
        section["unknown_requests"] = sorted(name for name in names if name not in known)
        section["static_flag_mismatch"] = sorted(set(static_flagged) - set(names))
        if names:
            section["status"] = "已建立"
            section["updated_at"] = evidence.get("updated_at") if isinstance(evidence, dict) else None
            section["method"] = evidence.get("method") if isinstance(evidence, dict) else None
            section["evidence_sha256"] = evidence.get("evidence_sha256") if isinstance(evidence, dict) else None
        elif unrecognized:
            section["status"] = "未建立（runtime-evidence.json 有 %d 条无法识别的记录）" % unrecognized
        else:
            section["status"] = "未建立（runtime-evidence.json 的 %s 为空）" % (evidence_field or "evidence")
    return section


def tutorial_face(tutorial: dict) -> dict:
    groups = tutorial.get("groups")
    if not isinstance(groups, list):
        raise ReportError("tutorial-loading-audit.json 缺少 groups 列表")
    per_group = []
    static_complete = []
    static_incomplete = []
    no_candidate = []
    missing_requests = set()
    for group in groups:
        candidates = group.get("feature_candidates") or []
        missing = sorted(c["request"] for c in candidates if not c.get("registered"))
        gid = group.get("group")
        per_group.append(
            {
                "group": gid,
                "steps": len(group.get("step_ids") or []),
                "candidates": len(candidates),
                "registered": len(candidates) - len(missing),
                "missing": len(missing),
                "missing_requests": missing,
                "names": group.get("names") or [],
                "views": group.get("views") or [],
            }
        )
        if not candidates:
            no_candidate.append(gid)
        elif missing:
            static_incomplete.append(gid)
            missing_requests.update(missing)
        else:
            static_complete.append(gid)
    per_group.sort(key=lambda item: item["group"])
    important = tutorial.get("important_request_registration") or {}
    counts = tutorial.get("counts") or {}
    return {
        "source": "90-notes/tutorial-loading-audit.json",
        "configured_rows": counts.get("rows"),
        "configured_groups": counts.get("groups", len(groups)),
        "grouped_rows": counts.get("grouped_rows"),
        "ungrouped_rows": counts.get("ungrouped_rows"),
        "launcher_handler_count": counts.get("handlers"),
        "static_complete_groups": len(static_complete),
        "static_incomplete_groups": len(static_incomplete),
        "no_candidate_groups": len(no_candidate),
        "static_complete_group_ids": static_complete,
        "static_incomplete_group_ids": static_incomplete,
        "no_candidate_group_ids": no_candidate,
        "missing_handler_requests": sorted(missing_requests),
        "missing_handler_request_count": len(missing_requests),
        "important_requests_total": len(important),
        "important_requests_registered": sum(1 for value in important.values() if value),
        "important_requests_missing": sorted(name for name, value in important.items() if not value),
        "per_group": per_group,
        "note": "候选请求来自配置页面与同功能 Lua 文件的静态扫描，不是实机调用图；"
                "完成/未完成只是「默认启动注册表是否覆盖候选请求」，不代表教程真的走得通。",
    }


def audit_face(audit: dict) -> dict:
    findings = audit.get("findings")
    if not isinstance(findings, list):
        raise ReportError("disconnect-audit.json 缺少 findings 列表")
    by_severity = Counter(finding.get("severity") for finding in findings)
    log = audit.get("log") or {}
    event_counts = log.get("event_counts") or {}
    maintainability = audit.get("maintainability") or {}
    return {
        "source": "90-notes/disconnect-audit.json",
        "schema_version": audit.get("schema_version"),
        "findings_total": len(findings),
        "findings_by_severity": {key: by_severity.get(key, 0) for key in ("P0", "P1", "P2")},
        "risk_signature_count": len(audit.get("risk_signatures") or []),
        "log_total_events": log.get("total_events"),
        "log_event_counts": event_counts,
        "log_failure_record_count": len(log.get("failure_records") or []),
        "log_unsupported_handler_count": len(log.get("unsupported_handlers") or {}),
        "log_request_interval_samples": (log.get("request_intervals") or {}).get("samples"),
        "registry_handler_count": (audit.get("registry") or {}).get("handler_count"),
        "static_test_method_count": maintainability.get("static_test_method_count"),
        "roadmap_items": len(audit.get("roadmap") or []),
        "note": "只统计当前重跑窗口；P0/P1 是已修复项与现存项的合计，须配合 findings[].status 阅读。",
    }


def step_index(steps_doc) -> dict:
    if not isinstance(steps_doc, dict):
        return {}
    index = {}
    for step in steps_doc.get("steps") or []:
        if isinstance(step, dict) and isinstance(step.get("name"), str):
            index[step["name"]] = step
    return index


def step_face(index: dict):
    if not index:
        return None
    summary = []
    for name, step in index.items():
        summary.append(
            {
                "name": name,
                "title": step.get("title"),
                "exit_code": step.get("exit_code"),
                "seconds": step.get("seconds"),
                "status": "ok" if step.get("exit_code") == 0 else "failed",
            }
        )
    return summary


def tests_face(index: dict) -> dict:
    step = index.get("unittest")
    if step is None:
        return {"status": "未运行", "ran": None, "failed": None, "seconds": None,
                "source": "run_all_checks.ps1 --steps-json（未提供）"}
    return {
        "status": "OK" if step.get("exit_code") == 0 else "FAILED",
        "ran": step.get("tests_ran"),
        "failed": step.get("tests_failed"),
        "seconds": step.get("seconds"),
        "exit_code": step.get("exit_code"),
        "suite": "07-server/tests (unittest discover)",
        "source": "run_all_checks.ps1/unittest",
    }


def encoding_face(index: dict) -> dict:
    step = index.get("script_encoding")
    if step is None:
        return {"status": "未运行", "violations": None, "checked": None,
                "source": "run_all_checks.ps1 --steps-json（未提供）"}
    return {
        "status": "OK" if step.get("exit_code") == 0 else "FAILED",
        "violations": step.get("violations"),
        "checked": step.get("checked"),
        "exit_code": step.get("exit_code"),
        "source": "02-tools/scripts/check_script_encoding.py --json",
    }


def cross_checks(coverage, tutorial, audit, tests):
    return {
        "coverage_registered_handlers": coverage.get("registered_handlers"),
        "audit_registry_handler_count": (audit.get("registry") or {}).get("handler_count"),
        "tutorial_launcher_handler_count": (tutorial.get("counts") or {}).get("handlers"),
        "audit_static_test_method_count": (audit.get("maintainability") or {}).get("static_test_method_count"),
        "tests_ran": tests.get("ran"),
        "note": "这些计数来自不同脚本的不同口径，只用于发现漂移，不要求相等。",
    }


def build_report(steps_doc, generated_at):
    coverage, coverage_raw = read_json(FEATURE_COVERAGE)
    tutorial, tutorial_raw = read_json(TUTORIAL_AUDIT)
    audit, audit_raw = read_json(DISCONNECT_AUDIT)
    index = step_index(steps_doc)
    runtime = runtime_verification(coverage)
    inputs = [
        fingerprint(FEATURE_COVERAGE, coverage_raw),
        fingerprint(TUTORIAL_AUDIT, tutorial_raw),
        fingerprint(DISCONNECT_AUDIT, audit_raw),
        fingerprint(RUNTIME_EVIDENCE, present=RUNTIME_EVIDENCE.is_file()),
    ]
    tests = tests_face(index)
    report = {
        "schema": 1,
        "tool": "parity_report.py",
        "generated_at": generated_at,
        "local_timezone": dt.datetime.now().astimezone().tzname(),
        "purpose": "与官服差距的可复算报表；静态登记不等于运行期验证。",
        "inputs": inputs,
        "requests": request_face(coverage),
        "runtime_verified": runtime,
        "tutorials": tutorial_face(tutorial),
        "audit": audit_face(audit),
        "tests": tests,
        "script_encoding": encoding_face(index),
        "steps": step_face(index),
        "cross_checks": cross_checks(coverage, tutorial, audit, tests),
        "known_limits": [
            STATIC_NOTE,
            RUNTIME_NOTE,
            "客户端可发送请求总数来自 05-protocol/endpoints.json 的恢复结果，可能随协议恢复继续变化。",
            "教程候选请求是静态扫描，不能证明某教程一定会发出或走到该请求。",
            "审计风险计数只覆盖当前重跑的日志窗口；日志继续增长后必须重跑。",
            "本报表不执行 07-server 源码或客户端，不改变运行时状态。",
        ],
    }
    report["gates"] = gates_face(index)
    return report


def gates_face(index) -> dict:
    required = ["unittest", "bootstrap_index", "script_encoding",
                "disconnect_audit", "tutorial_audit", "feature_coverage"]
    if not index:
        return {"status": "未运行", "all_green": None, "details": {name: None for name in required}}
    details = {name: (index[name].get("exit_code") if name in index else None) for name in required}
    all_green = all(details.get(name) == 0 for name in required)
    return {"status": "OK" if all_green else "FAILED", "all_green": all_green, "details": details}


def render_markdown(report: dict) -> str:
    requests = report["requests"]
    runtime = report["runtime_verified"]
    tutorials = report["tutorials"]
    audit = report["audit"]
    tests = report["tests"]
    encoding = report["script_encoding"]
    gates = report["gates"]
    lines = []
    add = lines.append
    add("# CrossCore 与官服差距 parity 报表")
    add("")
    add("生成时间：%s（%s）" % (report["generated_at"], report["local_timezone"]))
    add("")
    add("工具：%s；目标：%s" % (q(report["tool"]), report["purpose"]))
    add("")
    add("> 已知边界：%s" % STATIC_NOTE)
    add(">")
    add("> %s" % RUNTIME_NOTE)
    add("")
    add("## 1. 结论摘要")
    add("")
    add("| 指标 | 数值 | 来源 |")
    add("| --- | --- | --- |")
    add("| 客户端可发送请求 | %s | feature-coverage.json |" % requests["total"])
    add("| 已实现（静态登记） | %s | feature-coverage.json |" % requests["implemented"])
    add("| 缺口 | %s | feature-coverage.json |" % requests["gap"])
    add("| 已观测请求 | %s | feature-coverage.json |" % requests["observed_requests"])
    add("| runtime_verified | %s（%s） | runtime-evidence.json |" % (runtime["verified_count"], runtime["status"]))
    add("| 教程组 | %s 组，静态可覆盖 %s / 静态有缺 %s / 无候选 %s | tutorial-loading-audit.json |"
        % (tutorials["configured_groups"], tutorials["static_complete_groups"],
           tutorials["static_incomplete_groups"], tutorials["no_candidate_groups"]))
    add("| 单测 | %s（%s 项，失败 %s） | run_all_checks.ps1 |"
        % (tests["status"], tests["ran"], tests["failed"]))
    add("| 审计 P0/P1/P2 | %s / %s / %s | disconnect-audit.json |"
        % (audit["findings_by_severity"].get("P0"), audit["findings_by_severity"].get("P1"),
           audit["findings_by_severity"].get("P2")))
    add("| 脚本编码违规 | %s | check_script_encoding.py |" % encoding["violations"])
    add("| 门禁总状态 | %s | run_all_checks.ps1 |" % gates["status"])
    add("")
    add("## 2. 请求面（静态登记，按协议族）")
    add("")
    add("| 协议族 | 请求总数 | 已实现 | 缺口 | 已观测请求 | 静态覆盖率 |")
    add("| --- | ---: | ---: | ---: | ---: | ---: |")
    for item in requests["by_family"]:
        add("| %s | %s | %s | %s | %s | %s%% |"
            % (q(item["family"]), item["total"], item["implemented"], item["gap"],
               item["observed_requests"], item["coverage_percent"]))
    add("")
    add("缺口清单（前 50 条，完整清单见 parity-report.json 的 requests.missing_requests_sample）：")
    add("")
    add(", ".join(q(name) for name in requests["missing_requests_sample"]) or "（无）")
    add("")
    add("## 3. runtime_verified（运行期验证）")
    add("")
    add("权威来源：%s。" % q(runtime["authoritative_source"]))
    add("")
    add("| 项 | 值 |")
    add("| --- | --- |")
    add("| 证据文件存在 | %s |" % runtime["source_present"])
    add("| 状态 | %s |" % runtime["status"])
    add("| 已验证请求数 | %s |" % runtime["verified_count"])
    add("| feature-coverage 静态 runtime_verified 标记数 | %s |" % runtime["static_flag_count"])
    if runtime["verified_requests"]:
        add("")
        add("已通过运行期验证的请求：")
        add("")
        for name in runtime["verified_requests"]:
            add("- %s" % q(name))
    else:
        add("")
        add("**当前没有任何运行期验证证据：runtime_verified = %s（%s）。**" % (runtime["verified_count"], runtime["status"]))
    if runtime["unknown_requests"]:
        add("")
        add("证据文件里出现、但不在客户端请求清单里的条目：%s"
            % ", ".join(q(name) for name in runtime["unknown_requests"]))
    add("")
    add("## 4. 教程组")
    add("")
    add("| 指标 | 数值 |")
    add("| --- | --- |")
    add("| 配置项 | %s |" % tutorials["configured_rows"])
    add("| 教程组 | %s |" % tutorials["configured_groups"])
    add("| 有分组配置项 | %s |" % tutorials["grouped_rows"])
    add("| 未分组配置项 | %s |" % tutorials["ungrouped_rows"])
    add("| 默认启动 handler 数 | %s |" % tutorials["launcher_handler_count"])
    add("| 静态无缺口组 | %s |" % tutorials["static_complete_groups"])
    add("| 静态有缺口组（缺 handler） | %s |" % tutorials["static_incomplete_groups"])
    add("| 无静态候选组 | %s |" % tutorials["no_candidate_groups"])
    add("| 缺口请求（去重） | %s |" % tutorials["missing_handler_request_count"])
    add("| 重要请求已登记/总数 | %s / %s |"
        % (tutorials["important_requests_registered"], tutorials["important_requests_total"]))
    add("")
    add("> %s" % tutorials["note"])
    add("")
    add("| 教程组 | 步数 | 候选请求 | 已登记 | 缺 handler | 功能名 |")
    add("| ---: | ---: | ---: | ---: | ---: | --- |")
    for item in tutorials["per_group"]:
        add("| %s | %s | %s | %s | %s | %s |"
            % (item["group"], item["steps"], item["candidates"], item["registered"],
               item["missing"], ", ".join(item["names"]) or "-"))
    add("")
    add("## 5. 测试与门禁")
    add("")
    if report["steps"] is None:
        add("本次报表未收到 run_all_checks.ps1 的 steps JSON，测试数与编码违规数标注为未运行。")
    else:
        add("| 步骤 | 退出码 | 耗时(秒) | 状态 |")
        add("| --- | ---: | ---: | --- |")
        for step in report["steps"]:
            add("| %s | %s | %s | %s |"
                % (step["title"] or step["name"], step["exit_code"], step["seconds"], step["status"]))
    add("")
    add("## 6. 审计风险（disconnect-audit.json）")
    add("")
    add("| 指标 | 数值 |")
    add("| --- | --- |")
    add("| findings 合计 | %s |" % audit["findings_total"])
    add("| P0 | %s |" % audit["findings_by_severity"].get("P0"))
    add("| P1 | %s |" % audit["findings_by_severity"].get("P1"))
    add("| P2 | %s |" % audit["findings_by_severity"].get("P2"))
    add("| 风险签名数 | %s |" % audit["risk_signature_count"])
    add("| 日志事件数 | %s |" % audit["log_total_events"])
    add("| unsupported_handler 种类 | %s |" % audit["log_unsupported_handler_count"])
    add("| 失败记录数 | %s |" % audit["log_failure_record_count"])
    add("")
    add("日志事件计数：" + (", ".join("%s=%s" % (q(key), value)
                                    for key, value in sorted(audit["log_event_counts"].items())) or "-"))
    add("")
    add("## 7. 输入指纹（复算用）")
    add("")
    add("| 文件 | 存在 | 字节 | sha256 | 修改时间 |")
    add("| --- | --- | ---: | --- | --- |")
    for item in report["inputs"]:
        add("| %s | %s | %s | %s | %s |"
            % (q(item["path"]), item["present"], item["bytes"], q(item["sha256"]), item["mtime"]))
    add("")
    add("## 8. 已知边界")
    add("")
    for note in report["known_limits"]:
        add("- %s" % note)
    add("")
    return "\n".join(lines) + "\n"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="生成 CrossCore parity 报表")
    parser.add_argument("--steps-json", type=Path, default=None,
                        help="run_all_checks.ps1 产出的步骤结果 JSON（可选）")
    parser.add_argument("--output-json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--output-md", type=Path, default=DEFAULT_MD)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    generated_at = args.generated_at or dt.datetime.now().astimezone().isoformat(timespec="seconds")
    steps_doc = None
    if args.steps_json is not None:
        try:
            steps_doc = json.loads(Path(args.steps_json).read_bytes().decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            print("PARITY ERROR: 无法读取 steps JSON：%s" % error, file=sys.stderr)
            return 2
    try:
        report = build_report(steps_doc, generated_at)
    except ReportError as error:
        print("PARITY ERROR: %s" % error, file=sys.stderr)
        return 2

    report["steps_source"] = display_path(args.steps_json) if args.steps_json else None
    json_path, md_path = Path(args.output_json), Path(args.output_md)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    summary = {
        "reports": [display_path(json_path), display_path(md_path)],
        "requests": {key: report["requests"][key] for key in ("total", "implemented", "gap", "observed_requests")},
        "runtime_verified": report["runtime_verified"]["verified_count"],
        "runtime_status": report["runtime_verified"]["status"],
        "tutorial_groups": report["tutorials"]["configured_groups"],
        "tutorial_static_incomplete_groups": report["tutorials"]["static_incomplete_groups"],
        "tests": report["tests"]["ran"],
        "audit_findings": {key: report["audit"]["findings_by_severity"].get(key) for key in ("P0", "P1", "P2")},
        "encoding_violations": report["script_encoding"]["violations"],
        "gates": report["gates"]["status"],
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
