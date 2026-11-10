#!/usr/bin/env python3
"""regression_pack.py — 把已修复 bug 资产化为可执行回归重放包。

fix_watchlist 只做「修后片段还在」的弱信号巡查；本工具更进一步：为每个
active incident 生成一个**独立可执行的检查脚本**（无 bugx 依赖、可入 CI），
构建期绑定候选文件与期望片段，可选嵌入 fix_validation 的复验命令
（findings 里 fix_validation.incident_id 关联）。`run` 批量执行并输出
红/绿/跳过 三态——复发=红，比片段存在性强一个数量级。

用法：
  python3 regression_pack.py build --ledger <账本> --root <项目根> --out <包目录> \
    [--findings findings.json] [--limit 20]
  python3 regression_pack.py run --pack <包目录> [--json]

检查脚本退出码：0=绿（修复健在/复验通过），1=红（疑似回归），77=跳过
（候选文件全缺失，可能已重构归档）。
"""

from __future__ import annotations
# --- 引擎导入根（直接执行脚本时自举 core.* 命名空间）---
import sys as _sys
from pathlib import Path as _Path

for _p in _Path(__file__).resolve().parents:
    if (_p / "core" / "bugx.md").is_file():
        if str(_p) not in _sys.path:
            _sys.path.insert(0, str(_p))
        break
del _p

import argparse
import importlib.util
import json
import re
import shlex
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LEDGER = Path(".bugx/knowledge/lesson-ledger.json")
DEFAULT_LIMIT = 20
MIN_SNIPPET_CHARS = 12
# 受控测试引用契约：仅 allowlist 的可执行命令以 argv 列表执行，shell=False，fail-closed。
_ALLOWED_EXECUTABLES = {"pytest", "python", "python3", "true", "false"}
_ALLOWED_PYTEST_MODULES = {"pytest", "unittest"}
# 禁止的 shell 元字符/控制字符（fail-closed）
_FORBIDDEN_CHARS = (";", "|", "&", "$", "`", ">", "<", "\n", "\r", "\x00")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_./\-:@%+=]+$")

CHECK_TEMPLATE = '''#!/usr/bin/env python3
"""回归检查 {incident_id}：{summary}（由 regression_pack 生成，可入 CI）。"""
from __future__ import annotations

# 注意：模板是**字符串**，不在本模块的 AST 作用域里。任何"未使用 import"清理
# 工具都看不见这些名字的真实用途，一旦删掉，生成的每个回归检查都会在
# `path = Path(name)` / `sys.exit(main())` 处 NameError —— 全部判红（含无回归的项目），
# 回归包整体失效。删之前请先确认它不是模板正文的一部分。
import subprocess
import sys
from pathlib import Path

FILES = {files!r}
EXPECTED_SNIPPET = {snippet!r}
COMMAND = {command!r}


def main() -> int:
    present = None
    for name in FILES:
        path = Path(name)
        if not path.is_file():
            continue
        try:
            if EXPECTED_SNIPPET in path.read_text(encoding="utf-8", errors="replace"):
                present = path
                break
            present = False
        except OSError:
            continue
    if present is None:
        print("SKIP {incident_id}: 候选文件全部缺失（可能重构/归档）")
        return 77
    if present is False:
        print("RED {incident_id}: 修后片段不再存在于任何候选文件，疑似回归")
        return 1
    if COMMAND:
        result = subprocess.run(COMMAND, shell=False, capture_output=True, timeout=600)
        if result.returncode != 0:
            print("RED {incident_id}: 复验命令失败 exit=" + str(result.returncode))
            return 1
    print("GREEN {incident_id}: 修复健全 (%s)" % present)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


class PackError(ValueError):
    """回归包协议或输入无效。"""


def _ledger_module():
    path = SCRIPT_DIR / "ledger" / "lesson_ledger.py"
    spec = importlib.util.spec_from_file_location("bugx_pack_ledger", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reference_to_argv(reference: str) -> list[str] | None:
    """受控测试引用 → 安全 argv；fail-closed，非法一律返回 None。

    契约：
    - 仅允许 allowlist 可执行文件：true/false、pytest、python(-m pytest/unittest)
    - 禁止 shell 元字符 ;|&$`<>\n\r\0 出现在原始字符串
    - 超长 (>500) 或空串拒绝
    - 通过 shlex.split 解析后按 allowlist 校验，参数 token 需满足 _SAFE_TOKEN_RE
      或为安全用法的受控特例（如 -k 后的表达式 token 允许但仍受 allowlist 约束）
    - 非受控一律 None（build 期不嵌入命令，避免回归检查成为任意命令执行器）
    """
    if not isinstance(reference, str):
        return None
    ref = reference.strip()
    if not ref or len(ref) > 500:
        return None
    if any(ch in ref for ch in _FORBIDDEN_CHARS):
        return None
    # 额外禁止 $() / ${} 等已由 $ 覆盖，但显式检测
    if "$(" in ref or "${" in ref:
        return None
    try:
        argv = shlex.split(ref, posix=True)
    except ValueError:
        return None
    if not argv:
        return None
    exe = argv[0]
    # 禁止路径穿越/绝对路径执行任意二进制
    if "/" in exe or "\\" in exe:
        return None
    if exe not in _ALLOWED_EXECUTABLES:
        return None
    if exe in ("true", "false"):
        if len(argv) != 1:
            return None
        return argv
    if exe == "pytest":
        for tok in argv[1:]:
            # 允许 pytest 常见参数字符，拒绝含非法字符的 token
            if not tok:
                return None
            # token 级别同样禁止危险字符（防御 shlex 绕过）
            if any(ch in tok for ch in _FORBIDDEN_CHARS):
                return None
            # 对 token 做宽松但受控的字符集校验：允许 -、字母数字、._/:@%+=
            # 若 token 含空格（shlex 合并的引号内容），视为合法但需排除危险字符
            # 为兼容 "-k 'a or b'" 这类含空格的过滤表达式，允许空格出现在 token 中
            # 但仍需逐字符放行（字母数字空格及安全符号）
            if " " in tok:
                if not re.fullmatch(r"[A-Za-z0-9_./\-:@%+= ]+", tok):
                    return None
            elif not _SAFE_TOKEN_RE.fullmatch(tok):
                return None
        return argv
    if exe in ("python", "python3"):
        if len(argv) < 3 or argv[1] != "-m" or argv[2] not in _ALLOWED_PYTEST_MODULES:
            return None
        for tok in argv[3:]:
            if not tok or any(ch in tok for ch in _FORBIDDEN_CHARS):
                return None
            if " " in tok:
                if not re.fullmatch(r"[A-Za-z0-9_./\-:@%+= ]+", tok):
                    return None
            elif not _SAFE_TOKEN_RE.fullmatch(tok):
                return None
        return argv
    return None


def _fix_commands_by_incident(findings_path: Path | None) -> dict[str, list[str]]:
    """从 findings 的 fix_validation.incident_id 提取可重放复验命令（安全 argv，fail-closed）。"""
    if findings_path is None:
        return {}
    try:
        payload = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackError(f"findings 无法读取: {exc}") from exc
    commands: dict[str, list[str]] = {}
    for finding in payload.get("findings", []) if isinstance(payload, dict) else []:
        if not isinstance(finding, dict):
            continue
        lifecycle = finding.get("lifecycle")
        if not isinstance(lifecycle, dict):
            continue
        fix = lifecycle.get("fix_validation")
        if not isinstance(fix, dict):
            continue
        incident_id = fix.get("incident_id")
        reference = fix.get("test_reference")
        if not isinstance(incident_id, str) or not isinstance(reference, str):
            continue
        argv = _reference_to_argv(reference)
        if argv is not None:
            commands[incident_id] = argv
    return commands


def build(ledger_path: Path, root: Path, out: Path, *,
          findings_path: Path | None = None, limit: int = DEFAULT_LIMIT) -> dict:
    if not root.is_dir():
        raise PackError(f"--root 不是目录: {root}")
    ledger = _ledger_module()
    incidents = [
        item for item in ledger.list_incidents(ledger_path, limit=limit * 4)
        if item.get("status") in {"candidate", "active"}
    ][-limit:]
    commands = _fix_commands_by_incident(findings_path)
    checks_dir = out / "checks"
    checks_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    skipped = 0
    for incident in incidents:
        incident_id = str(incident.get("incident_id"))
        after = str(incident.get("after_snippet") or "").strip()
        if len(after) < MIN_SNIPPET_CHARS:
            skipped += 1
            continue
        # 构建期绑定候选文件：context 线索优先，兜底源码扫描（≤300 个）。
        files: list[str] = []
        hint = str(incident.get("context") or "")
        for token in hint.replace(",", " ").split():
            clean = token.strip("`'\"():")
            if "/" in clean:
                candidate = root / clean.lstrip("./")
                if candidate.is_file():
                    files.append(str(candidate))
        if not files:
            count = 0
            for path in sorted(root.rglob("*")):
                if count >= 300:
                    break
                if path.is_file() and path.suffix.lower() in {
                    ".py", ".js", ".ts", ".tsx", ".java", ".go", ".rs",
                    ".swift", ".kt", ".c", ".h", ".cpp", ".rb", ".php",
                }:
                    if ".git" not in path.parts and ".bugx" not in path.parts \
                            and "node_modules" not in path.parts:
                        files.append(str(path))
                        count += 1
        if not files:
            skipped += 1
            continue
        script = CHECK_TEMPLATE.format(
            incident_id=incident_id,
            summary=str(incident.get("summary") or "")[:60],
            files=files[:50],
            snippet=after,
            command=commands.get(incident_id),
        )
        (checks_dir / f"{incident_id}.py").write_text(script, encoding="utf-8")
        manifest.append({
            "incident_id": incident_id,
            "summary": incident.get("summary"),
            "command_replay": bool(commands.get(incident_id)),
            "check": f"checks/{incident_id}.py",
        })
    (out / "manifest.json").write_text(
        json.dumps({
            "schema_version": 1,
            "root": str(root),
            "generated_from": str(ledger_path),
            "checks_total": len(manifest),
            "skipped": skipped,
            "checks": manifest,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"status": "built", "out": str(out), "checks": len(manifest),
            "skipped": skipped,
            "note": "run 子命令批量执行；exit 0=绿 1=红 77=跳过"}


def run(pack: Path, *, timeout: float = 60.0) -> dict:
    import subprocess

    manifest_path = pack / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackError(f"包 manifest 无法读取: {exc}") from exc
    results = []
    green = red = skipped = 0
    for check in manifest.get("checks", []):
        script = pack / str(check["check"])
        if not script.is_file():
            results.append({**check, "outcome": "missing"})
            red += 1
            continue
        try:
            completed = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True, text=True, timeout=timeout,
            )
            code = completed.returncode
            outcome = {0: "green", 1: "red", 77: "skipped"}.get(code, f"error:{code}")
            detail = (completed.stdout or completed.stderr or "").strip()[:200]
        except subprocess.TimeoutExpired:
            outcome, detail = "error:timeout", "检查脚本超时"
        if outcome == "green":
            green += 1
        elif outcome in {"skipped"}:
            skipped += 1
        else:
            red += 1
        results.append({**check, "outcome": outcome, "detail": detail})
    return {
        "schema_version": 1,
        "evaluation": "regression-pack-run",
        "pack": str(pack),
        "checks_total": len(results),
        "green": green, "red": red, "skipped": skipped,
        "results": results,
        "note": "红 = 修后片段消失或复验命令失败；跳过 = 候选文件全缺失",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=("build", "run"))
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--out", type=Path, help="build 输出包目录")
    parser.add_argument("--pack", type=Path, help="run 输入包目录")
    parser.add_argument("--findings", type=Path,
                        help="可选 findings JSON：按 fix_validation.incident_id"
                             " 嵌入复验命令重放")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            if args.out is None:
                raise PackError("build 必须提供 --out")
            result: dict = build(
                args.ledger, args.root, args.out,
                findings_path=args.findings, limit=args.limit,
            )
        else:
            if args.pack is None:
                raise PackError("run 必须提供 --pack")
            result = run(args.pack)
    except (PackError, OSError, ValueError) as exc:
        print(f"[regression_pack] ✗ {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result.get("status") == "built":
            print(f"[regression_pack] 生成 {result['checks']} 个检查"
                  f"（跳过 {result['skipped']}）-> {result['out']}")
        else:
            print(f"[regression_pack] 执行 {result['checks_total']} 个检查："
                  f"绿 {result['green']} 红 {result['red']} 跳过 {result['skipped']}")
            for item in result["results"]:
                if item["outcome"] != "green":
                    print(f"  ⚠ [{item['outcome']}] {item['incident_id']} "
                          f"{str(item.get('summary') or '')[:50]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
