"""core/fuzzing - 模糊测试与差分预言机领域子包。

收拢变异模糊测试、有状态状态机推演、并发竞态时序探针与确定性双跑差分验证器。
"""

from __future__ import annotations

from .concurrency_fuzzer import (
    CONCURRENCY_CRASH_PATTERNS,
    ConcurrencyCrashError,
    ConcurrencyOracle,
    LockInversionDetector,
    SchedulePerturber,
    fuzz_command,
    normalize_command,
)
from .differential_oracle import (
    DifferentialOracleError,
    RunResult,
    extract_crash_signature,
    normalize_output,
    run_differential_verification,
    verify_differential_poc,
    verify_poc,
    verify_red_blue_debate,
)
from .fuzz_input import (
    STRATEGIES,
    extract_gap_tokens,
    fuzz_once,
    fuzz_target,
    load_gap_seeds,
    load_seed,
    mutate,
    parse_args as parse_fuzz_input_args,
    shell_argv,
    split_argv,
)
from .stateful_fuzzer import (
    ModelBasedSequenceGenerator,
    StatefulOracle,
    StatefulSequenceGenerator,
    Transition,
    fuzz_transitions,
)

__all__ = [
    # concurrency_fuzzer
    "CONCURRENCY_CRASH_PATTERNS",
    "ConcurrencyCrashError",
    "ConcurrencyOracle",
    "LockInversionDetector",
    "SchedulePerturber",
    "fuzz_command",
    "normalize_command",
    # differential_oracle
    "DifferentialOracleError",
    "RunResult",
    "extract_crash_signature",
    "normalize_output",
    "run_differential_verification",
    "verify_differential_poc",
    "verify_poc",
    "verify_red_blue_debate",
    # fuzz_input
    "STRATEGIES",
    "extract_gap_tokens",
    "fuzz_once",
    "fuzz_target",
    "load_gap_seeds",
    "load_seed",
    "mutate",
    "parse_fuzz_input_args",
    "shell_argv",
    "split_argv",
    # stateful_fuzzer
    "ModelBasedSequenceGenerator",
    "StatefulOracle",
    "StatefulSequenceGenerator",
    "Transition",
    "fuzz_transitions",
]
