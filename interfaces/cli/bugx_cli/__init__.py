"""BugX installable command package."""

import os

__version__ = os.environ.get("BUGX_VERSION", "0.1.6")
__build_id__: int = 1