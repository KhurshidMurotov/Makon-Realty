import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

# Allow running script directly: `python scripts/reset_ads.py`
sys.path.append(str(Path(__file__).resolve().parent.parent / "src"))

from real_estate_scanner.db.session import engine  # noqa: E402


async def reset_ads() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE ads RESTART IDENTITY CASCADE;"))


if __name__ == "__main__":
    print("Resetting ads table...")
    asyncio.run(reset_ads())
    print("Ads table truncated.")
