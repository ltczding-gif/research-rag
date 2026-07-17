"""
Shared pytest fixtures.

Adds the `scanner/` directory to sys.path so test modules can
`import _hashing`, `from backends import make_backend`, etc.
without needing the package to be installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCANNER_DIR = _REPO_ROOT / "scanner"

if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))
