from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import update

# Allow running script directly: `python scripts/reset_sale_broadcasts.py`
sys.path.append(str(Path(__file__).resolve().parent.parent / "src"))

from real_estate_scanner.db.init_db import init_db
from real_estate_scanner.db.models import SaleBroadcastState
from real_estate_scanner.db.session import AsyncSessionLocal


async def main() -> None:
    await init_db()
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(SaleBroadcastState).values(
                is_active=False,
                pending_ads=[],
            )
        )
        await session.commit()
    print("sale_broadcast_states reset complete")


if __name__ == "__main__":
    asyncio.run(main())
