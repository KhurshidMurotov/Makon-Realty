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
                city=filter_data.get("city"),
                rooms=filter_data.get("rooms"),
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
                    "city": filter_data.get("city"),
                    "rooms": filter_data.get("rooms"),
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

        rooms_condition = or_(
            Filter.rooms.is_(None),
            and_(Filter.rooms == 5, ad_rooms >= 5),
            and_(Filter.rooms != 5, Filter.rooms == ad_rooms),
        )

        price_min_ok = or_(Filter.price_min.is_(None), Filter.price_min <= ad_price)
        price_max_ok = or_(Filter.price_max.is_(None), Filter.price_max >= ad_price)
        price_condition = and_(price_min_ok, price_max_ok)

        area_min_ok = or_(Filter.area_min.is_(None), Filter.area_min <= area)
        area_max_ok = or_(Filter.area_max.is_(None), Filter.area_max >= area)
        area_condition = and_(area_min_ok, area_max_ok)

        stmt = (
            select(User.id)
            .join(Filter, Filter.user_id == User.id)
            .where(
                Filter.type == ad_type,
                Filter.city == ad_city,
                rooms_condition,
                price_condition,
                area_condition,
            )
        )

        res = await session.execute(stmt)
        return list(res.scalars().all())
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

