#!/usr/bin/env python3
"""core/domain_archetype.py - BugX 领域状态机可达性分析与规约断环治理引擎 (DOC-245).

职责:
1. 领域原型可达性分析 (Domain Archetype Reachability Analysis);
2. 识别需求生命周期断环 (如仅有 Login 消费凭据，缺少 Register/SSO 凭据生产);
3. 双账本物理隔离: 规约断环落盘至 .bugx/state/spec-gaps.json，与代码缺陷 (findings.json) 彻底解耦;
4. 底座测试夹具注入器 (Fixture Injection): 当缺少注册时，为登录深度测试注入合法凭据，保证物理闭环不发生死锁。
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

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0.0"


@dataclass
class SpecGap:
    """规约断环记录项 (物理隔离于代码缺陷)。"""

    domain: str
    gap_type: str
    severity: str  # "WARNING", "INFO", "CRITICAL"
    summary: str
    detail: str
    recommended_options: list[dict[str, str]] = field(default_factory=list)
    resolved_by: str | None = None  # None, "interactive_choice", "headless_fixture"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _detect_external_sso(project_root: Path) -> bool:
    """探测项目是否引入了外部 SSO / OAuth / LDAP 依赖或配置。"""
    sso_keywords = (
        "oauth", "sso", "saml", "ldap", "auth0", "keycloak",
        "google_auth", "github_oauth", "jwt_auth", "cas_auth",
    )
    # 检查常见依赖文件与配置文件
    for rel_path in ("requirements.txt", "package.json", "go.mod", "pom.xml", ".env", ".env.example"):
        p = project_root / rel_path
        if p.is_file():
            try:
                content = p.read_text(encoding="utf-8", errors="replace").lower()
                if any(kw in content for kw in sso_keywords):
                    return True
            except OSError:
                pass
    return False


def _detect_db_seed_or_fixtures(project_root: Path) -> bool:
    """探测项目是否存在数据库初始化 Seed 脚本或预置数据夹具。"""
    seed_patterns = (
        "seed.py", "seed.js", "seed.ts", "init.sql", "schema.sql",
        "fixtures.json", "initial_data.json", "fixtures.py",
    )
    for pattern in seed_patterns:
        if list(project_root.glob(f"**/{pattern}")):
            return True
    return False


def analyze_domain_completeness(
    spec: Any,
    project_root: Path,
    scope_decisions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """对规约与源码进行领域原型拓扑分析，输出 spec-gaps 账本。"""
    gaps: list[SpecGap] = []
    scope_decisions = scope_decisions or {}

    # 获取业务规约数据
    spec_data = spec.to_dict() if hasattr(spec, "to_dict") else (spec if isinstance(spec, dict) else {})
    endpoints = spec_data.get("endpoints", [])
    source_inversions = spec_data.get("source_inversions", [])

    has_login_endpoint = any("login" in str(ep.get("path", "")).lower() for ep in endpoints)
    has_login_source = any(inv.get("intent") == "login" for inv in source_inversions)
    has_login = has_login_endpoint or has_login_source

    has_register_endpoint = any("register" in str(ep.get("path", "")).lower() or "signup" in str(ep.get("path", "")).lower() for ep in endpoints)
    has_register_source = any(inv.get("intent") in {"register", "signup"} for inv in source_inversions)
    has_register = has_register_endpoint or has_register_source

    # 1. Auth 领域闭环分析
    if has_login and not has_register:
        # 用户已在交互层做了决定
        auth_decision = scope_decisions.get("auth")
        if auth_decision:
            # 已有人机交互决策，闭环视为已处置
            pass
        elif _detect_external_sso(project_root):
            # 存在外部 SSO/OAuth，合法的免注册企业系统
            pass
        elif _detect_db_seed_or_fixtures(project_root):
            # 存在内置 DB Seed，合法的预置账号系统
            pass
        else:
            # 真正构成“需求生命周期断环”
            gap = SpecGap(
                domain="auth",
                gap_type="MISSING_IDENTITY_PROVISIONING",
                severity="WARNING",
                summary="身份认证域缺少凭据生产通道（仅有登录，缺失注册与重置）",
                detail="系统声明或实现了登录凭据消费逻辑，但未声明自助注册接口、外部 SSO 联邦认证或预置种子数据，存在冷启动数据源断环隐患。",
                recommended_options=[
                    {
                        "value": "preset_only",
                        "label": "企业内网/预置账号模式（无需注册）",
                        "description": "标记该系统使用管理员预置凭据或内网账号，按封闭系统继续测试，不报注册缺失",
                    },
                    {
                        "value": "spec_gap_continue",
                        "label": "确认遗漏注册，但仅测登录逻辑",
                        "description": "承认需求文档漏写注册，调度侧注入底座测试夹具继续测试登录，并在报告中出具规约建议",
                    },
                    {
                        "value": "abort_for_clarification",
                        "label": "暂停本轮测试，待补充需求",
                        "description": "停止本轮流水线，等待产品人员补充 PRD 与账号生产方案后再行开工",
                    },
                ],
            )
            gaps.append(gap)

    # 2. 订单结算领域闭环分析 (Ecommerce / Order Lifecycle)
    has_order_create = any("order" in str(ep.get("path", "")).lower() and ep.get("method") == "POST" for ep in endpoints)
    has_order_cancel = any("cancel" in str(ep.get("path", "")).lower() or "refund" in str(ep.get("path", "")).lower() for ep in endpoints)
    if has_order_create and not has_order_cancel:
        order_decision = scope_decisions.get("order") or scope_decisions.get("ecommerce")
        if not order_decision and not _detect_db_seed_or_fixtures(project_root):
            gaps.append(
                SpecGap(
                    domain="order",
                    gap_type="MISSING_ORDER_SETTLEMENT_LIFECYCLE",
                    severity="WARNING",
                    summary="订单结算域缺少逆向终结通道（有下单无取消/退款）",
                    detail="系统声明了创建订单接口，但未声明取消或退款接口，存在状态机不可逆风险。",
                    recommended_options=[
                        {
                            "value": "one_way_order",
                            "label": "单向订单不可撤销模式",
                            "description": "确认该业务场景订单一旦创建不可撤回，闭环放行",
                        },
                        {
                            "value": "spec_gap_continue",
                            "label": "确认遗漏退款取消，注入底座夹具继续测试",
                            "description": "承认规约漏写取消退款，注入测试订单夹具继续测试创建逻辑",
                        },
                    ],
                )
            )

    result = {
        "schema_version": SCHEMA_VERSION,
        "is_complete": len(gaps) == 0,
        "gaps_count": len(gaps),
        "gaps": [g.to_dict() for g in gaps],
    }
    return result


def save_spec_gaps(project_root: Path, gaps_result: dict[str, Any]) -> Path:
    """原子落盘 spec-gaps.json (双账本物理隔离，绝不污染 findings.json)。"""
    state_dir = project_root / ".bugx" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    out_path = state_dir / "spec-gaps.json"
    tmp_path = out_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(gaps_result, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(out_path)
    return out_path


def load_spec_gaps(project_root: Path) -> dict[str, Any]:
    """安全读取 spec-gaps.json。"""
    gap_file = project_root / ".bugx" / "state" / "spec-gaps.json"
    if not gap_file.is_file():
        return {"schema_version": SCHEMA_VERSION, "is_complete": True, "gaps_count": 0, "gaps": []}
    try:
        return json.loads(gap_file.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": SCHEMA_VERSION, "is_complete": True, "gaps_count": 0, "gaps": []}


def provide_prerequisite_fixture(domain: str, project_root: Path, write_to_disk: bool = True) -> dict[str, Any]:
    """底座测试夹具注入器：当缺少必要前置时，为测试生成可用的受信前置凭据。"""
    d = domain.lower()
    if d == "auth":
        fixture = {
            "fixture_injected": True,
            "domain": "auth",
            "test_user": "test_audit_user@example.local",
            "test_pass": "TestAuditSecretPass123!",
            "injection_method": "in_memory_or_db_seed",
            "guidance": "使用此测试凭据驱动登录状态与安全性校验，无需依赖外部注册接口。",
        }
        if write_to_disk:
            fixtures_dir = project_root / "tests" / "fixtures"
            fixtures_dir.mkdir(parents=True, exist_ok=True)
            out_file = fixtures_dir / "auth_synthetic_fixture.json"
            out_file.write_text(json.dumps(fixture, indent=2, ensure_ascii=False), encoding="utf-8")
        return fixture
    elif d in {"order", "ecommerce"}:
        fixture = {
            "fixture_injected": True,
            "domain": "order",
            "test_order_id": "ORD-AUDIT-2026-SYNTH-001",
            "test_amount": 100.0,
            "test_currency": "USD",
            "injection_method": "in_memory_or_db_seed",
            "guidance": "使用此测试订单凭据驱动订单结算与取消逻辑验证。",
        }
        if write_to_disk:
            fixtures_dir = project_root / "tests" / "fixtures"
            fixtures_dir.mkdir(parents=True, exist_ok=True)
            out_file = fixtures_dir / "order_synthetic_fixture.json"
            out_file.write_text(json.dumps(fixture, indent=2, ensure_ascii=False), encoding="utf-8")
        return fixture
    elif d in {"wallet", "finance"}:
        fixture = {
            "fixture_injected": True,
            "domain": "wallet",
            "test_account_id": "ACC-AUDIT-2026-SYNTH-8888",
            "initial_balance": 1000.0,
            "injection_method": "in_memory_or_db_seed",
            "guidance": "使用此测试账户与初始余额驱动扣款与转账安全验证。",
        }
        if write_to_disk:
            fixtures_dir = project_root / "tests" / "fixtures"
            fixtures_dir.mkdir(parents=True, exist_ok=True)
            out_file = fixtures_dir / "wallet_synthetic_fixture.json"
            out_file.write_text(json.dumps(fixture, indent=2, ensure_ascii=False), encoding="utf-8")
        return fixture
    return {"fixture_injected": False, "domain": domain}


def inject_test_fixture(project_root: Path, gaps_result: dict[str, Any]) -> bool:
    """批量根据 spec-gaps 注入所有必要底座夹具。"""
    gaps = gaps_result.get("gaps", [])
    injected_any = False
    for g in gaps:
        d = g.get("domain", "")
        res = provide_prerequisite_fixture(d, project_root, write_to_disk=True)
        if res.get("fixture_injected"):
            injected_any = True
    return injected_any


def generate_domain_fixtures(
    worker_role: str,
    target_form: str = "backend",
    tech_stack: str = "generic",
    *,
    domain: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """生成领域专精合成测试底座夹具 (DOC-248)。

    支持 Auth、Logic、Exploit、Patch 以及 Mobile、Web 等形态特征。
    返回包含结构化夹具、测试脚手架代码 (scaffold_code) 与断言清单的字典。
    """
    role_raw = (worker_role or "generalist").lower()
    if "auth" in role_raw:
        eff_role = "Auth-Worker"
        eff_domain = "auth"
    elif "logic" in role_raw:
        eff_role = "Logic-Worker"
        eff_domain = "logic"
    elif "exploit" in role_raw:
        eff_role = "Exploit-Worker"
        eff_domain = "exploit"
    elif "patch" in role_raw:
        eff_role = "Patch-Worker"
        eff_domain = "patch"
    else:
        eff_role = "Recon-Worker"
        eff_domain = "recon"

    if domain:
        eff_domain = domain.lower()

    target_form_norm = (target_form or "backend").lower()
    tech_stack_norm = (tech_stack or "generic").lower()

    fixtures: dict[str, Any] = {}
    scaffold_code = ""
    guidance = ""

    # 1. 核心角色领域专精底座夹具
    if eff_domain == "auth":
        fixtures["dual_identity_token_matrix"] = {
            "role_a": {
                "user_id": "usr-audit-alice-01",
                "tenant_id": "tenant-alpha-001",
                "role": "tenant_member",
                "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.auth_token_tenant_a_user_alice",
                "headers": {
                    "Authorization": "Bearer auth_token_tenant_a_user_alice",
                    "X-Tenant-ID": "tenant-alpha-001",
                },
            },
            "role_b": {
                "user_id": "usr-audit-bob-02",
                "tenant_id": "tenant-beta-002",
                "role": "tenant_member",
                "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.auth_token_tenant_b_user_bob",
                "headers": {
                    "Authorization": "Bearer auth_token_tenant_b_user_bob",
                    "X-Tenant-ID": "tenant-beta-002",
                },
            },
        }
        fixtures["cross_tenant_access_assertion"] = {
            "rule": "Role A token MUST NOT access Tenant B resources",
            "expected_status_codes": [401, 403],
            "forbidden_status_codes": [200, 204],
            "assertion_snippet": (
                "# BOLA/IDOR Defense Assertion\n"
                "resp = client.get('/api/tenants/tenant-beta-002/data', headers=role_a_headers)\n"
                "assert resp.status_code in (401, 403), f'BOLA/IDOR Defect: Role A got {resp.status_code}'"
            ),
        }
        scaffold_code = (
            "import requests\n\n"
            "ROLE_A_HEADERS = {\n"
            "    'Authorization': 'Bearer auth_token_tenant_a_user_alice',\n"
            "    'X-Tenant-ID': 'tenant-alpha-001',\n"
            "}\n"
            "ROLE_B_HEADERS = {\n"
            "    'Authorization': 'Bearer auth_token_tenant_b_user_bob',\n"
            "    'X-Tenant-ID': 'tenant-beta-002',\n"
            "}\n\n"
            "def test_cross_tenant_idor_boundary(base_url: str):\n"
            "    target_url = f'{base_url}/api/tenants/tenant-beta-002/profile'\n"
            "    resp = requests.get(target_url, headers=ROLE_A_HEADERS, timeout=5.0)\n"
            "    assert resp.status_code in (401, 403), (\n"
            "        f'CRITICAL_IDOR_VULNERABILITY: Role A accessed Role B tenant with HTTP {resp.status_code}'\n"
            "    )\n"
        )
        guidance = (
            "使用注入的双租户身份矩阵 (Role A vs Role B)，重点验证横向越权 "
            "(BOLA/IDOR) 与租户数据隔离边界。严禁仅凭单账号通过断言安全。"
        )

    elif eff_domain == "logic":
        fixtures["concurrency_threadpool_10"] = {
            "max_workers": 10,
            "timeout_seconds": 3.0,
            "concurrency_limit_rule": "AUDIT-04: max_workers <= 10, timeout <= 3.0s",
            "synchronization": "threading.Barrier(10)",
            "target_scenario": "high-concurrency race condition (TOCTOU, balance deduction, voucher over-redemption)",
        }
        fixtures["race_condition_assertion"] = {
            "initial_balance": 100,
            "concurrent_requests": 10,
            "deduct_amount_per_request": 100,
            "expected_successful_deductions": 1,
            "expected_rejected_deductions": 9,
            "conservation_invariant": "final_balance >= 0 and successful_count == 1",
        }
        scaffold_code = (
            "import concurrent.futures\n"
            "import threading\n\n"
            "# AUDIT-04: Concurrency cap <= 10, hard timeout 3.0s circuit breaker\n"
            "MAX_WORKERS = 10\n"
            "TIMEOUT_SECONDS = 3.0\n\n"
            "def run_concurrency_race_scaffold(action_callable, payload):\n"
            "    barrier = threading.Barrier(MAX_WORKERS)\n"
            "    successes, failures = [], []\n\n"
            "    def _worker(thread_idx):\n"
            "        try:\n"
            "            barrier.wait(timeout=TIMEOUT_SECONDS)\n"
            "            res = action_callable(payload)\n"
            "            return ('SUCCESS', res)\n"
            "        except Exception as exc:\n"
            "            return ('FAIL', str(exc))\n\n"
            "    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:\n"
            "        futures = [executor.submit(_worker, i) for i in range(MAX_WORKERS)]\n"
            "        for fut in concurrent.futures.as_completed(futures, timeout=TIMEOUT_SECONDS):\n"
            "            status, val = fut.result()\n"
            "            if status == 'SUCCESS':\n"
            "                successes.append(val)\n"
            "            else:\n"
            "                failures.append(val)\n"
            "    return successes, failures\n"
        )
        guidance = (
            "使用基于 ThreadPoolExecutor(max_workers=10) 的高并发压测脚手架，测试状态机跃迁与 "
            "TOCTOU 竞争漏洞，受 3.0s 硬熔断保护 (AUDIT-04)。"
        )

    elif eff_domain == "exploit":
        fixtures["ast_taint_path_context"] = {
            "source_type": "external_untrusted_input",
            "source_locations": ["HTTP_GET_PARAMS", "HTTP_POST_BODY", "INTENT_EXTRAS"],
            "sanitizer_audit": "missing_or_flawed_escape",
            "dangerous_sinks": ["sql_exec", "os_system", "eval", "deserialize", "jndi_lookup"],
        }
        fixtures["twin_sandbox_oracle"] = {
            "invariant": "AUDIT-05: 0/1 Exit Code Differential Reproduction",
            "baseline_exit_code": 0,
            "exploit_exit_code": 1,
            "sole_ground_truth": "Exit code differential between benign and attack payload",
        }
        scaffold_code = (
            "#!/usr/bin/env python3\n"
            "# AUDIT-05: AST 污点路径验证与 0/1 退出码差分复现模板\n"
            "import sys\n\n"
            "def run_baseline_sandbox() -> int:\n"
            "    # 良性输入必须正常通过并返回 0\n"
            "    benign_payload = 'normal_safe_input'\n"
            "    # ... 执行良性逻辑 ...\n"
            "    return 0\n\n"
            "def run_exploit_sandbox() -> int:\n"
            "    # 攻击 Payload 触发未控利用或崩溃时确凿返回 1\n"
            "    exploit_payload = \"' OR 1=1 --\"\n"
            "    # ... 触发利用链 ...\n"
            "    # 当漏洞确凿复现成立时退出 1\n"
            "    return 1\n\n"
            "if __name__ == '__main__':\n"
            "    assert run_baseline_sandbox() == 0, 'Baseline sandbox regression'\n"
            "    if run_exploit_sandbox() == 1:\n"
            "        print('CONFIRMED_VULNERABILITY: 0/1 differential established')\n"
            "        sys.exit(1)\n"
            "    sys.exit(0)\n"
        )
        guidance = (
            "利用 AST 污点路径追踪上下文，构造双沙箱 0/1 退出码差分 PoC (AUDIT-05)。"
            "严禁仅凭 HTTP 500/403 判定漏洞。"
        )

    elif eff_domain == "patch":
        fixtures["cwe_knowledge_base"] = {
            "CWE-89": {"name": "SQL Injection", "defense": "Use parameterized queries or ORM bind variables; eliminate raw string concatenation."},
            "CWE-287": {"name": "Improper Authentication", "defense": "Enforce strict tenant and role authorization at the controller/service boundary."},
            "CWE-362": {"name": "Race Condition / TOCTOU", "defense": "Use database pessimistic locks (SELECT FOR UPDATE) or atomic CAS operations."},
            "CWE-502": {"name": "Deserialization of Untrusted Data", "defense": "Use safe JSON serialization, disable polymorphic auto-typing."},
            "CWE-79": {"name": "Cross-Site Scripting (XSS)", "defense": "Apply context-aware HTML/JS output encoding and strict Content-Security-Policy."},
            "CWE-22": {"name": "Path Traversal", "defense": "Sanitize input with os.path.basename or resolve relative to an absolute base dir."},
        }
        fixtures["path_sanitization_rules"] = {
            "rule": "Strip all local absolute paths from patches and logs",
            "pattern": r"(/Users/[^/\s]+|/home/[^/\s]+|[a-zA-Z]:\\[^\s]+)",
            "replacement": "<PROJECT_ROOT>",
        }
        fixtures["verify_py_scaffold"] = {
            "script_name": "verify.py",
            "contract": "Standalone regression gate: exit 0 when fixed, exit 1 when still vulnerable",
        }
        scaffold_code = (
            "#!/usr/bin/env python3\n"
            "\"\"\"verify.py - 自动化补丁回归验证守卫脚本 (DOC-248 Patch-Worker)。\n"
            "退出码规范：\n"
            "  0: 补丁验证通过，安全漏洞已彻底消除且无业务回归；\n"
            "  1: 漏洞仍可复现或修复引入了功能性破坏。\n"
            "\"\"\"\n"
            "import sys\n\n"
            "def verify_regression() -> bool:\n"
            "    # 1. 验证恶意 Payload 被安全拦截/中和\n"
            "    # 2. 验证良性业务输入依然正确处理\n"
            "    return True\n\n"
            "if __name__ == '__main__':\n"
            "    if verify_regression():\n"
            "        print('VERIFY_PASS: Patch verified and regression-free')\n"
            "        sys.exit(0)\n"
            "    print('VERIFY_FAIL: Vulnerability persists or regression detected')\n"
            "    sys.exit(1)\n"
        )
        guidance = "自动注入 CWE 知识库、绝对路径脱敏规则与 verify.py 生成脚手架，产出最小外科手术式修复。"

    else:  # recon
        fixtures["recon_asset_map"] = {
            "target_form": target_form_norm,
            "tech_stack": tech_stack_norm,
            "scope": "public_interfaces_and_routes",
        }
        scaffold_code = (
            "# Reconnaissance topology extraction template\n"
            "def extract_routes_and_surfaces():\n"
            "    return {'routes': [], 'auth_required': []}\n"
        )
        guidance = "执行快速资产拓扑与暴露面梳理，输出结构化发现供下游 Worker 使用。"

    # 2. 形态专属夹具补充 (Mobile / Web)
    if "mobile" in target_form_norm:
        if "ios" in target_form_norm:
            fixtures["ios_url_scheme_fixture"] = {
                "custom_scheme": "myapp://action?target=attacker&amount=1000",
                "entitlements_check": ["keychain-access-groups", "aps-environment"],
            }
        else:
            fixtures["android_intent_fixture"] = {
                "action": "android.intent.action.VIEW",
                "package": "com.example.app",
                "component": "com.example.app.PrivateExportedActivity",
                "flags": ["FLAG_ACTIVITY_NEW_TASK"],
                "extras": {"auth_bypass_token": "synth_injected_token_123"},
                "adb_command": "adb shell am start -n com.example.app/.PrivateExportedActivity --es auth_bypass_token synth_injected_token_123",
            }
    elif target_form_norm in ("web", "miniprogram"):
        fixtures["http_raw_request"] = {
            "method": "POST",
            "path": "/api/v1/resource",
            "headers": {
                "Host": "target.local",
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            "body_template": "{\"action\": \"query\", \"payload\": \"${PAYLOAD}\"}",
        }
        fixtures["playwright_session"] = {
            "headless": True,
            "viewport": {"width": 1280, "height": 720},
            "cookies": [{"name": "session_id", "value": "synth_web_session_abc", "domain": "target.local"}],
            "csrf_token": "csrf-synthetic-token-xyz",
        }

    return {
        "worker_role": eff_role,
        "target_form": target_form_norm,
        "tech_stack": tech_stack_norm,
        "domain": eff_domain,
        "fixtures": fixtures,
        "scaffold_code": scaffold_code,
        "guidance": guidance,
    }

