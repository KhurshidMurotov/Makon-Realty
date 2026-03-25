from __future__ import annotations

import random

import pytest
from sqlalchemy import select

from real_estate_scanner.db.crud import save_filter, upsert_user
from real_estate_scanner.db.models import Filter
from real_estate_scanner.db.session import AsyncSessionLocal


@pytest.mark.asyncio
async def test_upsert_user_and_single_active_filter() -> None:
    user_id = random.randint(10_000_000, 99_000_000)
    username = "pytest_user"

    filter_1 = {
        "type": "sale",
        "region": "tashkent",
        "city": "yashnabadskiy",
        "rooms": 2,
        "price_min": 1000000,
        "price_max": 2000000,
        "area_min": 45.0,
        "area_max": 90.0,
        "additional_params": {"name": "first"},
    }

    filter_2 = {
        **filter_1,
        "price_max": 9999999,
        "additional_params": {"name": "second"},
    }

    async with AsyncSessionLocal() as session:
        await upsert_user(session=session, user_id=user_id, username=username)
        await save_filter(session=session, user_id=user_id, filter_data=filter_1)

        res1 = await session.execute(select(Filter).where(Filter.user_id == user_id))
        rows1 = res1.scalars().all()
        assert len(rows1) == 1
        assert rows1[0].price_max == filter_1["price_max"]

        # Save second time; should overwrite existing single active filter.
        await save_filter(session=session, user_id=user_id, filter_data=filter_2)

        # Ensure SQLAlchemy fetches fresh values from DB.
        session.expire_all()

        res2 = await session.execute(select(Filter).where(Filter.user_id == user_id))
        rows2 = res2.scalars().all()
        assert len(rows2) == 1
        assert rows2[0].price_max == filter_2["price_max"]

