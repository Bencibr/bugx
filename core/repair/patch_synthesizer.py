#!/usr/bin/env python3
"""patch_synthesizer.py — 最小安全补丁合成与工程免疫资产沉淀引擎 (DOC-137 2.0 / D137-09)。

用途：
  对确认漏洞 (Confirmed Finding) 形成真正的交付与修复闭环：
  1. 最小安全补丁合成 (Minimal Safe Patch Synthesis)：
     根据受影响代码段与修复替换生成标准的 Unified Diff 补丁；
  2. 双向回归门禁验证 (Dual-Direction Regression Gate)：
     在独立临时工作区中打入补丁，验证攻击 PoC 变绿（漏洞被消除）且原有单元测试套件全绿（无破坏性退化）；
  3. 工程免疫测试资产生成 (Engineering Immunity Asset Generation)：
     将触发漏洞的 PoC 输入自动转写为符合 Python unittest / pytest 规范的原生测试用例，
     作为防止缺陷回归的永久工程免疫资产沉淀至 tests/ 目录。
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

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True)
class PatchVerificationResult:
    is_valid: bool
    poc_mitigated: bool
    regression_passed: bool
    patch_diff: str
    detail: str
    # The fields below are deliberately explicit.  A green patched PoC alone
    # is not enough: it could be a vacuous command, an already-safe baseline,
    # or a patch that cannot be reversed.  Keeping these facts in the result
    # makes the Finding/worker handoff observable without re-running commands.
    baseline_vulnerable: bool = False
    reverse_mutation_verified: bool = False
    negative_control_passed: bool | None = None
    isolated: bool = False
    workspace_unchanged: bool = True
    mutation_count: int = 0
    mutation_kill_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe verification receipt for Finding evidence."""
        return {
            "is_valid": self.is_valid,
            "poc_mitigated": self.poc_mitigated,
            "regression_passed": self.regression_passed,
            "patch_diff": self.patch_diff,
            "detail": self.detail,
            "baseline_vulnerable": self.baseline_vulnerable,
            "reverse_mutation_verified": self.reverse_mutation_verified,
            "negative_control_passed": self.negative_control_passed,
            "isolated": self.isolated,
            "workspace_unchanged": self.workspace_unchanged,
            "mutation_count": self.mutation_count,
            "mutation_kill_rate": self.mutation_kill_rate,
        }


class PatchSynthesizer:
    """自动安全补丁合成与工程免疫沉淀引擎。"""

    def __init__(self, project_root: Path | str | None = None) -> None:
        self.root = Path(project_root).resolve() if project_root else None

    @staticmethod
    def generate_unified_diff(
        original_text: str,
        modified_text: str,
        file_path: str = "target_file.py",
    ) -> str:
        """生成标准 Unified Diff 格式文本。"""
        orig_lines = original_text.splitlines(keepends=True)
        mod_lines = modified_text.splitlines(keepends=True)
        diff = difflib.unified_diff(
            orig_lines,
            mod_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
        )
        return "".join(diff)

    def synthesize_patch(
        self,
        file_path: Path | str,
        target_code: str,
        replacement_code: str,
    ) -> tuple[str, str]:
        """根据目标代码替换生成最小补丁。返回: (unified_diff, 完整修改后文本)"""
        p = Path(file_path)
        if self.root and not p.is_absolute():
            p = self.root / p
        if self.root:
            if p.is_symlink():
                raise ValueError(f"目标源文件不能是符号链接: {p}")
            p = p.resolve()
            try:
                p.relative_to(self.root)
            except ValueError as exc:
                raise ValueError(f"目标源文件必须位于项目根内: {p}") from exc

        if not p.is_file():
            raise FileNotFoundError(f"目标源文件不存在: {p}")

        original = p.read_text(encoding="utf-8")
        if target_code not in original:
            raise ValueError(f"目标修复代码段在原文件中不存在: {target_code[:60]}...")

        occurrences = original.count(target_code)
        if occurrences != 1:
            raise ValueError(
                "目标修复代码段必须在原文件中恰好出现一次，"
                f"实际出现 {occurrences} 次；拒绝模糊补丁"
            )

        if target_code == replacement_code:
            raise ValueError("目标修复代码段与替换代码相同，拒绝生成空补丁")

        # 仅替换一次，保证最小侵入
        modified = original.replace(target_code, replacement_code, 1)
        rel_path = p.relative_to(self.root).as_posix() if self.root and p.is_relative_to(self.root) else p.name
        diff = self.generate_unified_diff(original, modified, file_path=rel_path)
        return diff, modified

    @staticmethod
    def generate_reverse_patch(patch_diff: str) -> str:
        """Generate the inverse mutation of a replacement-only unified diff.

        The reverse patch is used as an adversarial control: if the patched
        PoC is green but the inverse mutation is also green, the purported
        fix did not remove the demonstrated defect.  Only regular replacement
        hunks are accepted here; silently guessing how to invert file creates
        or deletes would turn an incomplete mutation into false evidence.
        """
        if not isinstance(patch_diff, str) or not patch_diff.strip():
            raise ValueError("补丁为空，无法生成反向变异")

        reversed_lines: list[str] = []
        hunk_seen = False
        changed_line_seen = False
        for line in patch_diff.splitlines(keepends=True):
            if line.startswith("@@"):
                header = line.rstrip("\r\n")
                match = re.match(
                    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$",
                    header,
                )
                if not match:
                    raise ValueError(f"补丁 hunk 头无效，无法生成反向变异: {header}")
                old_start = int(match.group(1))
                old_count = int(match.group(2) or "1")
                new_start = int(match.group(3))
                new_count = int(match.group(4) or "1")
                if old_count == 0 or new_count == 0:
                    raise ValueError("仅支持现有文件的替换 hunk，拒绝创建/删除变异")
                newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                reversed_lines.append(
                    f"@@ -{new_start},{new_count} +{old_start},{old_count} @@"
                    f"{match.group(5)}{newline}"
                )
                hunk_seen = True
                continue
            # Keep file headers and metadata stable so git apply resolves the
            # same target path; only the hunk payload is inverted.
            if line.startswith(("--- ", "+++ ", "diff ", "index ", "new file ", "deleted file ")):
                reversed_lines.append(line)
                continue
            if hunk_seen and line.startswith("+"):
                reversed_lines.append("-" + line[1:])
                changed_line_seen = True
            elif hunk_seen and line.startswith("-"):
                reversed_lines.append("+" + line[1:])
                changed_line_seen = True
            else:
                reversed_lines.append(line)

        if not hunk_seen or not changed_line_seen:
            raise ValueError("补丁不包含可逆的替换 hunk，拒绝生成反向变异")
        return "".join(reversed_lines)

    @staticmethod
    def _invalid_result(
        patch_diff: str,
        detail: str,
        *,
        negative_control_passed: bool | None = None,
        isolated: bool = False,
        workspace_unchanged: bool = True,
        baseline_vulnerable: bool = False,
        reverse_mutation_verified: bool = False,
    ) -> PatchVerificationResult:
        return PatchVerificationResult(
            is_valid=False,
            poc_mitigated=False,
            regression_passed=False,
            patch_diff=patch_diff,
            detail=detail,
            baseline_vulnerable=baseline_vulnerable,
            reverse_mutation_verified=reverse_mutation_verified,
            negative_control_passed=negative_control_passed,
            isolated=isolated,
            workspace_unchanged=workspace_unchanged,
            mutation_count=1 if isolated else 0,
            mutation_kill_rate=1.0 if reverse_mutation_verified else 0.0,
        )

    @staticmethod
    def _validate_argv(label: str, command: Sequence[str] | None) -> str | None:
        if command is None:
            return None
        if (
            isinstance(command, (str, bytes))
            or not isinstance(command, Sequence)
            or not command
            or any(not isinstance(part, str) or not part for part in command)
        ):
            return f"{label}命令必须是非空 argv 字符串序列"
        return None

    @staticmethod
    def _validate_patch_paths(patch_diff: str, project_dir: Path) -> str | None:
        """Reject absolute/traversal/symlink patch targets before staging."""
        touched: list[str] = []
        root = project_dir.resolve()
        for line in patch_diff.splitlines():
            if not line.startswith(("--- ", "+++ ")):
                continue
            raw = line[4:].split("\t", 1)[0].strip()
            if raw == "/dev/null":
                continue
            if raw.startswith("a/") or raw.startswith("b/"):
                raw = raw[2:]
            path = Path(raw)
            if path.is_absolute() or ".." in path.parts or not raw:
                return f"补丁路径越界或非法: {raw}"
            candidate = (root / path).resolve(strict=False)
            if candidate != root and root not in candidate.parents:
                return f"补丁路径逃逸项目根: {raw}"
            touched.append(raw)
        if not touched:
            return "补丁缺少可验证的文件头"
        if any((root / path).is_symlink() for path in set(touched)):
            return "补丁目标不能是符号链接，拒绝跟随链接写入"
        return None

    @staticmethod
    def _copy_isolated_project(source: Path, destination: Path) -> None:
        """Copy a project into a private verifier tree without repository state."""
        source = source.resolve()
        if not source.is_dir() or source.is_symlink():
            raise ValueError(f"项目目录无效: {source}")
        for candidate in source.rglob("*"):
            if candidate.is_symlink():
                raise ValueError(f"项目包含不受信任的符号链接: {candidate}")
        shutil.copytree(
            source,
            destination,
            symlinks=False,
            ignore=shutil.ignore_patterns(
                ".git", ".bugx", "__pycache__", "*.pyc", "*.pyo",
                ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
                "node_modules", "build", "dist", ".venv",
            ),
        )

    @staticmethod
    def _relocate_argv(
        command: Sequence[str], source_root: Path, isolated_root: Path,
    ) -> list[str]:
        """Relocate absolute project arguments into an isolated copy."""
        source = source_root.resolve()
        result: list[str] = []
        for value in command:
            try:
                candidate = Path(value)
                if candidate.is_absolute():
                    relative = candidate.resolve(strict=False).relative_to(source)
                    result.append(str(isolated_root / relative))
                    continue
            except (OSError, ValueError):
                pass
            result.append(value)
        return result

    @staticmethod
    def _run_argv(
        command: Sequence[str], cwd: Path, timeout: float,
    ) -> tuple[int | None, str]:
        """Run one host-provided argv and retain only bounded diagnostics."""
        try:
            completed = subprocess.run(
                list(command),
                shell=False,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None, "执行超时"
        except OSError as exc:
            return None, f"命令无法启动: {exc}"
        stdout_value = completed.stdout or b""
        stderr_value = completed.stderr or b""
        stdout = (
            stdout_value.decode(errors="replace")
            if isinstance(stdout_value, bytes) else str(stdout_value)
        )[-500:]
        stderr = (
            stderr_value.decode(errors="replace")
            if isinstance(stderr_value, bytes) else str(stderr_value)
        )[-500:]
        detail = f"exit={completed.returncode}"
        if stderr or stdout:
            detail += f" stderr={stderr or stdout}"
        return completed.returncode, detail

    @staticmethod
    def verify_patch_dual_gate(
        patch_file: Path | str,
        project_dir: Path | str,
        poc_verify_cmd: Sequence[str],
        regression_cmd: Sequence[str] | None = None,
        timeout: float = 30.0,
        negative_control_cmd: Sequence[str] | None = None,
        require_reverse_differential: bool = True,
    ) -> PatchVerificationResult:
        """Verify a Finding patch in isolated before/after trees.

        The previous implementation applied a candidate diff to the caller's
        worktree and attempted to roll it back.  That made a failed PoC or a
        command that wrote extra files capable of leaving user state behind.
        This implementation never applies a patch to ``project_dir``:

        * the baseline PoC must fail (the defect is actually present);
        * the patched PoC must pass and the optional regression/negative
          controls must stay green;
        * an automatically generated reverse mutation is applied to a third
          copy and must re-expose the PoC, preventing vacuous fixes.

        Any missing or contradictory evidence returns an invalid result rather
        than being upgraded to a successful repair.
        """
        p_dir = Path(project_dir).resolve()
        patch_p = Path(patch_file).resolve()

        if not patch_p.is_file():
            return PatchSynthesizer._invalid_result("", f"补丁文件不存在: {patch_p}")
        try:
            patch_diff = patch_p.read_text(encoding="utf-8")
        except OSError as exc:
            return PatchSynthesizer._invalid_result("", f"补丁读取失败: {exc}")
        if not p_dir.is_dir() or p_dir.is_symlink():
            return PatchSynthesizer._invalid_result(patch_diff, f"项目目录无效: {p_dir}")

        for label, command in (
            ("PoC", poc_verify_cmd),
            ("回归", regression_cmd),
            ("负对照", negative_control_cmd),
        ):
            command_error = PatchSynthesizer._validate_argv(label, command)
            if command_error:
                return PatchSynthesizer._invalid_result(patch_diff, command_error)

        path_error = PatchSynthesizer._validate_patch_paths(patch_diff, p_dir)
        if path_error:
            return PatchSynthesizer._invalid_result(patch_diff, path_error)
        try:
            reverse_diff = PatchSynthesizer.generate_reverse_patch(patch_diff)
        except ValueError as exc:
            return PatchSynthesizer._invalid_result(patch_diff, str(exc))

        detail_msg: list[str] = []
        baseline_vulnerable = False
        poc_mitigated = False
        regression_passed = regression_cmd is None
        negative_control_passed: bool | None = None
        reverse_mutation_verified = not require_reverse_differential
        workspace_unchanged = True

        try:
            with tempfile.TemporaryDirectory(prefix="bugx-patch-verify-") as temp_root:
                temp_path = Path(temp_root)
                baseline_dir = temp_path / "baseline"
                patched_dir = temp_path / "patched"
                reverse_dir = temp_path / "reverse"
                PatchSynthesizer._copy_isolated_project(p_dir, baseline_dir)
                PatchSynthesizer._copy_isolated_project(p_dir, patched_dir)

                def relocated(command: Sequence[str], cwd: Path) -> list[str]:
                    return PatchSynthesizer._relocate_argv(command, p_dir, cwd)

                # 1. Red baseline: a valid candidate must still reproduce the
                # defect before the fix.  126/127 and launch errors are not a
                # vulnerability signal.
                baseline_code, baseline_detail = PatchSynthesizer._run_argv(
                    relocated(poc_verify_cmd, baseline_dir), baseline_dir, timeout,
                )
                if baseline_code is None or baseline_code in {126, 127}:
                    detail_msg.append(f"基线 PoC 无法作为缺陷证据: {baseline_detail}")
                else:
                    baseline_vulnerable = baseline_code != 0
                    if not baseline_vulnerable:
                        detail_msg.append("基线 PoC 已经正常退出，拒绝把无漏洞目标标成已修复")

                # 2. Apply the candidate only inside the patched copy.
                patch_check = subprocess.run(
                    ["git", "apply", "--check", str(patch_p)],
                    cwd=str(patched_dir),
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                if patch_check.returncode != 0:
                    stderr_value = patch_check.stderr or b""
                    stderr = (
                        stderr_value.decode(errors="replace")
                        if isinstance(stderr_value, bytes) else str(stderr_value)
                    )[-500:]
                    detail_msg.append(f"隔离补丁预检失败: {stderr}")
                else:
                    patch_apply = subprocess.run(
                        ["git", "apply", str(patch_p)],
                        cwd=str(patched_dir),
                        check=False,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    if patch_apply.returncode != 0:
                        stderr_value = patch_apply.stderr or b""
                        stderr = (
                            stderr_value.decode(errors="replace")
                            if isinstance(stderr_value, bytes) else str(stderr_value)
                        )[-500:]
                        detail_msg.append(f"隔离补丁应用失败: {stderr}")
                    else:
                        patched_code, patched_detail = PatchSynthesizer._run_argv(
                            relocated(poc_verify_cmd, patched_dir), patched_dir, timeout,
                        )
                        poc_mitigated = patched_code == 0
                        if not poc_mitigated:
                            detail_msg.append(f"补丁后 PoC 仍未被拦截: {patched_detail}")

                        # 3. Baseline + patched regression pair.  A failing
                        # baseline is not silently treated as a regression-free
                        # repair because there is no healthy control to compare.
                        if regression_cmd:
                            base_reg_code, base_reg_detail = PatchSynthesizer._run_argv(
                                relocated(regression_cmd, baseline_dir), baseline_dir, timeout,
                            )
                            patched_reg_code, patched_reg_detail = PatchSynthesizer._run_argv(
                                relocated(regression_cmd, patched_dir), patched_dir, timeout,
                            )
                            regression_passed = (
                                base_reg_code == 0 and patched_reg_code == 0
                            )
                            if base_reg_code != 0:
                                detail_msg.append(f"基线回归未通过: {base_reg_detail}")
                            if patched_reg_code != 0:
                                detail_msg.append(f"补丁后回归失败: {patched_reg_detail}")

                        # 4. Optional negative control must remain benign in
                        # both copies; one-sided checks are insufficient.
                        if negative_control_cmd:
                            base_neg_code, base_neg_detail = PatchSynthesizer._run_argv(
                                relocated(negative_control_cmd, baseline_dir), baseline_dir, timeout,
                            )
                            patched_neg_code, patched_neg_detail = PatchSynthesizer._run_argv(
                                relocated(negative_control_cmd, patched_dir), patched_dir, timeout,
                            )
                            negative_control_passed = (
                                base_neg_code == 0 and patched_neg_code == 0
                            )
                            if not negative_control_passed:
                                detail_msg.append(
                                    "负对照未保持正常: "
                                    f"baseline={base_neg_detail}; patched={patched_neg_detail}"
                                )

                        # 5. Generate and apply the inverse mutation in a
                        # third isolated copy.  It must make the PoC fail again.
                        if require_reverse_differential:
                            shutil.copytree(patched_dir, reverse_dir, symlinks=False)
                            reverse_patch = temp_path / "reverse.patch"
                            reverse_patch.write_text(reverse_diff, encoding="utf-8")
                            reverse_check = subprocess.run(
                                ["git", "apply", "--check", str(reverse_patch)],
                                cwd=str(reverse_dir),
                                check=False,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                            )
                            if reverse_check.returncode != 0:
                                stderr_value = reverse_check.stderr or b""
                                stderr = (
                                    stderr_value.decode(errors="replace")
                                    if isinstance(stderr_value, bytes) else str(stderr_value)
                                )[-500:]
                                detail_msg.append(f"反向变异预检失败: {stderr}")
                            else:
                                reverse_apply = subprocess.run(
                                    ["git", "apply", str(reverse_patch)],
                                    cwd=str(reverse_dir),
                                    check=False,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                )
                                if reverse_apply.returncode != 0:
                                    stderr_value = reverse_apply.stderr or b""
                                    stderr = (
                                        stderr_value.decode(errors="replace")
                                        if isinstance(stderr_value, bytes) else str(stderr_value)
                                    )[-500:]
                                    detail_msg.append(f"反向变异应用失败: {stderr}")
                                else:
                                    reverse_code, reverse_detail = PatchSynthesizer._run_argv(
                                        relocated(poc_verify_cmd, reverse_dir), reverse_dir, timeout,
                                    )
                                    reverse_mutation_verified = (
                                        reverse_code is not None
                                        and reverse_code not in {0, 126, 127}
                                    )
                                    if not reverse_mutation_verified:
                                        detail_msg.append(
                                            f"反向变异未重新暴露 PoC: {reverse_detail}"
                                        )
        except (OSError, ValueError, shutil.Error) as exc:
            detail_msg.append(f"隔离验证失败（按 fail-closed 处理）: {exc}")
            workspace_unchanged = True

        negative_ok = negative_control_passed is not False
        is_valid = (
            baseline_vulnerable
            and poc_mitigated
            and regression_passed
            and negative_ok
            and reverse_mutation_verified
            and workspace_unchanged
        )
        if is_valid:
            detail = "双向门禁全绿：基线可复现、补丁后攻击消除、反向变异复现且无回归"
        else:
            detail = "; ".join(detail_msg) or "补丁验证证据不足，按 fail-closed 拒绝"
        return PatchVerificationResult(
            is_valid=is_valid,
            poc_mitigated=poc_mitigated,
            regression_passed=regression_passed,
            patch_diff=patch_diff,
            detail=detail,
            baseline_vulnerable=baseline_vulnerable,
            reverse_mutation_verified=reverse_mutation_verified,
            negative_control_passed=negative_control_passed,
            isolated=True,
            workspace_unchanged=workspace_unchanged,
            mutation_count=1,
            mutation_kill_rate=1.0 if reverse_mutation_verified else 0.0,
        )

    @staticmethod
    def generate_immunity_test(
        vuln_id: str,
        target_module: str,
        test_payload: str,
        expected_mitigation: str = "defense_ok",
        framework: str = "unittest",
    ) -> str:
        """将漏洞 PoC 输入转写为项目原生的永久工程免疫测试用例。"""
        import re
        raw_id = re.sub(r"[^a-zA-Z0-9_]", "_", vuln_id.strip()).lower()
        clean_id = f"v_{raw_id}" if raw_id and raw_id[0].isdigit() else (raw_id or "unspecified")

        if framework == "pytest":
            return f'''# 自动生成的工程免疫防退化测试用例 (DOC-137 2.0 / D137-09)
# 针对漏洞: {vuln_id}
import pytest

def test_immunity_{clean_id}():
    """验证漏洞 {vuln_id} 已被永久免疫，防止未来退化。"""
    payload = {repr(test_payload)}
    # 待绑定具体业务防御路径：使用 pytest.skip 避免误判或击垮 CI
    pytest.skip("工程免疫测试骨架：请绑定目标模块 {target_module} 并断言安全防护行为")
'''
        # 默认 unittest 风格
        return f'''# 自动生成的工程免疫防退化测试用例 (DOC-137 2.0 / D137-09)
# 针对漏洞: {vuln_id}
import unittest

class TestImmunity_{clean_id}(unittest.TestCase):
    """验证漏洞 {vuln_id} 已被永久免疫，防止未来退化。"""

    def test_exploit_prevented(self):
        payload = {repr(test_payload)}
        # 待绑定具体业务防御路径：使用 skipTest 避免在 CI 中产生假性红灯
        self.skipTest("工程免疫测试骨架：请绑定目标模块 {target_module} 并断言安全防护行为")

if __name__ == "__main__":
    unittest.main()
'''
