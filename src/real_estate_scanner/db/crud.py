from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from real_estate_scanner.db.models import Ad, Filter, User

logger = logging.getLogger(__name__)


async def upsert_user(session: AsyncSession, user_id: int, username: str | None) -> None:
    """
    Добавляет пользователя в `users`.
    Если пользователь уже существует — обновляет `username`.
    """
    try:
        stmt = (
            pg_insert(User)
            .values(id=user_id, username=username)
            .on_conflict_do_update(
                index_elements=[User.id],
                set_={"username": username},
            )
        )
        await session.execute(stmt)
        await session.commit()
    except Exception:
        logger.exception("upsert_user failed (user_id=%s)", user_id)
        await session.rollback()
        raise


async def save_filter(session: AsyncSession, user_id: int, filter_data: dict[str, Any]) -> None:
    """
    Сохраняет фильтр пользователя.

    Реализация "один активный фильтр на пользователя":
    - делаем UPSERT по уникальному `filters.user_id`
    - не создаем новые строки при повторном сохранении
    """
    try:
        additional_params = filter_data.get("additional_params") or {}

        stmt = (
            pg_insert(Filter)
            .values(
                user_id=user_id,
                type=filter_data.get("type"),
                region=filter_data.get("region"),
                cities=filter_data.get("cities") or [],
                rooms=filter_data.get("rooms") or [],
                price_min=filter_data.get("price_min"),
                price_max=filter_data.get("price_max"),
                area_min=filter_data.get("area_min"),
                area_max=filter_data.get("area_max"),
                additional_params=additional_params,
            )
            .on_conflict_do_update(
                index_elements=[Filter.user_id],
                set_={
                    "type": filter_data.get("type"),
                    "region": filter_data.get("region"),
                    "cities": filter_data.get("cities") or [],
                    "rooms": filter_data.get("rooms") or [],
                    "price_min": filter_data.get("price_min"),
                    "price_max": filter_data.get("price_max"),
                    "area_min": filter_data.get("area_min"),
                    "area_max": filter_data.get("area_max"),
                    "additional_params": additional_params,
                },
            )
        )
        await session.execute(stmt)
        await session.commit()
    except Exception:
        logger.exception("save_filter failed (user_id=%s filter_data_keys=%s)", user_id, list(filter_data.keys()))
        await session.rollback()
        raise


async def is_new_ad(session: AsyncSession, olx_id: str) -> bool:
    """Возвращает True, если объявления с `olx_id` ещё нет в таблице `ads`."""
    try:
        stmt = select(Ad.id).where(Ad.olx_id == olx_id).limit(1)
        res = await session.execute(stmt)
        return res.scalar_one_or_none() is None
    except Exception:
        logger.exception("is_new_ad failed (olx_id=%s)", olx_id)
        await session.rollback()
        raise


async def add_ad(session: AsyncSession, ad_data: dict[str, Any]) -> None:
    """
    Сохраняет объявление в `ads`.

    Используем `ON CONFLICT DO NOTHING` по `olx_id`, чтобы избежать ошибок дублей.
    """
    try:
        values: dict[str, Any] = {
            "olx_id": ad_data["olx_id"],
            "price": ad_data["price"],
            "link": ad_data["link"],
            "title": ad_data["title"],
        }
        if "image_url" in ad_data:
            values["image_url"] = ad_data["image_url"]
        if "timestamp" in ad_data and ad_data["timestamp"] is not None:
            values["timestamp"] = ad_data["timestamp"]

        stmt = (
            pg_insert(Ad)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[Ad.olx_id])
        )
        await session.execute(stmt)
        await session.commit()
    except IntegrityError:
        # На случай если уникальный ключ изменится/не применилась конфигурация.
        logger.exception("add_ad integrity error (olx_id=%s)", ad_data.get("olx_id"))
        await session.rollback()
        raise
    except Exception:
        logger.exception("add_ad failed (olx_id=%s)", ad_data.get("olx_id"))
        await session.rollback()
        raise


async def get_users_for_ad(
    session: AsyncSession,
    ad_price: int,
    ad_rooms: int,
    ad_area: float,
    ad_type: str,
    ad_city: str,
) -> list[int]:
    """
    Ищет пользователей, у которых фильтр совпадает с новым объявлением.

    Возвращает список `users.id`.
    """
    try:
        # Numeric(12,2) в БД, поэтому удобно передавать Decimal.
        area = Decimal(str(ad_area))
        stmt = select(User.id, Filter).join(Filter, Filter.user_id == User.id).where(Filter.type == ad_type)
        res = await session.execute(stmt)

        matched_user_ids: list[int] = []
        for user_id, flt in res.all():
            filter_cities = list(flt.cities or [])
            filter_rooms = list(flt.rooms or [])

            city_ok = not filter_cities or ad_city in filter_cities
            rooms_ok = not filter_rooms or ad_rooms in filter_rooms
            price_ok = (flt.price_min is None or flt.price_min <= ad_price) and (
                flt.price_max is None or flt.price_max >= ad_price
            )
            area_ok = (flt.area_min is None or flt.area_min <= area) and (
                flt.area_max is None or flt.area_max >= area
            )

            if city_ok and rooms_ok and price_ok and area_ok:
                matched_user_ids.append(user_id)

        return matched_user_ids
    except Exception:
        logger.exception(
            "get_users_for_ad failed (price=%s rooms=%s area=%s type=%s city=%s)",
            ad_price,
            ad_rooms,
            ad_area,
            ad_type,
            ad_city,
        )
        await session.rollback()
        raise

