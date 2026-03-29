from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from real_estate_scanner.db.session import engine


async def main() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE ads RESTART IDENTITY"))
    print("ads table truncated")


if __name__ == "__main__":
    asyncio.run(main())
