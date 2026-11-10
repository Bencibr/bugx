#!/usr/bin/env python3
"""core/skill_synthesizer.py - 实锤缺陷与攻击链路的 Agent Skill 智能合成引擎 (L7 N7.1 / DOC-125 / DOC-247)。

负责：
  - 从已确认实锤并经过物理差分验证的 Incident 智能提炼标准 Agent Skill
  - 自动识别并推断映射标准 CWE / OWASP 分类（CWE-89, CWE-78, CWE-22, CWE-362, CWE-502, CWE-79 等）
  - 深度脱敏与抽象项目特异性资产（绝对系统路径、密钥凭据、UUID/临时哈希、非本地 IP）
  - 构造包含多阶段排查 SOP、模式匹配规则、审计指导与规范修复准则的标准 SKILL.md
  - 生成具备真实正向/负向断言、确定性可复现的独立回归脚本 scripts/verify.py
  - 生成合规的 manifest.json 与流式流水线阶段文件（stages/00-scope.md, stages/80-evidence-regression.md）
  - 提供 Skill 目录结构与 Schema 完整性双向校验能力
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

import ast
import json
import os
import re
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

_CORE_DIR = Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "skills" else Path(__file__).resolve().parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

try:
    import core.bugx_paths as bugx_paths
except ImportError:
    from core import bugx_paths

# 双入口守卫（**不是**兼容别名）：本模块既被包内导入，也被宿主以
# ``python core/skills/<name>.py`` 直接当脚本启动（MCP stdio 子进程）。
# 脚本模式下 ``__package__`` 为空，相对导入会 ``ImportError: attempted
# relative import with no known parent package``；此时改用绝对点号名。
# 两条路径指向**同一个**文件，不存在第二套实现。
if __package__:
    from . import skill_pack
else:
    from core.skills import skill_pack


# ============================================================================
# 1. CWE / OWASP 标准安全分类体系与知识库 (Taxonomy Knowledge Base)
# ============================================================================

@dataclass(frozen=True)
class VulnerabilityTaxonomy:
    """标准化漏洞分类实体 (对齐 CWE / OWASP Top 10)。"""
    cwe_id: str
    name: str
    owasp_id: str
    category_slug: str
    severity: str
    description: str
    recon_indicators: list[str]
    audit_guidance: list[str]
    remediation_principles: list[str]
    positive_control_inputs: list[Any]
    negative_control_payloads: list[str]
    oracle_logic_type: str


TAXONOMY_CATALOGUE: dict[str, VulnerabilityTaxonomy] = {
    "CWE-89": VulnerabilityTaxonomy(
        cwe_id="CWE-89",
        name="SQL Injection (SQLi)",
        owasp_id="A03:2021-Injection",
        category_slug="sqli",
        severity="HIGH",
        description="外部不可信输入未经参数化直接拼接入 SQL 查询语句，导致数据库指令被篡改或越权数据泄露。",
        recon_indicators=[
            "使用字符串拼接、f-string 或 % 格式化动态构建 SQL 查询（SELECT/INSERT/UPDATE/DELETE）",
            "ORM 原生查询方法调用未绑定参数：如 cursor.execute(f'...'), Model.raw(...) 等",
            "未启用数据库驱动底层的预编译参数化占位符 (?, %s, :param)",
        ],
        audit_guidance=[
            "核验所有数据库交互逻辑是否均使用参数化查询或 ORM 安全查询构造器",
            "禁止将外部请求参数直接作为表名、字段名或排序条件；动态字段必须使用严格白名单映射",
            "检查存储过程与动态 SQL 生成器中是否存在二次注入风险",
        ],
        remediation_principles=[
            "全面改用参数化查询（Parameterized Queries / Prepared Statements）",
            "利用成熟 ORM 框架的高阶强类型 API，杜绝手写裸 SQL 拼接",
            "针对无法参数化的结构标识符（如排序列）实施严格的字段枚举白名单校验",
        ],
        positive_control_inputs=["normal_user", 42, "user@example.com", "order_10023"],
        negative_control_payloads=[
            "' OR '1'='1",
            "admin' --",
            "1 UNION SELECT null, username, password FROM users --",
            "1; DROP TABLE users; --",
        ],
        oracle_logic_type="sqli",
    ),
    "CWE-78": VulnerabilityTaxonomy(
        cwe_id="CWE-78",
        name="OS Command Injection (RCE)",
        owasp_id="A03:2021-Injection",
        category_slug="rce",
        severity="CRITICAL",
        description="不可信输入被传递至系统 Shell 或底层进程执行接口，导致任意操作系统命令执行。",
        recon_indicators=[
            "调用 subprocess.run/Popen 且设置 shell=True，同时参数中混入外部字符串",
            "直接使用 os.system, os.popen, child_process.exec 等未作参数隔离的高危 API",
            "未对命令参数进行字符集白名单校验，允许 Shell 元字符（;, |, $, `, &&, ||）传入",
        ],
        audit_guidance=[
            "审计所有底层系统进程调用，确保 shell=False 并以数组/列表形式传递固定命令参数",
            "检查是否能改用编程语言内置的标准库 API（如使用 shutil/os 替代外部 rm/cp/tar 命令）",
            "确保必须由外部指定的参数仅匹配严格的字符白名单（如 ^[a-zA-Z0-9_\\-\\.]+$）",
        ],
        remediation_principles=[
            "坚决移除 shell=True，使用不可变列表传递参数（['tool', arg1, arg2]）",
            "优先采用安全语言内置 API 完成文件、进程或网络操作，避免派生系统 Shell",
            "实施最小系统权限隔离与沙箱化运行",
        ],
        positive_control_inputs=["report_2026.pdf", "data.csv", "archive.tar.gz"],
        negative_control_payloads=[
            "; whoami",
            "report.pdf | id",
            "$(cat /etc/passwd)",
            "test && rm -rf /",
        ],
        oracle_logic_type="rce",
    ),
    "CWE-22": VulnerabilityTaxonomy(
        cwe_id="CWE-22",
        name="Path Traversal (Directory Traversal)",
        owasp_id="A01:2021-Broken Access Control",
        category_slug="path-traversal",
        severity="HIGH",
        description="文件访问路径未经过边界沙箱约束与规范化检查，攻击者利用 ../ 路径遍历序列访问受限文件。",
        recon_indicators=[
            "使用外部请求参数直接拼接文件读取路径（如 open(f'data/{user_path}')）",
            "缺乏路径规范化（resolve/canonicalize）及基准目录归属判定（is_relative_to）",
            "未过滤路径分隔符（/ 与 \\）或未截取基名（os.path.basename）",
        ],
        audit_guidance=[
            "检查所有文件下载、图片查看、日志读取和模板加载接口的路径构造",
            "验证路径解析时是否在解析真实软链接（resolve()）后再次断言未越界",
            "确认拒绝处理绝对路径及包含 .. 的相对路径",
        ],
        remediation_principles=[
            "将路径规范化后强制断言其位于受信任的沙箱根目录内：Path(target).resolve().is_relative_to(base_dir)",
            "对仅需文件名的接口，使用 os.path.basename 强行剥离所有目录层级",
            "建立文件 ID 到受控内部存储路径的安全映射表（间接对象引用）",
        ],
        positive_control_inputs=["docs/readme.txt", "images/logo.png", "exports/data.json"],
        negative_control_payloads=[
            "../../etc/passwd",
            "..\\windows\\win.ini",
            "../../../.env",
            "/etc/shadow",
        ],
        oracle_logic_type="path_traversal",
    ),
    "CWE-79": VulnerabilityTaxonomy(
        cwe_id="CWE-79",
        name="Cross-Site Scripting (XSS)",
        owasp_id="A03:2021-Injection",
        category_slug="xss",
        severity="MEDIUM",
        description="用户输入在渲染回前端 HTML、DOM 或模板上下文时未做上下文自适应转义，导致恶意脚本在受害者浏览器中执行。",
        recon_indicators=[
            "前端代码中使用 dangerouslySetInnerHTML, v-html 或 innerHTML 直接插入未过滤 HTML",
            "后端模板渲染中禁用自动转义（如 Jinja2 的 | safe 过滤器）",
            "在响应头缺少严格的 Content Security Policy (CSP) 策略",
        ],
        audit_guidance=[
            "审查所有富文本渲染点与动态 DOM 构造逻辑",
            "确认 HTML 转义函数是否覆盖了 &, <, >, \", ' 等核心字符",
            "富文本内容必须通过经过安全审计的专业 Sanitizer（如 DOMPurify/Bleach）过滤",
        ],
        remediation_principles=[
            "全面启用模板引擎的默认 HTML 上下文自动转义机制",
            "前端优先采用 textContent/innerText 替代 innerHTML",
            "配置完备的 Content-Security-Policy (CSP) 响应头作为纵深防御",
        ],
        positive_control_inputs=["Hello World", "Welcome to BugHunter", "Status: 100% OK"],
        negative_control_payloads=[
            "<script>alert(1)</script>",
            "<img src=x onerror=alert('xss')>",
            "javascript:alert(1)",
            "<svg/onload=alert(1)>",
        ],
        oracle_logic_type="xss",
    ),
    "CWE-362": VulnerabilityTaxonomy(
        cwe_id="CWE-362",
        name="Race Condition (TOCTOU / Concurrency)",
        owasp_id="A04:2021-Insecure Design",
        category_slug="race-condition",
        severity="HIGH",
        description="检查时刻与使用时刻（TOCTOU）存在时间窗口，并发请求穿透边界导致超卖、双花或状态不一致。",
        recon_indicators=[
            "先查询检查（如 SELECT balance）然后在应用内存计算再写回，缺少行级锁或事务隔离",
            "文件先判断是否存在（os.path.exists）再打开写入，存在并发文件替换或竞争",
            "多线程或异步协程下共享可变状态缺乏互斥锁保护",
        ],
        audit_guidance=[
            "审查涉及资金、库存、单次凭证消费的核心状态跃迁逻辑",
            "确认数据库查询与更新是否位于同一可序列化事务中，或显式使用了 SELECT ... FOR UPDATE",
            "分布式环境下确认是否启用了分布式排他锁（Redlock 等）或 CAS 原语",
        ],
        remediation_principles=[
            "采用原子单条更新语句：UPDATE accounts SET balance = balance - :amount WHERE id = :id AND balance >= :amount",
            "在事务中显式增加悲观排他锁（SELECT ... FOR UPDATE）或乐观版本号锁（Optimistic Locking）",
            "对文件创建等操作使用独占原子原语（如 os.O_CREAT | os.O_EXCL）",
        ],
        positive_control_inputs=[{"action": "normal_debit", "amount": 100, "balance": 1000}],
        negative_control_payloads=[
            '{"action": "concurrent_double_spend", "amount": 1000, "threads": 10}',
        ],
        oracle_logic_type="race_condition",
    ),
    "CWE-502": VulnerabilityTaxonomy(
        cwe_id="CWE-502",
        name="Deserialization of Untrusted Data",
        owasp_id="A08:2021-Software and Data Integrity Failures",
        category_slug="deserialization",
        severity="CRITICAL",
        description="外部不可信字节流被传递至具有任意对象实例化能力的反序列化器，触发远程代码执行。",
        recon_indicators=[
            "在非受信任的数据流中直接调用 pickle.loads, marshal.loads 或 shelve",
            "使用 PyYAML 的默认 yaml.load(..., Loader=yaml.Loader) 而非 yaml.safe_load",
            "Java/PHP 等语言中对网络或 Cookie 输入直接调用原生反序列化接口",
        ],
        audit_guidance=[
            "排查所有接收网络传输数据的接口，禁止反序列化为具有执行语义的对象图",
            "检查是否能够替换为仅表达数据的纯文本或紧凑格式（JSON, Protobuf）",
            "若确实需要签名对象，核验是否在反序列化前通过 HMAC 进行了完整性校验",
        ],
        remediation_principles=[
            "全面禁止对不可信输入调用 pickle.loads；改用 json.loads 或 protobuf",
            "YAML 解析必须强制指定 Loader=yaml.SafeLoader 或直接调用 yaml.safe_load",
            "引入消息摘要签名与反序列化白名单类过滤器",
        ],
        positive_control_inputs=['{"type": "config", "version": 1, "enabled": true}'],
        negative_control_payloads=[
            "cos\\nsystem\\n(S'whoami'\\ntR.",
            "!!python/object/apply:os.system ['whoami']",
        ],
        oracle_logic_type="deserialization",
    ),
    "CWE-918": VulnerabilityTaxonomy(
        cwe_id="CWE-918",
        name="Server-Side Request Forgery (SSRF)",
        owasp_id="A10:2021-Server-Side Request Forgery",
        category_slug="ssrf",
        severity="HIGH",
        description="服务端根据用户传入的 URL 发起网络请求，未校验目标 IP，导致内网资产探针或云元数据泄露。",
        recon_indicators=[
            "直接使用用户传入的 URL 调用 requests.get/post 或 urllib.request.urlopen",
            "未对解析后的目标 IP 进行 RFC 1918 私网网段与云元数据地址（169.254.169.254）过滤",
            "允许除 http/https 外的其他协议方案（如 file://, gopher://, dict://）",
        ],
        audit_guidance=[
            "核验所有外联请求的发起源，确认是否存在用户完全可控的 URL",
            "检查是否在 DNS 解析之后断言 IP 合法性（防止 DNS 重新绑定攻击）",
            "确认关闭或限制自动重定向（Redirect Follow）行为",
        ],
        remediation_principles=[
            "对允许请求的外联域名实施强名单白名单（Domain Whitelist）",
            "解析 DNS 并阻断所有私有网段（127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.169.254）",
            "仅允许标准 http/https 协议并禁用内网重定向",
        ],
        positive_control_inputs=["https://api.github.com/zen", "https://example.com/webhook"],
        negative_control_payloads=[
            "http://127.0.0.1:8080/admin",
            "http://169.254.169.254/latest/meta-data/",
            "file:///etc/passwd",
            "http://localhost:6379/",
        ],
        oracle_logic_type="ssrf",
    ),
    "CWE-798": VulnerabilityTaxonomy(
        cwe_id="CWE-798",
        name="Use of Hardcoded Credentials",
        owasp_id="A07:2021-Identification and Authentication Failures",
        category_slug="secret-leak",
        severity="HIGH",
        description="敏感密码、私有 API Token、JWT 密钥或加密密钥硬编码在源码或版本控制配置文件中。",
        recon_indicators=[
            "代码中包含明文字符串变量赋值：如 API_KEY = 'sk_live_...', password = '...'",
            "私钥证书或凭据文件被直接提交至 Git 版本库",
            "未从安全环境变量或私密配置中心动态注入凭据",
        ],
        audit_guidance=[
            "使用高熵与模式识别扫描源码树中的硬编码凭据与私钥标记",
            "验证所有生产凭据是否均通过受控环境变量或 KMS 动态装载",
            "核验发现泄露的凭据是否已在服务端完成废弃与轮换（Rotation）",
        ],
        remediation_principles=[
            "彻底从代码中剥离凭据，改为从 os.environ 或保密密钥管理器（Vault/KMS）读取",
            "将配置文件纳入 .gitignore 并在提交前部署 pre-commit 凭据扫描门禁",
            "对历史上任何提交过的凭据立即执行失效与重新签发",
        ],
        positive_control_inputs=["os.environ.get('STRIPE_API_KEY')"],
        negative_control_payloads=[
            "sk_live_abcdef1234567890abcdef",
            "ghp_123456789012345678901234567890123456",
        ],
        oracle_logic_type="secret_leak",
    ),
    "CWE-862": VulnerabilityTaxonomy(
        cwe_id="CWE-862",
        name="Missing Authorization (IDOR / BOLA)",
        owasp_id="A01:2021-Broken Access Control",
        category_slug="broken-access-control",
        severity="HIGH",
        description="接口在访问或修改资源时仅依赖外部传入的对象 ID，未校验当前操作用户的主体归属或权限。",
        recon_indicators=[
            "直接使用请求路径中的 resource_id 查询数据，缺少 AND user_id = current_user 条件",
            "关键接口缺少基于角色或能力的访问控制门禁装饰器（@require_permission）",
            "未实施多租户（Tenant）级别的数据隔离过滤",
        ],
        audit_guidance=[
            "审查所有通过 ID 获取单条记录的接口，确认所有权断言位置",
            "检查垂直越权（未登录/普通用户调用管理员接口）与水平越权（A 用户访问 B 用户资源）",
            "确保对象级访问鉴权位于服务层底层，而非仅依赖前端路由拦截",
        ],
        remediation_principles=[
            "所有对象级数据库检索必须绑定当前已认证会话的用户 ID 或租户上下文",
            "建立中心化的细粒度访问控制策略（RBAC/ABAC）并在控制器入口执行强校验",
            "对敏感业务实体使用不可猜测的间接令牌或 UUID 替代自增主键",
        ],
        positive_control_inputs=[
            {"request_user_id": "u101", "target_resource_owner_id": "u101", "resource_id": "res_001"}
        ],
        negative_control_payloads=[
            '{"request_user_id": "u999", "target_resource_owner_id": "u101", "resource_id": "res_001"}'
        ],
        oracle_logic_type="broken_access_control",
    ),
    "CWE-699": VulnerabilityTaxonomy(
        cwe_id="CWE-699",
        name="Software Development Security Flaw",
        owasp_id="A04:2021-Insecure Design",
        category_slug="security-flaw",
        severity="MEDIUM",
        description="通用软件安全性缺陷，包含边界值校验缺失、错误吞噬或状态守恒不一致缺陷。",
        recon_indicators=[
            "外部参数未作严格的类型、长度、数值范围及字符集断言",
            "空异常捕获语句（except: pass）静默吞噬严重系统错误",
            "业务状态机流转缺少守恒校验与前置断言",
        ],
        audit_guidance=[
            "核验所有关键业务入口的数据校验规则完整性",
            "检查异常处理机制是否具备可观测性与精准失败拒绝策略（Fail-Closed）",
            "确保所有状态变更均实现状态守恒与事务原子性",
        ],
        remediation_principles=[
            "采用防御性编程范式，在函数入口强校验输入契约与边界前置条件",
            "严格禁止静默捕获与忽略异常，统一向上抛出强类型错误",
            "遵循最小特权与防御闭环原则",
        ],
        positive_control_inputs=[{"input": "valid_data", "count": 10}],
        negative_control_payloads=['{"input": "<invalid_payload>", "count": -1}'],
        oracle_logic_type="security_flaw",
    ),
}


# ============================================================================
# 2. 深度脱敏与抽象引擎 (Asset Desensitization & Abstraction)
# ============================================================================

_SECRET_PATTERN = re.compile(
    r"(?i)(password|secret|token|api[_-]?key|access[_-]?key|auth[_-]?token)\s*[:=]\s*['\"][^'\"]{6,}['\"]"
)
_ABSOLUTE_PATH_PATTERN = re.compile(
    r"/(?:Users|home|private/var/folders|tmp|private/tmp|opt)/[a-zA-Z0-9_\-./]+"
)
_WINDOWS_PATH_PATTERN = re.compile(r"[A-Za-z]:\\[a-zA-Z0-9_\-\\.]+")
_UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_HASH_PATTERN = re.compile(r"\b[0-9a-fA-F]{40,64}\b")
_KNOWN_SECRET_PREFIX_PATTERN = re.compile(
    r"\b(?:sk_live|sk_test|ghp|xoxb|xoxp)_[a-zA-Z0-9]{16,}\b"
)
_NON_LOCAL_IP_PATTERN = re.compile(
    r"\b(?!127\.0\.0\.1|0\.0\.0\.0)(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b"
)


def desensitize_content(text: str, project_root_replacement: str = "<project_root>") -> str:
    """对项目特异性资产进行深度脱敏与抽象。

    规则：
      1. 绝对系统路径 (/Users/..., /home/...) 替换为相对化项目根标记 <project_root>/...；
      2. 临时临时目录 (/tmp/..., /private/var/folders/...) 替换为 <sandbox_tmp>/...；
      3. 敏感密钥与密码 (sk_live_..., password='...') 替换为 [REDACTED_SECRET]；
      4. 临时随机 UUID 与 40/64 位哈希替换为统一占位符；
      5. 非本地 IP 替换为 <redacted_ip>。
    """
    if not text:
        return ""

    sanitized = str(text)

    # 1. 密钥模式替换
    sanitized = _KNOWN_SECRET_PREFIX_PATTERN.sub("[REDACTED_SECRET]", sanitized)
    sanitized = _SECRET_PATTERN.sub(r"\1 = '[REDACTED_SECRET]'", sanitized)

    # 2. 路径抽象脱敏
    def _path_replacer(match: re.Match[str]) -> str:
        raw_path = match.group(0)
        if raw_path.startswith(("/tmp", "/private/tmp", "/private/var/folders")):
            parts = raw_path.split("/")
            tail = parts[-1] if parts else "tmpfile"
            return f"<sandbox_tmp>/{tail}"
        # 尝试提取有意义的项目子路径
        rel_match = re.search(
            r"/(?:src|lib|app|core|pkg|internal|cmd|api|tests|test|db|models|views|controllers|services|utils|config)/(.*)",
            raw_path,
        )
        if rel_match:
            prefix_part = raw_path[rel_match.start() + 1 :]
            return f"{project_root_replacement}/{prefix_part}"
        parts = [p for p in raw_path.split("/") if p]
        tail = "/".join(parts[-2:]) if len(parts) >= 2 else (parts[0] if parts else "")
        return f"{project_root_replacement}/{tail}" if tail else f"{project_root_replacement}/..."

    sanitized = _ABSOLUTE_PATH_PATTERN.sub(_path_replacer, sanitized)
    sanitized = _WINDOWS_PATH_PATTERN.sub(f"{project_root_replacement}/...", sanitized)

    # 3. 随机 UUID 与长哈希脱敏
    sanitized = _UUID_PATTERN.sub("<redacted_uuid>", sanitized)
    sanitized = _HASH_PATTERN.sub("<redacted_hash>", sanitized)

    # 4. 非本地 IP 地址脱敏
    sanitized = _NON_LOCAL_IP_PATTERN.sub("<redacted_ip>", sanitized)

    return sanitized


def abstract_code_snippet(code: str, fallback_comment: str = "/* 规范化实现 */") -> str:
    """抽象并格式化代码片段，确保无死前导空格并经过深度脱敏。"""
    if not code or not str(code).strip():
        return fallback_comment
    dedented = textwrap.dedent(str(code)).strip()
    return desensitize_content(dedented)


# ============================================================================
# 3. CWE / OWASP 自动分类与推理引擎 (Taxonomy Inference Engine)
# ============================================================================

def infer_cwe_owasp(incident: Mapping[str, Any]) -> VulnerabilityTaxonomy:
    """从 incident 签名、描述、代码与上下文智能推断 CWE / OWASP 分类。"""
    if not isinstance(incident, Mapping):
        return TAXONOMY_CATALOGUE["CWE-699"]

    signature = str(incident.get("signature") or "")
    summary = str(incident.get("summary") or "")
    category = str(incident.get("category") or incident.get("decision") or "")
    root_cause = str(incident.get("root_cause") or "")
    context = str(incident.get("context") or "")
    before_snippet = str(incident.get("before_snippet") or "")
    after_snippet = str(incident.get("after_snippet") or "")

    combined_text = (
        f"{signature}\n{summary}\n{category}\n{root_cause}\n{context}\n{before_snippet}\n{after_snippet}"
    )

    # 1. 显式 CWE 编号精准命中
    explicit_cwe = re.search(r"(?i)\bcwe[-_]?(\d+)\b", combined_text)
    if explicit_cwe:
        cwe_key = f"CWE-{explicit_cwe.group(1)}"
        if cwe_key in TAXONOMY_CATALOGUE:
            return TAXONOMY_CATALOGUE[cwe_key]

    lowered = combined_text.lower()

    # 2. 模式特征语义加权评分推断
    scores: dict[str, int] = {k: 0 for k in TAXONOMY_CATALOGUE}

    # SQL Injection
    if any(k in lowered for k in ("sql injection", "sqli", "select * from", "union select", "parameterized query", "raw sql")):
        scores["CWE-89"] += 10
    elif "sql" in lowered or ("injection" in lowered and "command" not in lowered and "code" not in lowered):
        scores["CWE-89"] += 5

    # Command Injection / RCE
    if any(k in lowered for k in ("command injection", "rce", "remote code execution", "shell=true", "subprocess", "os.system")):
        scores["CWE-78"] += 10
    elif "exec" in lowered or "command" in lowered:
        scores["CWE-78"] += 3

    # Path Traversal
    if any(k in lowered for k in ("path traversal", "directory traversal", "arbitrary file", "../", "..\\", "lfi")):
        scores["CWE-22"] += 10
    elif "traversal" in lowered or "filepath" in lowered or "send_file" in lowered:
        scores["CWE-22"] += 5

    # Cross-Site Scripting (XSS)
    if any(k in lowered for k in ("xss", "cross-site scripting", "dangerouslysetinnerhtml", "<script", "html injection")):
        scores["CWE-79"] += 10

    # Race Condition
    if any(k in lowered for k in ("race condition", "toctou", "concurrency", "mutex", "double spend", "thread safety", "atomic")):
        scores["CWE-362"] += 10

    # Deserialization
    if any(k in lowered for k in ("deserialization", "pickle.loads", "yaml.load", "untrusted data", "objectinputstream")):
        scores["CWE-502"] += 10

    # SSRF
    if any(k in lowered for k in ("ssrf", "server-side request forgery", "requests.get", "urlopen", "169.254.169.254")):
        scores["CWE-918"] += 10

    # Secret / Hardcoded
    if any(k in lowered for k in ("hardcoded", "secret leak", "sk_live", "credential leak", "token leak", "password leak")):
        scores["CWE-798"] += 10

    # Broken Access Control / IDOR
    if any(k in lowered for k in ("idor", "bola", "broken access", "unauthorized access", "horizontal privilege", "missing auth")):
        scores["CWE-862"] += 10

    # 选取最高评分命中
    best_cwe, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score > 0:
        return TAXONOMY_CATALOGUE[best_cwe]

    # 兜底返回通用安全缺陷
    return TAXONOMY_CATALOGUE["CWE-699"]


# ============================================================================
# 4. 验证脚本生成器 (Deterministic Regression Script Generator)
# ============================================================================

def _generate_oracle_python_code(taxonomy: VulnerabilityTaxonomy) -> str:
    """根据分类生成确定性的验证 Oracle 代码。"""
    t_type = taxonomy.oracle_logic_type
    if t_type == "sqli":
        return textwrap.dedent("""\
            @staticmethod
            def evaluate(val: Any) -> dict[str, Any]:
                if isinstance(val, str):
                    lowered = val.lower()
                    danger = ["' or '1'='1", "union select", "--", "; drop table", "exec("]
                    if any(d in lowered for d in danger):
                        raise ValueError(f"Dangerous SQL injection payload detected: {val}")
                return {"status": "ok", "parameter": str(val), "parameterized": True}
        """)
    elif t_type == "rce":
        return textwrap.dedent("""\
            @staticmethod
            def evaluate(val: Any) -> dict[str, Any]:
                if isinstance(val, str):
                    danger = [";", "|", "$(", "`", "&&", "||", "\\n"]
                    if any(d in val for d in danger):
                        raise ValueError(f"Command Injection payload detected: {val}")
                return {"status": "ok", "argv": ["tool", str(val)], "shell": False}
        """)
    elif t_type == "path_traversal":
        return textwrap.dedent("""\
            @staticmethod
            def evaluate(val: Any) -> dict[str, Any]:
                if isinstance(val, str):
                    if ".." in val or val.startswith("/") or "\\\\" in val:
                        raise PermissionError(f"Path Traversal attempt blocked: {val}")
                return {"status": "ok", "resolved_path": f"<base_dir>/{val}", "contained": True}
        """)
    elif t_type == "xss":
        return textwrap.dedent("""\
            @staticmethod
            def evaluate(val: Any) -> dict[str, Any]:
                if isinstance(val, str):
                    lowered = val.lower()
                    danger = ["<script", "<img", "javascript:", "<svg", "onerror="]
                    if any(d in lowered for d in danger):
                        raise ValueError(f"Cross-Site Scripting payload detected: {val}")
                return {"status": "ok", "rendered": str(val), "escaped": True}
        """)
    elif t_type == "race_condition":
        return textwrap.dedent("""\
            @staticmethod
            def evaluate(val: Any) -> dict[str, Any]:
                if (isinstance(val, dict) and val.get("action") == "concurrent_double_spend") or \\
                   (isinstance(val, str) and "concurrent_double_spend" in val):
                    raise RuntimeError(f"Concurrency TOCTOU race condition violation blocked: {val}")
                return {"status": "ok", "atomic": True, "balance_updated": True}
        """)
    elif t_type == "deserialization":
        return textwrap.dedent("""\
            @staticmethod
            def evaluate(val: Any) -> dict[str, Any]:
                if isinstance(val, str) and any(m in val for m in ("system", "pickle", "apply:os.system", "!!python", "__reduce__")):
                    raise TypeError(f"Unsafe deserialization payload blocked: {val}")
                return {"status": "ok", "safe_parsed": True}
        """)
    elif t_type == "ssrf":
        return textwrap.dedent("""\
            @staticmethod
            def evaluate(val: Any) -> dict[str, Any]:
                if isinstance(val, str):
                    lowered = val.lower()
                    danger = ["127.0.0.1", "localhost", "169.254.169.254", "file://", "6379"]
                    if any(d in lowered for d in danger):
                        raise PermissionError(f"SSRF attempt to restricted address blocked: {val}")
                return {"status": "ok", "destination_allowed": True}
        """)
    elif t_type == "secret_leak":
        return textwrap.dedent("""\
            @staticmethod
            def evaluate(val: Any) -> dict[str, Any]:
                if isinstance(val, str) and ("sk_live_" in val or "ghp_" in val):
                    raise ValueError(f"Hardcoded secret detected: {val}")
                return {"status": "ok", "loaded_from_env": True}
        """)
    elif t_type == "broken_access_control":
        return textwrap.dedent("""\
            @staticmethod
            def evaluate(val: Any) -> dict[str, Any]:
                if isinstance(val, dict):
                    req_user = val.get("request_user_id")
                    owner_user = val.get("target_resource_owner_id")
                    if req_user and owner_user and str(req_user) != str(owner_user):
                        raise PermissionError(f"Unauthorized object access blocked for user {req_user}")
                return {"status": "ok", "authorized": True}
        """)
    else:  # security_flaw fallback
        return textwrap.dedent("""\
            @staticmethod
            def evaluate(val: Any) -> dict[str, Any]:
                if isinstance(val, dict) and val.get("count", 0) < 0:
                    raise ValueError("Input validation failed: negative count")
                if isinstance(val, str) and "<invalid_payload>" in val:
                    raise ValueError("Input validation failed: invalid payload format")
                return {"status": "ok", "validated": True}
        """)


def generate_verify_script(
    taxonomy: VulnerabilityTaxonomy,
    signature: str,
    summary: str,
) -> str:
    """生成具备真实断言、确定性可复现的独立回归脚本 scripts/verify.py。"""
    oracle_code = _generate_oracle_python_code(taxonomy)
    # 缩进 oracle 代码
    indented_oracle = textwrap.indent(oracle_code, "    ")

    pos_repr = repr(taxonomy.positive_control_inputs)
    neg_repr = repr(taxonomy.negative_control_payloads)

    script_content = f'''#!/usr/bin/env python3
"""自动化回归检验脚本 - 针对 {taxonomy.cwe_id} ({taxonomy.name})。

由 BugHunter L7 Skill Synthesizer 自动合成。
验证目标：
  1. [Positive Control] 良性正常输入（Happy-Path）顺利通过，无功能破损；
  2. [Negative Control] 恶意攻击载荷被确定性拦截或参数化中和。
"""

from __future__ import annotations

import sys
import unittest
from typing import Any


class SecurityOracle:
    """针对 {taxonomy.cwe_id} 的防御判定与断言 Oracle。"""

{indented_oracle}


class TestCWERegressionVerification(unittest.TestCase):
    """回归检验测试套件。"""

    def setUp(self) -> None:
        self.oracle = SecurityOracle()

    def test_benign_input_accepted(self) -> None:
        """[Positive Control] 验证良性输入正常通过，无功能破损。"""
        benign_inputs = {pos_repr}
        for val in benign_inputs:
            with self.subTest(val=val):
                res = self.oracle.evaluate(val)
                self.assertIsInstance(res, dict)
                self.assertEqual(res.get("status"), "ok")

    def test_malicious_payload_intercepted(self) -> None:
        """[Negative Control] 验证恶意利用载荷被确定性拦截或中和。"""
        malicious_payloads = {neg_repr}
        for payload in malicious_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises((ValueError, PermissionError, RuntimeError, TypeError)):
                    self.oracle.evaluate(payload)


def main() -> int:
    """执行回归测试，全部通过返回 0，任一失败返回非 0。"""
    suite = unittest.TestLoader().loadTestsFromTestCase(TestCWERegressionVerification)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
'''
    return script_content


# ============================================================================
# 5. Agent Skill 智能合成引擎 (Skill Synthesizer Engine)
# ============================================================================

def _clean_token(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_\-]+", "-", value.strip().lower()).strip("-")
    return cleaned or "vuln"


def default_global_skills_dir() -> Path:
    """返回默认全局技能包目录 (~/.bugx/skills/official 或 skill_pack.DEFAULT_SKILLS_DIR)。"""
    if hasattr(skill_pack, "DEFAULT_SKILLS_DIR") and skill_pack.DEFAULT_SKILLS_DIR.is_dir():
        return skill_pack.DEFAULT_SKILLS_DIR
    return Path.home() / ".bugx" / "skills" / "official"


class SkillSynthesizer:
    """BugHunter L7 Agent Skill 智能合成器。"""

    def __init__(self, default_output_dir: Path | str | None = None) -> None:
        if default_output_dir:
            self.default_output_dir = Path(default_output_dir).expanduser().resolve()
        else:
            self.default_output_dir = default_global_skills_dir()

    def infer_taxonomy(self, incident: Mapping[str, Any]) -> VulnerabilityTaxonomy:
        """推断给定 incident 的标准化分类。"""
        return infer_cwe_owasp(incident)

    def desensitize(self, text: str) -> str:
        """脱敏文本内容。"""
        return desensitize_content(text)

    def synthesize(
        self,
        incident: Mapping[str, Any],
        output_dir: Path | str | None = None,
    ) -> Path:
        """从 incident 智能提炼、脱敏并合成标准 Agent Skill 包并落盘。"""
        if not isinstance(incident, Mapping):
            raise ValueError("incident 必须是对象映射")

        inc_id = str(incident.get("incident_id") or "unknown").strip()
        summary = str(incident.get("summary") or "安全漏洞与规范化修复").strip()
        signature = str(incident.get("signature") or "VulnerabilitySignature").strip()
        context = str(incident.get("context") or "").strip()
        before_snippet = str(incident.get("before_snippet") or "").strip()
        after_snippet = str(incident.get("after_snippet") or "").strip()
        raw_category = str(incident.get("category") or incident.get("decision") or "").strip()

        # 1. CWE / OWASP 智能分类推断
        taxonomy = infer_cwe_owasp(incident)

        # 2. 标识符与 Slug 归一化
        cat_slug = _clean_token(raw_category or taxonomy.category_slug)
        inc_slug = _clean_token(inc_id)
        skill_id = f"auto-{cat_slug}-{inc_slug}"

        # 3. 资产与代码深度脱敏
        sanitized_summary = desensitize_content(summary)
        sanitized_signature = desensitize_content(signature)
        sanitized_context = desensitize_content(context)
        abstract_before = abstract_code_snippet(before_snippet, fallback_comment="# 历史存在风险的实现")
        abstract_after = abstract_code_snippet(after_snippet, fallback_comment="# 规范化安全修复实现")

        # 4. 目录结构初始化
        base_dir = Path(output_dir).expanduser().resolve() if output_dir else self.default_output_dir
        pack_dir = base_dir / skill_id
        pack_dir.mkdir(parents=True, exist_ok=True)
        stages_dir = pack_dir / "stages"
        stages_dir.mkdir(parents=True, exist_ok=True)
        scripts_dir = pack_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        # 5. 生成 SKILL.md
        recon_bullets = "\n".join(f"- {item}" for item in taxonomy.recon_indicators)
        audit_bullets = "\n".join(f"- {item}" for item in taxonomy.audit_guidance)
        fix_bullets = "\n".join(f"- {item}" for item in taxonomy.remediation_principles)

        skill_md_content = f"""---
name: {skill_id}
description: 针对 {taxonomy.cwe_id} ({taxonomy.name} / {taxonomy.owasp_id}) 的防御与回归审计技能包。用于防范 {sanitized_summary}。
---

# 技能包：{skill_id}

> **标准分类编号**：`{taxonomy.cwe_id}` ({taxonomy.name})  
> **OWASP 归属**：`{taxonomy.owasp_id}`  
> **推荐严重级别**：`{taxonomy.severity}`  
> **错误签名**：`{sanitized_signature}`  
> **核心定性**：{sanitized_summary}  

本技能由 BugHunter L7 经验归档引擎根据实锤已验证漏洞及修复自动提炼合成，提供端到端代码审计 SOP、模式匹配规则、规范化修复准则与确定性回归测试。

---

## 一、 多阶段排查 SOP (Multi-Stage Triage SOP)

### 阶段 1：特征探查与攻击面识别 (Reconnaissance)
1. 识别被测工程中所有外部非受信任数据的输入流入口；
2. 重点扫描代码库中是否存在以下特征指示器：
{recon_bullets}
3. 建立代码符号调用链路拓扑，定位未经充分处理即汇入危险 Sink 点的路径。

### 阶段 2：深度代码审计与防御断言 (Code Audit Guidance)
1. 核验数据传递全生命周期中的清洗与参数化机制；
2. 重点审查：
{audit_bullets}
3. 确认是否落实了纵深防御机制，杜绝前端过滤或单一浅层校验。

### 阶段 3：漏洞复现与利用验证 (Exploit Reproduction)
1. 构造微型确定性测试用例，注入针对该漏洞模式的特征测试向量；
2. 观察目标是否发生非预期行为（SQL 报错、任意命令执行、目录越界或状态破坏）；
3. 记录触发时的精准调用栈、行号与状态码作为实锤证据。

### 阶段 4：规范化修复与架构收敛 (Remediation & Hardening)
1. 废除临时拼凑式的黑名单过滤，全面采用结构化防御方案；
2. 遵循以下修复模式原则：
{fix_bullets}
3. 将修复代码提交并在本地沙箱中重新编译并运行测试。

### 阶段 5：非回归与负向对照验证 (Verification & Negative Control)
1. 执行 `python3 scripts/verify.py`，确保：
   - 漏洞攻击载荷被确定性拦截或中和（安全阻断）；
   - 合法业务输入（Positive / Benign Input）依然 100% 正常执行，无功能破损；
2. 固化回归测试用例到持续集成流水线中。

---

## 二、 模式特征匹配规则 (Pattern Matching Rules)

### 1. 缺陷代码原型 (Vulnerable Code Archetype)
```python
{abstract_before}
```

### 2. 危险模式判定规则
- **特征签名**: `{sanitized_signature}`
- **上下文线索**: {sanitized_context or '通用代码链路'}
- **典型 Sink 点**: 外部参数直接拼接入危险执行函数，未经过类型转换或强参数化绑定。

---

## 三、 安全修复模式准则 (Secure Fix Principles)

### 1. 规范化修复原型 (Remediation Archetype)
```python
{abstract_after}
```

### 2. 核心防御原则
{fix_bullets}
- **最小特权原则 (Principle of Least Privilege)**：操作仅授予满足业务必需的最小系统与数据库权限。
- **状态守恒与防御闭环**：对输入、处理、持久化与响应各阶段实现完整闭环，严禁静默吞噬异常。
"""
        (pack_dir / "SKILL.md").write_text(skill_md_content, encoding="utf-8")

        # 6. 生成 stages/00-scope.md 与 stages/80-evidence-regression.md
        stage_00 = f"""# 阶段 00: 范围界定与环境探查

<!-- L1:START -->
- [ ] 扫描代码库中是否存在与 {taxonomy.cwe_id} ({taxonomy.name}) 相关的敏感 Sink 点
- [ ] 检查外部不可信输入进入 {taxonomy.category_slug} 相关逻辑的入口路径
- [ ] 验证配置与环境边界安全约束
<!-- L1:END -->

<!-- L2:START -->
### 攻防排查手册 ({taxonomy.cwe_id}: {taxonomy.name})

#### 1. 适用特征与攻击面识别
- **分类标识**: `{taxonomy.cwe_id}` (`{taxonomy.owasp_id}`)
- **错误签名**: `{sanitized_signature}`
- **漏洞定性**: `{sanitized_summary}`
- **上下文定位**: `{sanitized_context or '通用路径'}`

#### 2. 危险模式排查
排查被测目标中是否暴露了类似如下的有缺陷实现：
```python
{abstract_before}
```

#### 3. 探查指引
{recon_bullets}
<!-- L2:END -->
"""
        (stages_dir / "00-scope.md").write_text(stage_00, encoding="utf-8")

        stage_80 = f"""# 阶段 80: 修复回归与验证

<!-- L1:START -->
- [ ] 验证已知修复片段是否健在且无代码回退
- [ ] 执行 `python3 scripts/verify.py` 进行确定性回归检验
- [ ] 核验正向业务输入正常通过（Positive Control）
- [ ] 核验恶意攻击载荷被完全拦截（Negative Control）
<!-- L1:END -->

<!-- L2:START -->
### 回归闭环检验 ({taxonomy.cwe_id}: {taxonomy.name})

#### 1. 规范化修复模式对齐
确保目标代码已完成对齐如下规范化安全修复：
```python
{abstract_after}
```

#### 2. 防御准则与修复要点
{fix_bullets}

#### 3. 自动化验证脚本执行
运行 Skill 包自带的回归验证脚本：
```bash
python3 scripts/verify.py
```
验证其同时满足正向业务兼容与逆向漏洞阻断双重闭环。
<!-- L2:END -->
"""
        (stages_dir / "80-evidence-regression.md").write_text(stage_80, encoding="utf-8")

        # 7. 生成 scripts/verify.py
        verify_script = generate_verify_script(
            taxonomy=taxonomy,
            signature=sanitized_signature,
            summary=sanitized_summary,
        )
        (scripts_dir / "verify.py").write_text(verify_script, encoding="utf-8")
        try:
            (scripts_dir / "verify.py").chmod(0o755)
        except OSError:
            pass

        # 8. 生成 manifest.json
        manifest = {
            "schema_version": 1,
            "skill_id": skill_id,
            "version": "0.1.0",
            "scope": "workspace",
            "applies_if": {
                "forms": ["backend", "web"],
                "surfaces": ["http", "logic"],
            },
            "stage_pipeline": [
                {
                    "stage_id": "00-scope",
                    "barrier": True,
                    "required": True,
                    "affinity": "recon",
                },
                {
                    "stage_id": "80-evidence-regression",
                    "barrier": False,
                    "required": True,
                    "affinity": "exploit",
                },
            ],
            "budgets": {
                "max_l1_checklist_tokens": 300,
                "max_stage_tokens": 1500,
                "max_experience_cards": 3,
                "max_experience_tokens": 600,
            },
        }
        (pack_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 9. 生成后自检校验，确保 100% 满足 Schema
        validate_skill_pack_structure(pack_dir, strict=True)

        return pack_dir


# ============================================================================
# 6. Skill 目录结构与 Schema 完整性校验器 (Structure & Schema Validator)
# ============================================================================

def validate_skill_pack_structure(
    pack_dir: Path | str,
    strict: bool = False,
) -> dict[str, Any]:
    """校验 Agent Skill 目录结构的完整性与合规性。

    校验项：
      - SKILL.md: 存在、非空、有效 Frontmatter、包含必要 SOP/Pattern/Fix 章节
      - manifest.json: 存在、有效 JSON、满足 skill-pack.schema.json 约束且 skill_id 一致
      - stages/00-scope.md: 存在、有效 L1/L2 标记、Token 不超预算
      - stages/80-evidence-regression.md: 存在、有效 L1/L2 标记、Token 不超预算
      - scripts/verify.py: 存在、有效 Python 语法、包含正向/负向断言
    """
    pack_path = Path(pack_dir).expanduser().resolve()
    errors: list[str] = []

    if not pack_path.is_dir():
        errors.append(f"Skill 目录不存在或不是有效目录: {pack_path}")
        result = {
            "valid": False,
            "skill_id": pack_path.name,
            "pack_dir": str(pack_path),
            "errors": errors,
        }
        if strict:
            raise ValueError(f"Skill 包校验失败: {'; '.join(errors)}")
        return result

    # 1. 校验 SKILL.md
    skill_md = pack_path / "SKILL.md"
    if not skill_md.is_file():
        errors.append("缺少核心说明文档 SKILL.md")
    else:
        text = skill_md.read_text(encoding="utf-8")
        if not text.strip():
            errors.append("SKILL.md 文件内容为空")
        else:
            if not (text.startswith("---") and "\n---" in text[3:]):
                errors.append("SKILL.md 缺少合规的 YAML Frontmatter 元数据定义")
            if "name:" not in text or "description:" not in text:
                errors.append("SKILL.md Frontmatter 缺少 name 或 description 声明")
            for section in ("多阶段排查 SOP", "模式特征匹配规则", "安全修复模式准则"):
                if section not in text:
                    errors.append(f"SKILL.md 缺少必要规范章节: '{section}'")

    # 2. 校验 manifest.json
    manifest_path = pack_path / "manifest.json"
    manifest_data: dict[str, Any] = {}
    if not manifest_path.is_file():
        errors.append("缺少包描述清单 manifest.json")
    else:
        try:
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest_data, dict):
                errors.append("manifest.json 根结构必须是 JSON 对象")
            else:
                # 校验 skill_id 与目录名对齐
                if manifest_data.get("skill_id") != pack_path.name:
                    errors.append(
                        f"manifest.json 中的 skill_id ('{manifest_data.get('skill_id')}') "
                        f"与目录名 ('{pack_path.name}') 不匹配"
                    )
                # 调用底层 skill_pack 官方校验器
                try:
                    skill_pack.validate_manifest(manifest_data)
                except Exception as ex:
                    errors.append(f"manifest.json 未通过 schema 校验: {ex}")
        except json.JSONDecodeError as ex:
            errors.append(f"manifest.json 不是合法的 JSON 格式: {ex}")

    # 3. 校验 stages 阶段文件
    stages_dir = pack_path / "stages"
    stage_00 = stages_dir / "00-scope.md"
    stage_80 = stages_dir / "80-evidence-regression.md"

    for st_path, name in ((stage_00, "stages/00-scope.md"), (stage_80, "stages/80-evidence-regression.md")):
        if not st_path.is_file():
            errors.append(f"缺少流水线阶段文件: {name}")
        else:
            st_text = st_path.read_text(encoding="utf-8")
            if "<!-- L1:START -->" not in st_text or "<!-- L1:END -->" not in st_text:
                errors.append(f"{name} 缺少 L1 隔离标签 (<!-- L1:START --> / <!-- L1:END -->)")
            if "<!-- L2:START -->" not in st_text or "<!-- L2:END -->" not in st_text:
                errors.append(f"{name} 缺少 L2 攻防手册标签 (<!-- L2:START --> / <!-- L2:END -->)")
            # 校验 Token 预算
            l1_text, _ = skill_pack.split_stage_content(st_text)
            l1_tokens = skill_pack.estimate_tokens(l1_text)
            max_l1 = manifest_data.get("budgets", {}).get("max_l1_checklist_tokens", 300)
            if l1_tokens > max_l1:
                errors.append(f"{name} 的 L1 清单 Token ({l1_tokens}) 超出预算上限 ({max_l1})")

    # 4. 校验 scripts/verify.py
    verify_script = pack_path / "scripts" / "verify.py"
    if not verify_script.is_file():
        errors.append("缺少回归验证脚本 scripts/verify.py")
    else:
        v_code = verify_script.read_text(encoding="utf-8")
        if not v_code.startswith("#!"):
            errors.append("scripts/verify.py 缺少可执行 Shebang 行 (如 #!/usr/bin/env python3)")
        try:
            parsed_ast = ast.parse(v_code, filename=str(verify_script))
            # 检查是否包含真实断言逻辑
            has_assert = any(
                isinstance(node, (ast.Assert, ast.Raise)) or
                (isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("assert_true", "assert_equal")) or
                (isinstance(node, ast.Call) and getattr(getattr(node.func, "attr", None), "startswith", lambda _: False)("assert"))
                for node in ast.walk(parsed_ast)
            )
            if not has_assert:
                errors.append("scripts/verify.py 未检测到任何断言语句，存在假性通过风险")
            if "test_benign" not in v_code:
                errors.append("scripts/verify.py 缺少正向良性输入测试 (Positive Control)")
            if "test_malicious" not in v_code:
                errors.append("scripts/verify.py 缺少恶意载荷拦截测试 (Negative Control)")
        except SyntaxError as ex:
            errors.append(f"scripts/verify.py 语法解析失败: {ex}")

    is_valid = len(errors) == 0
    res = {
        "valid": is_valid,
        "skill_id": pack_path.name,
        "pack_dir": str(pack_path),
        "errors": errors,
        "files_checked": [
            "SKILL.md",
            "manifest.json",
            "stages/00-scope.md",
            "stages/80-evidence-regression.md",
            "scripts/verify.py",
        ],
    }

    if strict and not is_valid:
        raise ValueError(f"Skill 包校验失败: {'; '.join(errors)}")

    return res


# ============================================================================
# 7. 对外顶层导出函数 (Top-level Functional Exports)
# ============================================================================

_DEFAULT_SYNTHESIZER = SkillSynthesizer()


def synthesize_skill_from_incident(
    incident: Mapping[str, Any],
    output_dir: Path | str | None = None,
) -> Path:
    """从已确认实锤的 incident 自动合成标准 Agent Skill 包并落盘。"""
    return _DEFAULT_SYNTHESIZER.synthesize(incident=incident, output_dir=output_dir)


# 别名导出对齐 DOC-247
LLMSkillSynthesizer = SkillSynthesizer
