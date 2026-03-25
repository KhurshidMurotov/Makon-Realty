import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

# Allow running script directly: `python scripts/reset_db.py`
sys.path.append(str(Path(__file__).resolve().parent.parent / "src"))

from real_estate_scanner.db.session import engine  # noqa: E402


async def reset_filters() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE filters CASCADE;"))


if __name__ == "__main__":
    print("Resetting filters table...")
    asyncio.run(reset_filters())
    print("Filters table truncated.")
