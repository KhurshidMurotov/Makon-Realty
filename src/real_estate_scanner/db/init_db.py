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
        await conn.execute(
            text("ALTER TABLE filters ADD COLUMN IF NOT EXISTS cities JSONB NOT NULL DEFAULT '[]'::jsonb")
        )
        await conn.execute(
            text(
                """
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_name = 'filters'
                          AND column_name = 'cities'
                          AND data_type <> 'jsonb'
                    ) THEN
                        ALTER TABLE filters
                        ALTER COLUMN cities TYPE JSONB
                        USING CASE
                            WHEN cities IS NULL THEN '[]'::jsonb
                            ELSE to_jsonb(cities)
                        END;
                    END IF;
                END
                $$;
                """
            )
        )
        await conn.execute(
            text(
                """
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_name = 'filters'
                          AND column_name = 'rooms'
                          AND data_type <> 'jsonb'
                    ) THEN
                        ALTER TABLE filters
                        ALTER COLUMN rooms TYPE JSONB
                        USING CASE
                            WHEN rooms IS NULL THEN '[]'::jsonb
                            ELSE to_jsonb(ARRAY[rooms])
                        END;
                    END IF;
                END
                $$;
                """
            )
        )
        await conn.execute(
            text(
                """
                UPDATE filters
                SET cities = CASE
                    WHEN city IS NULL OR city = '' THEN '[]'::jsonb
                    ELSE to_jsonb(ARRAY[city])
                END
                WHERE cities = '[]'::jsonb
                """
            )
        )
        await conn.execute(
            text(
                """
                ALTER TABLE filters
                ALTER COLUMN cities SET DEFAULT '[]'::jsonb
                """
            )
        )
        await conn.execute(
            text(
                """
                ALTER TABLE filters
                ALTER COLUMN rooms SET DEFAULT '[]'::jsonb
                """
            )
        )

