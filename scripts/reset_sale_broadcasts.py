from __future__ import annotations

import asyncio

from sqlalchemy import update

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
