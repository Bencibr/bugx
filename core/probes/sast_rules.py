#!/usr/bin/env python3
"""core/sast_rules.py — 跨语言声明式安全规则库与 Schema 定义 (DOC-061)。

支持统一的规则模型，同一漏洞规则（CWE/OWASP）可在多语言（Python, Go, Java,
JavaScript, TypeScript, C/C++, Rust, PHP）下定义特定的危险模式（Sinks）
与净化器（Sanitizers）。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.retrieval.universal_code_matcher import (
    CallNode,
    ParsedSource,
    UniversalCodeMatcher,
    detect_string_concat,
)

from core.planning.dynamic_flow_analyzer import DynamicFlowAnalyzer, STRONG_SANITIZERS


@dataclass
class LanguageRuleSpec:
    """针对特定语言的规则特征匹配契约"""
    callee_patterns: list[str] = field(default_factory=list)      # 危险函数调用正则列表
    require_string_concat: bool = False                           # 是否必须伴随实参动态字符串拼接
    arg_patterns: list[str] = field(default_factory=list)         # 实参中必须命中的特征正则
    sanitizer_patterns: list[str] = field(default_factory=list)   # 净化器过滤正则（命中则抵消告警）
    line_patterns: list[str] = field(default_factory=list)        # 行级特征正则（如弱类型比较运算符）
    require_param_operand: bool = False                           # 是否必须命中所在函数的形参（外部可控的最小判据）
    require_dynamic_origin: bool = False                          # 是否要求实参的**目标 origin** 动态可控（SSRF 专用）
    require_callee_base: tuple[str, ...] | None = None            # callee 基对象白名单；None=不约束，"" 表示裸调用
    arg_patterns_allow_taint: bool = False                        # arg_patterns 未命中时是否允许污点兜底（仅限「特征只是来源命名启发式」的规则）
    # 调用点邻近窗口特征（组间 AND、组内 OR，匹配净代码行 [line-before, end+after]）。
    # 用于「shell 特征不在调用实参而在属性赋值」的形态（如 swift Process.run()
    # 的 executableURL/arguments 赋值）——arg_patterns 对无实参调用结构性不可满足，
    # 只能由调用点上下文承担（靶场 I-CMD 实证零命中，F-RSI-20261118-003）。
    context_patterns: list[list[str]] = field(default_factory=list)


@dataclass
class SASTRule:
    id: str
    name: str
    category: str
    severity: str        # critical, high, medium, low
    confidence: str      # high, medium, low
    cwe: str             # e.g. CWE-78, CWE-89
    owasp: str           # e.g. A03:2021-Injection
    message: str
    remediation: str
    languages: dict[str, LanguageRuleSpec] = field(default_factory=dict)


# 内置核心高危规则库 (OWASP Top 10 多语言全谱系)
BUILTIN_RULES: list[SASTRule] = [
    # 1. 操作系统命令注入 (CWE-78)
    SASTRule(
        id="SEC-CMD-001",
        name="OS Command Injection",
        category="command-injection",
        severity="critical",
        confidence="high",
        cwe="CWE-78",
        owasp="A03:2021-Injection",
        message="调用命令执行函数且可能伴随未转义参数或 shell 解析，存在命令注入风险",
        remediation="避免将外部不可信数据拼接入命令；改用参数数组列表形式调用，禁用 shell 解析。",
        languages={
            "python": LanguageRuleSpec(
                callee_patterns=[r"^os\.(system|popen)$", r"^subprocess\.(run|Popen|call|check_call|check_output)$"],
                arg_patterns=[r"shell\s*=\s*True", r"^os\.(system|popen)"],
                sanitizer_patterns=[r"shlex\.quote\("],
            ),
            "go": LanguageRuleSpec(
                callee_patterns=[r"^exec\.Command(Context)?$"],
                arg_patterns=[r'("sh"|"bash"|"cmd\.exe")\s*,\s*("-c"|"/c")'],
                require_string_concat=True,
            ),
            "java": LanguageRuleSpec(
                callee_patterns=[r"^(Runtime\.getRuntime\(\)\.exec|ProcessBuilder)$"],
                require_string_concat=True,
            ),
            "javascript": LanguageRuleSpec(
                callee_patterns=[r"^(child_process\.)?(exec|execSync)$"],
                # `RegExp.prototype.exec` 与 `child_process.exec` 同名同参形状：
                # 压缩产物里的 `this.rules.inline.link.exec(e)`、`/x/g.exec(s)`
                # 曾被整批报成 critical 命令注入。区别只在 receiver——命令执行
                # 是裸调用或 child_process 命名空间调用，正则匹配则是「任意表达式
                # 上的方法」。白名单里的空串专指裸调用（见 ``_callee_base_allowed``）。
                require_callee_base=("", "child_process"),
                sanitizer_patterns=[r"execFile"],
            ),
            "typescript": LanguageRuleSpec(
                callee_patterns=[r"^(child_process\.)?(exec|execSync)$"],
                require_callee_base=("", "child_process"),
                sanitizer_patterns=[r"execFile"],
            ),
            "php": LanguageRuleSpec(
                callee_patterns=[r"^(system|exec|passthru|shell_exec|popen)$"],
                sanitizer_patterns=[r"escapeshellarg", r"escapeshellcmd"],
            ),
            "c": LanguageRuleSpec(
                callee_patterns=[r"^(system|popen)$"],
            ),
            "cpp": LanguageRuleSpec(
                callee_patterns=[r"^(system|popen)$"],
            ),
            "csharp": LanguageRuleSpec(
                callee_patterns=[r"^(System\.Diagnostics\.)?Process\.Start$"],
                arg_patterns=[r"\b(cmd|powershell|bash|sh|/c|-Command)\b", r"\b(input|request|query|args|command)\b"],
                sanitizer_patterns=[r"\bArgumentList\b", r"\bRegex\.Escape\("],
            ),
            "kotlin": LanguageRuleSpec(
                callee_patterns=[r"^(ProcessBuilder|Runtime\.getRuntime|exec)$"],
                arg_patterns=[r"\b(sh|bash|cmd\.exe|-c|/c)\b", r"\b(input|request|query|params|command)\b"],
                sanitizer_patterns=[r"\bProcessBuilder\s*\([^)]*\bcommandList\b", r"\bShellwords\b"],
            ),
            "swift": LanguageRuleSpec(
                callee_patterns=[r"^(Process|Foundation\.Process)\.run$"],
                # swift 的 Process.run() 惯用调用**无实参**——shell 特征在
                # executableURL/arguments 属性赋值行。原 arg_patterns 要求实参
                # 命中 /bin/sh 与输入命名，对无实参调用结构性不可满足，靶场
                # I-CMD 实证零命中（F-RSI-20261118-003）。改由调用点邻近窗口
                # 判定：可执行文件指向 sh/bash（组1）且 arguments 赋值含动态
                # 拼接或插值（组2，组间 AND）——纯字面量 arguments 不报。
                context_patterns=[
                    [r"/bin/(sh|bash)\b"],
                    [r"arguments\s*=[^\n]*(\+|\\\()"],
                ],
                sanitizer_patterns=[r"\barguments\s*:\s*\[\s*\"[^\"]+\"\s*\]"],
            ),
            "ruby": LanguageRuleSpec(
                callee_patterns=[r"^(system|exec|spawn|IO\.popen|Open3\.(capture\d*|pipeline))$"],
                arg_patterns=[r"\b(params|request|input|ENV|command)\b"],
                sanitizer_patterns=[r"\bShellwords\.escape\("],
            ),
        }
    ),

    # 2. SQL 字符串动态拼接注入 (CWE-89)
    SASTRule(
        id="SEC-SQLI-001",
        name="SQL Injection via String Concatenation",
        category="sql-injection",
        severity="critical",
        confidence="high",
        cwe="CWE-89",
        owasp="A03:2021-Injection",
        message="SQL 查询执行语句使用动态字符串格式化或拼接构造，存在直接 SQL 注入风险",
        remediation="严格改用预编译参数化占位符（Parameterized Prepared Statements），禁止字符串拼接查询语句。",
        languages={
            "python": LanguageRuleSpec(
                # 基对象不能限定成 cursor/db/conn/session 这几个字面名：
                # 真实代码里 sink 大量写作 ``_conn().execute(...)``、``cur.execute``、
                # ``self.db.execute``，甚至链式 ``_conn().execute(q).fetchall()``——
                # 后者的 callee 只剩裸 ``execute``。原模式对上述形态全部不匹配，
                # 规则等于只覆盖了四个变量名的运气。判定精度改由
                # ``require_string_concat`` 承担：只有实参确实是动态构造出来的
                # 查询串才报，参数化查询（``execute(sql, params)``）不触发。
                callee_patterns=[
                    r"(?:^|\.)(?:execute|executemany|executescript|raw|select|query)$",
                ],
                require_string_concat=True,
                require_param_operand=True,
                sanitizer_patterns=[r"parametrize", r"placeholder"],
            ),
            "go": LanguageRuleSpec(
                callee_patterns=[r"^(db|tx|stmt|conn)\.(Query|QueryRow|Exec|QueryContext|ExecContext)$"],
                require_string_concat=True,
                arg_patterns=[r'(\+\s*["\']|["\']\s*\+|\bfmt\.Sprintf\b)'],
            ),
            "java": LanguageRuleSpec(
                callee_patterns=[r"^(Statement|stmt|statement)\.(executeQuery|executeUpdate|execute)$"],
                require_string_concat=True,
                arg_patterns=[r'\+\s*[A-Za-z_]'],
            ),
            "javascript": LanguageRuleSpec(
                callee_patterns=[r"^(db|pool|client|connection)\.(query|execute)$"],
                require_string_concat=True,
                arg_patterns=[r'(`[^`]*\${|\+\s*[A-Za-z_])'],
                # 这里的 arg_patterns 描述的是「SQL 文本在调用点上被动态拼出来」。
                # 整条语句来自一个受污染变量（``db.query(sql)``，sql 赋值于
                # ``req.body.q``）时，调用点上根本看不到 `+`/`${`，拼接发生在
                # 上一行——污点已足以证明 SQL 文本动态可控，故允许它兜底。
                arg_patterns_allow_taint=True,
            ),
            "typescript": LanguageRuleSpec(
                callee_patterns=[r"^(db|pool|client|connection)\.(query|execute)$"],
                require_string_concat=True,
                arg_patterns=[r'(`[^`]*\${|\+\s*[A-Za-z_])'],
                arg_patterns_allow_taint=True,
            ),
            "php": LanguageRuleSpec(
                callee_patterns=[r"^(mysqli_query|mysql_query|\$db->query|\$pdo->query)$"],
                require_string_concat=True,
            ),
            "csharp": LanguageRuleSpec(
                callee_patterns=[r"^(SqlCommand|System\.Data\.SqlClient\.SqlCommand|.*\.Execute(Reader|NonQuery|Scalar))$"],
                require_string_concat=True,
                arg_patterns=[r"\+\s*[A-Za-z_]", r"\b(SELECT|INSERT|UPDATE|DELETE)\b"],
                sanitizer_patterns=[r"\bParameters\.Add\("],
            ),
            "kotlin": LanguageRuleSpec(
                # 原锚定 ``^SQLiteDatabase\.(rawQuery|execSQL)$`` 把接收者限定成
                # 字面类名，惯用 ``db.rawQuery(...)`` 全部漏检（靶场 A-SQLI 双
                # 形态实证：锚定变体命中、惯用变体漏检，F-RSI-20261118-003）。
                # rawQuery/execSQL 是 Android SQLite 专有 API 名，放开接收者
                # （任意 ``.rawQuery``/``.execSQL``）后精度仍由
                # require_string_concat + SQL 关键字 arg_patterns 承担。
                callee_patterns=[
                    r"(?:^|\.)(rawQuery|execSQL)$",
                    r"^(Statement\.(executeQuery|executeUpdate|execute)|JdbcTemplate\.(query|update))$",
                ],
                require_string_concat=True,
                arg_patterns=[r"\+\s*[A-Za-z_]", r"\b(SELECT|INSERT|UPDATE|DELETE)\b"],
                sanitizer_patterns=[r"\b(selectionArgs|arrayOf)\b", r"\?"],
            ),
            "swift": LanguageRuleSpec(
                callee_patterns=[r"^(SQLiteConnection|Database|statement|connection)\.execute$"],
                require_string_concat=True,
                arg_patterns=[r"\+\s*[A-Za-z_]", r"\b(SELECT|INSERT|UPDATE|DELETE)\b"],
                sanitizer_patterns=[r"\b(bind|bindValue|parameters)\b", r"\?"],
            ),
            "ruby": LanguageRuleSpec(
                callee_patterns=[r"^(db\.execute|find_by_sql)$"],
                require_string_concat=True,
                arg_patterns=[r"\+\s*[A-Za-z_]", r"\b(SELECT|INSERT|UPDATE|DELETE)\b"],
                arg_patterns_allow_taint=True,
                sanitizer_patterns=[r"\bsanitize_sql(?:_array|_for_conditions)?\b"],
            ),
        }
    ),

    # 3. 动态代码执行 / Eval 注入 (CWE-95)
    SASTRule(
        id="SEC-EVAL-001",
        name="Dangerous Dynamic Code Execution",
        category="code-injection",
        severity="critical",
        confidence="medium",
        cwe="CWE-95",
        owasp="A03:2021-Injection",
        message="直接使用 eval / exec 动态执行代码字符串，攻击者可利用输入实现任意代码执行",
        remediation="彻底移除 eval/exec 调用；解析结构化数据请改用 JSON 或安全字面量解析工具。",
        languages={
            "python": LanguageRuleSpec(
                callee_patterns=[r"^(eval|exec)$"],
                sanitizer_patterns=[r"ast\.literal_eval"],
            ),
            "javascript": LanguageRuleSpec(
                callee_patterns=[r"^(eval|Function)$"],
            ),
            "typescript": LanguageRuleSpec(
                callee_patterns=[r"^(eval|Function)$"],
            ),
            "php": LanguageRuleSpec(
                callee_patterns=[r"^(eval|assert)$"],
            ),
            "csharp": LanguageRuleSpec(
                callee_patterns=[r"^(CSharpScript\.Evaluate(?:Async)?|Microsoft\.CodeAnalysis\.CSharp\.Scripting\.CSharpScript\.EvaluateAsync)$"],
                arg_patterns=[r"\b(input|request|body|query|expression)\b"],
                sanitizer_patterns=[r"\b(ALLOWED|allowlist|SyntaxTree)\b"],
            ),
            "kotlin": LanguageRuleSpec(
                callee_patterns=[r"^(ScriptEngine|engine)\.eval$", r"^eval$"],
                arg_patterns=[r"\b(input|request|body|query|params|expression)\b"],
                sanitizer_patterns=[r"\b(ALLOWED|allowlist|SAFE_EXPRESSIONS)\b"],
            ),
            "swift": LanguageRuleSpec(
                callee_patterns=[r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)?evaluateScript$"],
                arg_patterns=[r"\b(input|request|body|query|expression)\b"],
                sanitizer_patterns=[r"\b(ALLOWED|allowlist|safeExpression)\b"],
            ),
            "ruby": LanguageRuleSpec(
                callee_patterns=[r"^(eval|instance_eval|class_eval|module_eval|ERB\.new)$"],
                arg_patterns=[r"\b(params|request|input|ENV|expression)\b"],
                sanitizer_patterns=[r"\b(ALLOWED|allowlist|ERB::Util)\b"],
            ),
        }
    ),

    # 4. 不安全的反序列化 (CWE-502)
    SASTRule(
        id="SEC-DESER-001",
        name="Insecure Deserialization",
        category="deserialization",
        severity="critical",
        confidence="high",
        cwe="CWE-502",
        owasp="A08:2021-Software-and-Data-Integrity-Failures",
        message="对未受信任的数据流执行对象反序列化，可导致任意对象实例化与远程代码执行",
        remediation="禁止反序列化外部不可信数据；采用结构化数据传输协议（如 JSON、Protocol Buffers）并加签防篡改。",
        languages={
            "python": LanguageRuleSpec(
                callee_patterns=[r"^(pickle|_pickle|cPickle|marshal)\.(load|loads)$"],
            ),
            "java": LanguageRuleSpec(
                callee_patterns=[r"^(ObjectInputStream\.readObject|XMLDecoder\.readObject)$"],
            ),
            "javascript": LanguageRuleSpec(
                callee_patterns=[r"^(node-serialize|serialize-to-js)\.(unserialize)$"],
            ),
            "php": LanguageRuleSpec(
                callee_patterns=[r"^(unserialize)$"],
            ),
            "csharp": LanguageRuleSpec(
                callee_patterns=[r"^(BinaryFormatter\.Deserialize|JsonConvert\.DeserializeObject|JavaScriptSerializer\.DeserializeObject)$"],
                arg_patterns=[r"\b(input|request|body|stream|payload)\b"],
                sanitizer_patterns=[r"\b(TypeNameHandling\.None|SafeMode|Binder)\b"],
            ),
            "kotlin": LanguageRuleSpec(
                callee_patterns=[r"^(ObjectInputStream\.readObject|Kryo\.readClassAndObject|Yaml\.load)$"],
                arg_patterns=[r"\b(input|request|body|stream|payload)\b"],
                sanitizer_patterns=[r"\b(SafeLoader|ClassWhitelist|allowList)\b"],
            ),
            "swift": LanguageRuleSpec(
                callee_patterns=[r"^NSKeyedUnarchiver\.(unarchiveObject|unarchiveTopLevelObjectWithData)$", r"^JSONSerialization\.jsonObject$"],
                arg_patterns=[r"\b(data|payload|input|request)\b"],
                sanitizer_patterns=[r"\b(requiresSecureCoding|allowedClasses|decodeIfPresent)\b"],
            ),
            "ruby": LanguageRuleSpec(
                callee_patterns=[r"^(Marshal\.load|YAML\.load|Psych\.load)$"],
                arg_patterns=[r"\b(params|request|payload|input|ENV)\b"],
                sanitizer_patterns=[r"\b(safe_load|permitted_classes|aliases\s*:\s*false)\b"],
            ),
        }
    ),

    # 5. 路径穿越与任意文件读写 (CWE-22)
    SASTRule(
        id="SEC-PATH-001",
        name="Path Traversal / Arbitrary File Access",
        category="path-traversal",
        severity="high",
        confidence="medium",
        cwe="CWE-22",
        owasp="A01:2021-Broken-Access-Control",
        message="直接将未经过滤的外部路径参数拼接入文件系统 I/O 函数，存在路径穿越风险",
        remediation="使用路径规范化函数处理路径，校验目标绝对路径必须落在预期白名单安全目录内。",
        languages={
            "python": LanguageRuleSpec(
                callee_patterns=[r"^(open|os\.open|os\.remove|os\.unlink)$"],
                require_string_concat=True,
                sanitizer_patterns=[r"os\.path\.abspath", r"Path\.resolve"],
            ),
            "go": LanguageRuleSpec(
                callee_patterns=[r"^(os\.Open|os\.ReadFile|os\.Create|ioutil\.ReadFile)$"],
                require_string_concat=True,
                sanitizer_patterns=[r"filepath\.Clean"],
            ),
            "java": LanguageRuleSpec(
                callee_patterns=[r"^(new FileInputStream|new FileOutputStream|Files\.readAllBytes|Files\.write)$"],
                require_string_concat=True,
                sanitizer_patterns=[r"getCanonicalPath", r"normalize\(\)"],
            ),
            "javascript": LanguageRuleSpec(
                callee_patterns=[r"^(fs\.)?(readFile|readFileSync|createReadStream|unlink|unlinkSync)$"],
                require_string_concat=True,
                sanitizer_patterns=[r"path\.resolve", r"path\.normalize"],
            ),
            "typescript": LanguageRuleSpec(
                callee_patterns=[r"^(fs\.)?(readFile|readFileSync|createReadStream|unlink|unlinkSync)$"],
                require_string_concat=True,
                sanitizer_patterns=[r"path\.resolve", r"path\.normalize"],
            ),
            "csharp": LanguageRuleSpec(
                callee_patterns=[r"^(File\.(ReadAllText|OpenRead|WriteAllText|Delete)|System\.IO\.File\.(ReadAllText|OpenRead|WriteAllText|Delete))$"],
                require_string_concat=True,
                sanitizer_patterns=[r"\bPath\.Get(?:FullPath|FileName|RelativePath)\("],
            ),
            "kotlin": LanguageRuleSpec(
                callee_patterns=[r"^(File|java\.io\.File|Files\.(readString|readAllBytes|write|deleteIfExists)|Paths\.get)$"],
                require_string_concat=True,
                sanitizer_patterns=[r"\b(canonical(File|Path)|normalize|toRealPath)\b"],
            ),
            "swift": LanguageRuleSpec(
                callee_patterns=[r"^(String|FileManager\.default\.(contents|removeItem|copyItem|moveItem))$"],
                require_string_concat=True,
                sanitizer_patterns=[r"\b(standardizedFileURL|resolvingSymlinksInPath|lastPathComponent)\b"],
            ),
            "ruby": LanguageRuleSpec(
                callee_patterns=[r"^(File\.(read|open|delete)|Pathname\.new)$"],
                require_string_concat=True,
                sanitizer_patterns=[r"\b(File\.expand_path|File\.basename|cleanpath)\b"],
            ),
        }
    ),

    # 5b. pathlib 拼接式路径穿越 (CWE-22)
    #
    # 现代 Python 惯用 `Path(base) / user_input` 组合路径后调用 read_text/write_text，
    # 既没有字符串拼接（不触发 require_string_concat），也不落在 open/os.open 上，
    # 因而逃过 SEC-PATH-001。解析器已把变量折叠进 resolved_callee
    # （形如 `base_dir / username.read_text`），故此处直接匹配「`/` 拼接 + pathlib sink」形态。
    #
    # 精度约束：`/` 右操作数必须**不是**字符串字面量——`Path(__file__).parent / "x.txt"`
    # 这类常量拼接不告警，避免对健康代码误报。
    SASTRule(
        id="SEC-PATH-002",
        name="pathlib Path Traversal",
        category="path-traversal",
        severity="high",
        confidence="medium",
        cwe="CWE-22",
        owasp="A01:2021-Broken-Access-Control",
        message="使用 `/` 运算符将外部可控变量拼入 Path 后直接做文件读写，未约束在基目录内，存在路径穿越风险",
        remediation="拼接后调用 `.resolve()` 归一化，并用 `is_relative_to(基目录)` 校验，或对输入做白名单校验。",
        languages={
            "python": LanguageRuleSpec(
                callee_patterns=[
                    r"/\s+(?![\"'])[A-Za-z_][\w.\[\]'\"]*\s*\.\s*"
                    r"(?:read_text|read_bytes|write_text|write_bytes|open)$",
                ],
                require_param_operand=True,
                sanitizer_patterns=[
                    r"\.resolve\(\)",
                    r"\bis_relative_to\b",
                    r"\.relative_to\(",
                    r"os\.path\.abspath",
                ],
            ),
        }
    ),

    # 6. 服务端请求伪造 SSRF (CWE-918)
    SASTRule(
        id="SEC-SSRF-001",
        name="Server-Side Request Forgery",
        category="ssrf",
        severity="high",
        confidence="medium",
        cwe="CWE-918",
        owasp="A10:2021-Server-Side-Request-Forgery",
        message="向外部动态提供的 URL 发起 HTTP/网络请求，可能导致内网端口探测与元数据服务暴露",
        remediation="对目标 URL 实施严格白名单域名与协议校验，阻断向 127.0.0.1/169.254.169.254 等私有内网段的请求。",
        languages={
            "python": LanguageRuleSpec(
                # 只认全限定名会漏掉最常见的两种导入写法，实测都是真阳性：
                # ``from urllib.request import urlopen`` 之后的裸 ``urlopen(...)``，
                # 以及 ``import urllib.request as u`` 之后的 ``u.urlopen(...)``。
                # ``urlopen`` 不是 ``exec`` 那种泛用名（不存在同名的高频标准库
                # 方法），所以放开单层别名 receiver 的误报面很小；即便命中，
                # 还要再过 require_dynamic_origin 这道 host 可控性判据。
                callee_patterns=[
                    r"^(requests\.(get|post|put|delete)"
                    r"|urllib\.request\.urlopen"
                    r"|[A-Za-z_]\w*\.urlopen"
                    r"|urlopen"
                    r"|httpx\.(get|post))$"
                ],
                require_dynamic_origin=True,
            ),
            "go": LanguageRuleSpec(
                callee_patterns=[r"^(http\.(Get|Post|Head)|http\.NewRequest)$"],
                require_dynamic_origin=True,
            ),
            "java": LanguageRuleSpec(
                callee_patterns=[r"^(new URL|HttpClient\.send|HttpClients\.createDefault)$"],
                require_dynamic_origin=True,
            ),
            "javascript": LanguageRuleSpec(
                callee_patterns=[r"^(axios\.(get|post)|fetch|http\.(get|request)|https\.(get|request))$"],
                require_dynamic_origin=True,
            ),
            "typescript": LanguageRuleSpec(
                callee_patterns=[r"^(axios\.(get|post)|fetch|http\.(get|request)|https\.(get|request))$"],
                require_dynamic_origin=True,
            ),
        }
    ),

    # 7. 弱加密算法与散列碰撞 (CWE-327)
    SASTRule(
        id="SEC-CRYPTO-001",
        name="Use of Broken or Weak Cryptographic Hash",
        category="weak-crypto",
        severity="medium",
        confidence="high",
        cwe="CWE-327",
        owasp="A02:2021-Cryptographic-Failures",
        message="使用了已证实存在碰撞或破损的弱哈希算法（如 MD5 / SHA1），不可用于安全散列或签名",
        remediation="改用 SHA-256、SHA-3 或安全密码散列算法（如 Argon2id、bcrypt、PBKDF2）。",
        languages={
            "python": LanguageRuleSpec(
                callee_patterns=[r"^hashlib\.(md5|sha1)$"],
            ),
            "go": LanguageRuleSpec(
                callee_patterns=[r"^(md5\.New|sha1\.New|md5\.Sum|sha1\.Sum)$"],
            ),
            "java": LanguageRuleSpec(
                callee_patterns=[r"^MessageDigest\.getInstance$"],
                arg_patterns=[r'["\'](MD5|SHA-1|SHA1)["\']'],
            ),
            "javascript": LanguageRuleSpec(
                callee_patterns=[r"^crypto\.createHash$"],
                arg_patterns=[r'["\'](md5|sha1)["\']'],
            ),
            "typescript": LanguageRuleSpec(
                callee_patterns=[r"^crypto\.createHash$"],
                arg_patterns=[r'["\'](md5|sha1)["\']'],
            ),
        }
    ),

    # 8. 不安全的临时文件创建竞态 (CWE-377)
    SASTRule(
        id="SEC-TEMP-001",
        name="Insecure Temporary File (TOCTOU Race Condition)",
        category="race-condition",
        severity="medium",
        confidence="high",
        cwe="CWE-377",
        owasp="A01:2021-Broken-Access-Control",
        message="使用不安全的临时文件创建函数，在文件名生成与打开之间存在 TOCTOU 竞争漏洞",
        remediation="改用原子创建临时文件的安全 API（如 tempfile.NamedTemporaryFile 或 mkstemp）。",
        languages={
            "python": LanguageRuleSpec(
                callee_patterns=[r"^tempfile\.mktemp$"],
            ),
            "c": LanguageRuleSpec(
                callee_patterns=[r"^(mktemp|tmpnam|tempnam)$"],
            ),
            "cpp": LanguageRuleSpec(
                callee_patterns=[r"^(mktemp|tmpnam|tempnam)$"],
            ),
        }
    ),

    # 9. Node.js 原型链污染 (CWE-1321)
    SASTRule(
        id="SEC-PROTO-001",
        name="Prototype Pollution",
        category="prototype-pollution",
        severity="high",
        confidence="high",
        cwe="CWE-1321",
        owasp="A03:2021-Injection",
        message="对象深拷贝、合并或属性动态赋值时未过滤 __proto__ / constructor，存在原型链污染风险",
        remediation="在合并对象属性前严格过滤 __proto__、constructor 等危险属性，或使用 Object.create(null)。",
        languages={
            "javascript": LanguageRuleSpec(
                callee_patterns=[r"^(lodash\.)?(merge|defaultsDeep|extend)$", r"^Object\.assign$", r"^deepMerge$"],
                arg_patterns=[r"(__proto__|constructor|prototype|req\.|request\.)"],
                sanitizer_patterns=[r"hasOwnProperty", r"Object\.create\(null\)"],
            ),
            "typescript": LanguageRuleSpec(
                callee_patterns=[r"^(lodash\.)?(merge|defaultsDeep|extend)$", r"^Object\.assign$", r"^deepMerge$"],
                arg_patterns=[r"(__proto__|constructor|prototype|req\.|request\.)"],
                sanitizer_patterns=[r"hasOwnProperty", r"Object\.create\(null\)"],
            ),
        }
    ),

    # 10. 服务端模版注入 SSTI (CWE-1336)
    SASTRule(
        id="SEC-SSTI-001",
        name="Server-Side Template Injection (SSTI)",
        category="template-injection",
        severity="critical",
        confidence="high",
        cwe="CWE-1336",
        owasp="A03:2021-Injection",
        message="直接动态编译或渲染外部可控的模版字符串，可能导致服务端模版注入 (SSTI) 与任意代码执行",
        remediation="改用静态模版文件并传递参数上下文变量（如 render_template），严禁拼接外部输入动态渲染模版。",
        languages={
            "python": LanguageRuleSpec(
                callee_patterns=[r"^(render_template_string|jinja2\.Template|Template|Environment\(\)\.from_string)$"],
                arg_patterns=[r'(\+|\%|\.format\(|f["\']|request\.|args\.)'],
            ),
            "javascript": LanguageRuleSpec(
                callee_patterns=[r"^(ejs|nunjucks|handlebars|pug)\.(render|compile)$"],
                arg_patterns=[r'(\+|req\.|request\.|`[^`]*\${)'],
            ),
            "typescript": LanguageRuleSpec(
                callee_patterns=[r"^(ejs|nunjucks|handlebars|pug)\.(render|compile)$"],
                arg_patterns=[r'(\+|req\.|request\.|`[^`]*\${)'],
            ),
        }
    ),

    # 11. PHP 弱类型哈希比较与魔法哈希 (CWE-208 / CWE-697)
    SASTRule(
        id="SEC-PHPHASH-001",
        name="PHP Loose Comparison Magic Hash Vulnerability",
        category="type-juggling",
        severity="high",
        confidence="high",
        cwe="CWE-208",
        owasp="A07:2021-Identification-and-Authentication-Failures",
        message="使用弱类型相等比较 (== 或 !=) 比对散列值，存在 PHP 魔法哈希 (0e...) 类型混淆与身份绕过风险",
        remediation="改用严格相等运算符 (=== 或 !==)，或使用 hash_equals() / password_verify() 进行常数时间安全比对。",
        languages={
            "php": LanguageRuleSpec(
                callee_patterns=[r"^(md5|sha1|hash|crypt)$"],
                line_patterns=[r'(==|!=)\s*(["\']0e\d+["\']|\$[A-Za-z0-9_]+)'],
                sanitizer_patterns=[r"hash_equals", r"password_verify"],
            ),
        }
    ),

    # 12. Ruby 动态方法反射调用 (CWE-470)
    SASTRule(
        id="SEC-DYNCALL-001",
        name="Ruby Unsafe Dynamic Method Dispatch",
        category="code-injection",
        severity="high",
        confidence="high",
        cwe="CWE-470",
        owasp="A03:2021-Injection",
        message="将外部未校验的参数直接传入动态方法分发调用 (send / public_send)，可导致调用任意私有方法或执行恶意代码",
        remediation="在调用 send / public_send 前，使用方法名白名单数组校验入参（例如 ALLOWED_ACTIONS.include?(action)）。",
        languages={
            "ruby": LanguageRuleSpec(
                callee_patterns=[r"^(send|public_send|__send__)$", r"\.(send|public_send|__send__)$"],
                arg_patterns=[r"(params|request|\bto_sym\b)"],
                sanitizer_patterns=[r"include\?"],
            ),
        }
    ),

    # 13. PHP 动态文件包含与伪协议流注入 (CWE-98)
    SASTRule(
        id="SEC-PHP-FILE-001",
        name="PHP Dynamic File Inclusion / Stream Injection",
        category="file-inclusion",
        severity="critical",
        confidence="high",
        cwe="CWE-98",
        owasp="A03:2021-Injection",
        message="动态包含外部可控的文件路径或 PHP 伪协议流 (phar://, php://filter)，存在本地/远程文件包含 (LFI/RFI) 与任意代码执行风险",
        remediation="禁止使用外部输入拼接 include / require 语句；使用严格白名单映射文件，并禁用 allow_url_include。",
        languages={
            "php": LanguageRuleSpec(
                callee_patterns=[r"^(include|include_once|require|require_once)$"],
                require_string_concat=True,
                arg_patterns=[r'(\$|phar:\/\/|php:\/\/|\.\.)'],
                sanitizer_patterns=[r"basename\(", r"in_array\("],
            ),
        }
    ),
    # 14. 移动端 PII 日志泄露 (CWE-532)：Log/System.out/print 将用户敏感字段
    # 拼入日志。靶场 A-LOG 实证零命中（F-RSI-20261118-003）；判据 = 日志调用
    # + 实参含 PII 形态（email 字面量或敏感命名）+ 动态拼接——纯静态文案
    # 日志（``Log.d("T", "started")``）不触发。
    SASTRule(
        id="SEC-PII-LOG",
        name="PII Leakage via Application Logging",
        category="privacy",
        severity="medium",
        confidence="medium",
        cwe="CWE-532",
        owasp="A09:2021-Security-Logging-and-Monitoring-Failures",
        message="日志调用拼接了疑似个人敏感信息（邮箱/手机号/token/密码等），存在凭据或 PII 落盘泄露风险",
        remediation="日志中禁止输出凭据与 PII；确需排障时对字段做脱敏（掩码/哈希前缀）并降低日志级别。",
        languages={
            "java": LanguageRuleSpec(
                callee_patterns=[r"^(Log|R\.Log)\.(d|i|w|e|v)$", r"^System\.(out|err)\.print(ln)?$"],
                arg_patterns=[r"@[\w.+-]+\.\w+", r"\b(email|phone|mobile|token|password|secret|ssn|api[_-]?key)\w*\b"],
                require_string_concat=True,
            ),
            "kotlin": LanguageRuleSpec(
                callee_patterns=[r"^(Log|R\.Log)\.(d|i|w|e|v)$", r"^println$", r"^System\.(out|err)\.print(ln)?$"],
                arg_patterns=[r"@[\w.+-]+\.\w+", r"\b(email|phone|mobile|token|password|secret|ssn|api[_-]?key)\w*\b"],
                require_string_concat=True,
            ),
            "swift": LanguageRuleSpec(
                callee_patterns=[r"^(print|NSLog|debugPrint)$"],
                arg_patterns=[r"@[\w.+-]+\.\w+", r"\b(email|phone|mobile|token|password|secret)\w*\b"],
                require_string_concat=True,
            ),
        }
    ),
]


def _touches_param(
    call: Any, resolved_callee: str, resolved_args: str, category: str
) -> bool:
    """命中的动态片段里是否引用了**函数形参**（「外部可控」的最小判据）。

    正则规则引擎没有真正的污点分析，「外部可控」只能用一个可判定的代理指标：
    被拼进去的那一段是否来自函数入参。局部计算结果与模块级常量不满足，因此不会把
    `Path(root) / hashed_name`、`f"SELECT {','.join(COLUMNS)} ..."` 这类内部构造
    当成注入。

    判据按漏洞类别取「可控片段」：
    - 路径穿越：``/`` 的**右操作数**。左操作数通常就是形参（``artifact_root / name``），
      把「形参出现在任意位置」当可控，会把内部拼接全部报出来。
    - 其它注入类：f-string 的 ``{...}`` 插值片段与 ``+`` 的右操作数。
    """
    params = getattr(call, "param_names", ()) or ()
    if not params:
        return False
    allowed = set(params)

    if category == "path-traversal":
        operands = re.findall(r"/\s+([A-Za-z_][\w.]*)", resolved_callee)
        if not operands:
            return False
        return any(
            operand.split(".")[0].split("[")[0] in allowed for operand in operands
        )

    fragments = re.findall(r"\{([^{}]*)\}", resolved_args)
    fragments += re.findall(r"\+\s*([A-Za-z_][\w.]*)", resolved_args)
    for fragment in fragments:
        for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", fragment):
            if name in allowed:
                return True
    return False


_ORIGIN_FIXED_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[A-Za-z0-9.\-]+")
_RELATIVE_URL_PREFIXES = ("/", "./", "../", "?", "#")


def _strip_quotes(text: str) -> str:
    """去掉包裹实参的引号/反引号与首尾空白，便于判定相对 URL。"""
    return text.strip().strip("\"'`").strip()


def _split_top_level(text: str, separators: str) -> list[str]:
    """按顶层分隔符切分表达式，忽略字符串字面量与括号内部的分隔符。"""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and char in separators:
            parts.append(text[start:index])
            start = index + 1
        index += 1
    parts.append(text[start:])
    return parts


def _first_positional_arg(text: str) -> str:
    """取第一个位置实参（到顶层逗号为止）。"""
    return _split_top_level(text, ",")[0].strip()


_BARE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sql_text_dynamic(raw_sql: str, resolved_sql: str) -> bool:
    """SQL 文本是否**在代码层面**被动态构造（f-string / 模板插值 / 顶层拼接）。

    只判第一个位置实参——``execute(sql, params)`` 里 bind 参数中的 f-string
    不构成注入，SQL 文本本身是参数化的。实测仓库自身因此产出 4 条 critical
    假阳性（``c.execute("... WHERE event_id=?", (f"...",))``）。

    原始实参是裸变量名时（``execute(sql)``）才采信折叠文本：真正的拼接发生在
    ``sql`` 的赋值行上。反之绝不采信——折叠器会在**字符串内容**里按变量名做
    替换，把静态三引号 DDL 改写成带插值的假 f-string。
    """
    if detect_string_concat(raw_sql):
        return True
    if _BARE_IDENTIFIER_RE.match(raw_sql.strip()):
        return detect_string_concat(resolved_sql)
    return False


# 折叠器把 `const cp = require("child_process"); cp.exec(cmd)` 的 receiver
# 还原成 `require("child_process").exec`。规则层的 callee 正则与 receiver
# 白名单此前只看原始 `base_obj='cp'`，认不出这个等价形态，导致别名调用漏报
# （而解构 `const {exec} = ...` 与全限定 `child_process.exec` 都命中）。
# 这里把「折叠后的模块调用」规范化回白名单声明的模块名，只认**双引号或单引号
# 包裹的精确模块名**，不做子串/模糊匹配，以免放行 `foobar.exec`。
_REQUIRE_MODULE_CALL_RE = re.compile(
    r'^require\(\s*["\'](?P<module>[A-Za-z0-9_@./\-]+)["\']\s*\)\.(?P<method>[A-Za-z_]\w*)$'
)


def _normalized_callee_base(call: CallNode) -> tuple[str, bool]:
    """返回 (用于白名单判定的基对象, 是否任意对象的成员调用)。

    对折叠器还原出的 ``require("mod").method`` 形态，提取 ``mod`` 作为
    基对象，并把 ``preceded_by_dot`` 归零——它是**模块命名空间调用**，
    与 ``re.exec`` / ``obj.exec`` 这类"任意对象上的同名方法"性质不同。
    其余调用沿用解析器原始的 ``base_obj`` / ``preceded_by_dot``。

    绝不能把还原形态标记为成员调用：那会让空串基对象（正则字面量
    ``/x/g.exec``、`re.exec`）借白名单里的 "" 混过——正是 RegExp.exec
    假阳性的根因。
    """
    resolved = call.resolved_callee or ""
    match = _REQUIRE_MODULE_CALL_RE.match(resolved)
    if match:
        return match.group("module"), False
    return call.base_obj, call.preceded_by_dot


def _callee_base_allowed(spec: LanguageRuleSpec, call: CallNode) -> bool:
    """callee 的 receiver 是否落在规则声明的白名单内。

    ``RegExp.prototype.exec`` 与 ``child_process.exec`` 在结构上只差 receiver：
    前者是 ``re.exec(s)`` / ``/x/g.exec(s)``（receiver 可以以 ``)``、``]`` 或
    正则字面量结尾，抽出来只剩裸方法名，``base_obj`` 为空），后者是裸
    ``exec(cmd)`` 或 ``child_process.exec(cmd)``。白名单里的空串专指**非成员
    调用**，所以 ``preceded_by_dot`` 为真的调用一律不算裸调用。

    ``require("child_process").exec`` 这类折叠后的模块调用先经
    ``_normalized_callee_base`` 还原为模块名再判定，别名赋值调用因此能命中，
    而 ``re.exec`` / ``obj.exec`` 仍因基对象不在白名单被拒。
    """
    allowed = spec.require_callee_base
    if allowed is None:
        return True
    base_obj, preceded_by_dot = _normalized_callee_base(call)
    if preceded_by_dot:
        # 任意对象上的同名方法（re.exec / obj.exec）不算裸调用。折叠还原的
        # require("mod").exec 已在 _normalized_callee_base 里归零 dotted，
        # 不会走到这里。
        return False
    return base_obj in allowed


def _unwrap_url_expression(text: str, depth: int = 0) -> str:
    """剥掉包在 URL 外面的构造器（``urllib.request.Request(...)`` / ``new Request(...)``）。

    解析器把 ``urlopen(req)`` 折叠成 ``Request(url, ...)`` 之后，真正的 URL 表达式在
    内层第一个实参上；不剥这一层就只能看到 ``Request(...)`` 这个壳，判不出 origin。
    """
    expression = _first_positional_arg(text)
    if depth >= 3 or not expression.endswith(")"):
        return expression
    match = re.match(r"^(?:new\s+)?[A-Za-z_][\w.]*\s*\(", expression)
    if not match:
        return expression
    inner = expression[match.end():].rstrip()[:-1]
    return _unwrap_url_expression(inner, depth + 1)


def _concat_chain(expression: str) -> list[str]:
    """顶层 ``+`` 链的操作数，从左到右。

    origin 由**最左操作数**决定：``base + "/api"`` 的 host 是 ``base``，
    ``BASE + "?" + q`` 的 host 是常量 ``BASE``。只看右操作数会把前者漏掉、把后者误报。
    """
    return [part.strip() for part in _split_top_level(expression, "+")]


_LITERAL_RE = re.compile(r"^[rbfuRBFU]*[\"'][^\"']*[\"']$")


def _is_literal(expression: str) -> bool:
    return bool(_LITERAL_RE.match(expression.strip()))


def _first_interpolation(expression: str, language: str) -> int | None:
    """第一个插值位置：JS 模板 ``${``，Python f-string ``{``。"""
    match = re.search(r"\$\{", expression)
    if match:
        return match.start()
    if language == "python" and re.search(r"[fF][\"']", expression):
        match = re.search(r"(?<![{\w])\{", expression)
        if match:
            return match.start()
    return None


def _ssrf_origin_dynamic(
    text: str, language: str, tainted: bool, param_names: tuple[str, ...] = ()
) -> bool:
    """URL 实参是否可能把**目标主机**交给外部输入。

    SSRF 的必要条件是 host 可控，而不是"实参里出现过拼接"。旧实现只看
    ``has_string_concat``，于是三类本该零告警的写法全被报出来：

    * 相对 URL（``fetch(`/api/x`)``）——origin 就是当前页面，打不到内网；
    * 常量 origin 只拼后缀（``BASE + "/x"``、``BASE + "?" + q``）——host 由常量决定；
    * origin 已写全（``f"https://api.x.com/{id}"``）——只有路径段可控。

    反过来，实参是**受污染**的裸标识符时无法证明 origin 固定，仍按动态处理——
    云端技能包下载就是这种形状（``url`` 由 ``base_url``/环境变量决定）。
    """
    if not text:
        return False
    expression = _unwrap_url_expression(text)
    if not expression:
        return False
    if _strip_quotes(expression).startswith(_RELATIVE_URL_PREFIXES):
        return False

    chain = _concat_chain(expression)
    if len(chain) > 1:
        base = chain[0]
        if _is_literal(base):
            # ``"https://" + host`` 只固定了协议，host 仍可控；写全 host 才算固定。
            return not _ORIGIN_FIXED_RE.match(_strip_quotes(base))
        # 基是标识符/调用：只有它能被外部输入决定时才可能换 host。
        if param_names and not tainted:
            return bool(set(re.findall(r"[A-Za-z_]\w*", base)) & set(param_names))
        return True

    position = _first_interpolation(expression, language)
    if position is None:
        # 没有可见的动态构造。两种情况下 origin 仍不可证固定：
        # 1. 实参受污染（云端技能包下载：``url`` 由 ``base_url``/环境变量决定）；
        # 2. 表达式**本身就是所在函数的形参**——``requests.get(url)`` 的 url
        #    完全由调用方决定，是最典型的 SSRF 形态，而形参不会被标记为污点，
        #    只按 ``tainted`` 判会把这条真阳性漏掉。
        # 字面量实参（``requests.get("https://api.x.com/x")``）与 JS 的
        # ``fetch(target.url)``（无 param_names）都不会落到这里为真。
        if tainted:
            return True
        return bool(param_names) and expression.strip() in param_names
    prefix = _strip_quotes(expression[:position])
    if _ORIGIN_FIXED_RE.match(prefix):
        return False
    if param_names and not tainted:
        fragment = expression[position:].split("}", 1)[0]
        return bool(set(re.findall(r"[A-Za-z_]\w*", fragment)) & set(param_names))
    return True



def _secret_value_looks_real(value: str) -> bool:
    """硬编码密钥字面量的高置信形态判据（SEC-SECRET-001 值过滤）。

    宁漏勿误：已知密钥前缀（AWS/GitHub/Slack/OpenAI/Google/bugx）直接
    放行；否则要求值含大写+小写+数字且不含 ``/``、空白、``_`` 之外的路径
    分隔符——URL 路径、小写配置词、模块路径因此不再误报，而随机混合串
    （靶场 ``Zq7Vm4Kd9Xw2Ps5Nb8Rt3Hf6L`` 类）继续命中。
    """
    known_prefixes = (
        "akia", "asia", "sk-", "sk_live_", "sk_test_", "ghp_", "gho_",
        "xoxb-", "xoxp-", "aiza", "bxk_", "-----begin",
    )
    lowered = value.lower()
    if lowered.startswith(known_prefixes):
        return True
    if any(ch in value for ch in ("/", " ")):
        return False
    has_upper = any(ch.isupper() for ch in value)
    has_lower = any(ch.islower() for ch in value)
    has_digit = any(ch.isdigit() for ch in value)
    return has_upper and has_lower and has_digit


def _call_context_window(
    parsed: "ParsedSource", call: Any, *, before: int = 8, after: int = 2,
) -> str:
    """取调用点邻近窗口的净代码文本（注释/字符串内容已剥离）。

    供 ``context_patterns`` 判定使用：行区间 [line-before, end_line+after]，
    越界自动夹取。净代码行保证注释里的文案提及（如规则说明文字）不会
    误触发上下文特征。
    """
    start = max(1, int(call.span.line) - before)
    end = min(len(parsed.lines), int(call.span.end_line) + after)
    if end < start:
        return ""
    # 用原始行而非净代码行：净代码行会把字符串内容替换成空格（实测
    # ``"/bin/sh"``/``"-c"`` 均被抹除，上下文特征因此不可见）。原始行
    # 剔除纯注释行（//、#、*、/*、--），规则文案提及不误触发。
    pieces: list[str] = []
    for raw in parsed.lines[start - 1:end]:
        stripped = raw.strip()
        if stripped.startswith(("//", "#", "*", "/*", "--")):
            continue
        pieces.append(raw)
    return "\n".join(pieces)


class SASTRuleMatcher:
    """基于声明式规则库对 ParsedSource 执行多语言模式匹配"""



    def __init__(self, rules: list[SASTRule] | None = None) -> None:
        self.rules = rules or BUILTIN_RULES
        # 逐文件解析失败的**结构化**跳过清单（F-207）。``_scan_python_specifics``
        # 在 ``ast.parse`` 失败时写入一条带 file/reason/行定位的记录；调用方
        # （``scan_multilang_sources`` → ``run_scan``）读取并对外暴露。
        # 没有这条记录时，「解析失败」与「文件里没缺陷」在证据上完全同形。
        self.parse_errors: list[dict[str, Any]] = []

    def match_file(self, parsed: ParsedSource) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        lang = parsed.language

        # 动态语言流分析器（主要用于强弱净化器消歧与框架 Source 判别）
        analyzer = DynamicFlowAnalyzer(language=lang)

        # 1. 结构化调用链与规则匹配 (直接复用 UniversalCodeMatcher 下沉内嵌的 CallNode 符号还原)
        for call in parsed.calls:
            resolved_callee = call.resolved_callee or call.callee
            resolved_args = call.resolved_args or call.args_raw
            is_tainted = call.is_tainted
            for rule in self.rules:
                spec = rule.languages.get(lang)
                if not spec:
                    # 尝试 generic 回退
                    spec = rule.languages.get("generic")
                if not spec:
                    continue

                # 检查 callee_patterns 命中 (支持原始与解析展开的 callee)。
                # 折叠器还原出的 `require("child_process").exec` 会被规范化成
                # `child_process.exec` 一并参与匹配——否则 `^(child_process\.)?exec$`
                # 这类别名调用会被锚定正则挡在门外（别名赋值调用的假阴性）。
                base_obj, _ = _normalized_callee_base(call)
                normalized_callee = (
                    f"{base_obj}.{call.method_name}" if base_obj else call.method_name
                )
                callee_matched = False
                for pat in spec.callee_patterns:
                    if (
                        re.search(pat, call.callee)
                        or re.search(pat, resolved_callee)
                        or re.search(pat, normalized_callee)
                        or re.search(pat, f"{call.callee}({call.args_raw})")
                        or re.search(pat, f"{resolved_callee}({resolved_args})")
                    ):
                        callee_matched = True
                        break
                if not callee_matched:
                    continue

                # callee 的 receiver 约束：把「裸函数调用」与「任意对象上的同名
                # 方法」区分开（JS 的 RegExp.exec vs child_process.exec）。
                if not _callee_base_allowed(spec, call):
                    continue

                # SQL 类规则的全部判据都只作用于第一个位置实参（SQL 文本）。
                # ``execute(sql, params)`` 的 bind 参数里出现 f-string / 形参
                # 都不是注入——判据作用在整段实参文本上会把参数化查询报成
                # critical SQL 注入（实测仓库自身 4 条）。
                if rule.category == "sql-injection":
                    analysis_raw = _first_positional_arg(call.args_raw or "")
                    analysis_resolved = _first_positional_arg(resolved_args or "")
                    concat = _sql_text_dynamic(analysis_raw, analysis_resolved)
                else:
                    analysis_raw = call.args_raw or ""
                    analysis_resolved = resolved_args
                    concat = call.has_string_concat

                # 检查行级特征匹配 (如弱类型比较运算符)
                if spec.line_patterns:
                    line_matched = any(re.search(pat, call.span.snippet) for pat in spec.line_patterns)
                    if not line_matched:
                        continue

                # 检查动态字符串拼接或受污染变量要求
                has_concat_or_taint = (
                    concat
                    or is_tainted
                    or analyzer.has_framework_source(
                        analyzer.payload_portion(analysis_resolved)
                    )
                    or analyzer.has_framework_source(
                        analyzer.payload_portion(analysis_raw)
                    )
                )
                if spec.require_string_concat and not has_concat_or_taint:
                    continue

                # SSRF 的必要条件是 **host 可控**，不是"实参里出现过拼接"。判据见
                # ``_ssrf_origin_dynamic``：相对 URL、只拼 query、origin 已写全都不算。
                #
                # 取哪份实参文本取决于解析器有没有真实作用域信息：Python 有 AST，
                # ``resolved_args`` 的变量折叠是精确的，能把 ``urlopen(req)`` 展开成
                # ``Request(url, ...)``；JS/TS 没有作用域，``resolved_args`` 会把整段
                # 变量初始化语句塞进来——压缩包一个 30KB 的 blob 里到处是 ``+``，
                # 拼接判据就彻底失效（实测仓库自身因此产出 13 条假 SSRF）。
                if spec.require_dynamic_origin:
                    url_text = resolved_args if call.param_names else (call.args_raw or "")
                    if not _ssrf_origin_dynamic(
                        url_text, lang, is_tainted, call.param_names or ()
                    ):
                        continue

                # 检查实参特征 (支持原始实参、解析展开后的实参与被调用者特征)
                #
                # arg_patterns 默认是**硬性判据**：它编码的是结构性必要条件
                # （``shell=True``、``"sh","-c"``、``"MD5"`` 字面量……），"实参
                # 被污点污染"不能替代它——否则 ``subprocess.run([git, "--version"])``
                # 这种列表形式、没开 shell 的调用会因为 git 是形参而被报成
                # critical 命令注入。只有声明了 ``arg_patterns_allow_taint`` 的
                # 规则（其 arg_patterns 只是「来源命名启发式」）才允许污点兜底。
                if spec.arg_patterns:
                    arg_matched = any(
                        re.search(pat, analysis_raw)
                        or re.search(pat, analysis_resolved)
                        or re.search(pat, call.callee)
                        or re.search(pat, resolved_callee)
                        for pat in spec.arg_patterns
                    )
                    if not arg_matched and spec.arg_patterns_allow_taint and is_tainted:
                        arg_matched = True
                    if not arg_matched:
                        continue

                # 调用点邻近窗口特征（组间 AND、组内 OR）：用于 shell 特征在
                # 属性赋值而非调用实参的形态（swift Process.run()）。窗口取
                # 净代码行（注释/字符串内容已剥离，文案提及不误报）。
                if spec.context_patterns:
                    window = _call_context_window(parsed, call, before=8, after=2)
                    if not all(
                        any(re.search(pat, window) for pat in group)
                        for group in spec.context_patterns
                    ):
                        continue

                # 「外部可控」的最小判据：命中的动态片段必须引用所在函数的形参。
                # 没有这一层，任何 `Path(x) / y` 后接文件读写、任何 f-string SQL
                # 都会被报——实测仓库自身就因此产出 8 条假路径穿越与 27 条假 SQLi。
                if spec.require_param_operand and not _touches_param(
                    call, resolved_callee, analysis_resolved, rule.category
                ):
                    continue

                # 检查规则自带的净化器阻断
                if spec.sanitizer_patterns:
                    is_sanitized = any(
                        re.search(pat, analysis_raw)
                        or re.search(pat, analysis_resolved)
                        or re.search(pat, call.span.snippet)
                        for pat in spec.sanitizer_patterns
                    )
                    if is_sanitized:
                        continue

                # 检查分类强类型安全防御净化器消歧
                if rule.category in STRONG_SANITIZERS:
                    if (
                        analyzer.is_strong_sanitized(analysis_raw, rule.category, lang)
                        or analyzer.is_strong_sanitized(analysis_resolved, rule.category, lang)
                        or analyzer.is_strong_sanitized(call.span.snippet, rule.category, lang)
                    ):
                        continue

                # 检查行级抑制注释
                if UniversalCodeMatcher.is_line_suppressed(parsed, call.span.line, rule.id):
                    continue

                findings.append({
                    "rule_id": rule.id,
                    "name": rule.name,
                    "category": rule.category,
                    "severity": rule.severity,
                    "confidence": rule.confidence,
                    "cwe": rule.cwe,
                    "owasp": rule.owasp,
                    "message": rule.message,
                    "file": parsed.rel_path,
                    "line": call.span.line,
                    "col": call.span.col,
                    "evidence": call.span.snippet or call.span.text,
                    "remediation": rule.remediation,
                    "language": lang,
                })

        # 2. 补充行级静态字面量扫描（如硬编码敏感凭据）
        secret_findings = self._scan_hardcoded_secrets(parsed)
        findings.extend(secret_findings)

        # 3. 针对动态语言专有语法结构补充扫描
        if lang in {"javascript", "typescript"}:
            findings.extend(self._scan_js_specifics(parsed))
        elif lang == "php":
            findings.extend(self._scan_php_specifics(parsed))
        elif lang == "ruby":
            findings.extend(self._scan_ruby_specifics(parsed))
        elif lang == "python":
            python_findings, parse_failure = self._scan_python_specifics(parsed)
            findings.extend(python_findings)
            if parse_failure is not None:
                self.parse_errors.append(parse_failure)

        # 去重并返回
        seen = set()
        deduped = []
        for f in findings:
            key = (f["rule_id"], f["file"], f["line"])
            if key not in seen:
                seen.add(key)
                deduped.append(f)
        return deduped

    def _scan_hardcoded_secrets(self, parsed: ParsedSource) -> list[dict[str, Any]]:
        findings = []
        # 标识符关键字放宽（F-RSI-20261118-003）：原正则以 ``\b`` 锚定裸名
        # （``api_key =``/``token =``），靶场 ``apiKeyValue``/``sessionTokenValue``
        # 这类驼峰复合名因无词边界而整体漏检（A-KEY/I-TOKEN 零命中实证）。
        # 现语义 = 标识符**包含**敏感关键字（前后不要求词边界）+ 12+ 字符
        # 字符串字面量赋值；短值与占位符仍被下方过滤排除。
        secret_kw = re.compile(r'(api[_-]?key|secret|password|passwd|private[_-]?key|token)\w*\s*[:=]\s*["\']([^"\']{12,})["\']', re.IGNORECASE)

        for line_no, line in enumerate(parsed.lines, 1):
            if UniversalCodeMatcher.is_line_suppressed(parsed, line_no, "SEC-SECRET-001"):
                continue
            m = secret_kw.search(line)
            if m:
                val = m.group(2)
                # 匹配必须落在代码区：净代码行在同位置已被抹成空白说明
                # 赋值发生在字符串/注释内容里（文档性提及 ``token='x'``、
                # ``len("耗时/Token:")`` 切片文案），不是硬编码凭据。
                clean = (
                    parsed.clean_lines[line_no - 1]
                    if 0 <= line_no - 1 < len(parsed.clean_lines) else ""
                )
                if clean and m.start(1) < len(clean) and clean[m.start(1)].isspace():
                    continue
                # 排除占位符
                if val.lower() in {"your_token_here", "placeholder", "changeme", "your_password", "env_variable"}:
                    continue
                # 排除模板占位（${VAR}/{{VAR}}/<VAR>）——不是字面量密钥。
                if val.startswith(("${", "{{")) or re.fullmatch(r"<[^>]*>", val):
                    continue
                # 排除全大写常量子符串（BUGX_HEADLESS_OK 类哨兵/标志值，
                # 真实密钥几乎总是混合大小写或含符号）。
                if re.fullmatch(r"[A-Z][A-Z0-9_]+", val):
                    continue
                # 值形态必须「像密钥」（宁漏勿误，仓库自身 17→7→5 收敛实证）：
                # 已知密钥前缀直接放行；否则要求大小写混合+数字且无路径/空白
                # 分隔符——``DEVICE_TOKEN_PATH = "/api/..."``（URL 路径）与
                # ``token_method = "worker_reported"``（小写配置值）不再报。
                if not _secret_value_looks_real(val):
                    continue
                findings.append({
                    "rule_id": "SEC-SECRET-001",
                    "name": "Hardcoded Secret or Access Token",
                    "category": "secrets",
                    "severity": "high",
                    "confidence": "medium",
                    "cwe": "CWE-798",
                    "owasp": "A07:2021-Identification-and-Authentication-Failures",
                    "message": f"在源码中疑似硬编码静态机密凭证 ({m.group(1)})",
                    "file": parsed.rel_path,
                    "line": line_no,
                    "col": m.start(),
                    "evidence": line.strip(),
                    "remediation": "将静态凭据移出代码，改用操作系统环境变量或受信任的 Secrets Manager 动态加载。",
                    "language": parsed.language,
                })
        return findings

    def _scan_python_specifics(
        self, parsed: ParsedSource,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Python 专有 AST 规则。

        返回 ``(findings, parse_failure)``：解析失败时 findings 为空，同时返回
        **结构化降级对象**（``skipped=True`` + ``reason`` + file/行定位），由
        ``match_file`` 记入 ``self.parse_errors``。原实现直接 ``return []``，
        让「解析失败」与「文件无缺陷」在调用方看来完全一样（F-207）。
        """
        findings: list[dict[str, Any]] = []
        try:
            tree = ast.parse(parsed.raw_text, filename=str(parsed.filepath))
        except (SyntaxError, ValueError) as exc:
            return [], self._parse_failure_record(parsed, exc)

        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    is_broad = node.type is None
                    if isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}:
                        is_broad = True
                    if is_broad:
                        if not UniversalCodeMatcher.is_line_suppressed(parsed, node.lineno, "SEC-ERR-001"):
                            findings.append({
                                "rule_id": "SEC-ERR-001",
                                "name": "Silent Generic Exception Swallowing",
                                "category": "error-handling",
                                "severity": "low",
                                "confidence": "high",
                                "cwe": "CWE-391",
                                "owasp": "A09:2021-Security-Logging-and-Monitoring-Failures",
                                "message": "静默捕获并吞没通用异常（except: pass），掩盖运行时系统故障",
                                "file": parsed.rel_path,
                                "line": node.lineno,
                                "col": node.col_offset,
                                "evidence": parsed.lines[node.lineno - 1].strip() if 1 <= node.lineno <= len(parsed.lines) else "",
                                "remediation": "指定具体的异常捕获类型，并在 except 块中输出诊断日志或妥善恢复。",
                                "language": "python",
                            })
        return findings, None

    @staticmethod
    def _parse_failure_record(parsed: ParsedSource, exc: Exception) -> dict[str, Any]:
        """把 ``ast.parse`` 的失败折成可区分、可定位的结构化跳过记录。

        ``SyntaxError`` 自带 lineno/offset/msg，直接透出；其余（如 NUL 字节触发的
        ``ValueError``）没有行定位，仍保留 file 与 reason，不用空值伪装成「扫过」。
        """
        if isinstance(exc, SyntaxError):
            reason = "syntax_error"
            line = int(exc.lineno or 0)
            col = int(exc.offset or 0)
            detail = exc.msg or ""
        else:
            reason = "parse_error"
            line = 0
            col = 0
            detail = str(exc)
        return {
            "skipped": True,
            "reason": reason,
            "stage": "python_ast_rules",
            "file": parsed.rel_path,
            "line": line,
            "col": col,
            "detail": detail,
        }

    def _scan_js_specifics(self, parsed: ParsedSource) -> list[dict[str, Any]]:
        findings = []
        proto_re = re.compile(r'(\[[\s*"\'](__proto__|constructor|prototype)[\s*"\']\]|__proto__)\s*=', re.IGNORECASE)
        for line_no, line in enumerate(parsed.lines, 1):
            if proto_re.search(line) and not UniversalCodeMatcher.is_line_suppressed(parsed, line_no, "SEC-PROTO-001"):
                findings.append({
                    "rule_id": "SEC-PROTO-001",
                    "name": "Prototype Pollution",
                    "category": "prototype-pollution",
                    "severity": "high",
                    "confidence": "high",
                    "cwe": "CWE-1321",
                    "owasp": "A03:2021-Injection",
                    "message": "代码中存在直接修改 __proto__ 或 constructor 原型对象的赋值操作",
                    "file": parsed.rel_path,
                    "line": line_no,
                    "col": 0,
                    "evidence": line.strip(),
                    "remediation": "在对象深拷贝或合并前过滤 __proto__、constructor 等危险属性，或使用 Object.create(null) 创建空原型字典。",
                    "language": parsed.language,
                })
        return findings

    def _scan_php_specifics(self, parsed: ParsedSource) -> list[dict[str, Any]]:
        findings = []
        include_re = re.compile(r'^\s*(include|include_once|require|require_once)\s+([^;]+);')
        hash_cmp_re = re.compile(r'\b(md5|sha1|hash)\s*\(.+?\)\s*(==|!=)\s*(["\']0e\d+["\']|\$[A-Za-z0-9_]+)', re.IGNORECASE)

        for line_no, line in enumerate(parsed.lines, 1):
            clean_line = parsed.clean_lines[line_no - 1] if line_no <= len(parsed.clean_lines) else line
            # 1. 动态文件包含语句（无括号形式）
            m_inc = include_re.search(clean_line)
            if m_inc:
                target_expr = m_inc.group(2).strip()
                if ("$" in target_expr or "." in target_expr or "phar://" in target_expr) and not UniversalCodeMatcher.is_line_suppressed(parsed, line_no, "SEC-PHP-FILE-001"):
                    if not any(san in target_expr for san in ["basename(", "in_array("]):
                        findings.append({
                            "rule_id": "SEC-PHP-FILE-001",
                            "name": "PHP Dynamic File Inclusion / Stream Injection",
                            "category": "file-inclusion",
                            "severity": "critical",
                            "confidence": "high",
                            "cwe": "CWE-98",
                            "owasp": "A03:2021-Injection",
                            "message": f"PHP 动态文件包含使用未严格校验的表达式 ({m_inc.group(1)} {target_expr})",
                            "file": parsed.rel_path,
                            "line": line_no,
                            "col": m_inc.start(),
                            "evidence": line.strip(),
                            "remediation": "禁止使用外部输入拼接 include / require 语句；使用严格白名单映射文件，并禁用 allow_url_include。",
                            "language": "php",
                        })

            # 2. 魔法哈希弱类型比较
            m_hash = hash_cmp_re.search(clean_line)
            if m_hash and not UniversalCodeMatcher.is_line_suppressed(parsed, line_no, "SEC-PHPHASH-001"):
                if "hash_equals" not in clean_line and "password_verify" not in clean_line:
                    findings.append({
                        "rule_id": "SEC-PHPHASH-001",
                        "name": "PHP Loose Comparison Magic Hash Vulnerability",
                        "category": "type-juggling",
                        "severity": "high",
                        "confidence": "high",
                        "cwe": "CWE-208",
                        "owasp": "A07:2021-Identification-and-Authentication-Failures",
                        "message": f"使用弱类型相等比较 ({m_hash.group(2)}) 比对哈希值，存在 PHP 魔法哈希类型混淆绕过风险",
                        "file": parsed.rel_path,
                        "line": line_no,
                        "col": m_hash.start(),
                        "evidence": line.strip(),
                        "remediation": "改用严格相等运算符 (=== 或 !==)，或使用 hash_equals() / password_verify() 进行常数时间安全比对。",
                        "language": "php",
                    })
        return findings

    def _scan_ruby_specifics(self, parsed: ParsedSource) -> list[dict[str, Any]]:
        findings = []
        dyncall_re = re.compile(r'\b(send|public_send|__send__)\s*\(?\s*(?:params|request|params\[:[A-Za-z0-9_]+\])', re.IGNORECASE)
        for line_no, line in enumerate(parsed.lines, 1):
            if dyncall_re.search(line) and not UniversalCodeMatcher.is_line_suppressed(parsed, line_no, "SEC-DYNCALL-001"):
                if "include?" not in line:
                    findings.append({
                        "rule_id": "SEC-DYNCALL-001",
                        "name": "Ruby Unsafe Dynamic Method Dispatch",
                        "category": "code-injection",
                        "severity": "high",
                        "confidence": "high",
                        "cwe": "CWE-470",
                        "owasp": "A03:2021-Injection",
                        "message": "将外部未校验的参数直接传入动态方法分发调用 (send / public_send)",
                        "file": parsed.rel_path,
                        "line": line_no,
                        "col": 0,
                        "evidence": line.strip(),
                        "remediation": "在调用 send / public_send 前，使用方法名白名单数组校验入参（例如 ALLOWED_ACTIONS.include?(action)）。",
                        "language": "ruby",
                    })
        return findings


def is_available(matcher: "SASTRuleMatcher | None" = None) -> tuple[bool, str]:
    """降级契约：机械探活规则库完整性（纯内存检查，零 I/O、零子进程）。

    规则库为空或存在缺语言规格的规则时返回 False；调用方必须走
    ``run_static_degraded`` 显式标记降级匹配，禁止把空结果伪装成「未发现漏洞」。
    """
    rules = (matcher.rules if matcher is not None else BUILTIN_RULES) or []
    if not rules:
        return False, "SAST 规则库为空；模式匹配能力不可用"
    broken = [rule.id for rule in rules if not rule.languages]
    if broken:
        return False, f"规则缺少语言规格（含 generic 回退也没有）: {', '.join(sorted(broken)[:8])}"
    return True, f"{len(rules)} 条内置规则就绪"


def run_static_degraded(parsed, *, matcher: "SASTRuleMatcher | None" = None) -> dict:
    """降级契约：目标语言无专属规则时回退 generic 规则，输出可区分的降级结果。

    输出与成功路径（``match_file`` 返回的裸 findings 列表）字段级可区分：
    本契约返回结构化 dict，``status="degraded"`` + ``degraded=True`` +
    ``degraded_reason``，并声明该语言有多少条规则是靠 generic 规格兜底匹配的。
    """
    active = matcher if matcher is not None else SASTRuleMatcher()
    language = getattr(parsed, "language", "") or ""
    native_rules = sum(1 for rule in active.rules if language in rule.languages)
    generic_only = native_rules == 0
    findings = active.match_file(parsed)
    return {
        "schema_version": 1,
        "success": True,
        "status": "degraded",
        "degraded": True,
        "degraded_reason": (
            "language_native_spec_missing" if generic_only
            else "static_rule_match_only"
        ),
        "probe_mode": "static_degraded",
        "language": language,
        "rules_with_native_spec": native_rules,
        "rules_total": len(active.rules),
        "total_findings": len(findings),
        "findings": findings,
        "evidence": {
            "generic_fallback_used": generic_only,
            "availability_detail": "规则库带 generic 回退" if generic_only else "目标语言有专属规则规格",
        },
        "metadata": {
            "degraded_mode": "declarative_rule_match_static",
            "fallback": "目标语言缺专属规则时只报 generic 覆盖的置信度较低模式；禁止把降级覆盖伪装成全量规则扫描",
        },
    }
