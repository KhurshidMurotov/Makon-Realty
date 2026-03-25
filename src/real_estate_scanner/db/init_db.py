from __future__ import annotations

from sqlalchemy import text

from real_estate_scanner.db.models import Base
from real_estate_scanner.db.session import engine


async def init_db() -> None:
    # Creates tables on startup. For production you should replace this with Alembic migrations.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Best-effort schema evolution for local runs (since we currently use create_all).
        # If ads.image_url column is missing, add it.
        await conn.execute(
            text("ALTER TABLE ads ADD COLUMN IF NOT EXISTS image_url TEXT")
        )

