"""First native target adapters for Python and Node.js/TypeScript projects.

Discovery is static: project setup scripts are never imported or run.  Every
generated check is one explicit CheckPlan, and path impact analysis only selects
tests with a concrete static import relationship.
"""

from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .agent_execution import (
    CheckPlan,
    DEFAULT_ENV_ALLOWLIST,
    ExecutionObservation,
    TargetCallTarget,
)


PROFILE_VERSION = 1
_MAX_PROJECT_FILES = 25000
_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "__pycache__", "node_modules",
    "dist", "build", "coverage", ".next", ".tox", ".mypy_cache", ".pytest_cache",
})
_PY_TEST_NAME = re.compile(r"^(?:test_.*|.*_test)\.py$")
_NODE_TEST_NAME = re.compile(r".+\.(?:test|spec)\.(?:[cm]?js|[cm]?ts|jsx|tsx)$")
_PYTHON_EXTS = frozenset({".py"})
_NODE_EXTS = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx"})
_TARGET_ENV = tuple(sorted(DEFAULT_ENV_ALLOWLIST))


@dataclass(frozen=True, slots=True)
class TargetDiagnostic:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class TargetProfile:
    profile_version: int
    profile_id: str
    language: str
    toolchain: Mapping[str, str]
    project_files: tuple[str, ...]
    source_roots: tuple[str, ...]
    test_roots: tuple[str, ...]
    prepare_plans: tuple[CheckPlan, ...]
    check_plans: tuple[CheckPlan, ...]
    test_map: Mapping[str, tuple[str, ...]]
    env_keys: tuple[str, ...]
    capabilities: tuple[str, ...]
    regression_incomplete: bool = False
    diagnostics: tuple[TargetDiagnostic, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_version": self.profile_version,
            "profile_id": self.profile_id,
            "language": self.language,
            "toolchain": dict(self.toolchain),
            "project_files": list(self.project_files),
            "source_roots": list(self.source_roots),
            "test_roots": list(self.test_roots),
            "prepare_plans": [plan.to_dict() for plan in self.prepare_plans],
            "check_plans": [plan.to_dict() for plan in self.check_plans],
            "test_map": {key: list(value) for key, value in sorted(self.test_map.items())},
            "env_keys": list(self.env_keys),
            "capabilities": list(self.capabilities),
            "regression_incomplete": self.regression_incomplete,
            "diagnostics": [asdict(item) for item in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class RegressionSelection:
    plans: tuple[CheckPlan, ...]
    regression_incomplete: bool
    unmapped_paths: tuple[str, ...] = ()

    @property
    def plan_ids(self) -> tuple[str, ...]:
        return tuple(plan.plan_id for plan in self.plans)


@dataclass(frozen=True, slots=True)
class TargetRunSummary:
    runner: str
    test_count: int | None
    passed: int | None
    failed: int | None
    failure_category: str
    failure_stack: tuple[str, ...] = ()
    test_names: tuple[str, ...] = ()
    target_call_evidence: tuple[object, ...] = ()


def adapt_project(root: str | os.PathLike[str], *, workcopy_id: str = "target") -> TargetProfile:
    """Detect and adapt the first supported native project target."""
    project_root = Path(root).resolve(strict=True)
    files = _walk_files(project_root)
    names = {path.relative_to(project_root).as_posix() for path in files}
    if "package.json" in names:
        return adapt_node_project(project_root, workcopy_id=workcopy_id, _files=files)
    if any(name in names for name in ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini")) or any(path.suffix == ".py" for path in files):
        return adapt_python_project(project_root, workcopy_id=workcopy_id, _files=files)
    raise ValueError("project is not a supported Python or Node.js target")


def adapt_python_project(
    root: str | os.PathLike[str],
    *,
    workcopy_id: str = "target",
    _files: Sequence[Path] | None = None,
) -> TargetProfile:
    """Build static Python prepare/check plans without importing project code."""
    project_root = Path(root).resolve(strict=True)
    files = tuple(_files) if _files is not None else tuple(_walk_files(project_root))
    scan_truncated = len(files) >= _MAX_PROJECT_FILES
    rels = tuple(sorted(_relative(project_root, path) for path in files))
    project_file_set = set(rels)
    configs = tuple(path for path in rels if PurePosixPath(path).name.casefold() in {
        "pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini",
    })
    test_files = tuple(
        path for path in rels
        if PurePosixPath(path).suffix == ".py"
        and _PY_TEST_NAME.fullmatch(PurePosixPath(path).name)
        and not _under_fixture(path)
    )
    test_roots = _roots_for_paths(test_files)
    source_files = tuple(
        path for path in rels
        if PurePosixPath(path).suffix == ".py" and path not in test_files and not _under_fixture(path)
        and PurePosixPath(path).name not in {"conftest.py", "setup.py"}
        and not _under_test_directory(path)
    )
    source_roots = _python_source_roots(project_root, source_files)
    diagnostics: list[TargetDiagnostic] = []
    parsed_tests: dict[str, ast.Module | None] = {}
    import_names: dict[str, set[str]] = {}
    test_styles: dict[str, set[str]] = {}
    for test_path in test_files:
        try:
            tree = ast.parse((project_root / test_path).read_text(encoding="utf-8"), filename=test_path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            parsed_tests[test_path] = None
            import_names[test_path] = _python_imports_from_text(project_root / test_path)
            test_styles[test_path] = _python_styles_from_text(project_root / test_path)
            diagnostics.append(TargetDiagnostic("test_syntax_unavailable", str(exc), test_path))
            continue
        parsed_tests[test_path] = tree
        import_names[test_path] = _python_imports(tree)
        test_styles[test_path] = _python_styles(tree)

    config_text = "\n".join(_read_text(project_root / item) for item in configs)
    pytest_enabled = any(
        PurePosixPath(path).name.casefold() == "pytest.ini"
        or "[tool.pytest" in _read_text(project_root / path).casefold()
        or "[tool:pytest]" in _read_text(project_root / path).casefold()
        for path in configs
    ) or bool(re.search(r"\bpytest\b", config_text)) or any("pytest" in styles for styles in test_styles.values())
    unittest_present = any("unittest" in styles for styles in test_styles.values())
    runner = "pytest" if pytest_enabled else "unittest"
    check_plans: list[CheckPlan] = []
    file_plans: dict[str, list[CheckPlan]] = {}
    target_by_test: dict[str, tuple[TargetCallTarget, ...]] = {}
    module_by_source = _python_modules(source_files, source_roots)

    for test_path in test_files:
        tree = parsed_tests[test_path]
        target_paths = _python_imported_sources(test_path, import_names.get(test_path, set()), module_by_source)
        targets = tuple(TargetCallTarget(path) for path in sorted(target_paths))
        target_by_test[test_path] = targets
        if runner == "pytest":
            node_ids = _pytest_node_ids(test_path, tree)
            if not node_ids:
                node_ids = [test_path]
            plans = [
                _plan(
                    plan_id=f"pytest:{node_id}",
                    argv=("python3", "-m", "pytest", "-q", "--tb=short", node_id),
                    workcopy_id=workcopy_id,
                    targets=targets,
                )
                for node_id in node_ids
            ]
        else:
            cases = _unittest_cases(tree)
            module = _python_test_module(test_path)
            if cases:
                plans = [
                    _plan(
                        plan_id=f"unittest:{module}.{case}",
                        argv=("python3", "-m", "unittest", f"{module}.{case}"),
                        workcopy_id=workcopy_id,
                        targets=targets,
                    )
                    for case in cases
                ]
            else:
                parent = PurePosixPath(test_path).parent.as_posix()
                start_dir = "." if parent == "." else parent
                plans = [
                    _plan(
                        plan_id=f"unittest-file:{test_path}",
                        argv=("python3", "-m", "unittest", "discover", "-s", start_dir,
                              "-p", PurePosixPath(test_path).name),
                        workcopy_id=workcopy_id,
                        targets=targets,
                    )
                ]
        file_plans[test_path] = plans
        check_plans.extend(plans)

    test_map_sets: dict[str, set[str]] = {}
    for source_path in source_files:
        matched = [
            test_path for test_path in test_files
            if _python_test_imports_source(
                source_path, import_names.get(test_path, set()), module_by_source
            )
        ]
        if matched:
            test_map_sets[source_path] = {
                plan.plan_id for test_path in matched for plan in file_plans.get(test_path, [])
            }
    for test_path, plans in file_plans.items():
        test_map_sets[test_path] = {plan.plan_id for plan in plans}

    prepare_plans = (
        _plan("python:version", ("python3", "--version"), workcopy_id),
    )
    capabilities = ["python", runner, "bounded_check_plans", "static_import_impact_map"]
    if runner == "pytest":
        capabilities.append("pytest_node_ids")
    elif unittest_present:
        capabilities.append("unittest_testcase_plans")
    unmapped_sources = [path for path in source_files if path not in test_map_sets]
    if unmapped_sources:
        diagnostics.append(TargetDiagnostic(
            "incomplete_impact_map",
            f"{len(unmapped_sources)} source file(s) have no statically linked test plan",
        ))
    if not test_files:
        diagnostics.append(TargetDiagnostic("no_supported_tests", "no Python test files were discovered"))
    if scan_truncated:
        diagnostics.append(TargetDiagnostic("project_file_scan_truncated", f"project scan reached {_MAX_PROJECT_FILES} files"))
    regression_incomplete = bool(
        unmapped_sources or scan_truncated
        or any(d.code == "test_syntax_unavailable" for d in diagnostics)
    )
    return TargetProfile(
        profile_version=PROFILE_VERSION,
        profile_id=f"python:{project_root.name or 'target'}",
        language="python",
        toolchain={"runtime": "python3", "runner": runner},
        project_files=tuple(path for path in rels if path in project_file_set and (
            path in configs or path in source_files or path in test_files
        )),
        source_roots=source_roots,
        test_roots=test_roots,
        prepare_plans=prepare_plans,
        check_plans=tuple(check_plans),
        test_map={key: tuple(sorted(value)) for key, value in sorted(test_map_sets.items())},
        env_keys=_TARGET_ENV,
        capabilities=tuple(sorted(set(capabilities))),
        regression_incomplete=regression_incomplete,
        diagnostics=tuple(diagnostics),
    )


def adapt_node_project(
    root: str | os.PathLike[str],
    *,
    workcopy_id: str = "target",
    _files: Sequence[Path] | None = None,
) -> TargetProfile:
    """Build native node:test plans; arbitrary package scripts are not invoked."""
    project_root = Path(root).resolve(strict=True)
    files = tuple(_files) if _files is not None else tuple(_walk_files(project_root))
    scan_truncated = len(files) >= _MAX_PROJECT_FILES
    rels = tuple(sorted(_relative(project_root, path) for path in files))
    configs = tuple(path for path in rels if PurePosixPath(path).name in {
        "package.json", "tsconfig.json", "jsconfig.json",
    } or PurePosixPath(path).name.endswith("lock"))
    package: Mapping[str, Any] = {}
    package_path = project_root / "package.json"
    diagnostics: list[TargetDiagnostic] = []
    try:
        parsed = json.loads(package_path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            package = parsed
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        diagnostics.append(TargetDiagnostic("invalid_package_manifest", str(exc), "package.json"))

    test_files = tuple(
        path for path in rels
        if PurePosixPath(path).suffix.casefold() in _NODE_EXTS
        and not _under_fixture(path)
        and (
            _NODE_TEST_NAME.fullmatch(PurePosixPath(path).name)
            or "node:test" in _read_text(project_root / path)
        )
    )
    test_roots = _roots_for_paths(test_files)
    source_files = tuple(
        path for path in rels
        if PurePosixPath(path).suffix.casefold() in _NODE_EXTS and path not in test_files
        and not _under_fixture(path) and not _under_test_directory(path)
        and not _is_node_config(path)
    )
    source_roots = _node_source_roots(project_root, source_files)
    deps: dict[str, Any] = {}
    for name in ("dependencies", "devDependencies", "optionalDependencies"):
        section = package.get(name, {})
        if isinstance(section, dict):
            deps.update(section)
    has_tsx = "tsx" in deps
    node_test_paths: dict[str, bool] = {}
    test_imports: dict[str, set[str]] = {}
    test_names: dict[str, tuple[str, ...]] = {}
    for test_path in test_files:
        text = _read_text(project_root / test_path)
        node_test_paths[test_path] = bool(re.search(r"(?:from\s*|require\s*\()\s*[\"']node:test[\"']", text))
        test_imports[test_path] = _node_relative_imports(test_path, text, project_root, source_files)
        test_names[test_path] = _node_test_names(text)
    scripts = package.get("scripts", {})
    test_script = scripts.get("test", "") if isinstance(scripts, dict) else ""
    has_node_test = any(node_test_paths.values()) or bool(re.search(
        r"(?:^|\s)node\s+--test(?:\s|$)", str(test_script)
    ))

    check_plans: list[CheckPlan] = []
    file_plans: dict[str, list[CheckPlan]] = {}
    if has_node_test:
        for test_path in test_files:
            ext = PurePosixPath(test_path).suffix.casefold()
            if ext in {".ts", ".mts", ".cts"} and not has_tsx:
                diagnostics.append(TargetDiagnostic(
                    "typescript_runner_unavailable",
                    "TypeScript test requires a declared local tsx dependency for the native node:test runner",
                    test_path,
                ))
                continue
            targets = tuple(TargetCallTarget(path) for path in sorted(test_imports[test_path]))
            if ext in {".ts", ".mts", ".cts"}:
                argv = ("node", "--import", "tsx", "--test", "--test-reporter=tap", test_path)
            else:
                argv = ("node", "--test", "--test-reporter=tap", test_path)
            plan = _plan(
                plan_id=f"node-test:{test_path}",
                argv=argv,
                workcopy_id=workcopy_id,
                targets=targets,
            )
            file_plans[test_path] = [plan]
            check_plans.append(plan)
    elif test_files:
        diagnostics.append(TargetDiagnostic(
            "node_test_framework_unsupported",
            "test files were found but no node:test import or node --test script was confirmed; arbitrary package scripts are not run",
        ))
    if not test_files:
        diagnostics.append(TargetDiagnostic("no_supported_tests", "no JavaScript or TypeScript test files were discovered"))
    if scan_truncated:
        diagnostics.append(TargetDiagnostic("project_file_scan_truncated", f"project scan reached {_MAX_PROJECT_FILES} files"))

    test_map_sets: dict[str, set[str]] = {}
    for source_path in source_files:
        related_tests = [path for path in test_files if source_path in test_imports.get(path, set())]
        if related_tests:
            test_map_sets[source_path] = {
                plan.plan_id for test_path in related_tests for plan in file_plans.get(test_path, [])
            }
    for test_path, plans in file_plans.items():
        test_map_sets[test_path] = {plan.plan_id for plan in plans}

    # Known non-mutating preparation only. package.json scripts and install hooks
    # are intentionally never executed during target discovery.
    prepare_plans = (_plan("node:version", ("node", "--version"), workcopy_id),)
    language = "typescript" if any(PurePosixPath(path).suffix.casefold() in {".ts", ".mts", ".cts", ".tsx"} for path in source_files) or "tsconfig.json" in rels else "javascript"
    capabilities = ["node", "node:test", "bounded_check_plans", "static_import_impact_map"] if has_node_test else ["node", "bounded_check_plans", "static_import_impact_map"]
    if has_tsx:
        capabilities.append("typescript_tests_via_local_tsx")
    unmapped_sources = [path for path in source_files if path not in test_map_sets]
    if unmapped_sources:
        diagnostics.append(TargetDiagnostic(
            "incomplete_impact_map",
            f"{len(unmapped_sources)} source file(s) have no statically linked test plan",
        ))
    if not has_node_test:
        # No reliable runner means every candidate change remains incomplete.
        diagnostics.append(TargetDiagnostic("regression_incomplete", "native test runner could not be confirmed"))
    unavailable_ts = any(item.code == "typescript_runner_unavailable" for item in diagnostics)
    return TargetProfile(
        profile_version=PROFILE_VERSION,
        profile_id=f"{language}:{project_root.name or 'target'}",
        language=language,
        toolchain={"runtime": "node", "runner": "node:test" if has_node_test else "unconfirmed", "typescript_loader": "tsx" if has_tsx else "none"},
        project_files=tuple(path for path in rels if path in configs or path in source_files or path in test_files),
        source_roots=source_roots,
        test_roots=test_roots,
        prepare_plans=prepare_plans,
        check_plans=tuple(check_plans),
        test_map={key: tuple(sorted(value)) for key, value in sorted(test_map_sets.items())},
        env_keys=_TARGET_ENV,
        capabilities=tuple(sorted(set(capabilities))),
        regression_incomplete=bool(unmapped_sources or unavailable_ts or scan_truncated or not has_node_test or any(d.code == "invalid_package_manifest" for d in diagnostics)),
        diagnostics=tuple(diagnostics),
    )


def analyze_affected_paths(profile: TargetProfile, affected_paths: Iterable[str]) -> RegressionSelection:
    """Select only statically mapped plans; unknown impact never expands to all."""
    by_id = {plan.plan_id: plan for plan in profile.check_plans}
    selected_ids: set[str] = set()
    unmapped: list[str] = []
    normalized_paths = tuple(_normalize_candidate_path(raw_path) for raw_path in affected_paths)
    for path in normalized_paths:
        plan_ids = profile.test_map.get(path)
        if not plan_ids:
            unmapped.append(path)
            continue
        selected_ids.update(plan_id for plan_id in plan_ids if plan_id in by_id)
    plans = tuple(plan for plan in profile.check_plans if plan.plan_id in selected_ids)
    incomplete = profile.regression_incomplete or bool(unmapped) or any(
        plan_id not in by_id for path in normalized_paths for plan_id in profile.test_map.get(path, ())
    )
    return RegressionSelection(plans, incomplete, tuple(sorted(set(unmapped))))


def parse_python_observation(
    observation: ExecutionObservation,
    *,
    test_roots: Sequence[str] = ("tests", "test"),
    source_roots: Sequence[str] = ("src", "lib", "."),
) -> TargetRunSummary:
    output = observation.stdout_summary + "\n" + observation.stderr_summary
    lowered = output.casefold()
    test_count: int | None = None
    passed: int | None = None
    failed: int | None = None
    match = re.search(r"Ran\s+(\d+)\s+tests?", output)
    if match:
        test_count = int(match.group(1))
        passed = test_count if re.search(r"\bOK\b", output) and not re.search(r"\bFAILED\b", output) else None
    if "pytest" in observation.argv:
        counts: dict[str, int] = {}
        for amount, label in re.findall(
            r"\b(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed)\b", output, re.I
        ):
            normalized = "error" if label.casefold() in {"error", "errors"} else label.casefold()
            counts[normalized] = counts.get(normalized, 0) + int(amount)
        if counts:
            passed = counts.get("passed", 0) + counts.get("xpassed", 0)
            failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
            test_count = passed + failed + counts.get("skipped", 0) + counts.get("xfailed", 0)
    fail_match = re.search(r"FAILED\s*\(.*?failures=(\d+).*?\)", output)
    if fail_match:
        failed = int(fail_match.group(1))
    elif "FAILED (" in output:
        counts = re.findall(r"(?:failures|errors)=(\d+)", output)
        failed = sum(map(int, counts)) if counts else failed
    if observation.timed_out:
        category = "timeout"
    elif observation.cancelled:
        category = "cancelled"
    elif observation.outcome_unknown:
        category = "outcome_unknown"
    elif observation.output_limit:
        category = "output_limit"
    elif re.search(r"No module named ['\"]?pytest|pytest: (?:command )?not found|No module named ['\"]?pip", output, re.I):
        category = "environment_setup_failure"
    elif "SyntaxError" in output or "IndentationError" in output:
        syntax_path = _first_path_in_syntax_error(output)
        category = "test_code_syntax_error" if _is_under_roots(syntax_path, test_roots) else "target_code_syntax_error"
    elif "AssertionError" in output or "FAIL:" in output:
        category = "target_assertion_failure"
    elif re.search(r"\b\d+ failed\b", output) or "FAILED (" in output:
        category = "test_failure"
    elif observation.exit_code == 0 and (test_count is None or failed in {None, 0}):
        category = "passed"
    elif "ModuleNotFoundError" in output or "ImportError" in output:
        category = "environment_setup_failure" if any(word in lowered for word in ("no module named", "cannot import name", "importerror")) else "test_error"
    elif observation.exit_code not in {0, None}:
        category = "test_error"
    else:
        category = "unknown"
    stack = tuple(line for line in output.splitlines() if "File \"" in line or line.lstrip().startswith(("E   ", "Traceback", "AssertionError", "SyntaxError")))
    return TargetRunSummary(
        runner="python",
        test_count=test_count,
        passed=passed,
        failed=failed,
        failure_category=category,
        failure_stack=stack,
        target_call_evidence=tuple(observation.target_call_evidence),
    )


def parse_node_observation(observation: ExecutionObservation) -> TargetRunSummary:
    output = observation.stdout_summary + "\n" + observation.stderr_summary
    lowered = output.casefold()
    tests = _tap_count(output, "tests")
    passed = _tap_count(output, "pass")
    failed = _tap_count(output, "fail")
    names = tuple(re.findall(r"^\s*# Subtest: (.+)$", output, flags=re.MULTILINE))
    if observation.timed_out:
        category = "timeout"
    elif observation.cancelled:
        category = "cancelled"
    elif observation.outcome_unknown:
        category = "outcome_unknown"
    elif observation.output_limit:
        category = "output_limit"
    elif re.search(r"Cannot find package ['\"]tsx|ERR_MODULE_NOT_FOUND|Cannot find module", output):
        category = "environment_setup_failure"
    elif "SyntaxError" in output or "ERR_TYPESCRIPT_SYNTAX" in output:
        syntax_path = _first_path_in_syntax_error(output)
        category = "test_code_syntax_error" if _looks_like_test_path(syntax_path) else "target_code_syntax_error"
    elif "AssertionError" in output or "ERR_ASSERTION" in output:
        category = "target_assertion_failure"
    elif failed not in {None, 0} or re.search(r"^not ok\s+\d+", output, re.MULTILINE):
        category = "test_failure"
    elif observation.exit_code == 0 and (failed in {None, 0}):
        category = "passed"
    elif observation.exit_code not in {0, None}:
        category = "test_error"
    else:
        category = "unknown"
    stack = tuple(line for line in output.splitlines() if re.search(r"\bat\s+.+:\d+:\d+", line) or "stack:" in line)
    return TargetRunSummary(
        runner="node:test",
        test_count=tests,
        passed=passed,
        failed=failed,
        failure_category=category,
        failure_stack=stack,
        test_names=names,
        target_call_evidence=tuple(observation.target_call_evidence),
    )


def _plan(
    plan_id: str,
    argv: Sequence[str],
    workcopy_id: str,
    targets: Sequence[TargetCallTarget] = (),
) -> CheckPlan:
    return CheckPlan(
        plan_id=plan_id,
        argv=tuple(argv),
        workcopy_id=workcopy_id,
        cwd=".",
        env_allowlist=_TARGET_ENV,
        timeout_seconds=120.0,
        output_limit_bytes=1024 * 1024,
        target_call_targets=tuple(targets),
    )


def _walk_files(root: Path) -> tuple[Path, ...]:
    found: list[Path] = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in _SKIP_DIRS and not name.startswith(".bugx"))
        for filename in sorted(filenames):
            path = Path(current) / filename
            if path.is_symlink() or not path.is_file():
                continue
            found.append(path)
            if len(found) >= _MAX_PROJECT_FILES:
                return tuple(found)
    return tuple(found)


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _roots_for_paths(paths: Sequence[str]) -> tuple[str, ...]:
    roots = {PurePosixPath(path).parts[0] for path in paths if PurePosixPath(path).parts}
    return tuple(sorted(roots))


def _under_fixture(path: str) -> bool:
    return any(part.casefold() in {"fixtures", "fixture", "__snapshots__"} for part in PurePosixPath(path).parts)


def _under_test_directory(path: str) -> bool:
    return any(part.casefold() in {"test", "tests", "spec", "specs"} for part in PurePosixPath(path).parts[:-1])


def _is_node_config(path: str) -> bool:
    name = PurePosixPath(path).name.casefold()
    return bool(re.match(r"(?:vite|jest|vitest|eslint|rollup|webpack|babel|postcss|prettier)\.config\.", name))


def _python_source_roots(root: Path, source_files: Sequence[str]) -> tuple[str, ...]:
    known = [name for name in ("src", "lib", "app") if (root / name).is_dir()]
    if any(PurePosixPath(path).parts and PurePosixPath(path).parts[0] not in known for path in source_files):
        known.append(".")
    if not known and source_files:
        known = ["."]
    return tuple(known)


def _node_source_roots(root: Path, source_files: Sequence[str]) -> tuple[str, ...]:
    known = [name for name in ("src", "lib", "source") if (root / name).is_dir()]
    if any(PurePosixPath(path).parts and PurePosixPath(path).parts[0] not in known for path in source_files):
        known.append(".")
    if not known and source_files:
        known = ["."]
    return tuple(known)


def _python_modules(source_files: Sequence[str], roots: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in source_files:
        rel = PurePosixPath(path)
        for source_root in roots or (".",):
            parts = rel.parts[1:] if source_root != "." and rel.parts[0] == source_root else rel.parts
            if source_root != "." and rel.parts[0] != source_root:
                continue
            module_parts = list(parts)
            if module_parts and module_parts[-1].endswith(".py"):
                module_parts[-1] = module_parts[-1][:-3]
            if module_parts and module_parts[-1] == "__init__":
                module_parts.pop()
            module = ".".join(module_parts)
            if module:
                result[module] = path
    return result


def _python_imports(tree: ast.Module) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            base = node.module
            result.add(base)
            result.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return result


def _python_imports_from_text(path: Path) -> set[str]:
    text = _read_text(path)
    names = set(re.findall(r"^\s*import\s+([A-Za-z_][\w.]*)", text, re.MULTILINE))
    for match in re.finditer(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+([^\n]+)", text, re.MULTILINE):
        names.add(match.group(1))
        names.update(f"{match.group(1)}.{name.strip().split(' as ')[0]}" for name in match.group(2).split(",") if name.strip() and name.strip() != "*")
    return names


def _python_styles(tree: ast.Module) -> set[str]:
    styles: set[str] = set()
    if any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        for node in tree.body
    ):
        styles.add("pytest")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "unittest" or alias.name.startswith("unittest.") for alias in node.names):
                styles.add("unittest")
            if any(alias.name == "pytest" for alias in node.names):
                styles.add("pytest")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "unittest" or (node.module or "").startswith("unittest."):
                styles.add("unittest")
            if node.module == "pytest":
                styles.add("pytest")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in {"pytest", "mark"}:
                styles.add("pytest")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(
            isinstance(base, ast.Attribute) and base.attr == "TestCase"
            or isinstance(base, ast.Name) and base.id == "TestCase"
            for base in node.bases
        ):
            styles.add("unittest")
    return styles


def _python_styles_from_text(path: Path) -> set[str]:
    text = _read_text(path)
    result = set()
    if "pytest" in text:
        result.add("pytest")
    if "unittest" in text or "TestCase" in text:
        result.add("unittest")
    return result


def _pytest_node_ids(path: str, tree: ast.Module | None) -> list[str]:
    if tree is None:
        return [path]
    found: list[str] = []
    for item in tree.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_"):
            found.append(f"{path}::{item.name}")
        elif isinstance(item, ast.ClassDef):
            methods = [
                child.name for child in item.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test_")
            ]
            found.extend(f"{path}::{item.name}::{method}" for method in methods)
    return found


def _unittest_cases(tree: ast.Module | None) -> list[str]:
    if tree is None:
        return []
    result = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        has_test = any(
            isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name.startswith("test")
            for member in node.body
        )
        is_case = any(
            isinstance(base, ast.Name) and base.id == "TestCase"
            or isinstance(base, ast.Attribute) and base.attr == "TestCase"
            for base in node.bases
        )
        if has_test and is_case:
            result.append(node.name)
    return result


def _python_test_module(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _python_imported_sources(
    test_path: str,
    imported: set[str],
    module_by_source: Mapping[str, str],
) -> set[str]:
    del test_path
    matched = set()
    for module, path in module_by_source.items():
        is_package_init = PurePosixPath(path).name == "__init__.py"
        if module in imported or (is_package_init and any(name.startswith(module + ".") for name in imported)):
            matched.add(path)
    return matched


def _python_test_imports_source(source_path: str, imported: set[str], module_by_source: Mapping[str, str]) -> bool:
    modules = [module for module, path in module_by_source.items() if path == source_path]
    for module in modules:
        if module in imported:
            return True
        if PurePosixPath(source_path).name == "__init__.py" and any(name.startswith(module + ".") for name in imported):
            return True
    return False


def _node_modules(source_files: Sequence[str], roots: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in source_files:
        rel = PurePosixPath(path)
        for source_root in roots or (".",):
            if source_root != "." and (not rel.parts or rel.parts[0] != source_root):
                continue
            parts = list(rel.parts[1:] if source_root != "." else rel.parts)
            if parts:
                parts[-1] = re.sub(r"\.(?:[cm]?js|[cm]?ts)$", "", parts[-1], flags=re.I)
                result["/".join(parts)] = path
    return result


def _node_relative_imports(test_path: str, text: str, root: Path, source_files: Sequence[str]) -> set[str]:
    import_specs = re.findall(
        r"(?:from\s*|import\s*\(|require\s*\()\s*[\"']([^\"']+)[\"']",
        text,
    )
    known = {str((root / path).resolve()): path for path in source_files}
    base = (root / test_path).parent
    result = set()
    for spec in import_specs:
        if not spec.startswith("."):
            continue
        target = (base / spec).resolve()
        choices = [target]
        if target.suffix:
            choices.extend(target.with_suffix(ext) for ext in _NODE_EXTS)
        else:
            choices.extend(target.with_suffix(ext) for ext in _NODE_EXTS)
            choices.extend(target / f"index{ext}" for ext in _NODE_EXTS)
        for candidate in choices:
            rel = known.get(str(candidate))
            if rel:
                result.add(rel)
                break
    return result


def _node_test_names(text: str) -> tuple[str, ...]:
    values = re.findall(r"\b(?:test|it)\s*\(\s*([\"'])(.+?)\1", text, flags=re.S)
    return tuple(name.strip() for _, name in values)


def _tap_count(output: str, key: str) -> int | None:
    match = re.search(rf"^\s*#\s+{re.escape(key)}\s+(\d+)\s*$", output, re.MULTILINE | re.I)
    return int(match.group(1)) if match else None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _first_path_in_syntax_error(output: str) -> str:
    for line in output.splitlines():
        match = re.search(r"File [\"'](.+?)[\"']", line)
        if match:
            return match.group(1).replace("\\", "/")
        match = re.search(r"(?:at\s+)?(.+?\.(?:py|[cm]?js|[cm]?ts)):\d+(?::\d+)?", line)
        if match:
            return match.group(1).replace("\\", "/")
    return ""


def _is_under_roots(path: str, roots: Sequence[str]) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    if not normalized:
        return False
    parts = PurePosixPath(normalized).parts
    return any(
        root == "." or normalized == root or normalized.startswith(root.rstrip("/") + "/")
        or root in parts
        for root in roots
    )


def _looks_like_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return any(part in {"test", "tests", "spec"} for part in PurePosixPath(normalized).parts) or bool(_NODE_TEST_NAME.fullmatch(PurePosixPath(normalized).name))


def _normalize_candidate_path(path: str) -> str:
    if not isinstance(path, str) or "\x00" in path:
        return "<invalid-path>"
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


__all__ = [
    "PROFILE_VERSION", "RegressionSelection", "TargetDiagnostic", "TargetProfile",
    "TargetRunSummary", "adapt_node_project", "adapt_project", "adapt_python_project",
    "analyze_affected_paths", "parse_node_observation", "parse_python_observation",
]
