from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from real_estate_scanner.config import settings
from real_estate_scanner.db.crud import add_ad, get_users_for_ad, is_new_ad
from real_estate_scanner.db.init_db import init_db
from real_estate_scanner.db.models import Filter
from real_estate_scanner.db.session import AsyncSessionLocal
from real_estate_scanner.parser.olx_client import ParsedAd, build_search_url, fetch_ads_from_search

logger = logging.getLogger(__name__)


def _build_html_notification(ad: ParsedAd) -> str:
    rooms = f"{ad.rooms} комнат" if ad.rooms is not None else "Комнаты: —"
    area_value = f"{ad.area:g} м2" if ad.area is not None else "—"
    district = ad.district or ad.city or "—"

    # Telegram HTML parse mode: keep text escaped, link as anchor.
    safe_title = escape(ad.title or "")
    safe_district = escape(district)
    safe_link = escape(ad.link or "", quote=True)

    return (
        f"🏠 <b>{safe_title}</b>\n"
        f"💰 Цена: <b>{ad.price}</b> сум\n"
        f"📍 Район: {safe_district}\n"
        f"📏 Площадь: {escape(area_value)}\n"
        f"🛏 {escape(rooms)}\n\n"
        f"<a href=\"{safe_link}\">Открыть на OLX</a>"
    )


def _build_inline_link(ad: ParsedAd) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Открыть на OLX", url=ad.link),
            ]
        ]
    )


async def _get_distinct_filter_targets_v2(session: AsyncSession) -> list[tuple[str, str, str]]:
    """
    Возвращает уникальные тройки (type, region, city_district) для парсинга.
    """
    stmt = select(Filter.type, Filter.region, Filter.city).where(
        Filter.type.is_not(None),
        Filter.region.is_not(None),
        Filter.city.is_not(None),
    ).distinct()
    res = await session.execute(stmt)
    rows = res.all()
    out: list[tuple[str, str, str]] = []
    for ad_type, region, city in rows:
        if ad_type and region and city:
            out.append((ad_type, region, city))
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

    if ad.rooms is None or ad.area is None:
        logger.info(
            "Не могу матчить olx_id=%s: rooms=%s area=%s",
            ad.olx_id,
            ad.rooms,
            ad.area,
        )
        # Persist the ad to avoid re-processing, but can't match without rooms/area.
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

    # 1) Try matching by raw OLX price (usually UZS)
    user_ids = await get_users_for_ad(
        session=session,
        ad_price=ad.price,
        ad_rooms=ad.rooms,
        ad_area=ad.area,
        ad_type=ad.ad_type,
        ad_city=ad.city,
    )

    # 2) If nothing matched, try a simple currency conversion (sum -> usd)
    if not user_ids and settings.USD_TO_SUM_RATE > 0:
        converted_price = int(ad.price / settings.USD_TO_SUM_RATE)
        logger.info(
            "Матчинг не найден по цене в суммах. Пробую конвертацию: %s сум -> %s USD (rate=%s)",
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
                    # For OLX URL building (especially for Tashkent region),
                    # use the *city* path (tashkent) not the district slug (yashnabadskiy).
                    if region_slug == "tashkent":
                        url_city_slug = "tashkent"
                        fetch_city_fallback = "tashkent"
                    else:
                        url_city_slug = district_city_slug
                        fetch_city_fallback = district_city_slug

                    url = build_search_url(ad_type=ad_type, city_slug=url_city_slug)
                    logger.info(
                        "Проверяю фильтр: type=%s region=%s city=%s -> url=%s",
                        ad_type,
                        region_slug,
                        district_city_slug,
                        url,
                    )

                    ads = await fetch_ads_from_search(url=url, ad_type=ad_type, city=fetch_city_fallback)
                    logger.info(
                        "Worker: fetched %s ads for url_city=%s (original district=%s)",
                        len(ads),
                        url_city_slug,
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

