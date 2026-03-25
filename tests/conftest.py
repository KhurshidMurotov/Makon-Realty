from __future__ import annotations

import sys
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    # Ensure `src/` is importable in tests.
    src_path = Path(__file__).resolve().parents[1] / "src"
    sys.path.insert(0, str(src_path))

