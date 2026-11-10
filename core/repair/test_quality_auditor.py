#!/usr/bin/env python3
"""core/test_quality_auditor.py - 测试用例四维质量量化审计评估器。

对自动生成、Agent 编写或人类维护的测试用例进行四维量化审查：
1. 准确性 (Accuracy, 25%): 语法正确、符号可达、无恒真/恒假等硬编码逻辑矛盾；
2. 完整性 (Completeness, 25%): 标准 AAA 结构、有效非空断言密度、异常防护闭环；
3. 有效性 (Effectiveness, 25%): 真实调用穿透目标符号、成对负对照验证、防 Flakiness；
4. 全面性 (Comprehensiveness, 25%): 场景谱系丰富度 (Happy Path + Boundary + Negative + Concurrency/Stateful)。
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
import ast
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if str(Path(__file__).resolve().parent.parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


class AuditorError(ValueError):
    """审计器参数或文件读取错误。"""


@dataclass
class DimensionScore:
    score: float
    max_score: float = 25.0
    issues: list[str] = field(default_factory=list)


@dataclass
class AuditReport:
    score: float
    level: str  # PASS, DEGRADED, FAIL
    file_path: str
    target_scope: str
    dimensions: dict[str, DimensionScore]
    summary: dict[str, Any]
    violations: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 1),
            "level": self.level,
            "file_path": self.file_path,
            "target_scope": self.target_scope,
            "dimensions": {
                k: {"score": round(v.score, 1), "max_score": v.max_score, "issues": v.issues}
                for k, v in self.dimensions.items()
            },
            "summary": self.summary,
            "violations": self.violations,
        }


class TestQualityAuditor:
    """测试用例 AST 与语义质量深度检测器。"""

    def __init__(self, target_scope: str = ""):
        self.target_scope = target_scope.strip()
        self.target_symbol = (
            target_scope.split("::")[-1].strip() if "::" in target_scope else target_scope
        )
        if "." in self.target_symbol:
            self.target_symbol = self.target_symbol.split(".")[-1]

    @staticmethod
    def _constant_bool(node: ast.AST) -> bool | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            value = TestQualityAuditor._constant_bool(node.operand)
            return None if value is None else not value
        if isinstance(node, ast.BoolOp):
            values = [TestQualityAuditor._constant_bool(value) for value in node.values]
            if isinstance(node.op, ast.And):
                if False in values:
                    return False
                if all(value is True for value in values):
                    return True
            elif isinstance(node.op, ast.Or):
                if True in values:
                    return True
                if all(value is False for value in values):
                    return False
        return None

    @staticmethod
    def _contains_broad_exception(node: ast.AST) -> bool:
        return any(
            isinstance(child, ast.Name) and child.id in {"Exception", "BaseException"}
            for child in ast.walk(node)
        )

    def audit_source(self, code: str, file_path: str = "<memory>") -> AuditReport:
        accuracy = DimensionScore(score=25.0)
        completeness = DimensionScore(score=25.0)
        effectiveness = DimensionScore(score=25.0)
        comprehensiveness = DimensionScore(score=25.0)
        violations: list[str] = []

        # --- 1. 准确性审计 (Accuracy) ---
        try:
            tree = ast.parse(code, filename=file_path)
        except SyntaxError as exc:
            accuracy.score = 0.0
            accuracy.issues.append(f"Python 语法错误: {exc.msg} (第 {exc.lineno} 行)")
            violations.append("ACCURACY_SYNTAX_ERROR")
            return self._build_report(0.0, file_path, accuracy, completeness, effectiveness, comprehensiveness, {}, violations)

        # 检查是否包含明显的无条件恒假/恒真断言 (如裸 assert True 或裸 assert False)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                test = node.test
                if isinstance(test, ast.Constant):
                    if test.value is False:
                        accuracy.score = max(0.0, accuracy.score - 10.0)
                        accuracy.issues.append(f"发现无条件硬编码失败断言 assert False (第 {node.lineno} 行)")
                        violations.append("ACCURACY_ASSERT_FALSE")
                    elif test.value is True:
                        accuracy.score = max(0.0, accuracy.score - 5.0)
                        accuracy.issues.append(f"发现无条件无意义恒真断言 assert True (第 {node.lineno} 行)")
                        violations.append("ACCURACY_ASSERT_TRUE")

            if isinstance(node, ast.Call):
                assertion_name = (
                    node.func.attr if isinstance(node.func, ast.Attribute)
                    else node.func.id if isinstance(node.func, ast.Name)
                    else ""
                )
                tautology = False
                if node.args and assertion_name in {"assertTrue", "assertIsNotNone"}:
                    tautology = self._constant_bool(node.args[0]) is True
                elif node.args and assertion_name == "assertFalse":
                    tautology = self._constant_bool(node.args[0]) is False
                elif len(node.args) >= 2 and assertion_name in {"assertEqual", "assertIs"}:
                    tautology = ast.dump(node.args[0]) == ast.dump(node.args[1])
                if tautology:
                    accuracy.score = max(0.0, accuracy.score - 10.0)
                    accuracy.issues.append(f"第 {node.lineno} 行包含恒真断言或自比较断言")
                    violations.append("ACCURACY_TAUTOLOGICAL_ASSERTION")

                if (
                    assertion_name in {"assertRaises", "assertRaisesRegex", "raises"}
                    and node.args
                    and self._contains_broad_exception(node.args[0])
                ):
                    accuracy.score = max(0.0, accuracy.score - 8.0)
                    accuracy.issues.append(f"第 {node.lineno} 行的负对照捕获了过宽异常类型")
                    violations.append("ACCURACY_BROAD_EXCEPTION_ASSERTION")

        # 检查是否包含无害捕获后裸 pass 的反模式 (except: pass)
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    # 如果不是在测试已知预期异常（如在subTest或明确注释中），扣分
                    if node.type is None or (isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException")):
                        accuracy.score = max(0.0, accuracy.score - 5.0)
                        accuracy.issues.append(f"第 {node.lineno} 行存在裸吞异常 `except: pass`，可能掩盖真实崩溃")

        # --- 2. 完整性审计 (Completeness) ---
        test_functions = []
        assertions_count = 0
        has_setup = False
        has_teardown = False
        skipped_test_count = 0

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_") or node.name.startswith("verify_"):
                    test_functions.append(node)
                    if any(
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "skipTest"
                        for sub in ast.walk(node)
                    ):
                        skipped_test_count += 1
                        completeness.score = max(0.0, completeness.score - 6.0)
                        completeness.issues.append(f"测试方法 `{node.name}` 无条件跳过，未执行验证")
                        violations.append(f"COMPLETENESS_SKIPPED_TEST:{node.name}")
                elif node.name in ("setUp", "setup_method"):
                    has_setup = True
                elif node.name in ("tearDown", "teardown_method"):
                    has_teardown = True

        if not test_functions:
            completeness.score = 0.0
            completeness.issues.append("未发现任何以 test_ 或 verify_ 开头的有效测试函数/方法")
            violations.append("COMPLETENESS_NO_TEST_METHODS")
        else:
            # 统计断言数量与密度
            for fn in test_functions:
                fn_asserts = 0
                fn_has_call = False
                for sub in ast.walk(fn):
                    if isinstance(sub, ast.Assert):
                        fn_asserts += 1
                        assertions_count += 1
                    elif isinstance(sub, ast.Call):
                        fn_has_call = True
                        if isinstance(sub.func, ast.Attribute) and sub.func.attr.startswith("assert"):
                            fn_asserts += 1
                            assertions_count += 1
                        elif isinstance(sub.func, ast.Name) and sub.func.id.startswith("assert"):
                            fn_asserts += 1
                            assertions_count += 1
                # 检查单个方法是否缺少断言 (0 断言是空跑)
                if fn_asserts == 0:
                    completeness.score = max(0.0, completeness.score - 6.0)
                    completeness.issues.append(f"测试方法 `{fn.name}` 缺少有效断言 (0 断言，属于空跑测试)")
                    violations.append(f"COMPLETENESS_ZERO_ASSERTION:{fn.name}")

                if not fn_has_call:
                    completeness.score = max(0.0, completeness.score - 6.0)
                    completeness.issues.append(f"测试方法 `{fn.name}` 缺少任何执行调用 (Act 阶段缺失)")

        if assertions_count == 0:
            completeness.score = max(0.0, completeness.score - 15.0)
            completeness.issues.append("整个测试文件中未找到任何断言语句")
            violations.append("COMPLETENESS_NO_ASSERTIONS")

        # --- 3. 有效性审计 (Effectiveness) ---
        target_called_count = 0
        has_negative_control = False
        flaky_risks: list[str] = []

        for node in ast.walk(tree):
            # 检查是否调用了目标符号
            if isinstance(node, ast.Call):
                call_name = ""
                if isinstance(node.func, ast.Name):
                    call_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    call_name = node.func.attr
                if self.target_symbol and (self.target_symbol in call_name or call_name in self.target_symbol):
                    target_called_count += 1

                # 检查负对照特征
                if isinstance(node.func, ast.Attribute) and node.func.attr in (
                    "assertRaises", "assertRaisesRegex", "raises", "fail",
                ):
                    has_negative_control = True

            # 检查成对负对照关键字
            # 检查 Flakiness 风险
            if isinstance(node, ast.Call):
                func_repr = ""
                if isinstance(node.func, ast.Attribute):
                    func_repr = f"{getattr(node.func.value, 'id', '')}.{node.func.attr}"
                if func_repr in ("random.random", "random.randint", "random.choice"):
                    flaky_risks.append(f"第 {node.lineno} 行使用未受控随机 `{func_repr}`，可能导致测试结果 Flaky")
                elif func_repr in ("time.sleep",):
                    flaky_risks.append(f"第 {node.lineno} 行硬编码 `time.sleep`，可能导致测试执行慢且竞态不稳定")

        if self.target_symbol and target_called_count == 0:
            effectiveness.score = max(0.0, effectiveness.score - 12.0)
            effectiveness.issues.append(f"测试用例未真实调用被测目标符号 `{self.target_symbol}` (存在虚假覆盖嫌疑)")
            violations.append("EFFECTIVENESS_TARGET_NOT_CALLED")

        if not has_negative_control:
            effectiveness.score = max(0.0, effectiveness.score - 6.0)
            effectiveness.issues.append("缺少成对负对照 (Negative Control) 检验（未证明异常仅由目标条件触发）")
            violations.append("EFFECTIVENESS_NO_NEGATIVE_CONTROL")

        if flaky_risks:
            effectiveness.score = max(0.0, effectiveness.score - min(len(flaky_risks) * 3.0, 7.0))
            effectiveness.issues.extend(flaky_risks)
            violations.append("EFFECTIVENESS_FLAKY_RISK")

        # --- 4. 全面性审计 (Comprehensiveness) ---
        fn_names = [fn.name.lower() for fn in test_functions]
        has_happy = any(
            any(k in n for k in ("happy", "normal", "basic", "default", "success", "pass"))
            for n in fn_names
        )
        has_boundary = any(any(k in n for k in ("boundary", "limit", "edge", "zero", "empty", "overflow")) for n in fn_names)
        has_error = any(any(k in n for k in ("error", "invalid", "exception", "negative", "type")) for n in fn_names)
        has_concurrency = any("concurren" in n or "race" in n or "thread" in n for n in fn_names)
        has_stateful = any("state" in n or "reentran" in n or "seq" in n or "idempot" in n for n in fn_names)

        scenarios_covered = sum([has_happy, has_boundary, has_error, has_concurrency or has_stateful])
        if not has_boundary:
            comprehensiveness.score = max(0.0, comprehensiveness.score - 6.0)
            comprehensiveness.issues.append("未覆盖边界极限值场景 (Boundary/Edge Cases)")
        if not has_error:
            comprehensiveness.score = max(0.0, comprehensiveness.score - 6.0)
            comprehensiveness.issues.append("未覆盖异常输入防御场景 (Error/Exception Handling)")
        if len(test_functions) < 2:
            comprehensiveness.score = max(0.0, comprehensiveness.score - 6.0)
            comprehensiveness.issues.append(f"测试用例数量过少 ({len(test_functions)} 个)，建议扩充测试场景矩阵")

        total_score = accuracy.score + completeness.score + effectiveness.score + comprehensiveness.score
        summary = {
            "test_methods_count": len(test_functions),
            "assertions_count": assertions_count,
            "target_symbol_calls_count": target_called_count,
            "has_setup": has_setup,
            "has_teardown": has_teardown,
            "has_negative_control": has_negative_control,
            "has_happy_testing": has_happy,
            "has_boundary_testing": has_boundary,
            "has_error_testing": has_error,
            "scenarios_covered": scenarios_covered,
            "flaky_risks_count": len(flaky_risks),
            "skipped_test_count": skipped_test_count,
        }
        return self._build_report(total_score, file_path, accuracy, completeness, effectiveness, comprehensiveness, summary, violations)

    def audit_file(self, file_path: Path) -> AuditReport:
        path = file_path.resolve()
        if not path.is_file():
            raise AuditorError(f"测试文件不存在: {file_path}")
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise AuditorError(f"无法读取测试文件: {file_path}: {exc}") from exc
        return self.audit_source(content, str(path))

    def _build_report(
        self,
        total_score: float,
        file_path: str,
        accuracy: DimensionScore,
        completeness: DimensionScore,
        effectiveness: DimensionScore,
        comprehensiveness: DimensionScore,
        summary: dict[str, Any],
        violations: list[str],
    ) -> AuditReport:
        if total_score >= 80.0 and not violations:
            level = "PASS"
        elif total_score >= 60.0:
            level = "DEGRADED"
        else:
            level = "FAIL"

        return AuditReport(
            score=total_score,
            level=level,
            file_path=file_path,
            target_scope=self.target_scope,
            dimensions={
                "accuracy": accuracy,
                "completeness": completeness,
                "effectiveness": effectiveness,
                "comprehensiveness": comprehensiveness,
            },
            summary=summary,
            violations=violations,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="待审计的测试用例源文件路径")
    parser.add_argument("--scope", default="", help="被测单元符号范围，如 path/foo.py::func_name")
    parser.add_argument("--min-score", type=float, default=70.0, help="最低准入质量得分 (默认 70.0)")
    parser.add_argument("--strict", action="store_true", help="严格模式：任何违规直接退出码 1")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出审计报告")
    args = parser.parse_args(argv)

    auditor = TestQualityAuditor(target_scope=args.scope)
    try:
        report = auditor.audit_file(args.file)
    except AuditorError as exc:
        print(f"[test_quality_auditor] ✗ 错误: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        status_icon = "✓" if report.level == "PASS" else ("⚠" if report.level == "DEGRADED" else "✗")
        print(f"[test_quality_auditor] {status_icon} 综合质量得分: {report.score:.1f} / 100.0 [{report.level}]")
        print(f"  目标文件: {report.file_path}")
        if report.target_scope:
            print(f"  被测目标: {report.target_scope}")
        print(f"  各维度评分:")
        for dim, info in report.dimensions.items():
            print(f"    - {dim.capitalize():17s}: {info.score:4.1f} / {info.max_score:.1f}")
            for issue in info.issues:
                print(f"        ⚠ {issue}")
        if report.violations:
            print(f"  硬违规缺陷清单: {', '.join(report.violations)}")

    if report.score < args.min_score or (args.strict and report.level != "PASS"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
