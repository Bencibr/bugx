# Distribution：分发与打包说明

> **定位**：发布到包管理器时**附带给使用者**的说明文件。
> **上级索引**：[docs/README.md](../../docs/README.md) ｜ **规范**：[02_architecture/07_DOC_GOVERNANCE_STANDARD.md](../02_architecture/07_DOC_GOVERNANCE_STANDARD.md)

## 内容

| 文件 | 分发目标 | 被谁使用 |
|---|---|---|
| `02_README_PYPI.md` | Python (PyPI) | `scripts/package_dist.py` 打包时作为包描述 |
| `01_README_NPM.md` | Node.js (npm) | `scripts/package_dist.py` 打包时作为包描述 |

## 维护规则

- 这两个文件**会随发行包发布**，属公开层：**严禁**出现内部工单编号或未实现规划。
- 修改打包脚本（`scripts/package_dist.py`）引用的文件名时，必须同步本目录与 README 索引。
