"""core/ledger - 错题集与经验账本模块化子系统 (DOC-036 / DOC-072)。"""

from __future__ import annotations

import sys
from pathlib import Path

_CURRENT_DIR = Path(__file__).resolve().parent
_CORE_DIR = _CURRENT_DIR.parent
_REPO_ROOT = _CORE_DIR.parent
for _p in (str(_REPO_ROOT), str(_CORE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from . import schema
    from . import patterns
    from . import incidents
    from . import sync_ops
    from . import archive_ops
    from . import export_ops
    from . import tool_experience
    from . import cli
    from . import finding_confirmation
    from . import finding_io
    from . import finding_lifecycle
    from . import finding_verification
    from . import lesson_replay
    from . import life_ledger
    from . import verify_life
    from . import receipt_contract
except (ImportError, ValueError):
    from core.ledger import schema
    from core.ledger import patterns
    from core.ledger import incidents
    from core.ledger import sync_ops
    from core.ledger import archive_ops
    from core.ledger import export_ops
    from core.ledger import tool_experience
    from core.ledger import cli
    from core.ledger import finding_confirmation
    from core.ledger import finding_io
    from core.ledger import finding_lifecycle
    from core.ledger import finding_verification
    from core.ledger import lesson_replay
    from core.ledger import life_ledger
    from core.ledger import verify_life
    from core.ledger import receipt_contract

__all__ = [
    "schema",
    "patterns",
    "incidents",
    "sync_ops",
    "archive_ops",
    "export_ops",
    "tool_experience",
    "cli",
    "finding_confirmation",
    "finding_io",
    "finding_lifecycle",
    "finding_verification",
    "lesson_replay",
    "life_ledger",
    "verify_life",
    "receipt_contract",
]
