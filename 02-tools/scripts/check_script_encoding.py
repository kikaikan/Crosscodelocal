#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CrossCore 本地部署 Windows 脚本编码门禁（P0.5）。

为什么需要这个检查器
--------------------
项目根目录的 start_local.cmd 用 powershell.exe（Windows PowerShell 5.1）调用
start_local.ps1。PowerShell 5.1 只有在文件带 UTF-8 BOM 时才按 UTF-8 读取脚本；
没有 BOM 时它按系统 ANSI 代码页解码，脚本里的中文会变成乱码并直接触发
ParserError（2026-10-05 真机实际发生过一次，启动器报「Unexpected token」）。
pwsh 7 默认按 UTF-8 读取，看不到这个问题，所以只用 pwsh 7 验证是不够的。

编码契约（本脚本按此逐条判定）
------------------------------
1. .ps1            : UTF-8 with BOM（EF BB BF）+ 合法 UTF-8 + 全 CRLF，禁止 UTF-16。
2. .cmd / .bat     : 纯 ASCII + 全 CRLF；中文提示由被调用的 .ps1 输出，
                     不要在这些批处理里写非 ASCII 字符。
3. 输出中文的 .ps1 : 顶部显式设置
                     [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
                     （重定向输出场景同时设置 $OutputEncoding），
                     建议用 try/catch 包裹，避免无控制台句柄的主机抛异常。
4. 写 JSON        : 不要用 Set-Content -Encoding utf8（5.1 带 BOM、7 不带 BOM，
                     行为不一致）；用
                     [System.IO.File]::WriteAllText($path, $json,
                         (New-Object System.Text.UTF8Encoding($false)))
                     显式写入无 BOM 的 UTF-8。
5. 读取端         : 必须容忍 BOM（Get-Content -Encoding UTF8 可以）。
6. 任何编辑器/脚本改完 .ps1 后都要保留 BOM，并用本检查器 + 双宿主语法解析复验：
       powershell.exe -NoProfile -Command "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('<绝对路径>',[ref]$null,[ref]$e); $e.Count"
       pwsh           -NoProfile -Command "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('<绝对路径>',[ref]$null,[ref]$e); $e.Count"
   两者的输出都必须是 0。

用法
----
    python 02-tools/scripts/check_script_encoding.py            # 人类可读，0 违规退出码 0，否则 1
    python 02-tools/scripts/check_script_encoding.py --json     # 机器可读 JSON

默认扫描仓库根目录（本文件所在目录的上两级）下的所有 .ps1/.cmd/.bat，
并跳过第三方运行时与依赖目录（见 EXCLUDED_DIRS）。可用 --root 指定其它根目录。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# 仓库根 = <root>/02-tools/scripts/check_script_encoding.py 的上两级。
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))

# 仓库相对路径（统一用 / 分隔、小写比较）下需要跳过的目录：
# 第三方运行时、虚拟环境、构建工具和缓存，不受本项目的编码契约约束。
EXCLUDED_DIRS = {
    "02-tools/python",
    "02-tools/capture-venv",
    "02-tools/frida",
    "02-tools/il2cppdumper",
    "02-tools/android-build-tools",
    "02-tools/mitmproxy",
    "__pycache__",
    "node_modules",
}

CHECKED_SUFFIXES = (".ps1", ".cmd", ".bat")

UTF8_BOM = b"\xef\xbb\xbf"
UTF16_LE_BOM = b"\xff\xfe"
UTF16_BE_BOM = b"\xfe\xff"


def is_excluded(rel_dir: str) -> bool:
    """rel_dir 为仓库相对目录（/ 分隔，无首尾斜杠）。"""
    if not rel_dir:
        return False
    parts = rel_dir.split("/")
    normalized = "/".join(parts).lower()
    for excluded in EXCLUDED_DIRS:
        if normalized == excluded or normalized.startswith(excluded + "/"):
            return True
        # 也允许目录名本身在任何位置命中（例如任意层级的 __pycache__ / node_modules）。
        if "/" not in excluded and excluded in [p.lower() for p in parts]:
            return True
    return False


def check_file(path: str) -> list[str]:
    """返回违规原因列表；空列表表示通过。"""
    problems: list[str] = []
    name = os.path.basename(path)
    suffix = os.path.splitext(name)[1].lower()
    with open(path, "rb") as handle:
        data = handle.read()

    # 1) 禁止 UTF-16（BOM 或大量 NUL 字节都视为 UTF-16）。
    if data.startswith(UTF16_LE_BOM) or data.startswith(UTF16_BE_BOM):
        return ["UTF-16 编码（检测到 UTF-16 BOM），脚本必须使用 UTF-8"]
    if b"\x00" in data:
        problems.append("包含 NUL 字节，疑似 UTF-16/二进制内容")

    # 2) 必须是合法 UTF-8。
    try:
        data.decode("utf-8")
        utf8_ok = True
    except UnicodeDecodeError as exc:
        utf8_ok = False
        problems.append("不是合法 UTF-8：%s" % exc)

    has_bom = data.startswith(UTF8_BOM)
    body = data[len(UTF8_BOM):] if has_bom else data

    # 3) 行尾必须是 CRLF：每个 LF 前面必须有 CR，且不存在孤立 CR。
    crlf = body.count(b"\r\n")
    lf = body.count(b"\n")
    cr = body.count(b"\r")
    lone_lf = lf - crlf
    lone_cr = cr - crlf
    if lone_lf:
        problems.append("存在 %d 个 LF 行尾，必须全部为 CRLF" % lone_lf)
    if lone_cr:
        problems.append("存在 %d 个孤立 CR，必须全部为 CRLF" % lone_cr)

    if suffix == ".ps1":
        # .ps1 必须有 UTF-8 BOM。
        if not has_bom:
            problems.append("缺少 UTF-8 BOM（Windows PowerShell 5.1 会按 ANSI 读取而乱码/ParserError）")
        if not utf8_ok:
            problems.append("PowerShell 脚本必须是合法 UTF-8")
    else:
        # .cmd/.bat 必须是纯 ASCII；BOM 也属于非 ASCII，会被下面这条抓到。
        try:
            data.decode("ascii")
        except UnicodeDecodeError as exc:
            problems.append("批处理文件必须纯 ASCII，存在非 ASCII 字节：%s" % exc)
        if has_bom:
            problems.append("批处理文件不应带 BOM（必须纯 ASCII）")

    return problems


def repair_file(path: str) -> list[str]:
    """就地修复可机械修复的编码问题，返回仍需人工处理的剩余原因。

    只做两件不会改变语义的事：补/去 BOM、把行尾统一成 CRLF。任何涉及字符集
    转换的情况（UTF-16、含非 ASCII 的批处理、非法 UTF-8）一律拒绝，避免静默
    改坏脚本后又被门禁放行。
    """
    suffix = os.path.splitext(path)[1].lower()
    with open(path, "rb") as handle:
        data = handle.read()
    if data.startswith(UTF16_LE_BOM) or data.startswith(UTF16_BE_BOM) or b"\x00" in data:
        return ["UTF-16 或二进制内容，拒绝自动修复，请另存为 UTF-8"]
    has_bom = data.startswith(UTF8_BOM)
    body = data[len(UTF8_BOM):] if has_bom else data
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        return ["不是合法 UTF-8，拒绝自动修复"]
    if suffix == ".ps1":
        wanted_bom = True
    else:
        try:
            body.decode("ascii")
        except UnicodeDecodeError:
            return ["批处理含非 ASCII 内容，拒绝自动修复（中文提示应由 .ps1 输出）"]
        wanted_bom = False
    normalized = body.replace(b"\r\n", b"\n").replace(b"\r", b"\n").replace(b"\n", b"\r\n")
    fixed = (UTF8_BOM if wanted_bom else b"") + normalized
    if fixed != data:
        with open(path, "wb") as handle:
            handle.write(fixed)
    return []


def iter_targets(root: str):
    for current, dirs, files in os.walk(root):
        rel_dir = os.path.relpath(current, root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        # 原地裁剪 os.walk，避免进入被排除的目录（也能跳过庞大的第三方树）。
        dirs[:] = sorted(d for d in dirs if not is_excluded((rel_dir + "/" + d).strip("/")))
        for filename in sorted(files):
            if filename.lower().endswith(CHECKED_SUFFIXES):
                yield os.path.join(current, filename), rel_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="检查仓库内 .ps1/.cmd/.bat 是否符合 Windows PowerShell 编码契约。"
    )
    parser.add_argument("--root", default=DEFAULT_ROOT, help="扫描根目录，默认仓库根")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--fix", action="store_true",
                        help="就地修复可机械修复的问题（补/去 BOM、统一 CRLF），然后重新检查")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print("扫描根目录不存在：%s" % root, file=sys.stderr)
        return 1

    violations = []
    repaired = []
    checked = 0
    for path, _rel_dir in iter_targets(root):
        checked += 1
        rel = os.path.relpath(path, root).replace("\\", "/")
        problems = check_file(path)
        if problems and args.fix:
            remaining = repair_file(path)
            if not remaining:
                repaired.append(rel)
                problems = check_file(path)
            else:
                problems = sorted(set(problems) | set(remaining))
        if problems:
            violations.append({"path": rel, "problems": problems})

    violations.sort(key=lambda item: item["path"])
    repaired.sort()

    if args.json:
        print(
            json.dumps(
                {
                    "root": root,
                    "checked": checked,
                    "violations": len(violations),
                    "repaired": repaired,
                    "ok": not violations,
                    "files": violations,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print("编码门禁：%s" % root)
        print("扫描脚本：%d 个（.ps1 / .cmd / .bat）" % checked)
        if repaired:
            print("已自动修复：%d 个" % len(repaired))
            for path in repaired:
                print("  [FIXED] %s" % path)
        if violations:
            print("违规：%d 个" % len(violations))
            for item in violations:
                print("  [FAIL] %s" % item["path"])
                for problem in item["problems"]:
                    print("         - %s" % problem)
        else:
            print("违规：0 个 —— 全部符合编码契约。")

    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
