#!/usr/bin/env python3
"""core/skill_pack.py - BugX 审计技能包（Skill Pack）加载、校验与流水线编译 (DOC-125)。

负责：
  - 技能包 Manifest 契约加载与 JSON Schema 校验
  - L1 行动清单 (≤300T) 与 L2 攻防手册物理隔离解析
  - 流式 DAG 阶段流水线 (Stage Pipeline) 装配
  - 动态可选模块 (Modules) 激活与裁剪
  - 确定性 plan_hash 生成与工程级版本追踪
"""

from __future__ import annotations

import hashlib
import base64
import datetime as dt
import json
from functools import lru_cache
import os
from pathlib import Path
import re
import shutil
import platform
import sys
import tarfile
import tempfile
from typing import Any, Iterable, Mapping

try:
    import jsonschema
except ImportError:
    jsonschema = None  # type: ignore[assignment]

try:  # Ed25519 is optional for the immutable builtin layer only.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except ImportError:  # pragma: no cover - exercised on minimal installations.
    InvalidSignature = Exception  # type: ignore[assignment,misc]
    Ed25519PublicKey = None  # type: ignore[assignment,misc]

_CORE_DIR = Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "skills" else Path(__file__).resolve().parent
if str(_CORE_DIR) not in sys.path:  # 幂等：重复执行模块不得堆叠 sys.path
    sys.path.insert(0, str(_CORE_DIR))

try:
    from core.infra.client_metadata import client_headers, tls_context
except ImportError:
    try:
        from core.infra.client_metadata import client_headers, tls_context
    except ImportError:
        client_headers, tls_context = None, None  # type: ignore[assignment]

SKILLS_DIR_NAME = "skills"
AUDIT_PACKS_DIR_NAME = "audit_packs"
_AUDIT_PACKS_DIR = _CORE_DIR / AUDIT_PACKS_DIR_NAME
_LEGACY_SKILLS_DIR = _CORE_DIR / "prompts" / "skills"
DEFAULT_SKILLS_DIR = _AUDIT_PACKS_DIR if _AUDIT_PACKS_DIR.is_dir() else _LEGACY_SKILLS_DIR
DEFAULT_AUDIT_PACKS_DIR = DEFAULT_SKILLS_DIR
SCHEMA_PATH = _CORE_DIR / "schemas" / "skill-pack.schema.json"

_L1_START_TAG = "<!-- L1:START -->"
_L1_END_TAG = "<!-- L1:END -->"
_L2_START_TAG = "<!-- L2:START -->"
_L2_END_TAG = "<!-- L2:END -->"

# 简易 Token 估算：按 4 字符 ≈ 1 Token 预估，中英混合按字符/空格加权
MAX_L1_TOKEN_DEFAULT = 300

# Fixed distribution trust root.  The corresponding private key is not stored
# in this repository; tests inject their own key through ``public_key``.
OFFICIAL_SKILL_PUBKEY = (
    "c13a88e62636d9d382c3687e50931efd32ab6bdcee84e9ea2214b3a484c978ad"
)
_SKILL_ID_RE = re.compile(r"^[a-z0-9-]+$")
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_OVERLAY_LAYERS = frozenset({"project", "global"})


class SkillPackError(ValueError):
    """Base error for an invalid or unavailable skill pack."""


class SkillSignatureError(SkillPackError):
    """The detached official Ed25519 signature is missing or invalid."""


class SkillCapabilityError(SkillPackError):
    """The local runtime cannot safely execute the requested skill profile."""


class SkillDowngradeError(SkillPackError):
    """The candidate is older than the locally accepted stable version."""


def estimate_tokens(text: str) -> int:
    """保守估算文本的 Token 消耗量。"""
    if not text:
        return 0
    # 粗略模型：英文单词 + 中文字符
    words = len(re.findall(r"\b\w+\b", text))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) // 4
    return max(words + cjk, other, 1)


def _canonical_json_bytes(obj: Any) -> bytes:
    """输出确定性排序紧凑 JSON 字节序列。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_plan_hash(manifest: Mapping[str, Any]) -> str:
    """计算技能包 Manifest 的确定性 SHA-256 哈希 (plan_hash)。"""
    digest = hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest()
    return f"sha256:{digest}"


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    """使用 skill-pack.schema.json 严格校验 Manifest 格式。"""
    if not isinstance(manifest, Mapping):
        raise SkillPackError("Skill pack manifest 必须是 JSON 对象")
    # DOC-126 names the field skill_version while DOC-125 used version.  Keep
    # both spellings readable, but normalize only for schema validation so the
    # signed bytes and plan hash remain exactly the publisher's bytes.
    candidate = dict(manifest)
    if "version" not in candidate and "skill_version" in candidate:
        candidate["version"] = candidate["skill_version"]
    if jsonschema is None:
        # Fallback 关键字段软校验
        required = ["schema_version", "skill_id", "version", "scope", "applies_if", "stage_pipeline", "budgets"]
        missing = [f for f in required if f not in candidate]
        if missing:
            raise ValueError(f"Skill pack manifest missing required fields: {missing}")
    elif SCHEMA_PATH.is_file():
        validator = _skill_pack_validator()
        if validator is not None:
            validator.validate(candidate)

    _validate_runtime_tool_ids(candidate)


@lru_cache(maxsize=1)
def _skill_pack_validator():
    """编译一次 Skill pack schema 并复用。

    此前每次校验都 ``json.load`` 整个 schema 文件再走 ``jsonschema.validate``，
    后者会**重新编译** schema —— cProfile 实测 ``jsonschema`` 校验占
    ``test_cli_hosts`` 4 个 case 的 0.549s（9478 次 validator 调用），
    折算 0.137s/case × 105 case ≈ 14.4s。schema 是仓库内静态资产（随 skill pack
    版本发布），进程内缓存安全；文件缺失或 jsonschema 不可用时返回 None，
    调用方保持"无 schema 即跳过"的既有语义。
    """
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema 是运行期可选依赖
        return None
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as sf:
            schema = json.load(sf)
        return jsonschema.Draft202012Validator(schema)
    except (OSError, ValueError, jsonschema.exceptions.SchemaError):
        return None


def split_stage_content(markdown_text: str, max_l1_tokens: int = MAX_L1_TOKEN_DEFAULT) -> tuple[str, str]:
    """物理隔离提取 L1 行动清单 与 L2 攻防手册。

    返回: (l1_checklist, l2_playbook)
    """
    l1_text = ""
    l2_text = ""

    l1_match = re.search(
        rf"{re.escape(_L1_START_TAG)}(.*?){re.escape(_L1_END_TAG)}",
        markdown_text,
        re.DOTALL,
    )
    if l1_match:
        l1_text = l1_match.group(1).strip()
    else:
        # 未显式标记则取前 300 字符作为 L1
        l1_text = markdown_text.strip()[:600]

    l2_match = re.search(
        rf"{re.escape(_L2_START_TAG)}(.*?){re.escape(_L2_END_TAG)}",
        markdown_text,
        re.DOTALL,
    )
    if l2_match:
        l2_text = l2_match.group(1).strip()
    else:
        # 去掉 L1 后的剩余部分作为 L2
        remainder = re.sub(
            rf"{re.escape(_L1_START_TAG)}.*?{re.escape(_L1_END_TAG)}",
            "",
            markdown_text,
            flags=re.DOTALL,
        ).strip()
        l2_text = remainder

    # 实施 L1 Token 预算硬隔离 (Hard Cap)
    estimated_l1_tokens = estimate_tokens(l1_text)
    if estimated_l1_tokens > max_l1_tokens:
        # 截断并附上超预算警告
        ratio = max_l1_tokens / float(estimated_l1_tokens)
        char_cutoff = int(len(l1_text) * ratio)
        l1_text = l1_text[:char_cutoff].rstrip() + f"\n... [L1 checklist budget exceeded ({max_l1_tokens}T), truncated]"

    return l1_text, l2_text


def _version_tuple(value: object) -> tuple[int, int, int]:
    text = str(value or "")
    match = _SEMVER_RE.fullmatch(text)
    if not match:
        raise SkillPackError(f"技能包版本不是 SemVer: {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _decode_signature(value: bytes) -> bytes:
    """Decode raw, hex, or base64 detached signatures."""
    # Do not strip raw binary signatures: a valid Ed25519 signature may end in
    # a byte that happens to be ASCII whitespace.
    if len(value) == 64:
        return value
    raw = value.strip()
    try:
        decoded = bytes.fromhex(raw.decode("ascii"))
        if len(decoded) == 64:
            return decoded
    except (UnicodeDecodeError, ValueError):
        pass
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise SkillSignatureError("manifest.sig 不是有效的 Ed25519 签名") from exc
    if len(decoded) != 64:
        raise SkillSignatureError("manifest.sig 长度无效")
    return decoded


def _decode_public_key(value: str | bytes | None) -> bytes:
    if value is None:
        # The distribution key is a trust root.  Environment overrides would
        # let an untrusted process replace the key used for official packs.
        value = OFFICIAL_SKILL_PUBKEY
    raw = value if isinstance(value, bytes) else str(value).strip().encode("ascii")
    if len(raw) == 32:
        return raw
    try:
        decoded = bytes.fromhex(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        try:
            decoded = base64.b64decode(raw, validate=True)
        except (ValueError, base64.binascii.Error) as b64_exc:
            raise SkillSignatureError("官方技能公钥格式无效") from b64_exc
    if len(decoded) != 32:
        raise SkillSignatureError("官方技能公钥长度无效")
    return decoded


def verify_manifest_signature(manifest: Mapping[str, Any], signature_path: Path,
                              public_key: str | bytes | None = None) -> None:
    """Verify canonical manifest bytes with the embedded official Ed25519 key."""
    if not signature_path.is_file() or signature_path.is_symlink():
        raise SkillSignatureError(f"缺少 manifest.sig: {signature_path}")
    signature = _decode_signature(signature_path.read_bytes())
    payload = _canonical_json_bytes(manifest)
    key = _decode_public_key(public_key)
    if Ed25519PublicKey is not None:
        try:
            Ed25519PublicKey.from_public_bytes(key).verify(signature, payload)
            return
        except (InvalidSignature, ValueError) as exc:
            raise SkillSignatureError("技能包 manifest Ed25519 验签失败") from exc
    try:  # pragma: no cover - fallback for environments without cryptography.
        from nacl.signing import VerifyKey
        VerifyKey(key).verify(payload, signature)
    except Exception as exc:  # noqa: BLE001
        raise SkillSignatureError("技能包验签不可用或验签失败") from exc


def _validate_issued_at(value: object, *, now: dt.datetime | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SkillPackError("Overlay manifest 缺少 issued_at")
    text = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SkillPackError("issued_at 必须是 ISO-8601 UTC 时间戳") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise SkillPackError("issued_at 必须明确标记 UTC（Z 或 +00:00）")
    current = now or dt.datetime.now(dt.timezone.utc)
    if parsed > current + dt.timedelta(minutes=5):
        raise SkillPackError("issued_at 不能晚于当前 UTC 时间")
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


class SkillPackLoader:
    """Load Project Overlay > Global Overlay > immutable Core Builtin packs."""

    def __init__(self, project_root: Path | str | None = None, *,
                 home: Path | str | None = None,
                 project_overlay: Path | str | None = None,
                 global_overlay: Path | str | None = None,
                 builtin_root: Path | str | None = None,
                 health_path: Path | str | None = None,
                 versions_path: Path | str | None = None,
                 public_key: str | bytes | None = None,
                 now: dt.datetime | None = None) -> None:
        self.project_root = Path(project_root).expanduser().resolve() if project_root else None
        self.home = Path(home).expanduser().resolve() if home else Path.home().resolve()
        self.project_overlay = Path(project_overlay).expanduser().resolve() if project_overlay else (
            self.project_root / ".bugx" / SKILLS_DIR_NAME if self.project_root else None
        )
        self.global_overlay = Path(global_overlay).expanduser().resolve() if global_overlay else (
            self.home / ".bugx" / SKILLS_DIR_NAME / "official"
        )
        self.builtin_root = Path(builtin_root).expanduser().resolve() if builtin_root else DEFAULT_SKILLS_DIR
        self.health_path = Path(health_path).expanduser().resolve() if health_path else self.home / ".bugx" / SKILLS_DIR_NAME / ".health.json"
        self.versions_path = Path(versions_path).expanduser().resolve() if versions_path else self.home / ".bugx" / SKILLS_DIR_NAME / ".versions.json"
        self.quarantine_root = self.home / ".bugx" / SKILLS_DIR_NAME / "quarantine"
        self.public_key = public_key
        self.now = now

    @staticmethod
    def capability_vector() -> dict[str, Any]:
        """Collect the bounded, non-secret capability vector sent to cloud."""
        memory_mb = 0
        try:
            pages = int(os.sysconf("SC_PHYS_PAGES"))
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            memory_mb = max(0, (pages * page_size) // (1024 * 1024))
        except (AttributeError, OSError, ValueError):
            pass
        return {
            "os": platform.system().lower(),
            "arch": platform.machine().lower(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "memory_mb": memory_mb,
            "has_docker": shutil.which("docker") is not None,
        }

    def request_context(self, entitlements: Iterable[str] | str | None = None,
                        profile: str | None = None) -> dict[str, Any]:
        if isinstance(entitlements, str):
            values = [x.strip() for x in entitlements.split(",") if x.strip()]
        else:
            values = sorted({str(x).strip() for x in (entitlements or ()) if str(x).strip()})
        headers = client_headers()
        headers["X-BugX-Entitlements"] = ",".join(values)
        return {
            "headers": headers,
            "capabilities": self.capability_vector(),
            "profile": profile or "auto",
        }

    def request_headers(self, entitlements: Iterable[str] | str | None = None) -> dict[str, str]:
        return dict(self.request_context(entitlements)["headers"])

    build_request_headers = request_headers

    def _layer_roots(self) -> list[tuple[str, Path]]:
        roots: list[tuple[str, Path]] = []
        if self.project_overlay is not None:
            roots.append(("project", self.project_overlay))
        roots.extend((("global", self.global_overlay), ("builtin", self.builtin_root)))
        return roots

    def _candidate_dirs(self, skill_id: str) -> list[tuple[str, Path]]:
        candidates: list[tuple[str, Path]] = []
        for layer, root in self._layer_roots():
            base = root / skill_id
            if layer in _OVERLAY_LAYERS and (self.quarantine_root / skill_id).is_dir():
                continue
            if (base / "manifest.json").is_file():
                candidates.append((layer, base))
            # A pack may carry separate full/lite manifests under one skill id.
            if base.is_dir() and not base.is_symlink():
                for profile_dir in sorted(base.iterdir()):
                    if profile_dir.is_dir() and not profile_dir.is_symlink() \
                            and (profile_dir / "manifest.json").is_file():
                        candidates.append((layer, profile_dir))
        return candidates

    @staticmethod
    def _read_manifest(pack_dir: Path) -> dict[str, Any]:
        manifest_path = pack_dir / "manifest.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise FileNotFoundError(f"Skill pack manifest not found at {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SkillPackError(f"无法读取技能包 manifest: {manifest_path}") from exc
        validate_manifest(manifest)
        return manifest

    def _read_state(self, path: Path) -> dict[str, Any]:
        if not path.is_file() or path.is_symlink():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    def _write_version(self, manifest: Mapping[str, Any]) -> None:
        state = self._read_state(self.versions_path)
        versions = state.setdefault("skills", {})
        versions[str(manifest["skill_id"])] = {
            "version": str(manifest["version"]),
            "issued_at": manifest.get("issued_at"),
            "plan_hash": compute_plan_hash(manifest),
        }
        _atomic_json_write(self.versions_path, state)

    def _check_version(self, manifest: Mapping[str, Any], layer: str) -> str | None:
        version = _version_tuple(manifest.get("version"))
        issued = _validate_issued_at(manifest.get("issued_at"), now=self.now) if layer in _OVERLAY_LAYERS else None
        if layer not in _OVERLAY_LAYERS:
            return issued
        previous = self._read_state(self.versions_path).get("skills", {}).get(str(manifest["skill_id"]), {})
        if isinstance(previous, Mapping) and previous.get("version"):
            old_version = _version_tuple(previous["version"])
            if version < old_version:
                raise SkillDowngradeError(
                    f"拒绝技能包降级: {manifest['skill_id']} {manifest['version']} < {previous['version']}"
                )
            if version == old_version and previous.get("issued_at"):
                same_pack = previous.get("plan_hash") == compute_plan_hash(manifest)
                if issued == str(previous["issued_at"]) and same_pack:
                    return issued
                if issued <= str(previous["issued_at"]):
                    raise SkillDowngradeError("拒绝技能包重放：issued_at 未前进")
        return issued

    @staticmethod
    def _requirements(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
        value = manifest.get("required_capabilities", {})
        if not isinstance(value, Mapping):
            return {}
        profile = str(manifest.get("profile") or "")
        selected = value.get(profile)
        return selected if isinstance(selected, Mapping) else value

    @classmethod
    def check_capabilities(cls, manifest: Mapping[str, Any], capabilities: Mapping[str, Any] | None = None) -> None:
        current = dict(capabilities or cls.capability_vector())
        requirements = cls._requirements(manifest)
        for key in ("os", "arch"):
            needed = requirements.get(key)
            if needed and current.get(key) not in ({str(x).lower() for x in needed} if isinstance(needed, (list, tuple, set)) else {str(needed).lower()}):
                raise SkillCapabilityError(f"技能包需要 {key}={needed}，当前为 {current.get(key)}")
        if requirements.get("has_docker") is True and not bool(current.get("has_docker")):
            raise SkillCapabilityError("技能包需要 Docker，但当前环境不可用")
        if requirements.get("memory_mb") is not None:
            try:
                current_mb = int(current.get("memory_mb") or 0)
                needed_mb = int(requirements["memory_mb"])
                if 0 < current_mb < needed_mb:
                    raise SkillCapabilityError(f"技能包需要至少 {needed_mb}MB 内存，当前仅 {current_mb}MB")
            except (TypeError, ValueError) as exc:
                raise SkillCapabilityError("技能包 memory_mb 能力声明无效") from exc
        required_python = requirements.get("python")
        if required_python:
            actual = _version_tuple(f"{current.get('python', '0.0')}.0")
            match = re.search(r"(\d+)(?:\.(\d+))?", str(required_python))
            if match and actual[:2] < (int(match.group(1)), int(match.group(2) or 0)):
                raise SkillCapabilityError(f"技能包需要 Python {required_python}，当前为 {current.get('python')}")

    def _load_candidate(self, layer: str, pack_dir: Path, *, capabilities: Mapping[str, Any] | None,
                        entitlements: set[str], profile: str | None,
                        unsigned_allowed: bool = False,
                        skip_version: bool = False,
                        record_version: bool = True) -> dict[str, Any]:
        manifest = self._read_manifest(pack_dir)
        if layer in _OVERLAY_LAYERS and not unsigned_allowed:
            verify_manifest_signature(manifest, pack_dir / "manifest.sig", self.public_key)
        if not skip_version:
            self._check_version(manifest, layer)
        candidate_profile = str(manifest.get("profile") or "")
        if profile and profile != "auto" and candidate_profile and candidate_profile != profile \
                and not (profile == "full" and candidate_profile == "lite"):
            raise SkillCapabilityError(f"技能包 profile={candidate_profile or 'default'} 不满足请求 profile={profile}")
        required_entitlements = {str(x) for x in (manifest.get("entitlements") or [])}
        if not required_entitlements.issubset(entitlements):
            missing = ", ".join(sorted(required_entitlements - entitlements))
            raise SkillCapabilityError(f"技能包缺少订阅权益: {missing}")
        self.check_capabilities(manifest, capabilities)
        stages: dict[str, dict[str, Any]] = {}
        stages_dir = pack_dir / "stages"
        if stages_dir.is_dir():
            for stage_file in sorted(stages_dir.glob("*.md")):
                stage_id = stage_file.stem
                content = stage_file.read_text(encoding="utf-8")
                l1, l2 = split_stage_content(content, max_l1_tokens=manifest.get("budgets", {}).get("max_l1_checklist_tokens", MAX_L1_TOKEN_DEFAULT))
                stages[stage_id] = {"file_path": str(stage_file), "l1_checklist": l1, "l2_playbook": l2, "full_text": content}
        modules: dict[str, dict[str, Any]] = {}
        modules_dir = pack_dir / "modules"
        if modules_dir.is_dir():
            for module_file in sorted(modules_dir.glob("*.md")):
                modules[module_file.stem] = {"file_path": str(module_file), "content": module_file.read_text(encoding="utf-8")}
        pack = {
            "manifest": manifest,
            "plan_hash": compute_plan_hash(manifest),
            "stages": stages,
            "modules": modules,
            "pack_dir": str(pack_dir),
            "source_layer": layer,
            "profile": candidate_profile or "default",
            "capabilities": dict(capabilities or self.capability_vector()),
        }
        if layer in _OVERLAY_LAYERS and not unsigned_allowed and record_version:
            self._write_version(manifest)
        return pack

    @staticmethod
    def _session_file(session_path: Path | str | None) -> Path | None:
        if session_path is None:
            return None
        path = Path(session_path).expanduser()
        return path / "session.json" if path.is_dir() or path.suffix == "" else path

    def _pin(self, session_path: Path | str | None, pack: Mapping[str, Any]) -> None:
        path = self._session_file(session_path)
        if path is None:
            return
        state = self._read_state(path)
        pinned = state.setdefault("pinned_skills", {})
        skill_id = str(pack["manifest"]["skill_id"])
        existing = pinned.get(skill_id)
        if existing:
            return
        snapshot_path = path.parent / ".skill-packs" / skill_id / str(pack["manifest"]["version"])
        if snapshot_path.exists():
            shutil.rmtree(snapshot_path)
        try:
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(pack["pack_dir"], snapshot_path)
        except OSError:
            snapshot_path = None  # type: ignore[assignment]
        pinned[skill_id] = {
            "version": pack["manifest"]["version"],
            "plan_hash": pack["plan_hash"],
            "source_layer": pack.get("source_layer"),
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
        }
        _atomic_json_write(path, state)

    def load(self, skill_id: str, *, session_path: Path | str | None = None,
             profile: str | None = None, entitlements: Iterable[str] | str | None = None,
             capabilities: Mapping[str, Any] | None = None,
             allow_degraded: bool = True) -> dict[str, Any]:
        if not _SKILL_ID_RE.fullmatch(str(skill_id)):
            raise SkillPackError(f"非法 skill_id: {skill_id!r}")
        requested_entitlements = set(
            x.strip() for x in (entitlements.split(",") if isinstance(entitlements, str) else (entitlements or ())) if str(x).strip()
        )
        session_file = self._session_file(session_path)
        pinned = self._read_state(session_file).get("pinned_skills", {}).get(skill_id, {}) if session_file else {}
        errors: list[Exception] = []
        if pinned.get("snapshot_path"):
            snapshot = Path(str(pinned["snapshot_path"])).expanduser().resolve()
            if snapshot.is_dir() and not snapshot.is_symlink():
                try:
                    layer = str(pinned.get("source_layer") or "builtin")
                    pack = self._load_candidate(
                        layer, snapshot, capabilities=capabilities, entitlements=requested_entitlements,
                        profile=profile, unsigned_allowed=layer == "builtin", skip_version=True,
                    )
                    if pack["plan_hash"] == pinned.get("plan_hash"):
                        pack["pinned_snapshot"] = True
                        return pack
                except (OSError, SkillPackError, ValueError, UnicodeError) as exc:
                    errors.append(exc)
        for layer, pack_dir in self._candidate_dirs(skill_id):
            try:
                manifest = self._read_manifest(pack_dir)
                if pinned:
                    if str(manifest.get("version")) != str(pinned.get("version")):
                        continue
                pack = self._load_candidate(
                    layer, pack_dir, capabilities=capabilities, entitlements=requested_entitlements,
                    profile=profile, unsigned_allowed=False,
                )
                if pinned and pinned.get("plan_hash") != pack["plan_hash"]:
                    continue
                self._pin(session_path, pack)
                return pack
            except (OSError, SkillPackError, ValueError, UnicodeError) as exc:
                errors.append(exc)
        if pinned:
            # A quarantined pinned overlay must not brick a Session; use the
            # immutable builtin as the last safe boundary and expose the fact.
            for layer, pack_dir in self._candidate_dirs(skill_id):
                if layer != "builtin":
                    continue
                try:
                    pack = self._load_candidate(layer, pack_dir, capabilities=capabilities,
                                                entitlements=requested_entitlements, profile=None,
                                                unsigned_allowed=True)
                    pack["pinned_fallback"] = True
                    return pack
                except (OSError, SkillPackError, ValueError, UnicodeError) as exc:
                    errors.append(exc)
        if errors and not allow_degraded:
            raise errors[-1]
        if errors:
            # If no builtin survived, preserve the most actionable validation
            # failure rather than hiding the security boundary.
            raise errors[-1]
        raise FileNotFoundError(f"Skill pack not found in overlays or builtin: {skill_id}")

    def load_skill_pack(self, skill_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.load(skill_id, **kwargs)

    def list_skill_ids(self) -> list[str]:
        ids: set[str] = set()
        for _layer, root in self._layer_roots():
            if root.is_dir():
                ids.update(p.name for p in root.iterdir() if p.is_dir() and _SKILL_ID_RE.fullmatch(p.name))
        return sorted(ids)

    def _health(self) -> dict[str, Any]:
        value = self._read_state(self.health_path)
        value.setdefault("skills", {})
        return value

    def record_success(self, skill_id: str) -> dict[str, Any]:
        health = self._health()
        item = health["skills"].setdefault(skill_id, {})
        item.update({"consecutive_failures": 0, "last_status": "ok"})
        _atomic_json_write(self.health_path, health)
        return item

    def record_crash(self, skill_id: str, pack_dir: Path | str | None = None,
                     error: object | None = None) -> dict[str, Any]:
        health = self._health()
        item = health["skills"].setdefault(skill_id, {})
        item["consecutive_failures"] = int(item.get("consecutive_failures", 0) or 0) + 1
        item["crash_count"] = int(item.get("crash_count", 0) or 0) + 1
        item["last_status"] = "crash"
        item["last_error"] = str(error or "")[:500]
        if item["consecutive_failures"] >= 2:
            source = Path(pack_dir).expanduser().resolve() if pack_dir else None
            if source is None:
                for layer, candidate in self._candidate_dirs(skill_id):
                    if layer in _OVERLAY_LAYERS:
                        source = candidate
                        break
            if source and source.is_dir() and not source.is_symlink():
                try:
                    source_is_overlay = False
                    try:
                        source.relative_to(self.global_overlay)
                        source_is_overlay = True
                    except ValueError:
                        if self.project_overlay is not None:
                            try:
                                source.relative_to(self.project_overlay)
                                source_is_overlay = True
                            except ValueError:
                                pass
                    if source_is_overlay:
                        self.quarantine_root.mkdir(parents=True, exist_ok=True)
                        target = self.quarantine_root / skill_id
                        if target.exists():
                            target = self.quarantine_root / f"{skill_id}-{int(dt.datetime.now().timestamp())}"
                        shutil.move(str(source), str(target))
                        item["quarantined"] = str(target)
                        item["circuit_breaker"] = "open"
                except (OSError, ValueError):
                    pass
        _atomic_json_write(self.health_path, health)
        return item

    def execute(self, skill_id: str, callback: Any, *args: Any, **kwargs: Any) -> Any:
        """Run one skill callback and feed its uncaught failure to the breaker."""
        pack = self.load(skill_id, **kwargs.pop("load_options", {}))
        try:
            result = callback(pack, *args, **kwargs)
        except Exception as exc:
            self.record_crash(skill_id, pack.get("pack_dir"), exc)
            raise
        self.record_success(skill_id)
        return result

    def import_bundle(self, bundle: Path | str, *, target_root: Path | str | None = None) -> dict[str, Any]:
        """Verify and atomically import an offline signed skill archive."""
        destination = Path(target_root).expanduser().resolve() if target_root else self.global_overlay
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="bugx-skill-import-") as temp_dir:
            staging = Path(temp_dir)
            with tarfile.open(Path(bundle).expanduser(), "r:*") as archive:
                members = archive.getmembers()
                for member in members:
                    name = Path(member.name)
                    if name.is_absolute() or ".." in name.parts or member.issym() or member.islnk():
                        raise SkillPackError("技能包归档包含不安全路径")
                archive.extractall(staging)
            manifests = list(staging.rglob("manifest.json"))
            if len(manifests) != 1:
                raise SkillPackError("离线技能包必须包含且仅包含一个 manifest.json")
            pack_dir = manifests[0].parent
            manifest = self._read_manifest(pack_dir)
            self._load_candidate(
                "global", pack_dir, capabilities=None, entitlements=set(), profile=None,
                record_version=False,
            )
            skill_id = str(manifest["skill_id"])
            target = destination / skill_id
            old_manifest = target / "manifest.json"
            if old_manifest.is_file():
                old = self._read_manifest(target)
                if _version_tuple(manifest["version"]) < _version_tuple(old["version"]):
                    raise SkillDowngradeError("离线技能包版本低于已安装版本")
            publish = destination / f".{skill_id}.incoming"
            if publish.exists():
                shutil.rmtree(publish)
            shutil.copytree(pack_dir, publish)
            try:
                if target.exists():
                    backup = destination / f".{skill_id}.previous"
                    if backup.exists():
                        shutil.rmtree(backup)
                    os.replace(target, backup)
                os.replace(publish, target)
                if 'backup' in locals() and backup.exists():
                    shutil.rmtree(backup)
            finally:
                if publish.exists():
                    shutil.rmtree(publish)
            return self.load(skill_id)

    def download_skill_pack(
        self,
        skill_id: str,
        base_url: str | None = None,
        *,
        auth_token: str | None = None,
        profile: str | None = None,
        entitlements: Iterable[str] | str | None = None,
        target_root: Path | str | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """Download, verify, and atomically install a signed skill pack from cloud."""
        import urllib.request
        import urllib.error
        url_base = (
            base_url
            or os.environ.get("BUGX_CLOUD_API_BASE")
            or os.environ.get("BUGX_CLOUD_API", "https://bugx.dev")
        ).rstrip("/")
        url = f"{url_base}/api/v1/agent/skills/{skill_id}/bundle"
        ctx = self.request_context(entitlements, profile)
        headers = dict(ctx["headers"])
        headers["Accept"] = "application/gzip, application/octet-stream"
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            try:
                if url.startswith("https://"):
                    resp_ctx = tls_context(base_url=url_base)
                    resp = urllib.request.urlopen(req, timeout=timeout, context=resp_ctx)
                else:
                    resp = urllib.request.urlopen(req, timeout=timeout)
            except TypeError:
                resp = urllib.request.urlopen(req, timeout=timeout)
            with resp:
                raw_data = resp.read()
        except urllib.error.HTTPError as exc:
            raise SkillPackError(f"云端技能包拉取失败 (HTTP {exc.code}): {exc}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SkillPackError(f"云端技能包网络传输失败: {exc}") from exc
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp_file:
            tmp_path = Path(tmp_file.name)
            tmp_file.write(raw_data)
        try:
            return self.import_bundle(tmp_path, target_root=target_root)
        finally:
            tmp_path.unlink(missing_ok=True)


def global_overlay_dir() -> Path:
    """返回官方/全局商业技能包 Overlay 存放路径。"""
    home = Path(os.environ.get("BUGX_HOME", str(Path.home() / ".bugx"))).expanduser().resolve()
    return home / SKILLS_DIR_NAME / "official"


def import_skill_bundle(bundle: Path | str, target_dir: Path | str | None = None) -> dict[str, Any]:
    """验证并原子导入离线签名技能包。"""
    loader = SkillPackLoader(global_overlay=target_dir) if target_dir else SkillPackLoader()
    pack = loader.import_bundle(bundle, target_root=target_dir)
    manifest = pack.get("manifest", {})
    return {
        "skill_id": manifest.get("skill_id"),
        "version": manifest.get("version"),
        "path": pack.get("pack_dir"),
        "plan_hash": pack.get("plan_hash"),
        "pack": pack,
    }


def pull_skill_pack(skill_id: str, **kwargs: Any) -> dict[str, Any]:
    """从云端拉取、验证并安装技能包。"""
    return SkillPackLoader().download_skill_pack(skill_id, **kwargs)


def load_skill_pack(skill_id: str, skills_dir: Path | None = None, **kwargs: Any) -> dict[str, Any]:
    """Compatibility wrapper around :class:`SkillPackLoader`."""
    if skills_dir is not None:
        # Legacy callers pass the immutable builtin directory.  Keep that
        # compatibility path explicit; overlay candidates still go through
        # the normal detached-signature check in SkillPackLoader.load().
        loader_options = {
            key: kwargs.pop(key) for key in (
                "project_root", "home", "project_overlay", "global_overlay", "builtin_root",
                "health_path", "versions_path", "public_key", "now",
            ) if key in kwargs
        }
        loader = SkillPackLoader(builtin_root=skills_dir, **loader_options)
        return loader.load(skill_id, **kwargs)
    loader_options = {
        key: kwargs.pop(key) for key in (
            "project_root", "home", "project_overlay", "global_overlay", "builtin_root",
            "health_path", "versions_path", "public_key", "now",
        ) if key in kwargs
    }
    return SkillPackLoader(**loader_options).load(skill_id, **kwargs)


def find_matching_skill_packs(
    classification: Mapping[str, Any],
    skills_dir: Path | None = None,
    *,
    project_root: Path | str | None = None,
    entitlements: Iterable[str] | str | None = None,
    capabilities: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """根据项目的技术栈分类查找适用的技能包。"""
    loader = (SkillPackLoader(project_root=project_root, builtin_root=skills_dir)
              if skills_dir is not None else SkillPackLoader(project_root=project_root))
    if skills_dir is not None:
        ids = sorted(p.name for p in skills_dir.iterdir()
                     if p.is_dir() and _SKILL_ID_RE.fullmatch(p.name)) if skills_dir.is_dir() else []
    else:
        ids = loader.list_skill_ids()

    forms = set(classification.get("forms") or [])
    surfaces = set(classification.get("surfaces") or [])
    frameworks = set(classification.get("frameworks") or [])

    matches = []
    for skill_id in ids:
        try:
            pack = loader.load(skill_id, entitlements=entitlements, capabilities=capabilities)
            manifest = pack["manifest"]
        except Exception:
            continue

        applies_if = manifest.get("applies_if", {})
        req_forms = set(applies_if.get("forms") or [])
        req_surfaces = set(applies_if.get("surfaces") or [])
        req_frameworks = set(applies_if.get("frameworks") or [])

        # 匹配逻辑：form 和 surface 只要有重叠即命中
        form_match = not req_forms or bool(forms & req_forms)
        surface_match = not req_surfaces or bool(surfaces & req_surfaces)
        framework_match = not req_frameworks or bool(frameworks & req_frameworks)

        if form_match and surface_match:
            score = 0
            if form_match:
                score += len(forms & req_forms) * 2
            if surface_match:
                score += len(surfaces & req_surfaces) * 3
            if framework_match and req_frameworks:
                score += len(frameworks & req_frameworks) * 5

            matches.append({
                "skill_id": manifest["skill_id"],
                "version": manifest["version"],
                "score": score,
                "manifest": manifest,
                "plan_hash": compute_plan_hash(manifest),
            })

    matches.sort(key=lambda item: item["score"], reverse=True)
    return matches


def runtime_requirements(manifest: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """归一化技能包的**运行时能力需求**（DOC-233 设计项 1）。

    与 ``required_capabilities``（**主机环境**：os/arch/python/memory/docker）语义分离：
    本字段只声明"挖掘需要哪些 MCP / 工具"，供能力编排器消费。

    工具缺省为必需项，只有显式 ``tier="optional"`` 才允许跳过；MCP
    缺省仍为 optional。未声明该字段视为无运行时需求。
    """
    raw = manifest.get("required_runtime")
    if raw is None:
        return {"mcp": [], "tools": []}
    if not isinstance(raw, Mapping):
        raise SkillPackError("required_runtime 必须是对象")
    result: dict[str, list[dict[str, Any]]] = {"mcp": [], "tools": []}
    for kind in ("mcp", "tools"):
        items = raw.get(kind)
        if items is None:
            continue
        if not isinstance(items, (list, tuple)):
            raise SkillPackError(f"required_runtime.{kind} 必须是数组")
        default_tier = "required" if kind == "tools" else "optional"
        allowed_tiers = {"required", "optional"} if kind == "tools" else {
            "optional", "hard_for_mcp_auto",
        }
        for item in items:
            if isinstance(item, str) and item.strip():
                if kind == "tools":
                    raise SkillPackError(
                        "required_runtime.tools 条目必须是带 stage_ids 的对象"
                    )
                result[kind].append({"name": item.strip(), "tier": default_tier})
            elif isinstance(item, Mapping):
                name = str(item.get("name") or "").strip()
                if not name:
                    raise SkillPackError(f"required_runtime.{kind} 条目缺少 name")
                tier = str(item.get("tier", default_tier)).strip()
                if tier not in allowed_tiers:
                    raise SkillPackError(
                        f"required_runtime.{kind}.{name} 的 tier 无效: {tier}"
                    )
                requirement: dict[str, Any] = {"name": name, "tier": tier}
                if kind == "tools":
                    stage_ids = item.get("stage_ids")
                    if not isinstance(stage_ids, (list, tuple)) or not stage_ids \
                            or any(not isinstance(stage, str) or not stage.strip()
                                   for stage in stage_ids):
                        raise SkillPackError(
                            f"required_runtime.tools.{name}.stage_ids 必须是非空字符串数组"
                        )
                    normalized_stages = [stage.strip() for stage in stage_ids]
                    if len(set(normalized_stages)) != len(normalized_stages):
                        raise SkillPackError(
                            f"required_runtime.tools.{name}.stage_ids 不得包含重复阶段"
                        )
                    requirement["stage_ids"] = normalized_stages
                if kind == "tools" and "targets" in item:
                    targets = item["targets"]
                    if not isinstance(targets, (list, tuple)) or not targets \
                            or any(not isinstance(target, str) or not target.strip()
                                   for target in targets):
                        raise SkillPackError(
                            f"required_runtime.tools.{name}.targets 必须是非空字符串数组"
                        )
                    normalized_targets = [target.strip().casefold() for target in targets]
                    if len(set(normalized_targets)) != len(normalized_targets):
                        raise SkillPackError(
                            f"required_runtime.tools.{name}.targets 不得包含重复目标"
                        )
                    requirement["targets"] = normalized_targets
                result[kind].append(requirement)
            else:
                raise SkillPackError(f"required_runtime.{kind} 包含无效条目")
    return result


def _validate_runtime_tool_ids(manifest: Mapping[str, Any]) -> None:
    """Reject Skill tool declarations that have no trusted probe implementation."""
    tool_requirements = runtime_requirements(manifest).get("tools", [])
    if not tool_requirements:
        return
    raw_stages = manifest.get("stage_pipeline")
    if not isinstance(raw_stages, (list, tuple)) or not raw_stages:
        raise SkillPackError("required_runtime.tools 要求非空 stage_pipeline")
    stage_ids: list[str] = []
    for stage in raw_stages:
        if not isinstance(stage, Mapping):
            raise SkillPackError("stage_pipeline 包含无效阶段")
        stage_id = str(stage.get("stage_id") or "").strip()
        if not stage_id:
            raise SkillPackError("stage_pipeline 阶段缺少 stage_id")
        stage_ids.append(stage_id)
    if len(set(stage_ids)) != len(stage_ids):
        raise SkillPackError("stage_pipeline.stage_id 必须唯一")
    valid_stage_ids = set(stage_ids)
    try:
        from core.probes import probe_registry
    except ImportError:
        try:
            from core import probe_registry  # type: ignore[no-redef]
        except ImportError as exc:
            raise SkillPackError(
                "required_runtime.tools 无法校验：BugX probe 注册表不可用"
            ) from exc

    for item in tool_requirements:
        name = item["name"]
        unknown_stages = sorted(set(item["stage_ids"]) - valid_stage_ids)
        if unknown_stages:
            raise SkillPackError(
                f"required_runtime.tools.{name} 引用了未登记 stage_id: "
                + ", ".join(unknown_stages)
            )
        descriptor = probe_registry.get_probe(name)
        if descriptor is None:
            raise SkillPackError(
                f"required_runtime.tools 引用了未注册的 BugX probe: {name}"
            )
        if item["tier"] == "required" and descriptor.lifecycle not in {
            "preflight", "runtime",
        }:
            raise SkillPackError(
                f"required_runtime.tools probe {name} 的 lifecycle 无效: "
                f"{descriptor.lifecycle}"
            )
        if item["tier"] == "required" and descriptor.lifecycle == "runtime" \
                and not descriptor.readiness_args:
            raise SkillPackError(
                f"required_runtime.tools 要求的 runtime probe {name} 未声明安全就绪动作"
            )


def compile_skill_plan(
    skill_id: str,
    classification: Mapping[str, Any],
    skills_dir: Path | None = None,
    *,
    project_root: Path | str | None = None,
    session_path: Path | str | None = None,
    profile: str | None = None,
    entitlements: Iterable[str] | str | None = None,
    capabilities: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """为特定项目编译生成激活的技能包计划 (skill_plan)。"""
    try:
        loader = (SkillPackLoader(project_root=project_root, builtin_root=skills_dir)
                  if skills_dir is not None else SkillPackLoader(project_root=project_root))
        pack = loader.load(
            skill_id, session_path=session_path, profile=profile,
            entitlements=entitlements, capabilities=capabilities)
    except (FileNotFoundError, ValueError):
        return None

    manifest = pack["manifest"]
    surfaces = set(classification.get("surfaces") or [])

    # 根据当前项目的实际表面动态激活可选模块 (Module Activation)
    active_modules = []
    for mod in manifest.get("optional_modules", []):
        if mod in surfaces or (mod == "upload" and "file" in surfaces) or (mod == "multi-tenant" and "auth" in surfaces):
            active_modules.append(mod)

    return {
        "skill_id": manifest["skill_id"],
        "version": manifest["version"],
        "plan_hash": pack["plan_hash"],
        "stage_pipeline": manifest["stage_pipeline"],
        # Keep manifest runtime needs in the compiled plan passed to the stage
        # harness. Dropping them here made the worker plan lose its declared
        # tools even though runtime_requirements() had already normalized them.
        "required_runtime": runtime_requirements(manifest),
        "active_modules": active_modules,
        "budgets": manifest["budgets"],
        "experience_queries": manifest.get("experience_queries", {}),
    }


def patch_skill_plan(
    plan: Mapping[str, Any],
    new_modules: Iterable[str],
    skills_dir: Path | None = None,
    *,
    project_root: Path | str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """在探索期动态打补丁激活可选模块 (DOC-125 动态自愈激活)。

    返回: (patched_plan, added_modules)
    """
    skill_id = str(plan.get("skill_id") or "")
    if not skill_id:
        return dict(plan), []

    try:
        loader = (SkillPackLoader(project_root=project_root, builtin_root=skills_dir)
                  if skills_dir is not None else SkillPackLoader(project_root=project_root))
        pack = loader.load(skill_id)
    except (FileNotFoundError, ValueError):
        return dict(plan), []

    manifest = pack["manifest"]
    optional_allowed = set(manifest.get("optional_modules", []))

    current_active = list(plan.get("active_modules") or [])
    current_set = set(current_active)

    added = []
    for mod in new_modules:
        m = str(mod).strip().lower()
        if m in optional_allowed and m not in current_set and m not in added:
            added.append(m)

    if not added:
        return dict(plan), []

    patched = dict(plan)
    patched["active_modules"] = sorted(current_set | set(added))

    # 重新计算 plan_hash：基于当前 plan 内容的确定性哈希
    raw_bytes = _canonical_json_bytes({
        "skill_id": patched.get("skill_id"),
        "version": patched.get("version"),
        "active_modules": patched.get("active_modules"),
        "stage_pipeline": patched.get("stage_pipeline"),
        "budgets": patched.get("budgets"),
    })
    patched["parent_plan_hash"] = plan.get("plan_hash")
    patched["plan_hash"] = f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"
    return patched, added


_SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cloud_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN [^-]{0,40}PRIVATE KEY-----")),
    # PE-20261004-005B：值部分不含点/冒号（文件路径:行号类散文不再误报），
    # 显式覆盖 "bearer <JWT/长token>"（旧式漏报）；≥16 字符保持口令/hex 检出。
    # PE-20261008-001：原子组禁止回溯 + 值段紧跟中文视为散文——派生 signature
    # 是 "{category}: {title}"（如 "authorization: write_text_atomic 的…"），
    # category 恰为敏感词且 title 以 ≥16 字符标识符开头，不构成凭据赋值；
    # 真凭据值后不会紧跟中文散文。
    ("credential_assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|authorization|cookie)"
        r"\s*[:=]\s*[\"']?(?:bearer\s+)?"
        # 负向先行：值为源文件引用（路径 + 源码扩展名 [+ :行号]）时不算凭据
        r"(?![A-Za-z0-9_./-]*\.(?:py|js|ts|go|md|sql|json|ya?ml|txt|sh|rb|java|toml|cfg|ini|lock|whl|gemspec)\b(?::\d+)?)"
        r"(?>[A-Za-z0-9+/=_\-]{16,})(?!\s*[\u4e00-\u9fff])"
    )),
    ("ip_address", re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b")),
)


class CandidatePromotionGuard:
    """DOC-125: 经验到 Skill Candidate 晋级硬门槛守卫 (Adversarial Regression Guard)。"""

    @staticmethod
    def scan_sensitive(content: str) -> list[str]:
        """扫描内容中是否包含敏感信息（API Key, Token, IP 等）。"""
        hits = []
        for name, pat in _SENSITIVE_PATTERNS:
            if pat.search(content):
                hits.append(name)
        return hits

    @classmethod
    def evaluate_candidate(
        cls,
        candidate_record: Mapping[str, Any],
    ) -> tuple[bool, list[str]]:
        """评估候选经验是否满足 4 道晋级硬门槛。

        门槛要求：
        1. 多周期验证：lifecycle_count >= 2 且 passed_tests >= 3；
        2. 对抗性样本库回放 (0 误报铁律)：clean_code_false_positives == 0 且 fp_growth_rate <= 0.0；
        3. 敏感信息脱敏：绝无真实 Token、API Key、内网 IP 等；
        4. 提供确定性回滚依据 (rollback_target)。

        返回: (passed, reasons)
        """
        reasons = []

        # 1. 多周期与用例验证
        lifecycles = int(candidate_record.get("lifecycle_count", 0))
        passed_tests = int(candidate_record.get("passed_tests", 0))
        if lifecycles < 2 or passed_tests < 3:
            reasons.append(
                f"多周期门槛未达标: lifecycles={lifecycles} (需>=2), passed_tests={passed_tests} (需>=3)"
            )

        # 2. 对抗性干净样本库回放 (0 误报)
        clean_fps = int(candidate_record.get("clean_code_false_positives", 0))
        fp_growth = float(candidate_record.get("fp_growth_rate", 0.0))
        if clean_fps > 0 or fp_growth > 0.0:
            reasons.append(
                f"对抗性样本库回放未过: clean_fps={clean_fps} (必须=0), fp_growth={fp_growth} (必须<=0%)"
            )

        # 3. 敏感信息脱敏硬扫描
        text_to_scan = json.dumps(dict(candidate_record), ensure_ascii=False)
        sensitive_hits = cls.scan_sensitive(text_to_scan)
        if sensitive_hits:
            reasons.append(f"发现敏感凭据未脱敏: {', '.join(sensitive_hits)}")

        # 4. 回滚能力
        if not candidate_record.get("rollback_target"):
            reasons.append("缺少 rollback_target 回滚定位标识")

        return len(reasons) == 0, reasons
