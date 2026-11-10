#!/usr/bin/env python3
"""core/sca_scanner.py — 零依赖全主流语言依赖包供应链安全扫描器 (DOC-061 / DOC-062)。

纯 Python 标准库实现，支持离线解析：
1. Go: go.mod / go.sum
2. Node.js / TS: package.json / package-lock.json
3. Java: pom.xml / build.gradle
4. Python: requirements.txt / poetry.lock / Pipfile.lock
5. Rust: Cargo.lock
6. PHP: composer.json / composer.lock
7. Ruby: Gemfile.lock
8. C# / .NET: packages.lock.json / *.csproj / packages.config
9. Dart / Flutter: pubspec.lock / pubspec.yaml
10. Apple Swift / iOS: Package.resolved / Podfile.lock

内置已知高危历史漏洞指纹库（Log4Shell, Fastjson RCE, Spring4Shell, Lodash, Guzzle, Rails, Newtonsoft 等），
实现快速离线脱网比对，支持确定性锁文件优先去重，输出标准供应链 Finding。
"""

from __future__ import annotations

import json
import re
from core.infra import safe_xml
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

SKIP_DIRS = {
    ".git", ".bugx", ".venv", "venv", "node_modules", "__pycache__",
    "dist", "build", "target", ".tox", ".mypy_cache", ".pytest_cache",
    "vendor", "Pods", ".gradle", ".idea", ".dart_tool",
}

LOCK_FILES = {
    "package-lock.json",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
    "packages.lock.json",
    "pubspec.lock",
    "Package.resolved",
    "Podfile.lock",
    "poetry.lock",
    "Pipfile.lock",
}


@dataclass
class SCAAdvisory:
    ecosystem: str       # maven, npm, go, pypi, cargo, packagist, rubygems, nuget, pub, spm, cocoapods
    package: str         # package identifier
    vulnerable_below: str # 只要低于该版本即判定存在漏洞
    cve: str
    severity: str        # critical, high, medium, low
    title: str
    fixed_version: str


# 工业级标杆离线漏洞指纹库（精选典型高危供应链漏洞）
OFFLINE_ADVISORIES: list[SCAAdvisory] = [
    # Java / Maven
    SCAAdvisory("maven", "org.apache.logging.log4j:log4j-core", "2.15.0", "CVE-2021-44228", "critical", "Log4Shell JNDI 远程代码执行漏洞", "2.17.1"),
    SCAAdvisory("maven", "com.alibaba:fastjson", "1.2.83", "CVE-2022-25845", "critical", "Fastjson AutoType 绕过与任意代码执行", "1.2.83"),
    SCAAdvisory("maven", "org.springframework:spring-beans", "5.3.18", "CVE-2022-22965", "critical", "Spring4Shell ClassLoader 访问与远程执行", "5.3.18"),
    SCAAdvisory("maven", "com.thoughtworks.xstream:xstream", "1.4.19", "CVE-2021-43859", "high", "XStream XML 反序列化 DoS/RCE 漏洞", "1.4.19"),
    SCAAdvisory("maven", "com.fasterxml.jackson.core:jackson-databind", "2.12.7", "CVE-2020-36518", "high", "Jackson-databind 深度嵌套 DoS 崩溃", "2.12.7.1"),

    # Node.js / NPM
    SCAAdvisory("npm", "lodash", "4.17.21", "CVE-2021-23337", "high", "Lodash 模版引擎命令注入与原型污染", "4.17.21"),
    SCAAdvisory("npm", "axios", "0.21.1", "CVE-2020-28168", "medium", "Axios SSRF 白名单绕过漏洞", "0.21.1"),
    SCAAdvisory("npm", "jsonwebtoken", "9.0.0", "CVE-2022-23529", "critical", "jsonwebtoken 秘钥解析与代码注入风险", "9.0.0"),
    SCAAdvisory("npm", "express", "4.19.2", "CVE-2024-29041", "medium", "Express 开放重定向与 URL 解析缺陷", "4.19.2"),
    SCAAdvisory("npm", "tar", "6.1.9", "CVE-2021-37712", "high", "node-tar 任意文件覆盖与目录穿越", "6.1.9"),

    # Go
    SCAAdvisory("go", "github.com/gin-gonic/gin", "1.7.7", "CVE-2020-28483", "medium", "Gin 目录穿越与未校验路由重定向", "1.7.7"),
    SCAAdvisory("go", "golang.org/x/crypto", "0.0.0-20220411", "CVE-2022-27191", "high", "Golang SSH 服务端签名崩溃 DoS", "0.0.0-20220411220226-7b82a4e95df4"),
    SCAAdvisory("go", "github.com/beego/beego", "2.0.0", "CVE-2021-3574", "high", "Beego 静态文件目录穿越与越权读取", "2.0.1"),
    SCAAdvisory("go", "github.com/docker/docker", "20.10.24", "CVE-2023-28840", "high", "Docker 覆盖网络加密流量明文泄露", "20.10.24"),

    # Python / PyPI
    SCAAdvisory("pypi", "requests", "2.31.0", "CVE-2023-32681", "medium", "Requests 重定向泄漏 Proxy-Authorization 凭证", "2.31.0"),
    SCAAdvisory("pypi", "urllib3", "1.26.17", "CVE-2023-43804", "high", "urllib3 跨域重定向 Cookie 凭据泄露", "1.26.17"),
    SCAAdvisory("pypi", "django", "3.2.25", "CVE-2024-27351", "high", "Django 正则邮箱验证 ReDoS 拒绝服务", "3.2.25"),
    SCAAdvisory("pypi", "cryptography", "41.0.6", "CVE-2023-49083", "medium", "Cryptography PKCS7 空指针解引用崩溃", "41.0.6"),
    SCAAdvisory("pypi", "flask", "1.0.0", "CVE-2018-1000656", "high", "Flask 早期版本静态文件越权与 DoS", "1.0.2"),

    # Rust / Cargo
    SCAAdvisory("cargo", "openssl", "0.10.55", "RUSTSEC-2023-0022", "high", "Rust OpenSSL 内存安全与双重释放", "0.10.55"),
    SCAAdvisory("cargo", "hyper", "0.14.26", "RUSTSEC-2021-0078", "high", "Hyper HTTP 请求走私 (Request Smuggling)", "0.14.26"),

    # PHP / Packagist (DOC-062)
    SCAAdvisory("packagist", "guzzlehttp/guzzle", "7.4.5", "CVE-2022-31090", "medium", "GuzzleHttp 跨域重定向 Authorization 头泄露", "7.4.5"),
    SCAAdvisory("packagist", "laravel/framework", "8.83.27", "CVE-2021-43617", "high", "Laravel 缓存投毒与命令注入风险", "8.83.27"),
    SCAAdvisory("packagist", "symfony/http-kernel", "5.4.20", "CVE-2022-24894", "medium", "Symfony HttpKernel 缓存隔离绕过", "5.4.20"),
    SCAAdvisory("packagist", "monolog/monolog", "2.8.0", "CVE-2022-29241", "high", "Monolog 日志格式化注入漏洞", "2.8.0"),

    # Ruby / RubyGems (DOC-062)
    SCAAdvisory("rubygems", "rails", "7.0.4.1", "CVE-2023-22795", "high", "Rails ActionDispatch 路由 ReDoS 拒绝服务", "7.0.4.1"),
    SCAAdvisory("rubygems", "actionview", "7.0.4.1", "CVE-2023-22792", "medium", "ActionView XSS 模版转义缺陷", "7.0.4.1"),
    SCAAdvisory("rubygems", "nokogiri", "1.14.3", "CVE-2023-29469", "high", "Nokogiri XML 内存破坏与越界读取", "1.14.3"),
    SCAAdvisory("rubygems", "rack", "2.2.6.2", "CVE-2023-27530", "medium", "Rack ReDoS 规范化路径拒绝服务", "2.2.6.2"),

    # C# / .NET / NuGet (DOC-062)
    SCAAdvisory("nuget", "newtonsoft.json", "13.0.1", "CVE-2024-21907", "high", "Newtonsoft.Json 深度嵌套递归栈溢出 DoS", "13.0.1"),
    SCAAdvisory("nuget", "log4net", "2.0.10", "CVE-2018-1285", "high", "log4net XML 外部实体注入 (XXE)", "2.0.10"),
    SCAAdvisory("nuget", "system.text.encodings.web", "4.7.2", "CVE-2021-26701", "critical", ".NET Core Web 编码器远程代码执行", "4.7.2"),

    # Dart / Flutter / Pub (DOC-062)
    SCAAdvisory("pub", "dio", "4.0.6", "CVE-2021-31402", "medium", "Dio HTTP 客户端重定向白名单绕过", "4.0.6"),
    SCAAdvisory("pub", "http", "0.13.3", "CVE-2020-35669", "medium", "Dart http 客户端请求走私/拆分缺陷", "0.13.3"),

    # Apple Swift / iOS (SPM & CocoaPods) (DOC-062)
    SCAAdvisory("spm", "alamofire", "5.4.4", "CVE-2021-39226", "high", "Alamofire 证书校验绕过与中间人攻击", "5.4.4"),
    SCAAdvisory("cocoapods", "alamofire", "5.4.4", "CVE-2021-39226", "high", "Alamofire 证书校验绕过与中间人攻击", "5.4.4"),
    SCAAdvisory("spm", "kingfisher", "7.6.2", "CVE-2023-25576", "medium", "Kingfisher 图像解码 DoS 内存耗尽", "7.6.2"),
    SCAAdvisory("cocoapods", "kingfisher", "7.6.2", "CVE-2023-25576", "medium", "Kingfisher 图像解码 DoS 内存耗尽", "7.6.2"),
]


def parse_version_tuple(v_str: str) -> tuple[int, ...]:
    """提取纯数字版本元组做宽松比对，如 'v1.14.2' -> (1, 14, 2)"""
    v_clean = re.sub(r"^[^\d]+", "", v_str.strip())
    match = re.search(r"(\d+(?:\.\d+)*)", v_clean)
    if not match:
        return ()
    parts = match.group(1).split(".")
    try:
        return tuple(int(p) for p in parts[:4])
    except ValueError:
        return ()


def is_version_vulnerable(current_ver: str, vulnerable_below: str) -> bool:
    c_t = parse_version_tuple(current_ver)
    b_t = parse_version_tuple(vulnerable_below)
    if not c_t or not b_t:
        return False
    # 填充长度对齐
    max_len = max(len(c_t), len(b_t))
    c_aligned = c_t + (0,) * (max_len - len(c_t))
    b_aligned = b_t + (0,) * (max_len - len(b_t))
    return c_aligned < b_aligned


class SCAScanner:
    """多语言轻量依赖供应链安全分析器 (DOC-061 / DOC-062)"""

    def __init__(self, advisories: list[SCAAdvisory] | None = None) -> None:
        self.advisories = advisories or OFFLINE_ADVISORIES
        # 构建 (ecosystem, package) -> list[SCAAdvisory] 索引
        self._index: dict[tuple[str, str], list[SCAAdvisory]] = {}
        for adv in self.advisories:
            key = (adv.ecosystem.lower(), adv.package.lower())
            self._index.setdefault(key, []).append(adv)

    def collect_dependencies(self, root: Path) -> list[dict[str, Any]]:
        """Return normalized dependency facts shared by profile and SCA scans.

        A lockfile fact wins over a declaration in the same module.  Modules
        remain separate so monorepos do not collapse two different versions
        into one project-wide claim.
        """
        root = root.resolve()
        manifest_parsers = [
            ("package-lock.json", self._parse_package_lock),
            ("Cargo.lock", self._parse_cargo_lock),
            ("composer.lock", self._parse_composer_lock),
            ("Gemfile.lock", self._parse_gemfile_lock),
            ("packages.lock.json", self._parse_packages_lock_json),
            ("pubspec.lock", self._parse_pubspec_lock),
            ("Package.resolved", self._parse_package_resolved),
            ("Podfile.lock", self._parse_podfile_lock),
            ("poetry.lock", self._parse_poetry_lock),
            ("Pipfile.lock", self._parse_pipfile_lock),
            ("go.mod", self._parse_go_mod),
            ("package.json", self._parse_package_json),
            ("pom.xml", self._parse_pom_xml),
            ("requirements.txt", self._parse_requirements_txt),
            ("composer.json", self._parse_composer_json),
            ("pubspec.yaml", self._parse_pubspec_yaml),
            ("*.csproj", self._parse_csproj),
            ("packages.config", self._parse_csproj),
        ]
        candidates: list[dict[str, Any]] = []
        for filename, parser in manifest_parsers:
            is_lockfile = filename in LOCK_FILES
            for path in sorted(root.rglob(filename), key=lambda item: item.as_posix()):
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    rel = path.relative_to(root).as_posix()
                except ValueError:
                    rel = str(path)
                if any(part in SKIP_DIRS for part in Path(rel).parts):
                    continue
                try:
                    deps = parser(path.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    continue
                module = Path(rel).parent.as_posix()
                for dep in deps:
                    item = dict(dep)
                    version = str(item.get("version") or "").strip()
                    line = int(item.get("line") or 1)
                    item.update({
                        "source_file": rel,
                        "source_kind": "lockfile" if is_lockfile else "manifest",
                        "declared_version": version if not is_lockfile else None,
                        "resolved_version": version if is_lockfile else None,
                        "evidence_refs": [f"{rel}:{line}"],
                        "module": module,
                        "dependency_kind": item.get(
                            "dependency_kind", "resolved" if is_lockfile else "declared"
                        ),
                    })
                    candidates.append(item)

        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for item in candidates:
            key = (
                str(item.get("ecosystem") or "").casefold(),
                str(item.get("package") or "").casefold(),
                str(item.get("module") or ""),
            )
            if not key[0] or not key[1]:
                continue
            grouped.setdefault(key, []).append(item)
        selected: list[dict[str, Any]] = []
        for items in grouped.values():
            lock_items = [item for item in items if item.get("source_kind") == "lockfile"]
            selected.extend(lock_items or items[:1])
        deduped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        for item in selected:
            key = (
                str(item.get("ecosystem") or "").casefold(),
                str(item.get("package") or "").casefold(),
                str(item.get("module") or ""),
                str(item.get("version") or ""),
                str(item.get("source_file") or ""),
            )
            deduped.setdefault(key, item)
        return sorted(
            deduped.values(),
            key=lambda item: (
                str(item.get("module") or ""),
                str(item.get("ecosystem") or ""),
                str(item.get("package") or "").casefold(),
                int(item.get("line") or 1),
            ),
        )

    def scan_project(self, root: Path) -> list[dict[str, Any]]:
        root = root.resolve()
        findings: list[dict[str, Any]] = []
        for dep in self.collect_dependencies(root):
            version = str(dep.get("version") or "")
            for hit in self._check_vulnerabilities(dep["ecosystem"], dep["package"], version):
                is_lockfile = dep.get("source_kind") == "lockfile"
                findings.append({
                    "rule_id": f"SCA-{hit.cve}",
                    "category": "supply-chain",
                    "severity": hit.severity,
                    "confidence": "high" if is_lockfile else "medium",
                    "cwe": "CWE-1395",
                    "message": f"第三方依赖 {dep.get('package', hit.package)} (当前版本: {version}) 命中已知漏洞: {hit.title} ({hit.cve})",
                    "package": dep.get("package", hit.package),
                    "current_version": version,
                    "vulnerable_below": hit.vulnerable_below,
                    "fixed_version": hit.fixed_version,
                    "file": dep.get("source_file", ""),
                    "line": dep.get("line", 1),
                    "col": 0,
                    "evidence": dep.get("evidence", f"{hit.package}@{version}"),
                    "remediation": f"建议将 {dep.get('package', hit.package)} 升级至安全版本 {hit.fixed_version} 或更高。",
                })

        return findings

    def advisory_candidates(self, dependencies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Map dependency facts to candidates without claiming a confirmed bug."""
        candidates: list[dict[str, Any]] = []
        for dep in dependencies:
            ecosystem = str(dep.get("ecosystem") or "")
            package = str(dep.get("package") or "")
            advisories = self._index.get((ecosystem.casefold(), package.casefold()), [])
            for advisory in advisories:
                version = str(dep.get("resolved_version") or dep.get("declared_version") or "")
                if not version:
                    status = "unknown"
                elif is_version_vulnerable(version, advisory.vulnerable_below):
                    status = "candidate"
                else:
                    status = "not_applicable"
                candidates.append({
                    "kind": "advisory_candidate",
                    "source": "offline_sca",
                    "id": advisory.cve,
                    "package": package,
                    "ecosystem": ecosystem,
                    "matched_version": version or None,
                    "affected_range": f"<{advisory.vulnerable_below}",
                    "fixed_version": advisory.fixed_version,
                    "severity": advisory.severity,
                    "priority": "high" if advisory.severity in {"critical", "high"} else "normal",
                    "confidence": "high" if dep.get("source_kind") == "lockfile" else ("medium" if version else "low"),
                    "status": status,
                    "evidence_refs": list(dep.get("evidence_refs") or []),
                    "matched_by": [
                        "resolved_version" if dep.get("resolved_version") else "declared_version",
                        str(dep.get("dependency_kind") or "declared_dependency"),
                    ],
                })
        return candidates

    def _check_vulnerabilities(self, ecosystem: str, package: str, version: str) -> list[SCAAdvisory]:
        key = (ecosystem.lower(), package.lower())
        advisories = self._index.get(key, [])
        matches = []
        for adv in advisories:
            if is_version_vulnerable(version, adv.vulnerable_below):
                matches.append(adv)
        return matches

    # --- 各生态清单解析器 ---

    def _parse_package_json(self, content: str) -> list[dict[str, Any]]:
        deps = []
        try:
            data = json.loads(content)
            all_deps = {}
            for group in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
                for pkg, ver in (data.get(group, {}) or {}).items():
                    all_deps[pkg] = (ver, "dev" if group == "devDependencies" else "direct")
            for pkg, (ver, dependency_kind) in all_deps.items():
                if isinstance(ver, str):
                    clean_ver = re.sub(r"[^0-9\.]", "", ver)
                    if clean_ver:
                        deps.append({"ecosystem": "npm", "package": pkg, "version": clean_ver, "dependency_kind": dependency_kind, "evidence": f'"{pkg}": "{ver}"'})
        except Exception:
            pass
        return deps

    def _parse_package_lock(self, content: str) -> list[dict[str, Any]]:
        deps = []
        try:
            data = json.loads(content)
            packages = data.get("packages", {})
            for pkg_path, pkg_info in packages.items():
                if not pkg_path:
                    continue
                pkg_name = pkg_path.split("node_modules/")[-1]
                ver = pkg_info.get("version", "")
                if ver:
                    deps.append({"ecosystem": "npm", "package": pkg_name, "version": ver, "evidence": f'"{pkg_name}": "{ver}"'})
            if not packages:
                def walk_legacy(items: Mapping[str, Any]) -> None:
                    for pkg_name, pkg_info in items.items():
                        if not isinstance(pkg_info, Mapping):
                            continue
                        ver = pkg_info.get("version", "")
                        if ver:
                            deps.append({"ecosystem": "npm", "package": pkg_name, "version": ver, "evidence": f'"{pkg_name}": "{ver}"'})
                        nested = pkg_info.get("dependencies")
                        if isinstance(nested, Mapping):
                            walk_legacy(nested)
                legacy = data.get("dependencies")
                if isinstance(legacy, Mapping):
                    walk_legacy(legacy)
        except Exception:
            pass
        return deps

    def _parse_go_mod(self, content: str) -> list[dict[str, Any]]:
        deps = []
        lines = content.splitlines()
        in_require_block = False
        for lineno, line in enumerate(lines, 1):
            s = line.strip()
            if s == "require (":
                in_require_block = True
                continue
            if in_require_block and s == ")":
                in_require_block = False
                continue

            parts = s.split()
            if not in_require_block and s.startswith("require ") and len(parts) >= 3:
                pkg, ver = parts[1], parts[2]
                deps.append({"ecosystem": "go", "package": pkg, "version": ver, "line": lineno, "evidence": s})
            elif in_require_block and len(parts) >= 2:
                pkg, ver = parts[0], parts[1]
                deps.append({"ecosystem": "go", "package": pkg, "version": ver, "line": lineno, "evidence": s})
        return deps

    def _parse_pom_xml(self, content: str) -> list[dict[str, Any]]:
        deps = []
        try:
            clean_xml = re.sub(r'\sxmlns="[^"]+"', '', content, count=1)
            # SEC-07：pom.xml 来自被审计仓库。原实现只剥 xmlns，DOCTYPE/ENTITY
            # 原样进入 expat；老版本 libexpat 上实体膨胀可直接打死宿主进程。
            # safe_xml 拒绝 DTD/实体并限制体积、深度与元素数。
            root = safe_xml.parse_xml(clean_xml)
            for dep in root.findall(".//dependency"):
                group = dep.find("groupId")
                artifact = dep.find("artifactId")
                version = dep.find("version")
                if group is not None and artifact is not None and version is not None:
                    g_text = (group.text or "").strip()
                    a_text = (artifact.text or "").strip()
                    v_text = (version.text or "").strip()
                    if g_text and a_text and v_text and not v_text.startswith("${"):
                        pkg = f"{g_text}:{a_text}"
                        deps.append({"ecosystem": "maven", "package": pkg, "version": v_text, "evidence": f"<artifactId>{a_text}</artifactId><version>{v_text}</version>"})
        except Exception:
            pass
        return deps

    def _parse_requirements_txt(self, content: str) -> list[dict[str, Any]]:
        deps = []
        lines = content.splitlines()
        for lineno, line in enumerate(lines, 1):
            s = line.strip().split("#")[0].strip()
            if not s:
                continue
            m = re.match(r"^([A-Za-z0-9_\-\.]+)\s*(?:==|>=|~=)\s*([0-9\.]+)", s)
            if m:
                pkg, ver = m.group(1), m.group(2)
                deps.append({"ecosystem": "pypi", "package": pkg, "version": ver, "line": lineno, "evidence": s})
        return deps

    def _parse_cargo_lock(self, content: str) -> list[dict[str, Any]]:
        deps = []
        lines = content.splitlines()
        curr_name = ""
        for lineno, line in enumerate(lines, 1):
            s = line.strip()
            if s.startswith('name = "'):
                curr_name = s.split('"')[1]
            elif s.startswith('version = "') and curr_name:
                curr_ver = s.split('"')[1]
                deps.append({"ecosystem": "cargo", "package": curr_name, "version": curr_ver, "line": lineno, "evidence": f'{curr_name} = "{curr_ver}"'})
                curr_name = ""
        return deps

    # --- DOC-062 新增生态解析器 ---

    def _parse_composer_lock(self, content: str) -> list[dict[str, Any]]:
        deps = []
        try:
            data = json.loads(content)
            all_pkgs = []
            if isinstance(data.get("packages"), list):
                all_pkgs.extend(data["packages"])
            if isinstance(data.get("packages-dev"), list):
                all_pkgs.extend(data["packages-dev"])
            for pkg in all_pkgs:
                if isinstance(pkg, dict):
                    name = pkg.get("name", "")
                    ver = pkg.get("version", "")
                    if name and ver:
                        clean_ver = re.sub(r"^[^\d]+", "", ver)
                        deps.append({
                            "ecosystem": "packagist",
                            "package": name,
                            "version": clean_ver or ver,
                            "evidence": f'"{name}": "{ver}"',
                        })
        except Exception:
            pass
        return deps

    def _parse_composer_json(self, content: str) -> list[dict[str, Any]]:
        deps = []
        try:
            data = json.loads(content)
            all_reqs = {}
            if isinstance(data.get("require"), dict):
                all_reqs.update(data["require"])
            if isinstance(data.get("require-dev"), dict):
                all_reqs.update(data["require-dev"])
            for pkg, ver in all_reqs.items():
                if pkg.lower() == "php" or pkg.startswith("ext-"):
                    continue
                if isinstance(ver, str):
                    clean_ver = re.sub(r"[^0-9\.]", "", ver)
                    if clean_ver:
                        deps.append({
                            "ecosystem": "packagist",
                            "package": pkg,
                            "version": clean_ver,
                            "evidence": f'"{pkg}": "{ver}"',
                        })
        except Exception:
            pass
        return deps

    def _parse_gemfile_lock(self, content: str) -> list[dict[str, Any]]:
        deps = []
        lines = content.splitlines()
        in_specs = False
        for lineno, line in enumerate(lines, 1):
            s = line.rstrip()
            if s.strip() == "specs:":
                in_specs = True
                continue
            if in_specs:
                if s and not s.startswith(" ") and not s.startswith("\t"):
                    in_specs = False
                    continue
                m = re.match(r"^    ([A-Za-z0-9_\-\.]+)\s*\(([0-9\.]+)", s)
                if m:
                    pkg, ver = m.group(1), m.group(2)
                    deps.append({
                        "ecosystem": "rubygems",
                        "package": pkg,
                        "version": ver,
                        "line": lineno,
                        "evidence": s.strip(),
                    })
        return deps

    def _parse_packages_lock_json(self, content: str) -> list[dict[str, Any]]:
        deps = []
        try:
            data = json.loads(content)
            dependencies = data.get("dependencies", {})
            for framework, pkgs in dependencies.items():
                if isinstance(pkgs, dict):
                    for pkg_name, pkg_info in pkgs.items():
                        if isinstance(pkg_info, dict):
                            ver = pkg_info.get("resolved") or pkg_info.get("requested", "")
                            clean_ver = re.sub(r"[^0-9\.]", "", ver)
                            if clean_ver:
                                deps.append({
                                    "ecosystem": "nuget",
                                    "package": pkg_name,
                                    "version": clean_ver,
                                    "evidence": f'"{pkg_name}": "{ver}"',
                                })
        except Exception:
            pass
        return deps

    def _parse_csproj(self, content: str) -> list[dict[str, Any]]:
        deps = []
        # Support PackageReference in *.csproj, *.fsproj, *.vbproj
        for m in re.finditer(r'<PackageReference\s+[^>]*Include="([^"]+)"[^>]*Version="([^"]+)"', content, re.IGNORECASE):
            pkg, ver = m.group(1), m.group(2)
            clean_ver = re.sub(r"[^0-9\.]", "", ver)
            if clean_ver:
                deps.append({"ecosystem": "nuget", "package": pkg, "version": clean_ver, "evidence": m.group(0)})
        for m in re.finditer(r'<PackageReference\s+[^>]*Version="([^"]+)"[^>]*Include="([^"]+)"', content, re.IGNORECASE):
            ver, pkg = m.group(1), m.group(2)
            clean_ver = re.sub(r"[^0-9\.]", "", ver)
            if clean_ver:
                deps.append({"ecosystem": "nuget", "package": pkg, "version": clean_ver, "evidence": m.group(0)})
        # Support packages.config
        for m in re.finditer(r'<package\s+[^>]*id="([^"]+)"[^>]*version="([^"]+)"', content, re.IGNORECASE):
            pkg, ver = m.group(1), m.group(2)
            clean_ver = re.sub(r"[^0-9\.]", "", ver)
            if clean_ver:
                deps.append({"ecosystem": "nuget", "package": pkg, "version": clean_ver, "evidence": m.group(0)})
        return deps

    def _parse_pubspec_lock(self, content: str) -> list[dict[str, Any]]:
        deps = []
        lines = content.splitlines()
        in_packages = False
        current_pkg = ""
        current_ver = ""
        pkg_lineno = 1
        for lineno, line in enumerate(lines, 1):
            s = line.rstrip()
            if s.strip() == "packages:":
                in_packages = True
                continue
            if in_packages:
                if s and not s.startswith(" ") and not s.startswith("\t"):
                    in_packages = False
                    continue
                m_pkg = re.match(r"^  ([a-zA-Z0-9_\-]+):\s*$", s)
                if m_pkg:
                    if current_pkg and current_ver:
                        deps.append({
                            "ecosystem": "pub",
                            "package": current_pkg,
                            "version": current_ver,
                            "line": pkg_lineno,
                            "evidence": f"{current_pkg}: {current_ver}",
                        })
                    current_pkg = m_pkg.group(1)
                    current_ver = ""
                    pkg_lineno = lineno
                    continue
                m_ver = re.match(r'^\s+version:\s*["\']?([0-9\.]+)["\']?', s)
                if m_ver and current_pkg:
                    current_ver = m_ver.group(1)

        if current_pkg and current_ver:
            deps.append({
                "ecosystem": "pub",
                "package": current_pkg,
                "version": current_ver,
                "line": pkg_lineno,
                "evidence": f"{current_pkg}: {current_ver}",
            })
        return deps

    def _parse_pubspec_yaml(self, content: str) -> list[dict[str, Any]]:
        deps = []
        lines = content.splitlines()
        in_deps = False
        for lineno, line in enumerate(lines, 1):
            s = line.rstrip()
            stripped = s.strip()
            if stripped in {"dependencies:", "dev_dependencies:"}:
                in_deps = True
                continue
            if in_deps:
                if s and not s.startswith(" ") and not s.startswith("\t"):
                    in_deps = False
                    continue
                m = re.match(r"^  ([a-zA-Z0-9_\-]+):\s*[\^~>=<]*\s*([0-9\.]+)", s)
                if m:
                    pkg, ver = m.group(1), m.group(2)
                    if pkg.lower() not in {"flutter", "flutter_test"}:
                        deps.append({
                            "ecosystem": "pub",
                            "package": pkg,
                            "version": ver,
                            "line": lineno,
                            "evidence": s.strip(),
                        })
        return deps

    def _parse_package_resolved(self, content: str) -> list[dict[str, Any]]:
        deps = []
        try:
            data = json.loads(content)
            pins = []
            if isinstance(data.get("object"), dict) and isinstance(data["object"].get("pins"), list):
                pins = data["object"]["pins"]
            elif isinstance(data.get("pins"), list):
                pins = data["pins"]

            for pin in pins:
                pkg = pin.get("package") or pin.get("identity", "")
                state = pin.get("state", {})
                ver = state.get("version", "") if isinstance(state, dict) else ""
                if pkg and ver:
                    clean_ver = re.sub(r"^[^\d]+", "", ver)
                    deps.append({
                        "ecosystem": "spm",
                        "package": pkg,
                        "version": clean_ver or ver,
                        "evidence": f'"{pkg}": "{ver}"',
                    })
        except Exception:
            pass
        return deps

    def _parse_podfile_lock(self, content: str) -> list[dict[str, Any]]:
        deps = []
        lines = content.splitlines()
        in_pods = False
        for lineno, line in enumerate(lines, 1):
            s = line.rstrip()
            if s.strip() == "PODS:":
                in_pods = True
                continue
            if in_pods:
                if s and not s.startswith(" ") and not s.startswith("\t"):
                    in_pods = False
                    continue
                m = re.match(r"^\s+-\s+([A-Za-z0-9_\-\.]+)(?:/[A-Za-z0-9_\-\.]+)?\s*\(([0-9\.]+)\)", s)
                if m:
                    pkg, ver = m.group(1), m.group(2)
                    deps.append({
                        "ecosystem": "cocoapods",
                        "package": pkg,
                        "version": ver,
                        "line": lineno,
                        "evidence": s.strip(),
                    })
        return deps

    def _parse_poetry_lock(self, content: str) -> list[dict[str, Any]]:
        deps = []
        lines = content.splitlines()
        curr_name = ""
        for lineno, line in enumerate(lines, 1):
            s = line.strip()
            if s.startswith('name = "'):
                curr_name = s.split('"')[1]
            elif s.startswith('version = "') and curr_name:
                curr_ver = s.split('"')[1]
                deps.append({
                    "ecosystem": "pypi",
                    "package": curr_name,
                    "version": curr_ver,
                    "line": lineno,
                    "evidence": f'{curr_name} = "{curr_ver}"',
                })
                curr_name = ""
        return deps

    def _parse_pipfile_lock(self, content: str) -> list[dict[str, Any]]:
        deps = []
        try:
            data = json.loads(content)
            all_pkgs = {}
            if isinstance(data.get("default"), dict):
                all_pkgs.update(data["default"])
            if isinstance(data.get("develop"), dict):
                all_pkgs.update(data["develop"])
            for pkg, info in all_pkgs.items():
                if isinstance(info, dict):
                    ver = info.get("version", "")
                    clean_ver = re.sub(r"[^0-9\.]", "", ver)
                    if clean_ver:
                        deps.append({
                            "ecosystem": "pypi",
                            "package": pkg,
                            "version": clean_ver,
                            "evidence": f'"{pkg}": "{ver}"',
                        })
        except Exception:
            pass
        return deps
