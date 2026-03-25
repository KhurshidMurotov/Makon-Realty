from __future__ import annotations

from real_estate_scanner.db.models import Base
from real_estate_scanner.db.session import engine


async def init_db() -> None:
    # Creates tables on startup. For production you should replace this with Alembic migrations.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

