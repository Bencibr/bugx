#!/usr/bin/env python3
"""fuzz_input.py — 变异模糊矩阵工具（并发批量轰输入，筛出异常样本）。

用途：取一份合法输入，逐字段/逐字节变异成批量变体，**并发**喂给目标，
收集每次 exit code + 输出，筛出异常样本（崩溃/挂起/非零退出）供进一步
最小化与根因分析。与 minimize_repro.py 配套：fuzz 负责"广撒网找异常"，
minimize 负责"把异常缩到最小复现"。

用法：
  python3 fuzz_input.py --input <合法输入文件> \
      --cmd '<目标命令，{input} 占位>' \
      --out <输出目录> [--count N] [--jobs P] [--timeout S] [--seed S]

  # 从多语言种子语料选起点变异（--lang python/java/rust/swift/kotlin/
  # c/cpp/typescript/javascript/css），比随机起点命中率更高：
  python3 fuzz_input.py --lang python \
      --cmd '<目标命令，{input} 占位>' --out /tmp/fuzz-out --count 500 --jobs 8

示例：
  # 对 json 解析器变异 500 个样本，8 并发，异常存到 /tmp/fuzz-out
  python3 fuzz_input.py \
      --input valid.json --cmd 'python3 -m json.tool {input}' \
      --out /tmp/fuzz-out --count 500 --jobs 8

变异策略（each sample applies one random strategy）：
  - truncate      截断文件尾部（模拟不完整数据）
  - flip_byte     随机翻转一个字节
  - insert_garbage 在随机位置插入一段垃圾字节
  - mutate_numeric 把数字字段放大/改负/改零（正则）
  - mutate_string 把字符串字段换成超长/CJK/emoji/控制字符
  - duplicate     重复文件中间一段（制造超大/重复结构）

种子语料：seed_corpus/ 目录存放 10 种常用语言的典型语法片段与易错输入，
`--lang` 从对应文件随机选一行作为变异起点（--input 与 --lang 二选一）。

筛选标准：目标进程 exit code 非 0（崩溃/报错）或超时（挂起）→ 记为异常样本，
原始输入与 stderr 一并保存到输出目录。

安全：命令默认按 argv 执行；使用 shell 运算符时必须显式加
`--allow-shell`，且仅应对完全可信的命令模板使用。
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
import hashlib
import json
import os
import random
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Mapping

_CORE_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _CORE_DIR.parent
for _p in (str(_REPO_ROOT), str(_CORE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import core.bugx_paths as bugx_paths

from core.supervisor.worker_execution import run_bounded

SEED_CORPUS = Path(__file__).resolve().parents[1] / "seed_corpus"


def _read_confined_input(root: str | Path, raw_path: str | Path) -> bytes:
    """读取项目内受控文件作为 fuzz 起点。

    ``input_path`` 来自 Worker，而被审计项目的一切内容都不可信；必须做
    ``resolve() + relative_to(root)`` 与逐级符号链接检查，否则它就是一个
    任意文件读取入口（读到的内容会被写进临时输入文件并喂给目标命令）。
    """
    return bugx_paths.read_confined_bytes(
        Path(root), raw_path, max_bytes=MAX_INPUT_BYTES, error_cls=ValueError,
    )

LANGS = [
    "python", "go", "java", "rust", "swift", "kotlin",
    "c", "cpp", "typescript", "javascript", "css",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group()
    source.add_argument("--input", help="合法输入文件路径（与 --lang 二选一）")
    source.add_argument("--lang", choices=LANGS,
                        help="从种子语料选起点变异（%s）" % ", ".join(LANGS))
    p.add_argument("--cmd", required=True,
                   help="目标命令，用 {input} 占位表示输入文件路径")
    p.add_argument("--out", required=True, help="异常样本输出目录")
    p.add_argument("--root", type=Path, default=Path.cwd(),
                   help="项目根目录；--target-gaps 中引用的文件必须落在其内（默认当前目录）")
    p.add_argument("--target-gaps", type=Path, default=None,
                   help="coverage_intelligence gaps JSON：把未覆盖/欠覆盖文件"
                        "自身作为种子优先变异（定向模糊，无插桩环境可用）")
    p.add_argument("--count", type=int, default=100, help="变异样本数（默认 100）")
    p.add_argument("--jobs", type=int, default=4, help="并发数（默认 4）")
    p.add_argument("--timeout", type=float, default=10, help="单次执行超时秒（默认 10）")
    p.add_argument("--seed", type=int, default=None, help="随机种子（可复现）")
    p.add_argument("--allow-shell", action="store_true",
                   help="允许通过 shell 执行命令（仅限完全可信的命令模板）")
    p.add_argument("--evolve", type=Path, default=None,
                   help="行为引导进化目录：跨轮维护行为哈希库，把触发新行为的"
                        "样本回填为下轮种子（dir/behaviors.json + dir/seeds/）")
    p.add_argument("--accepted-exits", default="0",
                   help="预期退出码逗号列表（默认 0；如 CLI 参数拒绝可用 0,2）")
    p.add_argument("--feedback-json", action="store_true",
                   help="解析 stdout 最后一条 JSON feedback envelope")
    p.add_argument("--latency-baseline", type=float, default=None,
                   help="延迟基线秒；只有显式设置后才启用慢响应标记")
    p.add_argument("--latency-factor", type=float, default=3.0,
                   help="慢响应阈值倍数（默认 3）")
    p.add_argument("--watch-status", default="500,502,503",
                   help="feedback envelope 中需关注的 HTTP 状态码，逗号列表")
    return p.parse_args(argv)


MAX_GAP_SEED_FILES = 50
MAX_GAP_SEED_BYTES = 256 * 1024
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_EVOLUTION_SEED_BYTES = MAX_INPUT_BYTES
_SHELL_PUNCTUATION = ";&|<>`$()"
_SHELL_OPERATORS = frozenset({
    ";", "&", "&&", "|", "||", "<", ">", ">>", "<<", "<&", ">&",
    "`", "$(", "(", ")",
})


class _PayloadLimitError(ValueError):
    """单个 fuzz 样本超过执行边界。"""


def split_argv(template: str) -> list[str]:
    """解析 argv 模板，并拒绝未授权的 shell 语法。

    这是全仓命令拆分的唯一实现（QUA-03）：``minimize_repro`` 与
    ``differential_probe`` 直接复用本函数，不再各自复制一份。
    """
    lexer = shlex.shlex(template, posix=True, punctuation_chars=_SHELL_PUNCTUATION)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        argv = list(lexer)
    except ValueError as exc:
        raise ValueError(f"命令无法解析: {exc}") from exc
    if not argv:
        raise ValueError("命令不能为空")
    if any(token in _SHELL_OPERATORS for token in argv):
        raise ValueError("命令包含 shell 运算符；如确需使用，请显式传入 --allow-shell")
    return argv


def shell_argv(rendered: str) -> list[str]:
    """把已渲染的命令串交给 shell；Windows 用 COMSPEC，POSIX 用 /bin/sh。"""
    if os.name == "nt":  # pragma: no cover - 项目宿主目前为 POSIX
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", rendered]
    return ["/bin/sh", "-c", rendered]


# 兼容本模块既有的私有调用点；实现只此一份。
_split_argv = split_argv
_shell_argv = shell_argv


def _as_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value or "").encode("utf-8", errors="replace")


def load_gap_seeds(gaps_path: Path | None, *, root: Path) -> list[bytes]:
    """从 coverage gaps JSON 提取「欠覆盖文件本身」作为定向种子。

    变异对象 = 覆盖缺口文件内容：无插桩环境里最直接的定向模糊形态——
    变异产物落在同材质输入域，比通用语料更可能踩进未覆盖分支。

    ``unit["path"]`` 来自被测项目的 coverage 产物（不可信）。提供 ``root``
    时该路径必须落在项目内且不含符号链接，否则跳过——否则 gaps JSON 可以
    把任意系统文件变成 fuzz 种子。
    """
    if gaps_path is None:
        return []
    try:
        data = json.loads(gaps_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[fuzz_input] ⚠ gaps 文件无法读取（忽略定向）: {exc}")
        return []
    units = data.get("units") if isinstance(data, dict) else data
    if not isinstance(units, list):
        print("[fuzz_input] ⚠ gaps JSON 无 units 数组（忽略定向）")
        return []
    seeds: list[bytes] = []
    for unit in units:
        if len(seeds) >= MAX_GAP_SEED_FILES:
            break
        if not isinstance(unit, dict):
            continue
        if unit.get("covered") is True:
            continue
        path = unit.get("path")
        if not isinstance(path, str) or not path:
            continue
        try:
            content = bugx_paths.read_confined_bytes(
                Path(root), path, max_bytes=MAX_GAP_SEED_BYTES, error_cls=ValueError,
            )
        except (ValueError, OSError):
            continue
        if content:
            seeds.append(content)
    return seeds


_RESERVED_WORDS = {
    b"def", b"class", b"return", b"import", b"from", b"with", b"as",
    b"if", b"elif", b"else", b"while", b"for", b"in", b"try", b"except",
    b"finally", b"raise", b"yield", b"async", b"await", b"pass", b"true",
    b"false", b"none", b"self", b"cls",
}


def extract_gap_tokens(gaps_path: Path | None, *, root: Path) -> list[bytes]:
    """从 coverage gaps JSON 提取未覆盖符号、函数名及目标标识符作为定向变异词库。

    ``unit["path"]`` 属于被测项目产物（不可信）；提供 ``root`` 时只读取项目内
    的普通文件，且不跟随符号链接。
    """
    if gaps_path is None:
        return []
    try:
        data = json.loads(gaps_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    units = data.get("units") if isinstance(data, dict) else data
    if not isinstance(units, list):
        return []
    tokens: set[bytes] = set()
    ident_pattern = re.compile(rb"[a-zA-Z_][a-zA-Z0-9_]{2,32}")
    for unit in units:
        if not isinstance(unit, dict) or unit.get("covered") is True:
            continue
        # 1. 显式提取 symbols / functions / missing / uncovered_symbols 字段
        for field in ("symbols", "functions", "uncovered_symbols", "missing", "uncovered_branches"):
            vals = unit.get(field)
            if isinstance(vals, list):
                for v in vals:
                    if isinstance(v, str) and v.strip():
                        tokens.add(v.strip().encode("utf-8"))
        # 2. 提取文件名标识符与文件内未覆盖符号
        path = unit.get("path")
        if isinstance(path, str) and path:
            stem = Path(path).stem
            if len(stem) >= 3:
                tokens.add(stem.encode("utf-8"))
            try:
                content = bugx_paths.read_confined_bytes(
                    Path(root), path, max_bytes=MAX_GAP_SEED_BYTES,
                    error_cls=ValueError,
                )
            except (ValueError, OSError):
                content = b""
            if content:
                for m in ident_pattern.findall(content):
                    lower_m = m.lower()
                    if lower_m not in _RESERVED_WORDS:
                        tokens.add(m)
                        if len(tokens) >= 120:
                            break
        if len(tokens) >= 120:
            break
    return sorted(tokens)


def load_seed(lang: str) -> list[bytes]:
    """从语言种子语料加载全部种子（每行一个，空行跳过）。"""
    f = SEED_CORPUS / f"{lang}.txt"
    if not f.is_file():
        print(f"[fuzz_input] 种子语料缺失: {f}")
        return []
    return [
        ln.encode("utf-8")
        for ln in f.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


# ---------- 变异策略 ----------

def _mutate_truncate(data: bytes, rng: random.Random) -> bytes:
    if len(data) <= 1:
        return b""
    cut = rng.randint(1, len(data) - 1)
    return data[:cut]


def _mutate_flip_byte(data: bytes, rng: random.Random) -> bytes:
    if not data:
        return data
    pos = rng.randrange(len(data))
    val = data[pos] ^ (1 << rng.randrange(8))
    return data[:pos] + bytes([val]) + data[pos + 1:]


def _mutate_insert_garbage(data: bytes, rng: random.Random) -> bytes:
    garbage = bytes(
        rng.choice(b"\x00\xff\x7f\n\r\x01\x1b") for _ in range(rng.randint(1, 16))
    )
    pos = rng.randrange(len(data) + 1)
    return data[:pos] + garbage + data[pos:]


_NUM_RE = re.compile(rb"-?\d+\.?\d*")

def _mutate_numeric(data: bytes, rng: random.Random) -> bytes:
    choices = [b"0", b"-1", b"999999999999999999999", b"1e999", b"-0.0"]
    def repl(m: re.Match) -> bytes:
        return rng.choice(choices)
    mutated, matches = _NUM_RE.subn(repl, data, count=rng.randint(1, 3))
    if matches:
        return mutated
    insertion = rng.choice(choices)
    position = rng.randrange(len(data) + 1)
    return data[:position] + insertion + data[position:]


_STR_FILL = [b"a" * 1000, "你好世界".encode(), "🎉🎉🎉".encode(),
             b"\x00\x01\x02", b"%s%s%s%s"]

_STRING_RE = re.compile(
    rb'(?P<prefix>[bBrRuUfF]{0,3})(?P<quote>"""|\'\'\'|"|\')'
    rb'(?P<body>(?:\\.|[^\\])*?)(?P=quote)'
)

def _mutate_string(data: bytes, rng: random.Random) -> bytes:
    def repl(match: re.Match) -> bytes:
        body = match.group("body")
        candidates = [fill for fill in _STR_FILL if fill != body]
        replacement = rng.choice(candidates) if candidates else body + b"x"
        return b"".join((
            match.group("prefix"), match.group("quote"), replacement,
            match.group("quote"),
        ))

    mutated, matches = _STRING_RE.subn(repl, data, count=rng.randint(1, 2))
    if matches:
        return mutated
    marker = rng.choice(_STR_FILL)
    position = rng.randrange(len(data) + 1)
    return data[:position] + b'"' + marker + b'"' + data[position:]


def _mutate_duplicate(data: bytes, rng: random.Random) -> bytes:
    if not data:
        return data
    mid = len(data) // 2
    chunk = data[mid:mid + rng.randint(1, max(1, len(data) // 4))]
    return data + chunk



# ---------- 语法感知与语义深层变异（Grammar-Aware Structural Mutators） ----------

_SECURITY_PAYLOADS = [
    b"../../../etc/passwd",
    b"'; SELECT pg_sleep(1); --",
    b"${IFS}cat${IFS}/etc/passwd",
    b"\x00\xff\xfe\xfd",
    b"A" * 4096,
    b"NaN",
    b"Infinity",
    b"-Infinity",
]


def _mutate_boundary_injection(data: bytes, rng: random.Random) -> bytes:
    """在输入任意位置注入经典安全与数值边界载荷。"""
    payload = rng.choice(_SECURITY_PAYLOADS)
    pos = rng.randrange(len(data) + 1)
    return data[:pos] + payload + data[pos:]


def _mutate_json_structural(data: bytes, rng: random.Random) -> bytes:
    """语法树保持型 JSON 结构化突变：穿透语法解析层，直击深层业务逻辑崩溃。"""
    try:
        raw_str = data.decode("utf-8")
        parsed = json.loads(raw_str)
    except Exception:
        return _mutate_flip_byte(data, rng)

    def _walk_and_mutate(obj: Any, depth: int = 0) -> Any:
        if depth > 4:
            return obj
        if isinstance(obj, dict):
            if not obj:
                return {"_fuzz_injected": "payload"}
            keys = list(obj.keys())
            action = rng.choice(["swap_val", "delete_key", "pollute_key", "nest_bomb", "walk"])
            target_key = rng.choice(keys)
            if action == "swap_val":
                obj[target_key] = rng.choice([None, {}, [], 1e308, -1, "\x00\xff", True, 9223372036854775807])
            elif action == "delete_key" and len(obj) > 1:
                del obj[target_key]
            elif action == "pollute_key":
                obj[rng.choice(["__proto__", "constructor", "prototype", "\x00key"])] = {"polluted": True}
            elif action == "nest_bomb":
                bomb: Any = "deep_recursion_overflow"
                for _ in range(25):
                    bomb = {"nested": bomb}
                obj[target_key] = bomb
            else:
                obj[target_key] = _walk_and_mutate(obj[target_key], depth + 1)
            return obj
        elif isinstance(obj, list):
            if not obj:
                return ["injected_item"]
            action = rng.choice(["insert_large", "type_swap", "mutate_item"])
            if action == "insert_large":
                obj.extend([rng.randint(-999999, 999999) for _ in range(20)])
            elif action == "type_swap":
                idx = rng.randrange(len(obj))
                obj[idx] = rng.choice([{}, None, "overflow_string", 0, 1e308])
            elif action == "mutate_item":
                idx = rng.randrange(len(obj))
                obj[idx] = _walk_and_mutate(obj[idx], depth + 1)
            return obj
        elif isinstance(obj, str):
            payloads = [
                "../../../../../../etc/passwd",
                "' OR '1'='1",
                "%s%s%s%s%n",
                "\x00\x01\x02",
                "A" * 1500,
                "\u202e\u202d\ufeff",
            ]
            return rng.choice(payloads)
        elif isinstance(obj, (int, float)):
            return rng.choice([0, -1, 1e308, -1e308, 9223372036854775807, -9223372036854775808])
        elif isinstance(obj, bool):
            return not obj
        return obj

    import copy
    mutated_obj = _walk_and_mutate(copy.deepcopy(parsed))
    try:
        return json.dumps(mutated_obj, ensure_ascii=False).encode("utf-8")
    except Exception:
        return data


def _mutate_gap_guided(data: bytes, rng: random.Random, tokens: list[bytes]) -> bytes:
    """覆盖导向自适应变异：将未覆盖目标符号定向合成到当前载荷中。"""
    if not tokens:
        return data
    target_token = rng.choice(tokens)
    stripped = data.strip()
    if (stripped.startswith(b"{") and stripped.endswith(b"}")) or (stripped.startswith(b"[") and stripped.endswith(b"]")):
        try:
            parsed = json.loads(data.decode("utf-8"))
            token_str = target_token.decode("utf-8", errors="replace")
            if isinstance(parsed, dict):
                key_choice = rng.choice(["inject_key", "inject_val", "inject_action"])
                if key_choice == "inject_key":
                    parsed[token_str] = "gap_targeted_probe"
                elif key_choice == "inject_val" and parsed:
                    k = rng.choice(list(parsed.keys()))
                    parsed[k] = token_str
                else:
                    parsed["action"] = token_str
                return json.dumps(parsed, ensure_ascii=False).encode("utf-8")
        except Exception:
            pass

    pos = rng.randrange(len(data) + 1)
    sep = b" " if (data and not data.endswith(b" ")) else b""
    return data[:pos] + sep + target_token + sep + data[pos:]


STRATEGIES = [
    ("truncate", _mutate_truncate),
    ("flip_byte", _mutate_flip_byte),
    ("insert_garbage", _mutate_insert_garbage),
    ("mutate_numeric", _mutate_numeric),
    ("mutate_string", _mutate_string),
    ("duplicate", _mutate_duplicate),
    ("boundary_injection", _mutate_boundary_injection),
    ("json_structural", _mutate_json_structural),
]


def mutate(
    data: bytes,
    rng: random.Random,
    gap_tokens: list[bytes] | None = None,
) -> tuple[str, bytes]:
    """对输入施加自适应变异策略，支持语法树保持与覆盖导向符号注入。"""
    # 覆盖导向增强：若提供未覆盖缺失符号，以 40% 倾向性权重定向注入缺失符号
    if gap_tokens and rng.random() < 0.40:
        mutated = _mutate_gap_guided(data, rng, gap_tokens)
        if mutated != data:
            return "gap_guided", mutated

    # 自省检测：如果是合法的 JSON 结构化数据，优先以 65% 概率调用语法感知结构变异
    stripped = data.strip()
    if (stripped.startswith(b"{") and stripped.endswith(b"}")) or (stripped.startswith(b"[") and stripped.endswith(b"]")):
        if rng.random() < 0.65:
            mutated = _mutate_json_structural(data, rng)
            if mutated != data:
                return "json_structural", mutated

    name, fn = rng.choice(STRATEGIES)
    mutated = fn(data, rng)
    if mutated != data:
        return name, mutated
    return name, _mutate_flip_byte(data, rng) if data else b"\x00"


def _seed_for_index(seeds: list[bytes], index: int) -> bytes:
    if not seeds:
        raise ValueError("seeds 不能为空")
    return seeds[index % len(seeds)]


def _crash_signature(kind: str, exit_code: object, stderr: bytes = b"") -> str:
    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr)
    text = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", text)
    text = re.sub(r"\b\d+\b", "N", text)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())[:500]
    value = "\0".join((kind, str(exit_code), text))
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def _group_crashes(crashes: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for crash in sorted(crashes, key=lambda item: item["idx"]):
        signature = crash["crash_signature"]
        current = grouped.get(signature)
        if current is None:
            current = dict(crash)
            current["occurrences"] = 0
            current["sample_indices"] = []
            current["payload_hashes"] = []
            grouped[signature] = current
        current["occurrences"] += 1
        current["sample_indices"].append(crash["idx"])
        payload_hash = crash.get("payload_sha256")
        if payload_hash and payload_hash not in current["payload_hashes"]:
            current["payload_hashes"].append(payload_hash)
    return list(grouped.values())


# ---------- 执行与筛选 ----------

def _parse_code_set(value: str, name: str) -> frozenset[int]:
    try:
        values = frozenset(int(item.strip()) for item in value.split(",") if item.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是逗号分隔的整数") from exc
    if not values:
        raise ValueError(f"{name} 不能为空")
    return values


_FEEDBACK_KEYS = frozenset({"status_code", "state", "error_signature", "feedback"})
_FEEDBACK_SECRET = re.compile(
    r"(?:bearer\s+\S+|(?:token|secret|password|passwd|api[_-]?key)\s*[:=]\s*\S+)",
    re.I,
)


def _feedback_value(value: object) -> str | int | float | bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and value == value and abs(value) != float("inf"):
        return value
    if isinstance(value, str):
        value = _FEEDBACK_SECRET.sub("[REDACTED]", value.strip())[:128]
        return value or None
    return None


def _extract_feedback(stdout: bytes, stderr: bytes, enabled: bool) -> dict:
    """读取显式 envelope；非 JSON 或未知字段都按无反馈处理。"""
    if not enabled:
        return {}
    for stream in (stdout, stderr):
        lines = stream.decode("utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(decoded, dict):
                continue
            candidate = decoded.get("feedback") if isinstance(decoded.get("feedback"), dict) else decoded
            vector: dict[str, object] = {}
            for key in _FEEDBACK_KEYS - {"feedback"}:
                if key not in candidate:
                    continue
                safe = _feedback_value(candidate[key])
                if safe is not None:
                    vector[key] = safe
            nested = candidate.get("feedback") if isinstance(candidate, dict) else None
            if isinstance(nested, dict):
                safe_nested = {
                    str(key)[:64]: safe
                    for key, value in list(nested.items())[:8]
                    if (safe := _feedback_value(value)) is not None
                }
                if safe_nested:
                    vector["feedback"] = safe_nested
            return vector
    return {}


def _feedback_vector(feedback: dict, duration_ms: float,
                     latency_baseline: float | None, latency_factor: float,
                     watch_status: frozenset[int]) -> dict:
    vector = dict(feedback)
    status = vector.get("status_code")
    if isinstance(status, str) and status.isdigit():
        status = int(status)
        vector["status_code"] = status
    slow = (
        latency_baseline is not None
        and latency_baseline > 0
        and latency_factor > 0
        and duration_ms / 1000.0 >= latency_baseline * latency_factor
    )
    vector["latency_class"] = "slow" if slow else "normal"
    vector["watched_status"] = isinstance(status, int) and status in watch_status
    return vector


def _behavior_hash(exit_code: object, stdout: bytes = b"", stderr: bytes = b"",
                   feedback: Mapping[str, object] | None = None) -> str:
    """行为指纹：退出码、双流与显式反馈向量的 SHA-256。"""
    digest = hashlib.sha256()
    digest.update(str(exit_code).encode())
    digest.update(b"\0")
    digest.update(stdout[:4096])
    digest.update(b"\0")
    digest.update(stderr[:4096])
    digest.update(b"\0")
    digest.update(json.dumps(feedback or {}, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8"))
    return digest.hexdigest()


def _load_evolution(evolve: Path | None) -> tuple[set[str], list[bytes]]:
    """读取既有行为哈希库与进化种子；目录不存在则初始化空库。"""
    behaviors: set[str] = set()
    seeds: list[bytes] = []
    if evolve is None:
        return behaviors, seeds
    index = evolve / "behaviors.json"
    if index.is_file():
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
            behaviors = {
                str(item) for item in (data or {}).get("behaviors", [])
                if isinstance(item, str)
            }
        except (OSError, json.JSONDecodeError):
            print(f"[fuzz_input] ⚠ 行为库损坏，按空库重启: {index}")
    seeds_dir = evolve / "seeds"
    if seeds_dir.is_dir():
        for path in sorted(seeds_dir.glob("*.bin")):
            if path.is_symlink():
                continue
            try:
                if path.stat().st_size > MAX_EVOLUTION_SEED_BYTES:
                    continue
                seeds.append(path.read_bytes())
            except OSError:
                continue
    return behaviors, seeds


def _save_evolution(evolve: Path, behaviors: set[str],
                    new_records: list[dict], max_seeds: int = 200) -> int:
    """把新行为样本写回进化目录（种子 + 行为库），返回新增种子数。"""
    bugx_paths.ensure_dir(evolve)
    seeds_dir = bugx_paths.ensure_dir(evolve / "seeds")
    written = 0
    for record in new_records:
        payload = record.get("payload")
        if not isinstance(payload, bytes) or len(payload) > MAX_EVOLUTION_SEED_BYTES:
            continue
        name = f"seed-{record['idx']:08d}-{record['behavior_sha256'][:12]}.bin"
        target = seeds_dir / name
        if target.is_symlink():
            continue
        target.write_bytes(payload)
        written += 1
    existing_seeds = sorted(seeds_dir.glob("*.bin"))
    if len(existing_seeds) > max_seeds:
        for stale in existing_seeds[:len(existing_seeds) - max_seeds]:
            try:
                stale.unlink()
            except OSError:
                pass
    index_path = evolve / "behaviors.json"
    if index_path.is_symlink():
        raise ValueError(f"拒绝写入符号链接行为库: {index_path}")
    content = json.dumps({
            "schema_version": 1,
            "behaviors": sorted(behaviors),
            "seeds": min(len(existing_seeds), max_seeds),
        }, ensure_ascii=False, indent=2) + "\n"
    temporary = index_path.with_name(f".{index_path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(index_path)
    return written


def _run_target(cmd: str, payload: bytes, tmp: Path,
                timeout: float, allow_shell: bool = False) -> subprocess.CompletedProcess:
    if len(payload) > MAX_INPUT_BYTES:
        raise _PayloadLimitError(
            f"输入样本超过 {MAX_INPUT_BYTES} 字节上限: {len(payload)}"
        )
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("timeout 必须是正数")
    if not tmp.is_dir():
        raise ValueError(f"执行工作目录不是目录: {tmp}")
    inp = tmp / "input"
    if inp.is_symlink():
        inp.unlink()
    inp.write_bytes(payload)
    if allow_shell:
        rendered = cmd.replace("{input}", shlex.quote(str(inp)))
        argv = _shell_argv(rendered)
    else:
        argv = [token.replace("{input}", str(inp)) for token in _split_argv(cmd)]
    return run_bounded(
        argv,
        cwd=tmp,
        timeout=timeout,
        max_output_bytes=MAX_OUTPUT_BYTES,
    )


def fuzz_once(cmd: str, data: bytes, rng: random.Random, tmp: Path,
              timeout: float, idx: int, allow_shell: bool = False,
              seen_payloads: set[str] | None = None,
              seen_lock: threading.Lock | None = None,
              track_behavior: bool = False,
              seen_behaviors: set[str] | None = None,
              accepted_exits: frozenset[int] | None = None,
              feedback_json: bool = False,
              latency_baseline: float | None = None,
              latency_factor: float = 3.0,
              watch_status: frozenset[int] | None = None,
              gap_tokens: list[bytes] | None = None) -> dict | None:
    """跑一个变异样本；异常或新的反馈行为返回记录，正常返回 None。

    ``track_behavior`` 开启后，正常退出但行为指纹未见过的样本也返回
    ``kind="new-behavior"`` 记录——它们会被回填为下一轮种子，实现
    行为多样性引导（无插桩环境下的最小覆盖引导形态）。
    """
    name, payload = mutate(data, rng, gap_tokens=gap_tokens)
    accepted_exits = accepted_exits or frozenset({0})
    watch_status = watch_status or frozenset()
    payload_hash = hashlib.sha256(payload).hexdigest()
    if seen_payloads is not None:
        if seen_lock is None:
            duplicate = payload_hash in seen_payloads
            seen_payloads.add(payload_hash)
        else:
            with seen_lock:
                duplicate = payload_hash in seen_payloads
                seen_payloads.add(payload_hash)
        if duplicate:
            return {
                "idx": idx, "strategy": name, "kind": "duplicate",
                "len": len(payload), "payload_sha256": payload_hash,
            }
    case_tmp = tmp / f"case-{idx:08d}"
    case_tmp.mkdir(parents=True, exist_ok=True)
    try:
        r = _run_target(cmd, payload, case_tmp, timeout, allow_shell)
    except _PayloadLimitError as exc:
        behavior = _behavior_hash("INPUT_LIMIT")
        return {
            "idx": idx, "strategy": name, "kind": "input-limit",
            "exit": "INPUT_LIMIT", "len": len(payload), "payload": payload,
            "payload_sha256": payload_hash,
            "crash_signature": _crash_signature("input-limit", "INPUT_LIMIT"),
            "behavior_sha256": behavior, "error": str(exc),
        }
    except OSError as exc:
        behavior = _behavior_hash("EXECUTION_ERROR")
        return {
            "idx": idx, "strategy": name, "kind": "execution-error",
            "exit": "EXECUTION_ERROR", "len": len(payload), "payload": payload,
            "payload_sha256": payload_hash,
            "crash_signature": _crash_signature("execution-error", "EXECUTION_ERROR"),
            "behavior_sha256": behavior, "error": str(exc),
        }
    if getattr(r, "timed_out", False):
        behavior = _behavior_hash("TIMEOUT")
        return {
            "idx": idx, "strategy": name, "kind": "timeout",
            "exit": "TIMEOUT", "len": len(payload), "payload": payload,
            "payload_sha256": payload_hash,
            "crash_signature": _crash_signature("timeout", "TIMEOUT"),
            "behavior_sha256": behavior,
        }
    stdout = _as_bytes(r.stdout)
    stderr = _as_bytes(r.stderr)
    duration_ms = round(float(getattr(r, "duration_seconds", 0.0) or 0.0) * 1000, 2)
    feedback = _extract_feedback(stdout, stderr, feedback_json)
    vector = _feedback_vector(
        feedback, duration_ms, latency_baseline, latency_factor, watch_status,
    )
    if getattr(r, "output_exceeded", False):
        behavior = _behavior_hash("OUTPUT_LIMIT", stdout, stderr, vector)
        return {
            "idx": idx, "strategy": name, "kind": "output-limit",
            "exit": "OUTPUT_LIMIT", "len": len(payload), "payload": payload,
            "stderr": stderr[:2000], "payload_sha256": payload_hash,
            "crash_signature": _crash_signature("output-limit", "OUTPUT_LIMIT", stderr),
            "behavior_sha256": behavior, "output_limit": MAX_OUTPUT_BYTES,
            "duration_ms": duration_ms, "feedback": vector,
        }
    behavior = _behavior_hash(r.returncode, stdout, stderr, vector)
    if r.returncode not in accepted_exits:
        return {
            "idx": idx, "strategy": name, "kind": "crash",
            "exit": r.returncode, "len": len(payload), "payload": payload,
            "stderr": stderr[:2000],
            "payload_sha256": payload_hash,
            "crash_signature": _crash_signature("crash", r.returncode, stderr),
            "behavior_sha256": behavior, "duration_ms": duration_ms,
            "feedback": vector,
        }
    if vector.get("watched_status") or vector.get("latency_class") == "slow":
        kind = "feedback-anomaly" if vector.get("watched_status") else "latency-anomaly"
        return {
            "idx": idx, "strategy": name, "kind": kind,
            "exit": r.returncode, "len": len(payload), "payload": payload,
            "stderr": stderr[:2000], "payload_sha256": payload_hash,
            "crash_signature": _crash_signature(kind, vector, stderr),
            "behavior_sha256": behavior, "duration_ms": duration_ms,
            "feedback": vector,
        }
    if track_behavior and seen_behaviors is not None:
        if seen_lock is None:
            known = behavior in seen_behaviors
            seen_behaviors.add(behavior)
        else:
            with seen_lock:
                known = behavior in seen_behaviors
                seen_behaviors.add(behavior)
        if not known:
            return {
                "idx": idx, "strategy": name, "kind": "new-behavior",
                "exit": 0, "len": len(payload), "payload": payload,
                "payload_sha256": payload_hash,
                "behavior_sha256": behavior,
                "duration_ms": duration_ms, "feedback": vector,
            }
    return None


def fuzz_target(
    cmd: str,
    *,
    input_content: str | bytes | None = None,
    input_path: str | Path | None = None,
    root: str | Path | None = None,
    lang: str | None = None,
    count: int = 50,
    jobs: int = 4,
    timeout: float = 5.0,
    allow_shell: bool = False,
    out_dir: str | Path | None = None,
    target_gaps: str | Path | None = None,
    accepted_exits: set[int] | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """供 Python 模块或 MCP 工具调用的输入变异模糊测试执行引擎。

    ``input_content`` 是**纯内容**：即使它的字面量恰好等于某个已存在文件的
    路径，也不会被当作文件读取。需要「从项目内文件取种子」时显式使用
    ``input_path``（必须落在 ``root`` 内且不含符号链接）。把两者合并成
    「恰好是文件就当路径」的嗅探，曾让 ``input_content`` 变成任意文件读取入口。
    """
    if count < 1 or jobs < 1 or timeout <= 0:
        raise ValueError("count、jobs 必须 >= 1，timeout 必须 > 0")
    if not allow_shell:
        _split_argv(cmd)

    actual_seed = seed if seed is not None else random.SystemRandom().randrange(2**63)
    seed_inputs: list[bytes] | None = None
    gap_path = Path(target_gaps) if target_gaps else None
    gap_root = Path(root) if root is not None else None
    gap_seeds = load_gap_seeds(gap_path, root=gap_root)
    gap_tokens = extract_gap_tokens(gap_path, root=gap_root)

    if input_content is not None and input_path is not None:
        raise ValueError("input_content 与 input_path 不能同时提供")
    if input_path is not None:
        if root is None:
            raise ValueError("使用 input_path 时必须提供 root 以校验项目边界")
        data = _read_confined_input(root, input_path)
    elif input_content is not None:
        if isinstance(input_content, bytes):
            data = input_content
        else:
            data = str(input_content).encode("utf-8")
    elif gap_seeds:
        seed_inputs = gap_seeds
        data = seed_inputs[0]
    elif lang:
        seed_inputs = load_seed(lang)
        if not seed_inputs:
            raise ValueError(f"指定语言种子缺失: {lang}")
        data = seed_inputs[0]
    else:
        data = b'{"test": 1, "value": "demo"}'

    temp_out_dir = None
    if out_dir:
        out = Path(out_dir).expanduser().resolve()
        bugx_paths.ensure_dir(out)
    else:
        temp_out_dir = tempfile.TemporaryDirectory(prefix="bugx-fuzz-")
        out = Path(temp_out_dir.name)

    crashes_dir = bugx_paths.ensure_dir(out / "crashes")

    crashes: list[dict] = []
    duplicates_skipped = 0
    seen_payloads: set[str] = set()
    seen_lock = threading.Lock()
    accepted = accepted_exits or {0}

    with tempfile.TemporaryDirectory(prefix="fuzz-tmp-") as td:
        tmp = Path(td)

        def _seed_pool(index: int) -> bytes:
            base = [data] if seed_inputs is None else seed_inputs
            pool = gap_seeds + base
            return pool[index % len(pool)]

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futs = {
                pool.submit(
                    fuzz_once, cmd,
                    _seed_pool(i),
                    random.Random(actual_seed + i), tmp,
                    timeout, i, allow_shell,
                    seen_payloads, seen_lock,
                    False, None, accepted, None,
                    None, 3.0, None, gap_tokens,
                ): i
                for i in range(count)
            }
            for fut in as_completed(futs):
                try:
                    res = fut.result()
                except Exception:
                    continue
                if res is not None and res.get("kind") == "duplicate":
                    duplicates_skipped += 1
                elif res is not None and res.get("kind") != "new-behavior":
                    crashes.append(res)

    unique_crashes = _group_crashes(crashes)
    records = []
    for c in unique_crashes:
        rec = {k: v for k, v in c.items() if k != "payload"}
        if isinstance(rec.get("stderr"), bytes):
            rec["stderr"] = rec["stderr"].decode("utf-8", errors="replace")
        records.append(rec)

    return {
        "status": "ok",
        "total": count,
        "executed": count - duplicates_skipped,
        "duplicates_skipped": duplicates_skipped,
        "anomalies": len(crashes),
        "unique_anomalies": len(unique_crashes),
        "crashes": records[:10],
        "crash_signatures": sorted(list({c.get("crash_signature", "") for c in unique_crashes if c.get("crash_signature")})),
        "out_dir": str(out),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.count < 1 or args.jobs < 1 or args.timeout <= 0:
        print("[fuzz_input] --count、--jobs 必须 >= 1，--timeout 必须 > 0")
        return 2
    if args.latency_baseline is not None and args.latency_baseline <= 0:
        print("[fuzz_input] --latency-baseline 必须 > 0")
        return 2
    if args.latency_factor <= 0:
        print("[fuzz_input] --latency-factor 必须 > 0")
        return 2
    try:
        accepted_exits = _parse_code_set(args.accepted_exits, "--accepted-exits")
        watch_status = _parse_code_set(args.watch_status, "--watch-status")
    except ValueError as exc:
        print(f"[fuzz_input] {exc}")
        return 2
    if not args.allow_shell:
        try:
            _split_argv(args.cmd)
        except ValueError as exc:
            print(f"[fuzz_input] 命令无效: {exc}")
            return 2
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**63)
    seed_inputs: list[bytes] | None = None
    gap_seeds = load_gap_seeds(args.target_gaps, root=args.root)
    gap_tokens = extract_gap_tokens(args.target_gaps, root=args.root)
    if gap_seeds:
        print(f"[fuzz_input] 定向模式：{len(gap_seeds)} 个欠覆盖文件作为优先种子")
    if gap_tokens:
        print(f"[fuzz_input] 覆盖导向：提取到 {len(gap_tokens)} 个缺失符号引导变异")
    if args.input:
        src = Path(args.input)
        if not src.is_file():
            print(f"[fuzz_input] 输入文件不存在: {src}")
            return 2
        data = src.read_bytes()
        source_name = src.name
    elif gap_seeds:
        seed_inputs = gap_seeds
        data = seed_inputs[0]
        source_name = f"gap-seeds（{len(gap_seeds)} 个欠覆盖文件）"
    elif args.lang:
        seed_inputs = load_seed(args.lang)
        if not seed_inputs:
            return 2
        data = seed_inputs[0]
        source_name = f"seed:{args.lang}（{len(seed_inputs)} 条，按样本轮询）"
    else:
        print("[fuzz_input] 必须提供 --input / --lang / --target-gaps 之一作为种子来源")
        return 2
    out = Path(args.out).expanduser()
    if out.is_symlink():
        print(f"[fuzz_input] 拒绝写入符号链接输出目录: {out}")
        return 2
    out = out.resolve()
    bugx_paths.ensure_dir(out)
    crashes_dir = bugx_paths.ensure_dir(out / "crashes")

    evolve_arg = args.evolve.expanduser() if args.evolve else None
    if evolve_arg and evolve_arg.is_symlink():
        print(f"[fuzz_input] 拒绝使用符号链接进化目录: {evolve_arg}")
        return 2
    evolve = evolve_arg.resolve() if evolve_arg else None
    seen_behaviors, evolve_seeds = _load_evolution(evolve)
    if evolve and evolve_seeds:
        print(f"[fuzz_input] 进化种子库: {len(evolve_seeds)} 条 "
              f"(已知行为 {len(seen_behaviors)} 个)")

    print(f"[fuzz_input] 目标: {source_name} ({len(data)} 字节) "
          f"样本={args.count} 并发={args.jobs} 种子={seed}")
    crashes: list[dict] = []
    new_behaviors: list[dict] = []
    duplicates_skipped = 0
    seen_payloads: set[str] = set()
    seen_lock = threading.Lock()
    with tempfile.TemporaryDirectory(prefix="fuzz-") as td:
        tmp = Path(td)

        def _seed_pool(index: int) -> bytes:
            base = [data] if seed_inputs is None else seed_inputs
            pool = gap_seeds + base + evolve_seeds
            return pool[index % len(pool)]

        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futs = {
                pool.submit(fuzz_once, args.cmd,
                            _seed_pool(i),
                            random.Random(seed + i), tmp,
                            args.timeout, i, args.allow_shell,
                            seen_payloads, seen_lock,
                            bool(evolve),
                            seen_behaviors if evolve else None,
                            accepted_exits, args.feedback_json,
                            args.latency_baseline, args.latency_factor,
                            watch_status, gap_tokens): i
                for i in range(args.count)
            }
            for fut in as_completed(futs):
                try:
                    res = fut.result()
                except Exception as e:  # noqa: BLE001
                    print(f"  ! 任务异常: {e}")
                    continue
                if res is not None and res.get("kind") == "duplicate":
                    duplicates_skipped += 1
                elif res is not None and res.get("kind") == "new-behavior":
                    new_behaviors.append(res)
                elif res is not None:
                    crashes.append(res)

    # 写异常样本
    meta = out / "summary.json"
    unique_crashes = _group_crashes(crashes)
    records = []
    for c in unique_crashes:
        fn = crashes_dir / (
            f"crash-{c['idx']:04d}-{c['strategy']}-exit{c['exit']}"
            f"-{c['crash_signature']}"
        )
        fn.write_bytes(c["payload"])
        rec = {k: v for k, v in c.items() if k != "payload"}
        if isinstance(rec.get("stderr"), bytes):
            rec["stderr"] = rec["stderr"].decode("utf-8", errors="replace")
        rec["file"] = fn.name
        records.append(rec)
    meta.write_text(
        json.dumps({
            "total": args.count,
            "executed": args.count - duplicates_skipped,
            "duplicates_skipped": duplicates_skipped,
            "anomalies": len(crashes),
            "unique_anomalies": len(unique_crashes),
            "new_behaviors": len(new_behaviors) if evolve else None,
            "feedback_anomalies": sum(
                1 for item in crashes if item.get("kind") == "feedback-anomaly"
            ),
            "latency_anomalies": sum(
                1 for item in crashes if item.get("kind") == "latency-anomaly"
            ),
            "accepted_exits": sorted(accepted_exits),
            "feedback_json": args.feedback_json,
            "evolve_dir": str(evolve) if evolve else None,
            "crashes": records,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if evolve:
        # 异常行为同样入行为库：同一崩溃签名跨轮不必重复留种。
        for c in crashes:
            if c.get("behavior_sha256"):
                seen_behaviors.add(c["behavior_sha256"])
        seed_records = new_behaviors + [
            c for c in crashes if c.get("behavior_sha256")
        ]
        written = _save_evolution(evolve, seen_behaviors, seed_records)
        print(f"[fuzz_input] 进化回写: 新行为 {len(new_behaviors)} 个，"
              f"新增种子 {written} 条（库内行为共 {len(seen_behaviors)} 个）-> {evolve}")

    by_kind: dict[str, int] = {}
    for c in crashes:
        by_kind[c["kind"]] = by_kind.get(c["kind"], 0) + 1
    print(f"[fuzz_input] 完成: {args.count} 样本, 去重跳过 {duplicates_skipped} 个，"
          f"异常 {len(crashes)} 个（唯一签名 {len(unique_crashes)}，{by_kind}）")
    print(f"[fuzz_input] 异常样本已写入: {crashes_dir}")
    print(f"[fuzz_input] 汇总: {meta}")
    if crashes:
        print("  下一步: 用 minimize_repro.py 把异常样本缩到最小复现，再定位根因。")
    if evolve:
        print("  进化模式: 新行为样本已回填种子，下一轮 --evolve 同目录继续。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
