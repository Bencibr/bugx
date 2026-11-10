"""Codex 的 CLI 侧实现体（约定名 ``commands/plugin_<host>.py``）。

通用层（``commands/agent.py``）只按约定名寻址 ``validate_install`` 钩子，不再持有
「Codex 的入口在哪、旧路径怎么提示」这类事实。入口路径与遗留路径都来自
``core/hosts/codex.py``（``install.path`` / ``install.legacy_paths``）。

宿主名由本模块的约定名派生（``plugin_codex`` → ``codex``），因此改名只需改文件名，
不会在通用层留下第二份声明。
"""

from __future__ import annotations

from pathlib import Path

from ..common import HOST_ALL_MCP
from ..hosts.registry import engine_host_plugin

_HOST_NAME = __name__.rsplit(".", 1)[-1].removeprefix("plugin_")
_PLUGIN = engine_host_plugin(_HOST_NAME)


def validate_install(root: Path) -> tuple[bool, str]:
    """安装诊断：技能入口 + 真的会被 Codex 加载的 MCP 注册。

    DOC-169：Codex 官方项目发现路径是 ``$CWD/.agents/skills``；旧 ``.codex/skills``
    仅作迁移提示。MCP 侧必须同时满足两个条件（缺一即恒为死配置）：

    * ``.codex/config.toml`` 里有 BugX 的 ``[mcp_servers.*]`` 表；
    * 项目在 ``$CODEX_HOME/config.toml`` 中被标记 trusted——官方文档明确
      untrusted 项目会跳过整个项目级 ``.codex/`` 层。
    """

    root = Path(root).resolve()
    skill_rel = _PLUGIN.install.path
    skill = root / skill_rel
    skill_ok = skill.is_file()
    skill_detail = f"{skill_rel} 技能就绪（{_PLUGIN.identity.label} 项目发现路径）"
    if not skill_ok:
        for legacy_rel in _PLUGIN.install.legacy_paths:
            if (root / legacy_rel).is_file():
                skill_ok = True
                skill_detail = (
                    f"{legacy_rel}（旧路径）技能就绪；建议重跑 "
                    f"bugx install --agent {_PLUGIN.name} 迁移到 {skill_rel}"
                )
                break
    if not skill_ok:
        skill_detail = f"缺少 {skill_rel}；请运行 bugx install --agent {_PLUGIN.name}"

    inspect = _PLUGIN.mcp_write.inspect
    if inspect is None:  # 契约层已保证存在；此处只防御性兜底
        return skill_ok, skill_detail
    report = inspect(root, tuple(HOST_ALL_MCP))
    config_path = report.get("path")
    missing = list(report.get("missing") or ())
    trust = str(report.get("trust") or "untrusted")
    if report.get("error"):
        return False, f"{config_path} 无法解析：{report['error']}；请修复或删除后重跑安装"
    if missing:
        return False, (
            f"{skill_detail}；缺少 MCP 注册 {', '.join(missing)}（{config_path}）；"
            f"请运行 bugx install --agent {_PLUGIN.name}"
        )
    if trust != "trusted":
        trust_path = report.get("trust_path")
        if trust == "absent":
            return skill_ok, f"{skill_detail}；MCP 已注册，但本机未安装 {_PLUGIN.identity.label}，未验证信任态"
        return False, (
            f"{skill_detail}；MCP 已写入 {config_path}，但项目未在 {trust_path} 中标记 "
            f'trust_level = "trusted"，Codex 会整层跳过该配置（等于没有 MCP）'
        )
    # 技能入口缺失时即便 MCP 就绪也不得报通过——else 分支会把「缺少 SKILL」当成功。
    return skill_ok, f"{skill_detail}；MCP 已注册且项目受信任（{config_path}）"

