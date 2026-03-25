"""
Package shim.

The project keeps the actual sources in `src/real_estate_scanner/`.
This shim allows running `python -m real_estate_scanner...` from the repo root
without needing to set `PYTHONPATH=src`.
"""

from __future__ import annotations

import pkgutil
import sys
from pathlib import Path

__path__ = pkgutil.extend_path(__path__, __name__)  # type: ignore[name-defined]

_src_pkg = Path(__file__).resolve().parent.parent / "src" / "real_estate_scanner"
if _src_pkg.exists():
    # Make submodules discoverable (e.g. real_estate_scanner.bot.main)
    __path__.append(str(_src_pkg))  # type: ignore[attr-defined]
    sys.path.insert(0, str(_src_pkg.parent))

