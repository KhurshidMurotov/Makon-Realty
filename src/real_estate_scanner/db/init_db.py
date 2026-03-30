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
            text("ALTER TABLE ads ADD COLUMN IF NOT EXISTS currency VARCHAR(16)")
        )
        await conn.execute(
            text("ALTER TABLE ads ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ")
        )
        await conn.execute(
            text("ALTER TABLE ads ADD COLUMN IF NOT EXISTS url TEXT")
        )
        await conn.execute(
            text("ALTER TABLE ads ADD COLUMN IF NOT EXISTS category VARCHAR(64)")
        )
        await conn.execute(
            text("ALTER TABLE ads ADD COLUMN IF NOT EXISTS raw_details JSONB NOT NULL DEFAULT '{}'::jsonb")
        )
        await conn.execute(
            text("ALTER TABLE ads ADD COLUMN IF NOT EXISTS scanned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        )
        await conn.execute(
            text("UPDATE ads SET url = COALESCE(url, link) WHERE url IS NULL")
        )
        await conn.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS ux_ads_url ON ads(url)")
        )
        await conn.execute(
            text("ALTER TABLE filters ADD COLUMN IF NOT EXISTS cities JSONB NOT NULL DEFAULT '[]'::jsonb")
        )
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_access_allowed BOOLEAN NOT NULL DEFAULT FALSE")
        )
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NULL")
        )
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS access_updated_at TIMESTAMPTZ NULL")
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
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_name = 'filters'
                          AND column_name = 'city'
                    ) THEN
                        UPDATE filters
                        SET cities = CASE
                            WHEN city IS NULL OR city = '' THEN '[]'::jsonb
                            ELSE to_jsonb(ARRAY[city])
                        END
                        WHERE cities = '[]'::jsonb;
                    END IF;
                END
                $$;
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
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS sale_broadcast_states (
                    user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    is_active BOOLEAN NOT NULL DEFAULT FALSE,
                    started_at TIMESTAMPTZ NULL,
                    window_start TIMESTAMPTZ NULL,
                    window_end TIMESTAMPTZ NULL,
                    last_batch_at TIMESTAMPTZ NULL,
                    total_found BIGINT NOT NULL DEFAULT 0,
                    pending_ads JSONB NOT NULL DEFAULT '[]'::jsonb,
                    selected_categories JSONB NOT NULL DEFAULT '[]'::jsonb,
                    sent_olx_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
                    send_interval_seconds BIGINT NOT NULL DEFAULT 30
                )
                """
            )
        )
        await conn.execute(
            text("ALTER TABLE sale_broadcast_states ADD COLUMN IF NOT EXISTS selected_categories JSONB NOT NULL DEFAULT '[]'::jsonb")
        )
        await conn.execute(
            text("ALTER TABLE sale_broadcast_states ADD COLUMN IF NOT EXISTS next_category VARCHAR(64)")
        )
        await conn.execute(
            text("ALTER TABLE sale_broadcast_states ADD COLUMN IF NOT EXISTS send_interval_seconds BIGINT NOT NULL DEFAULT 30")
        )

