from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from real_estate_scanner.config import settings
from real_estate_scanner.db.crud import add_ad, get_users_for_ad, is_new_ad
from real_estate_scanner.db.init_db import init_db
from real_estate_scanner.db.models import Filter, User
from real_estate_scanner.db.session import AsyncSessionLocal
from real_estate_scanner.parser.olx_client import ParsedAd, build_search_url, fetch_ads_from_search

logger = logging.getLogger(__name__)


def _display_city_name(city_slug: str | None) -> str:
    mapping = {
        "tashkent": "Ташкент",
        "mirzoulugbek": "Мирзо-Улугбек",
        "yashnabadskiy": "Яшнабад",
        "yunusabadskiy": "Юнусабад",
        "chilanzarskiy": "Чиланзар",
        "yakkasarayskiy": "Яккасарай",
    }
    return mapping.get(city_slug or "", city_slug or "Ташкент")


def _build_html_notification(ad: ParsedAd) -> str:
    layout = f"{ad.rooms}-комн" if ad.rooms is not None else "—-комн"
    area_value = f"{ad.area:g}" if ad.area is not None else "—"
    district_name = escape(ad.district or _display_city_name(ad.city) or "Район не указан")
    price_usd = int(ad.price / settings.USD_TO_SUM_RATE) if settings.USD_TO_SUM_RATE > 0 else ad.price
    price_per_m2 = round(price_usd / ad.area) if ad.area else 0
    market_price = price_usd
    description_short = escape((ad.title or "").strip() or "Описание не указано")
    author = "Не указан"
    created_at = "Не указано"

    return "\n".join(
        [
            f"{escape(layout)}, {escape(area_value)} m²",
            f"Ташкент, {district_name}",
            f"${price_usd} ({price_per_m2} $/m²)",
            f"Рыночная цена: ${market_price}",
            "",
            description_short,
            "",
            f"От: {author}",
            f"Создано: {created_at}",
            "Источник: OLX.uz",
        ]
    )


async def _get_users_by_price_city_only(
    session: AsyncSession,
    *,
    ad_price: int,
    ad_type: str,
    ad_city: str,
) -> list[int]:
    """
    Матчинг без rooms/area: type + city(район) + price диапазон.
    """
    price_min_ok = or_(Filter.price_min.is_(None), Filter.price_min <= ad_price)
    price_max_ok = or_(Filter.price_max.is_(None), Filter.price_max >= ad_price)
    price_condition = and_(price_min_ok, price_max_ok)

    stmt = select(User.id, Filter).join(Filter, Filter.user_id == User.id).where(Filter.type == ad_type)
    res = await session.execute(stmt)
    matched_user_ids: list[int] = []
    for user_id, flt in res.all():
        filter_cities = list(flt.cities or [])
        city_ok = not filter_cities or ad_city in filter_cities
        if city_ok and (flt.price_min is None or flt.price_min <= ad_price) and (flt.price_max is None or flt.price_max >= ad_price):
            matched_user_ids.append(user_id)
    return matched_user_ids


def _build_inline_link(ad: ParsedAd) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="↗ Перейти к объявлению", url=ad.link),
            ]
        ]
    )


async def _get_distinct_filter_targets_v2(session: AsyncSession) -> list[tuple[str, str, str]]:
    """
    Возвращает уникальные тройки (type, region, city_district) для парсинга.
    """
    stmt = select(Filter.type, Filter.region, Filter.cities).where(
        Filter.type.is_not(None),
        Filter.region.is_not(None),
        Filter.cities.is_not(None),
    ).distinct()
    res = await session.execute(stmt)
    rows = res.all()
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for ad_type, region, cities in rows:
        for city in cities or []:
            if ad_type and region and city:
                item = (ad_type, region, city)
                if item not in seen:
                    seen.add(item)
                    out.append(item)
    return out


async def _notify_users_for_ad(*, bot: Bot, session: AsyncSession, ad: ParsedAd) -> None:
    """
    Предполагается, что объявление новое (olx_id ещё нет в БД).
    """
    logger.info(
        "Проверяю матчинг для объявления %s (Цена: %s, Район: %s)",
        ad.olx_id,
        ad.price,
        ad.district or ad.city,
    )

    if ad.city == "tashkent":
        mismatch_stmt = (
            select(Filter.cities)
            .where(
                Filter.type == ad.ad_type,
                Filter.cities.is_not(None),
            )
            .distinct()
        )
        mismatch_res = await session.execute(mismatch_stmt)
        for wanted_cities in mismatch_res.scalars().all():
            for wanted_city in wanted_cities or []:
                if wanted_city != "tashkent":
                    logger.info("District mismatch: user wants %s, ad is %s", wanted_city, ad.city)

    user_ids: list[int] = []

    # Full match requires rooms+area.
    if ad.rooms is not None and ad.area is not None:
        user_ids = await get_users_for_ad(
            session=session,
            ad_price=ad.price,
            ad_rooms=ad.rooms,
            ad_area=ad.area,
            ad_type=ad.ad_type,
            ad_city=ad.city,
        )

        # If nothing matched, try currency conversion (sum -> usd) for full match too.
        if not user_ids and settings.USD_TO_SUM_RATE > 0:
            converted_price = int(ad.price / settings.USD_TO_SUM_RATE)
            logger.info(
                "Матчинг не найден. Пробую конвертацию: %s сум -> %s USD (rate=%s)",
                ad.price,
                converted_price,
                settings.USD_TO_SUM_RATE,
            )
            user_ids = await get_users_for_ad(
                session=session,
                ad_price=converted_price,
                ad_rooms=ad.rooms,
                ad_area=ad.area,
                ad_type=ad.ad_type,
                ad_city=ad.city,
            )
            if user_ids:
                logger.info("Матчинг успешен после конвертации цены.")

    # Fallback: if rooms is missing, try matching by price + district only.
    elif ad.rooms is None and ad.area is not None:
        logger.info("Fallback matching (rooms missing): olx_id=%s", ad.olx_id)
        user_ids = await _get_users_by_price_city_only(
            session=session,
            ad_price=ad.price,
            ad_type=ad.ad_type,
            ad_city=ad.city,
        )

        if not user_ids and settings.USD_TO_SUM_RATE > 0:
            converted_price = int(ad.price / settings.USD_TO_SUM_RATE)
            logger.info(
                "Fallback: матчинг не найден. Конвертирую цену: %s сум -> %s USD (rate=%s)",
                ad.price,
                converted_price,
                settings.USD_TO_SUM_RATE,
            )
            user_ids = await _get_users_by_price_city_only(
                session=session,
                ad_price=converted_price,
                ad_type=ad.ad_type,
                ad_city=ad.city,
            )
            if user_ids:
                logger.info("Fallback: матчинг успешен после конвертации цены.")

    else:
        # Can't match if area missing.
        logger.info(
            "Не могу матчить olx_id=%s: rooms=%s area=%s",
            ad.olx_id,
            ad.rooms,
            ad.area,
        )
        await add_ad(
            session,
            {
                "olx_id": ad.olx_id,
                "price": ad.price,
                "link": ad.link,
                "title": ad.title,
                "image_url": ad.image_url,
                "timestamp": None,
            },
        )
        return

    if not user_ids:
        logger.info(
            "Нет пользователей с такими фильтрами для olx_id=%s (type=%s city=%s price=%s rooms=%s area=%s).",
            ad.olx_id,
            ad.ad_type,
            ad.city,
            ad.price,
            ad.rooms,
            ad.area,
        )
        # Still persist the ad so we don't process it again.
        await add_ad(
            session,
            {
                "olx_id": ad.olx_id,
                "price": ad.price,
                "link": ad.link,
                "title": ad.title,
                "image_url": ad.image_url,
                "timestamp": None,
            },
        )
        return

    # Save ad and send notifications
    await add_ad(
        session,
        {
            "olx_id": ad.olx_id,
            "price": ad.price,
            "link": ad.link,
            "title": ad.title,
            "image_url": ad.image_url,
            "timestamp": None,
        },
    )

    text = _build_html_notification(ad)
    markup = _build_inline_link(ad)

    for user_id in user_ids:
        try:
            if ad.image_url:
                try:
                    await bot.send_photo(
                        chat_id=user_id,
                        photo=ad.image_url,
                        caption=text,
                        reply_markup=markup,
                        parse_mode="HTML",
                    )
                    continue
                except Exception:
                    logger.exception(
                        "send_photo failed (user_id=%s, olx_id=%s). Fallback to send_message.",
                        user_id,
                        ad.olx_id,
                    )

            await bot.send_message(chat_id=user_id, text=text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            logger.exception("Failed to send ad notification (user_id=%s, olx_id=%s)", user_id, ad.olx_id)


async def run_worker(bot: Bot, *, interval_seconds: int = 600) -> None:
    """
    Бесконечный воркер: каждые 10 минут парсит OLX и отправляет уведомления.
    """
    logger.info("Worker started (interval=%ss)", interval_seconds)

    # Ensure db schema exists (safe no-op if tables already created)
    await init_db()

    while True:
        try:
            async with AsyncSessionLocal() as session:
                targets = await _get_distinct_filter_targets_v2(session)
                logger.info("Worker: targets=%s", targets)

                for ad_type, region_slug, district_city_slug in targets:
                    if region_slug == "tashkent":
                        fetch_city_fallback = "tashkent"
                        if ad_type == "rent":
                            url = f"{settings.OLX_BASE_URL}/nedvizhimost/kvartiry/arenda-dolgosrochnaya/tashkent/"
                        else:
                            url = f"{settings.OLX_BASE_URL}/nedvizhimost/kvartiry/prodazha/tashkent/"
                    else:
                        fetch_city_fallback = district_city_slug
                        url = build_search_url(ad_type=ad_type, city_slug=district_city_slug)
                    logger.info(
                        "Проверяю фильтр: type=%s region=%s city=%s -> url=%s",
                        ad_type,
                        region_slug,
                        district_city_slug,
                        url,
                    )
                    logger.info("ЗАПУСКАЮ ПОИСК ПО URL: %s", url)

                    ads = await fetch_ads_from_search(url=url, ad_type=ad_type, city=fetch_city_fallback)
                    logger.info(
                        "Worker: fetched %s ads for fetch_city=%s (original district=%s)",
                        len(ads),
                        fetch_city_fallback,
                        district_city_slug,
                    )

                    # Determine which ads are new (avoid duplicate processing/notifications).
                    new_ads: list[ParsedAd] = []
                    seen_in_batch: set[str] = set()
                    for ad in ads:
                        if ad.olx_id in seen_in_batch:
                            continue
                        seen_in_batch.add(ad.olx_id)
                        if await is_new_ad(session, ad.olx_id):
                            new_ads.append(ad)

                    logger.info("Найдено новых объявлений: %s", len(new_ads))

                    for ad in new_ads:
                        await _notify_users_for_ad(bot=bot, session=session, ad=ad)

        except asyncio.CancelledError:
            logger.info("Worker cancelled")
            raise
        except Exception:
            logger.exception("Worker loop failed")

        await asyncio.sleep(interval_seconds)

