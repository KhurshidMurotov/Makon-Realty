from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from real_estate_scanner.db.models import Ad, Filter, SaleBroadcastState, User

logger = logging.getLogger(__name__)
AD_RETENTION_DAYS = 30


def get_ad_retention_cutoff(*, now: datetime | None = None) -> datetime:
    reference = now or datetime.now(timezone.utc)
    return reference - timedelta(days=AD_RETENTION_DAYS)


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


async def get_user_notifications_enabled(session: AsyncSession, user_id: int) -> bool:
    stmt = select(User.notifications_enabled).where(User.id == user_id).limit(1)
    res = await session.execute(stmt)
    value = res.scalar_one_or_none()
    return True if value is None else bool(value)


async def set_user_notifications_enabled(session: AsyncSession, user_id: int, enabled: bool) -> bool:
    stmt = (
        pg_insert(User)
        .values(id=user_id, notifications_enabled=enabled)
        .on_conflict_do_update(
            index_elements=[User.id],
            set_={"notifications_enabled": enabled},
        )
        .returning(User.notifications_enabled)
    )
    res = await session.execute(stmt)
    await session.commit()
    value = res.scalar_one()
    return bool(value)


async def toggle_user_notifications_enabled(session: AsyncSession, user_id: int) -> bool:
    current = await get_user_notifications_enabled(session, user_id)
    return await set_user_notifications_enabled(session, user_id, not current)


async def save_filter(session: AsyncSession, user_id: int, filter_data: dict[str, Any]) -> None:
    """
    Сохраняет фильтр пользователя.

    Реализация "один активный фильтр на пользователя":
    - удаляем старый фильтр пользователя
    - вставляем только один новый актуальный фильтр
    """
    try:
        additional_params = filter_data.get("additional_params") or {}
        cities = list(filter_data.get("cities") or [])
        rooms = list(filter_data.get("rooms") or [])

        await session.execute(delete(Filter).where(Filter.user_id == user_id))

        stmt = (
            pg_insert(Filter)
            .values(
                user_id=user_id,
                type=filter_data.get("type"),
                region=filter_data.get("region"),
                cities=cities,
                rooms=rooms,
                price_min=filter_data.get("price_min"),
                price_max=filter_data.get("price_max"),
                area_min=filter_data.get("area_min"),
                area_max=filter_data.get("area_max"),
                additional_params=additional_params,
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
    ad_area: float | None,
    ad_type: str,
    ad_city: str,
) -> list[int]:
    """
    Ищет пользователей, у которых фильтр совпадает с новым объявлением.

    Возвращает список `users.id`.
    """
    try:
        # Numeric(12,2) в БД, поэтому удобно передавать Decimal.
        area = Decimal(str(ad_area)) if ad_area is not None else None
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
            area_ok = True
            if area is not None:
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


async def get_sale_broadcast_state(session: AsyncSession, user_id: int) -> SaleBroadcastState | None:
    stmt = select(SaleBroadcastState).where(SaleBroadcastState.user_id == user_id)
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def upsert_sale_broadcast_state(
    session: AsyncSession,
    *,
    user_id: int,
    is_active: bool,
    started_at,
    window_start,
    window_end,
    last_batch_at,
    total_found: int,
    pending_ads: list[dict[str, Any]],
    selected_categories: list[str],
    sent_olx_ids: list[str],
    send_interval_seconds: int,
) -> None:
    stmt = (
        pg_insert(SaleBroadcastState)
        .values(
            user_id=user_id,
            is_active=is_active,
            started_at=started_at,
            window_start=window_start,
            window_end=window_end,
            last_batch_at=last_batch_at,
            total_found=total_found,
            pending_ads=pending_ads,
            selected_categories=selected_categories,
            sent_olx_ids=sent_olx_ids,
            send_interval_seconds=send_interval_seconds,
        )
        .on_conflict_do_update(
            index_elements=[SaleBroadcastState.user_id],
            set_={
                "is_active": is_active,
                "started_at": started_at,
                "window_start": window_start,
                "window_end": window_end,
                "last_batch_at": last_batch_at,
                "total_found": total_found,
                "pending_ads": pending_ads,
                "selected_categories": selected_categories,
                "sent_olx_ids": sent_olx_ids,
                "send_interval_seconds": send_interval_seconds,
            },
        )
    )
    await session.execute(stmt)
    await session.commit()


async def list_active_sale_broadcast_states(session: AsyncSession) -> list[SaleBroadcastState]:
    stmt = select(SaleBroadcastState).where(SaleBroadcastState.is_active.is_(True))
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def ad_exists(session: AsyncSession, olx_id: str) -> bool:
    stmt = select(Ad.id).where(Ad.olx_id == olx_id).limit(1)
    res = await session.execute(stmt)
    return res.scalar_one_or_none() is not None


async def ad_scanned_within_hours(session: AsyncSession, olx_id: str, *, hours: int) -> bool:
    threshold = func.now() - text(f"INTERVAL '{int(hours)} hours'")
    stmt = (
        select(Ad.id)
        .where(Ad.olx_id == olx_id)
        .where(Ad.scanned_at >= threshold)
        .limit(1)
    )
    res = await session.execute(stmt)
    return res.scalar_one_or_none() is not None


async def delete_expired_ads(session: AsyncSession, *, cutoff: datetime | None = None) -> int:
    retention_cutoff = cutoff or get_ad_retention_cutoff()
    stmt = delete(Ad).where(
        or_(
            Ad.published_at < retention_cutoff,
            Ad.published_at.is_(None) & (Ad.scanned_at < retention_cutoff),
        )
    )
    result = await session.execute(stmt)
    await session.commit()
    return int(result.rowcount or 0)


async def upsert_scanned_ad(
    session: AsyncSession,
    *,
    olx_id: str,
    title: str,
    price: int,
    currency: str | None,
    published_at,
    url: str,
    category: str,
    raw_details: dict[str, Any],
    image_url: str | None = None,
) -> None:
    stmt = (
        pg_insert(Ad)
        .values(
            olx_id=olx_id,
            title=title,
            price=price,
            currency=currency,
            published_at=published_at,
            url=url,
            category=category,
            raw_details=raw_details,
            scanned_at=func.now(),
            link=url,
            image_url=image_url,
        )
        .on_conflict_do_update(
            index_elements=[Ad.olx_id],
            set_={
                "title": title,
                "price": price,
                "currency": currency,
                "published_at": published_at,
                "url": url,
                "category": category,
                "raw_details": raw_details,
                "scanned_at": func.now(),
                "link": url,
                "image_url": image_url,
            },
        )
    )
    await session.execute(stmt)
    await session.commit()


async def get_recent_ads_raw(
    session: AsyncSession,
    *,
    category: str,
    window_start,
    window_end,
    scanned_since=None,
) -> list[dict[str, Any]]:
    stmt = (
        select(Ad)
        .where(Ad.category == category)
        .where(Ad.published_at.is_not(None))
        .where(Ad.published_at >= window_start)
        .where(Ad.published_at <= window_end)
        .order_by(Ad.published_at.asc())
    )
    if scanned_since is not None:
        stmt = stmt.where(Ad.scanned_at >= scanned_since)
    res = await session.execute(stmt)
    rows = list(res.scalars().all())
    payloads: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row.raw_details or {})
        payload.setdefault("olx_id", row.olx_id)
        payload.setdefault("title", row.title)
        payload.setdefault("price", row.price)
        payload.setdefault("link", row.url or row.link)
        payload.setdefault("image_url", row.image_url)
        payload.setdefault("ad_type", row.category)
        payload.setdefault("published_at", row.published_at.isoformat() if row.published_at else None)
        payload.setdefault("details_loaded", False)
        payloads.append(payload)
    return payloads

