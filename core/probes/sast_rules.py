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

from core.retrieval.universal_code_matcher import CallNode, ParsedSource, UniversalCodeMatcher

from core.planning.dynamic_flow_analyzer import DynamicFlowAnalyzer, STRONG_SANITIZERS


@dataclass
class LanguageRuleSpec:
    """针对特定语言的规则特征匹配契约"""
    callee_patterns: list[str] = field(default_factory=list)      # 危险函数调用正则列表
    require_string_concat: bool = False                           # 是否必须伴随实参动态字符串拼接
    arg_patterns: list[str] = field(default_factory=list)         # 实参中必须命中的特征正则
    sanitizer_patterns: list[str] = field(default_factory=list)   # 净化器过滤正则（命中则抵消告警）
    line_patterns: list[str] = field(default_factory=list)        # 行级特征正则（如弱类型比较运算符）


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
                sanitizer_patterns=[r"execFile"],
            ),
            "typescript": LanguageRuleSpec(
                callee_patterns=[r"^(child_process\.)?(exec|execSync)$"],
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
                arg_patterns=[r"/bin/(sh|bash)\b", r"\b(input|request|query|command)\b"],
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
                callee_patterns=[r"^(cursor|db|conn|session)\.(execute|raw|select|query)$"],
                require_string_concat=True,
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
            ),
            "typescript": LanguageRuleSpec(
                callee_patterns=[r"^(db|pool|client|connection)\.(query|execute)$"],
                require_string_concat=True,
                arg_patterns=[r'(`[^`]*\${|\+\s*[A-Za-z_])'],
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
                callee_patterns=[r"^(Statement\.(executeQuery|executeUpdate|execute)|JdbcTemplate\.(query|update)|SQLiteDatabase\.(rawQuery|execSQL))$"],
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
                callee_patterns=[r"^(requests\.(get|post|put|delete)|urllib\.request\.urlopen|httpx\.(get|post))$"],
                require_string_concat=True,
            ),
            "go": LanguageRuleSpec(
                callee_patterns=[r"^(http\.(Get|Post|Head)|http\.NewRequest)$"],
                require_string_concat=True,
            ),
            "java": LanguageRuleSpec(
                callee_patterns=[r"^(new URL|HttpClient\.send|HttpClients\.createDefault)$"],
                require_string_concat=True,
            ),
            "javascript": LanguageRuleSpec(
                callee_patterns=[r"^(axios\.(get|post)|fetch|http\.(get|request)|https\.(get|request))$"],
                require_string_concat=True,
            ),
            "typescript": LanguageRuleSpec(
                callee_patterns=[r"^(axios\.(get|post)|fetch|http\.(get|request)|https\.(get|request))$"],
                require_string_concat=True,
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
]


class SASTRuleMatcher:
    """基于声明式规则库对 ParsedSource 执行多语言模式匹配"""

    def __init__(self, rules: list[SASTRule] | None = None) -> None:
        self.rules = rules or BUILTIN_RULES

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

                # 检查 callee_patterns 命中 (支持原始与解析展开的 callee)
                callee_matched = False
                for pat in spec.callee_patterns:
                    if (
                        re.search(pat, call.callee)
                        or re.search(pat, resolved_callee)
                        or re.search(pat, f"{call.callee}({call.args_raw})")
                        or re.search(pat, f"{resolved_callee}({resolved_args})")
                    ):
                        callee_matched = True
                        break
                if not callee_matched:
                    continue

                # 检查行级特征匹配 (如弱类型比较运算符)
                if spec.line_patterns:
                    line_matched = any(re.search(pat, call.span.snippet) for pat in spec.line_patterns)
                    if not line_matched:
                        continue

                # 检查动态字符串拼接或受污染变量要求
                has_concat_or_taint = (
                    call.has_string_concat
                    or is_tainted
                    or analyzer.has_framework_source(resolved_args)
                    or analyzer.has_framework_source(call.args_raw)
                )
                if spec.require_string_concat and not has_concat_or_taint:
                    continue

                # 检查实参特征 (支持原始实参、解析展开后的实参与被调用者特征)
                if spec.arg_patterns:
                    arg_matched = any(
                        re.search(pat, call.args_raw)
                        or re.search(pat, resolved_args)
                        or re.search(pat, call.callee)
                        or re.search(pat, resolved_callee)
                        for pat in spec.arg_patterns
                    )
                    if not arg_matched and is_tainted:
                        arg_matched = True
                    if not arg_matched:
                        continue

                # 检查规则自带的净化器阻断
                if spec.sanitizer_patterns:
                    is_sanitized = any(
                        re.search(pat, call.args_raw)
                        or re.search(pat, resolved_args)
                        or re.search(pat, call.span.snippet)
                        for pat in spec.sanitizer_patterns
                    )
                    if is_sanitized:
                        continue

                # 检查分类强类型安全防御净化器消歧
                if rule.category in STRONG_SANITIZERS:
                    if (
                        analyzer.is_strong_sanitized(call.args_raw, rule.category, lang)
                        or analyzer.is_strong_sanitized(resolved_args, rule.category, lang)
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
            findings.extend(self._scan_python_specifics(parsed))

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
        secret_kw = re.compile(r'\b(api[_-]?key|secret|password|passwd|private_key|token|access_token)\b\s*[:=]\s*["\']([^"\']{12,})["\']', re.IGNORECASE)

        for line_no, line in enumerate(parsed.lines, 1):
            if UniversalCodeMatcher.is_line_suppressed(parsed, line_no, "SEC-SECRET-001"):
                continue
            m = secret_kw.search(line)
            if m:
                val = m.group(2)
                # 排除占位符
                if val.lower() in {"your_token_here", "placeholder", "changeme", "your_password", "env_variable"}:
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

    def _scan_python_specifics(self, parsed: ParsedSource) -> list[dict[str, Any]]:
        findings = []
        try:
            tree = ast.parse(parsed.raw_text, filename=str(parsed.filepath))
        except Exception:
            return []

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
        return findings

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
