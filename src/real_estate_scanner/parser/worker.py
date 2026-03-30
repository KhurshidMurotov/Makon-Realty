from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from sqlalchemy.ext.asyncio import AsyncSession

from real_estate_scanner.db.crud import (
    ad_scanned_within_hours,
    ad_exists,
    delete_expired_ads,
    get_recent_ads_raw,
    get_sale_broadcast_state,
    list_active_sale_broadcast_states,
    upsert_scanned_ad,
    upsert_sale_broadcast_state,
)
from real_estate_scanner.db.init_db import init_db
from real_estate_scanner.db.models import SaleBroadcastState
from real_estate_scanner.db.session import AsyncSessionLocal
from real_estate_scanner.parser.olx_client import (
    ParsedAd,
    detect_last_page_for_search,
    enrich_ad_with_details,
    fetch_ads_from_search,
)

logger = logging.getLogger(__name__)
_LOCAL_TZ = ZoneInfo("Asia/Tashkent")

APARTMENTS_BUTTON = "\u041a\u0432\u0430\u0440\u0442\u0438\u0440\u044b | \u041f\u0440\u043e\u0434\u0430\u0436\u0430 | \u0422\u0430\u0448\u043a\u0435\u043d\u0442"
STOP_BUTTON = "\u0421\u0442\u043e\u043f"

APARTMENTS_SALE_URL = "https://www.olx.uz/nedvizhimost/kvartiry/prodazha/tashkent/?currency=UYE"
COMMERCIAL_SALE_URL = "https://www.olx.uz/nedvizhimost/kommercheskie-pomeshcheniya/prodazha/tashkent/?currency=UYE"
COMMERCIAL_RENT_URL = "https://www.olx.uz/nedvizhimost/kommercheskie-pomeshcheniya/arenda/tashkent/?currency=UYE"
SEND_INTERVAL_SECONDS = 30
WORKER_POLL_INTERVAL_SECONDS = 5
MAX_CAPTION = 500
SALE_BUCKET_MAX_PRICE = 5_000_000
COMMERCIAL_RENT_BUCKET_MAX_PRICE = 20_000
SCRAPING_INTERVAL_SECONDS = 40 * 60
BROADCAST_RECENT_SEEN_HOURS = 24
PAGE_SCAN_MAX_PAGE = 25
PAGE_SCAN_MIN_PAGE = 1
PAGE_SCAN_DELAY_SECONDS = 4
FEED_SWITCH_DELAY_SECONDS = 6
DEEP_SCAN_START_PAGE = PAGE_SCAN_MAX_PAGE
DEEP_SCAN_WINDOW_PAGES = 1
DEEP_SCAN_PAGE_DELAY_SECONDS = PAGE_SCAN_DELAY_SECONDS
is_initial_scan = True


def is_initial_scan_active() -> bool:
    return is_initial_scan


def finish_initial_scan() -> None:
    global is_initial_scan
    if not is_initial_scan:
        return
    is_initial_scan = False
    logger.info("[SYSTEM] Первый круг завершен. База наполнена. Включаю режим уведомлений.")


def _display_city_name(city_slug: str | None) -> str:
    mapping = {
        "tashkent": "\u0422\u0430\u0448\u043a\u0435\u043d\u0442",
        "mirzoulugbek": "\u041c\u0438\u0440\u0437\u043e-\u0423\u043b\u0443\u0433\u0431\u0435\u043a",
        "yashnabadskiy": "\u042f\u0448\u043d\u0430\u0431\u0430\u0434",
        "yunusabadskiy": "\u042e\u043d\u0443\u0441\u0430\u0431\u0430\u0434",
        "chilanzarskiy": "\u0427\u0438\u043b\u0430\u043d\u0437\u0430\u0440",
        "yakkasarayskiy": "\u042f\u043a\u043a\u0430\u0441\u0430\u0440\u0430\u0439",
        "mirabadskiy": "\u041c\u0438\u0440\u0430\u0431\u0430\u0434",
        "almazarskiy": "\u0410\u043b\u043c\u0430\u0437\u0430\u0440",
        "uchtepinskiy": "\u0423\u0447\u0442\u0435\u043f\u0430",
        "sergeli": "\u0421\u0435\u0440\u0433\u0435\u043b\u0438",
    }
    return mapping.get(city_slug or "", city_slug or "\u0422\u0430\u0448\u043a\u0435\u043d\u0442")


def _format_sum_price(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " y.e"


def _looks_like_photo_url(url: str | None) -> bool:
    if not url:
        return False
    normalized = url.lower()
    return any(token in normalized for token in (".jpg", ".jpeg", ".png", ".webp", "/image;", "/files/"))


def _describe_ad_type(ad_type: str) -> str:
    mapping = {
        "sale": "\u041a\u0432\u0430\u0440\u0442\u0438\u0440\u044b | \u041f\u0440\u043e\u0434\u0430\u0436\u0430",
        "commercial_sale": "\u041a\u043e\u043c\u043c\u0435\u0440\u0446\u0438\u044f | \u041f\u0440\u043e\u0434\u0430\u0436\u0430",
        "commercial_rent": "\u041a\u043e\u043c\u043c\u0435\u0440\u0446\u0438\u044f | \u0410\u0440\u0435\u043d\u0434\u0430",
    }
    return mapping.get(ad_type, ad_type)


def _format_created_at_for_notification(ad: ParsedAd) -> str:
    if ad.published_at is not None:
        created_at = ad.published_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=_LOCAL_TZ)
        else:
            created_at = created_at.astimezone(_LOCAL_TZ)
        if created_at.hour == 0 and created_at.minute == 0:
            return created_at.strftime("%d.%m.%Y")
        return created_at.strftime("%d.%m.%Y %H:%M")
    return ad.created_at_text or "\u041d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e"


def _build_html_notification(ad: ParsedAd) -> str:
    rooms_value = str(ad.rooms) if ad.rooms is not None else "-"
    floor_value = str(ad.floor) if ad.floor is not None else "-"
    total_floors_value = str(ad.total_floors) if ad.total_floors is not None else "-"
    area_value = f"{ad.area:g}" if ad.area is not None else "-"
    district_name = escape(ad.district or _display_city_name(ad.city) or "\u0420\u0430\u0439\u043e\u043d \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d")
    raw_description = (ad.description or ad.title or "").strip() or "\u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e"
    if raw_description.casefold().startswith("\u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435"):
        raw_description = raw_description[len("\u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435") :].lstrip(" :\n\r\t-")
    description = escape(raw_description).replace("\n", "<br>")
    author = escape(ad.author_name or "\u041d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d")
    owner_type = escape(ad.owner_type or "\u041d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e")
    created_at = escape(_format_created_at_for_notification(ad))
    seller_phone = escape(ad.seller_phone) if ad.seller_phone else None
    ad_type_label = escape(_describe_ad_type(ad.ad_type))

    return "\n".join(
        [
            ad_type_label,
            f"{rooms_value}/{floor_value}/{total_floors_value}, {escape(area_value)} \u043c\u00b2",
            f"\u0422\u0430\u0448\u043a\u0435\u043d\u0442, {district_name}",
            escape(_format_sum_price(ad.price)),
            "",
            owner_type,
            "\u041e\u041f\u0418\u0421\u0410\u041d\u0418\u0415",
            description,
            "",
            f"\u041e\u0442: {author}",
            f"\u0421\u043e\u0437\u0434\u0430\u043d\u043e: {created_at}",
            f"\u0422\u0435\u043b\u0435\u0444\u043e\u043d: {seller_phone}" if seller_phone else "",
            "\u0418\u0441\u0442\u043e\u0447\u043d\u0438\u043a: OLX.uz",
        ]
    )


def _get_valid_photo_urls(ad: ParsedAd) -> list[str]:
    urls = list(ad.image_urls or [])
    if ad.image_url and ad.image_url not in urls:
        urls.insert(0, ad.image_url)
    return [url for url in urls if _looks_like_photo_url(url)]


def clean_text(text: str) -> str:
    if not text:
        return text

    text = text.replace("<br>", "\n")
    text = text.replace("<br/>", "\n")
    text = text.replace("<br />", "\n")
    text = re.sub(r"<.*?>", "", text)
    return text.strip()


def remove_links(text: str) -> str:
    return re.sub(r"https?://\S+", "", text).strip()


def smart_truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    truncated = text[:limit].rstrip()
    last_space = truncated.rfind(" ")
    if last_space > max(0, limit - 80):
        truncated = truncated[:last_space].rstrip()
    return truncated.rstrip(".,;:!- ") + "..."


async def _send_ad_payload(*, bot: Bot, chat_id: int, ad: ParsedAd, text: str, markup: InlineKeyboardMarkup) -> None:
    text = clean_text(text)
    text = remove_links(text)
    if not text:
        text = "Описание не указано"

    photo_urls = _get_valid_photo_urls(ad)
    if photo_urls:
        if len(photo_urls) == 1:
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo_urls[0],
            )
        else:
            for start in range(0, len(photo_urls), 10):
                chunk = photo_urls[start : start + 10]
                media = [InputMediaPhoto(media=url) for url in chunk]
                await bot.send_media_group(chat_id=chat_id, media=media)
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=markup,
        )
        return

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=markup,
    )


def iter_sale_price_buckets() -> list[tuple[int, int]]:
    buckets: list[tuple[int, int]] = [
        (0, 20_000),
        (20_000, 30_000),
        (30_000, 40_000),
        (40_000, 44_000),
        (44_000, 46_000),
        (46_000, 50_000),
    ]
    current_from = 50_000
    while current_from < 150_000:
        current_to = min(current_from + 1_000, 150_000)
        buckets.append((current_from, current_to))
        current_from = current_to
    buckets.extend(
        [
            (150_000, 154_000),
            (154_000, 160_000),
            (160_000, 164_000),
            (164_000, 170_000),
            (170_000, 180_000),
            (180_000, 190_000),
            (190_000, 200_000),
        ]
    )
    current_from = 200_000
    while current_from < 300_000:
        current_to = min(current_from + 10_000, 300_000)
        buckets.append((current_from, current_to))
        current_from = current_to
    buckets.extend(
        [
            (300_000, 350_000),
            (350_000, 400_000),
            (400_000, 700_000),
            (700_000, 1_000_000),
        ]
    )
    return buckets


def iter_commercial_rent_price_buckets() -> list[tuple[int, int]]:
    buckets: list[tuple[int, int]] = [
        (0, 10),
        (10, 30),
        (30, 100),
        (100, 200),
        (200, 500),
        (500, 900),
        (900, 1400),
        (1400, 1900),
    ]
    current_from = 1900
    while current_from < COMMERCIAL_RENT_BUCKET_MAX_PRICE:
        current_to = min(current_from + 500, COMMERCIAL_RENT_BUCKET_MAX_PRICE)
        buckets.append((current_from, current_to))
        current_from = current_to
    return buckets


def _dedupe_ads_by_olx_id(ads: list[ParsedAd]) -> list[ParsedAd]:
    unique: dict[str, ParsedAd] = {}
    for ad in ads:
        existing = unique.get(ad.olx_id)
        if existing is None:
            unique[ad.olx_id] = ad
            continue
        existing_published = existing.published_at or datetime.min.replace(tzinfo=_LOCAL_TZ)
        current_published = ad.published_at or datetime.min.replace(tzinfo=_LOCAL_TZ)
        if current_published >= existing_published:
            unique[ad.olx_id] = ad
    return list(unique.values())


def _merge_snapshot_ads(
    *,
    cached_ads: list[ParsedAd],
    fresh_ads: list[ParsedAd],
    window_start: datetime,
    window_end: datetime,
) -> list[ParsedAd]:
    cached_ads = cached_ads or []
    fresh_ads = fresh_ads or []
    merged = _dedupe_ads_by_olx_id([*cached_ads, *fresh_ads])
    filtered = [
        ad
        for ad in merged
        if ad.published_at is not None and window_start <= ad.published_at <= window_end
    ]
    filtered.sort(key=lambda item: item.published_at or window_start)
    return filtered


async def _cleanup_expired_db_ads() -> int:
    async with AsyncSessionLocal() as session:
        deleted_count = await delete_expired_ads(session)
    if deleted_count:
        logger.info("[CLEANUP] Deleted %s ads older than 30 days from DB.", deleted_count)
    return deleted_count


async def _scan_feed_page(
    *,
    session: AsyncSession,
    url: str,
    ad_type: str,
    city: str,
    page_number: int,
    start_page: int,
    window_start: datetime,
    window_end: datetime,
    per_page_limit: int,
    known_ids: set[str],
) -> list[ParsedAd]:
    collected: list[ParsedAd] = []
    page_ads = await fetch_ads_from_search(
        url=url,
        ad_type=ad_type,
        city=city,
        limit=per_page_limit,
        page_number=page_number,
    )
    if not page_ads:
        logger.info("OLX feed page returned no ads: ad_type=%s page=%s", ad_type, page_number)
        await asyncio.sleep(PAGE_SCAN_DELAY_SECONDS)
        return collected

    logger.info(
        "[SCAN] Feed %s page %s/%s: found=%s",
        ad_type,
        page_number,
        start_page,
        len(page_ads),
    )

    page_saved_count = 0
    page_skip_already_in_db = 0
    page_skip_scanned_24h = 0
    page_skip_older_than_window = 0
    total_on_page = len(page_ads)

    for index, ad in enumerate(page_ads, start=1):
        if ad.olx_id in known_ids:
            page_skip_already_in_db += 1
            continue

        if await ad_scanned_within_hours(session, ad.olx_id, hours=24):
            page_skip_scanned_24h += 1
            known_ids.add(ad.olx_id)
            continue

        if ad.published_at is not None and (
            ad.published_at < window_start or ad.published_at > window_end
        ):
            page_skip_older_than_window += 1
            continue

        listing_ad = replace(ad, details_loaded=False)
        collected.append(listing_ad)
        known_ids.add(listing_ad.olx_id)
        await upsert_scanned_ad(
            session,
            olx_id=listing_ad.olx_id,
            title=listing_ad.title,
            price=listing_ad.price,
            currency="UYE",
            published_at=listing_ad.published_at,
            url=listing_ad.link,
            category=listing_ad.ad_type,
            raw_details=_serialize_parsed_ad(listing_ad),
            image_url=listing_ad.image_url,
        )
        page_saved_count += 1
        logger.info(
            "[SAVE] draft %s/%s id=%s page=%s ad_type=%s",
            index,
            total_on_page,
            listing_ad.olx_id,
            page_number,
            ad_type,
        )

    logger.info(
        "[PAGE] summary: saved=%s skipped_db=%s skipped_24h=%s skipped_old=%s ad_type=%s page=%s",
        page_saved_count,
        page_skip_already_in_db,
        page_skip_scanned_24h,
        page_skip_older_than_window,
        ad_type,
        page_number,
    )
    await asyncio.sleep(PAGE_SCAN_DELAY_SECONDS)
    return collected


async def _collect_ads_for_feed_pages(
    *,
    url: str,
    ad_type: str,
    city: str,
    window_start: datetime,
    window_end: datetime,
    per_page_limit: int = 60,
    known_olx_ids: set[str] | None = None,
) -> list[ParsedAd]:
    collected: list[ParsedAd] = []
    known_ids = set(known_olx_ids or set())

    last_page = await detect_last_page_for_search(url=url, max_page=PAGE_SCAN_MAX_PAGE)
    if last_page is None:
        logger.info("OLX feed skipped by empty first page: ad_type=%s", ad_type)
        return collected

    start_page = max(PAGE_SCAN_MIN_PAGE, min(int(last_page), PAGE_SCAN_MAX_PAGE))
    logger.info(
        "[SCAN] Feed %s: scanning pages %s -> %s without price buckets.",
        ad_type,
        start_page,
        PAGE_SCAN_MIN_PAGE,
    )

    async with AsyncSessionLocal() as session:
        for page_number in range(start_page, PAGE_SCAN_MIN_PAGE - 1, -1):
            collected.extend(
                await _scan_feed_page(
                    session=session,
                    url=url,
                    ad_type=ad_type,
                    city=city,
                    page_number=page_number,
                    start_page=start_page,
                    window_start=window_start,
                    window_end=window_end,
                    per_page_limit=per_page_limit,
                    known_ids=known_ids,
                )
            )

    await asyncio.sleep(FEED_SWITCH_DELAY_SECONDS)
    return collected


async def _run_interleaved_scraper_cycle(*, per_page_limit: int = 60) -> None:
    window_end = datetime.now(_LOCAL_TZ)
    window_start = window_end - timedelta(days=30)
    await _cleanup_expired_db_ads()

    feeds = [
        ("sale", APARTMENTS_SALE_URL, "tashkent"),
        ("commercial_sale", COMMERCIAL_SALE_URL, "tashkent"),
        ("commercial_rent", COMMERCIAL_RENT_URL, "tashkent"),
    ]

    async with AsyncSessionLocal() as session:
        known_ids_by_feed: dict[str, set[str]] = {}
        start_pages: dict[str, int] = {}

        for ad_type, url, _city in feeds:
            cached_payloads = await get_recent_ads_raw(
                session,
                category=ad_type,
                window_start=window_start,
                window_end=window_end,
            )
            known_ids_by_feed[ad_type] = {
                payload.get("olx_id")
                for payload in cached_payloads
                if payload.get("olx_id")
            }

            last_page = await detect_last_page_for_search(url=url, max_page=PAGE_SCAN_MAX_PAGE)
            if last_page is None:
                logger.info("OLX feed skipped by empty first page: ad_type=%s", ad_type)
                continue

            start_page = max(PAGE_SCAN_MIN_PAGE, min(int(last_page), PAGE_SCAN_MAX_PAGE))
            start_pages[ad_type] = start_page
            logger.info(
                "[SCAN] Feed %s: scheduled interleaved pages %s -> %s.",
                ad_type,
                start_page,
                PAGE_SCAN_MIN_PAGE,
            )

        if not start_pages:
            logger.info("Scraper cycle found no available OLX feeds to scan.")
            return

        max_start_page = max(start_pages.values())
        logger.info(
            "[SCAN] Interleaved scraper cycle started: pages %s -> %s across %s feeds.",
            max_start_page,
            PAGE_SCAN_MIN_PAGE,
            len(start_pages),
        )

        for page_number in range(max_start_page, PAGE_SCAN_MIN_PAGE - 1, -1):
            active_feeds = [
                (ad_type, url, city)
                for ad_type, url, city in feeds
                if start_pages.get(ad_type, 0) >= page_number
            ]
            if not active_feeds:
                continue

            logger.info(
                "[SCAN] Interleaved page %s: switching through %s feed(s).",
                page_number,
                len(active_feeds),
            )

            for index, (ad_type, url, city) in enumerate(active_feeds, start=1):
                await _scan_feed_page(
                    session=session,
                    url=url,
                    ad_type=ad_type,
                    city=city,
                    page_number=page_number,
                    start_page=start_pages[ad_type],
                    window_start=window_start,
                    window_end=window_end,
                    per_page_limit=per_page_limit,
                    known_ids=known_ids_by_feed.setdefault(ad_type, set()),
                )
                if index < len(active_feeds):
                    await asyncio.sleep(FEED_SWITCH_DELAY_SECONDS)

    if is_initial_scan:
        finish_initial_scan()


async def _collect_ads_for_price_buckets(
    *,
    url: str,
    ad_type: str,
    city: str,
    buckets: list[tuple[int, int]],
    window_start: datetime,
    window_end: datetime,
    per_page_limit: int = 60,
    known_olx_ids: set[str] | None = None,
) -> list[ParsedAd]:
    collected: list[ParsedAd] = []
    known_ids = set(known_olx_ids or set())

    async with AsyncSessionLocal() as session:
        for price_from, price_to in buckets:
            logger.info(
                "Collecting OLX bucket: ad_type=%s price_from=%s price_to=%s",
                ad_type,
                price_from,
                price_to,
            )

            target_page = await detect_last_page_for_search(
                url=url,
                price_from=price_from,
                price_to=price_to,
                max_page=DEEP_SCAN_START_PAGE,
            )

            if target_page is None:
                logger.info(
                    "OLX bucket skipped by empty first page: ad_type=%s price_from=%s price_to=%s",
                    ad_type,
                    price_from,
                    price_to,
                )
                continue

            end_page = max(1, target_page - DEEP_SCAN_WINDOW_PAGES + 1)
            logger.info(
                "[SCAN] ? ?????? %s-%s ??????? ????? %s ???????. ???????? ?? ????????? (%s/%s).",
                price_from,
                price_to,
                target_page,
                target_page,
                target_page,
            )
            logger.info(
                "[DEEP_SCAN] ??????? ???? ???? ?? 1 ???????? ? ????? (Page %s -> %s) ??? ???? %s-%s.",
                target_page,
                end_page,
                price_from,
                price_to,
            )

            for page_number in range(target_page, end_page - 1, -1):
                page_ads = await fetch_ads_from_search(
                    url=url,
                    ad_type=ad_type,
                    city=city,
                    limit=per_page_limit,
                    price_from=price_from,
                    price_to=price_to,
                    page_number=page_number,
                )
                if not page_ads:
                    logger.info(
                        "OLX bucket exhausted: ad_type=%s price_from=%s price_to=%s page=%s",
                        ad_type,
                        price_from,
                        price_to,
                        page_number,
                    )
                    break

                logger.info(
                    "[SCAN] Найдено %s потенциальных объявлений на странице. ad_type=%s price_from=%s price_to=%s page=%s",
                    len(page_ads),
                    ad_type,
                    price_from,
                    price_to,
                    page_number,
                )

                has_older_than_window = False
                total_on_page = len(page_ads)
                for index, ad in enumerate(page_ads, start=1):
                    if ad.olx_id in known_ids:
                        logger.info("[SKIP] ID %s пропущено, причина: already_in_db", ad.olx_id)
                        continue

                    if await ad_scanned_within_hours(session, ad.olx_id, hours=24):
                        logger.info("[SKIP] ID %s пропущено, причина: scanned_within_24h", ad.olx_id)
                        known_ids.add(ad.olx_id)
                        continue

                    if ad.published_at is not None and ad.published_at < window_start:
                        logger.info("[SKIP] ID %s пропущено, причина: older_than_window", ad.olx_id)
                        has_older_than_window = True
                        break

                    logger.info("[PROCESS] Захожу внутрь (%s/%s): %s", index, total_on_page, ad.link)
                    try:
                        detailed_ad = await enrich_ad_with_details(ad)
                    except Exception:
                        logger.info("[SKIP] ID %s пропущено, причина: enrich_failed", ad.olx_id)
                        logger.exception("bucket enrich failed (ad_type=%s olx_id=%s)", ad_type, ad.olx_id)
                        detailed_ad = ad

                    detailed_ad = replace(detailed_ad, details_loaded=True)
                    logger.info(
                        "[VALIDATE] %s успешно (Площадь: %s, Комнат: %s, Фото: %s шт).",
                        detailed_ad.olx_id,
                        detailed_ad.area,
                        detailed_ad.rooms,
                        len(detailed_ad.image_urls or ([detailed_ad.image_url] if detailed_ad.image_url else [])),
                    )
                    if not detailed_ad.image_urls and not detailed_ad.image_url:
                        logger.warning("[LOW_QUALITY] Объявление %s сохранено без фотографий", detailed_ad.olx_id)
                    collected.append(detailed_ad)
                    known_ids.add(detailed_ad.olx_id)
                    await upsert_scanned_ad(
                        session,
                        olx_id=detailed_ad.olx_id,
                        title=detailed_ad.title,
                        price=detailed_ad.price,
                        currency="UYE",
                        published_at=detailed_ad.published_at,
                        url=detailed_ad.link,
                        category=detailed_ad.ad_type,
                        raw_details=_serialize_parsed_ad(detailed_ad),
                        image_url=detailed_ad.image_url,
                    )
                    logger.info("[DB] Успешный Upsert в PostgreSQL для ID %s.", detailed_ad.olx_id)
                    logger.info("[PERF] ID %s полностью обработан и сохранен.", detailed_ad.olx_id)

                if has_older_than_window:
                    logger.info(
                        "OLX bucket stopped by older ad: ad_type=%s price_from=%s price_to=%s page=%s",
                        ad_type,
                        price_from,
                        price_to,
                        page_number,
                    )
                    break

                if page_number > end_page:
                    await asyncio.sleep(DEEP_SCAN_PAGE_DELAY_SECONDS)



async def _send_ad_payload_with_retry(
    *,
    bot: Bot,
    chat_id: int,
    ad: ParsedAd,
    text: str,
    markup: InlineKeyboardMarkup,
) -> None:
    delays = (1, 2, 4)
    last_error: Exception | None = None
    for attempt, delay in enumerate(delays, start=1):
        try:
            await _send_ad_payload(
                bot=bot,
                chat_id=chat_id,
                ad=ad,
                text=text,
                markup=markup,
            )
            return
        except TelegramBadRequest:
            raise
        except (TelegramNetworkError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = exc
            if attempt == len(delays):
                break
            logger.warning(
                "Telegram send retry scheduled (attempt=%s/%s, user_id=%s, olx_id=%s): %s",
                attempt,
                len(delays),
                chat_id,
                ad.olx_id,
                exc,
            )
            await asyncio.sleep(delay)

    assert last_error is not None
    raise last_error


def _build_inline_link(ad: ParsedAd) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="\u2197 \u041f\u0435\u0440\u0435\u0439\u0442\u0438 \u043a \u043e\u0431\u044a\u044f\u0432\u043b\u0435\u043d\u0438\u044e", url=ad.link),
            ]
        ]
    )


def _serialize_parsed_ad(ad: ParsedAd) -> dict:
    payload = asdict(ad)
    payload["published_at"] = ad.published_at.isoformat() if ad.published_at else None
    return payload


def _deserialize_parsed_ad(payload: dict) -> ParsedAd:
    payload = dict(payload)
    payload.pop("retry_count", None)
    published_at = payload.get("published_at")
    if isinstance(published_at, str):
        try:
            payload["published_at"] = datetime.fromisoformat(published_at)
        except ValueError:
            payload["published_at"] = None
    return ParsedAd(**payload)


def _deserialize_db_parsed_ad(payload: dict) -> ParsedAd:
    data = dict(payload)
    published_at = data.get("published_at")
    if isinstance(published_at, str):
        try:
            data["published_at"] = datetime.fromisoformat(published_at)
        except ValueError:
            data["published_at"] = None
    data.setdefault("details_loaded", True)
    return ParsedAd(**data)


def _broadcast_payload_sort_key(payload: dict, fallback: datetime) -> datetime:
    for key in ("published_at", "scanned_at", "timestamp"):
        value = payload.get(key)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                continue
    return fallback


def _normalize_broadcast_categories(categories: list[str] | None) -> list[str]:
    category_order = ("sale", "commercial_sale", "commercial_rent")
    selected = set(categories or [])
    return [category for category in category_order if category in selected]


def _interleave_broadcast_payloads(payloads_by_category: dict[str, list[dict]]) -> list[dict]:
    category_order = ("sale", "commercial_sale", "commercial_rent")
    queues = {category: list(payloads_by_category.get(category, [])) for category in category_order}
    merged: list[dict] = []
    while any(queues[category] for category in category_order):
        for category in category_order:
            if queues[category]:
                merged.append(queues[category].pop(0))
    return merged


async def _refresh_pending_broadcast_queue(
    session: AsyncSession,
    state: SaleBroadcastState,
) -> tuple[list[dict], int]:
    window_end = datetime.now(_LOCAL_TZ)
    window_start = window_end - timedelta(days=30)
    scanned_since = window_end - timedelta(hours=BROADCAST_RECENT_SEEN_HOURS)
    sent_ids = set(state.sent_olx_ids or [])
    payloads_by_category: dict[str, list[dict]] = {}

    for category in _normalize_broadcast_categories(list(state.selected_categories or [])):
        rows = await get_recent_ads_raw(
            session,
            category=category,
            window_start=window_start,
            window_end=window_end,
            scanned_since=scanned_since,
        )
        category_payloads: list[dict] = []
        for payload in rows:
            olx_id = payload.get("olx_id")
            if not olx_id or olx_id in sent_ids:
                continue
            prepared_payload = dict(payload)
            prepared_payload["details_loaded"] = False
            category_payloads.append(prepared_payload)
        category_payloads.sort(key=lambda item: _broadcast_payload_sort_key(item, window_start))
        payloads_by_category[category] = category_payloads

    refreshed_queue = _interleave_broadcast_payloads(payloads_by_category)
    return refreshed_queue, len(refreshed_queue)


async def build_apartments_sale_snapshot(*, limit: int = 200) -> list[ParsedAd]:
    window_end = datetime.now(_LOCAL_TZ)
    window_start = window_end - timedelta(days=30)
    await _cleanup_expired_db_ads()
    async with AsyncSessionLocal() as session:
        cached_payloads = await get_recent_ads_raw(
            session,
            category="sale",
            window_start=window_start,
            window_end=window_end,
        )
    cached_ads = [_deserialize_db_parsed_ad(payload) for payload in cached_payloads]
    cached_ids = {ad.olx_id for ad in cached_ads}
    fresh_ads = await _collect_ads_for_feed_pages(
        url=APARTMENTS_SALE_URL,
        ad_type="sale",
        city="tashkent",
        window_start=window_start,
        window_end=window_end,
        per_page_limit=min(limit, 60),
        known_olx_ids=cached_ids,
    )
    if is_initial_scan:
        finish_initial_scan()
    return _merge_snapshot_ads(
        cached_ads=cached_ads,
        fresh_ads=fresh_ads,
        window_start=window_start,
        window_end=window_end,
    ) or []


async def build_commercial_snapshot(*, limit_per_feed: int = 200) -> tuple[list[ParsedAd], list[ParsedAd]]:
    window_end = datetime.now(_LOCAL_TZ)
    window_start = window_end - timedelta(days=30)
    await _cleanup_expired_db_ads()
    async with AsyncSessionLocal() as session:
        cached_sale_payloads = await get_recent_ads_raw(
            session,
            category="commercial_sale",
            window_start=window_start,
            window_end=window_end,
        )
        cached_rent_payloads = await get_recent_ads_raw(
            session,
            category="commercial_rent",
            window_start=window_start,
            window_end=window_end,
        )
    cached_sale_ads = [_deserialize_db_parsed_ad(payload) for payload in cached_sale_payloads]
    cached_rent_ads = [_deserialize_db_parsed_ad(payload) for payload in cached_rent_payloads]

    sale_ads = await _collect_ads_for_feed_pages(
        url=COMMERCIAL_SALE_URL,
        ad_type="commercial_sale",
        city="tashkent",
        window_start=window_start,
        window_end=window_end,
        per_page_limit=min(limit_per_feed, 60),
        known_olx_ids={ad.olx_id for ad in cached_sale_ads},
    )
    rent_ads = await _collect_ads_for_feed_pages(
        url=COMMERCIAL_RENT_URL,
        ad_type="commercial_rent",
        city="tashkent",
        window_start=window_start,
        window_end=window_end,
        per_page_limit=min(limit_per_feed, 60),
        known_olx_ids={ad.olx_id for ad in cached_rent_ads},
    )
    if is_initial_scan:
        finish_initial_scan()
    return (
        _merge_snapshot_ads(
            cached_ads=cached_sale_ads,
            fresh_ads=sale_ads,
            window_start=window_start,
            window_end=window_end,
        ),
        _merge_snapshot_ads(
            cached_ads=cached_rent_ads,
            fresh_ads=rent_ads,
            window_start=window_start,
            window_end=window_end,
        ),
    )


def filter_ads_for_window(
    ads: list[ParsedAd],
    *,
    window_start: datetime,
    window_end: datetime,
    oldest_first: bool = False,
) -> list[ParsedAd]:
    result: list[ParsedAd] = []
    seen: set[str] = set()
    for ad in ads:
        if ad.olx_id in seen:
            continue
        seen.add(ad.olx_id)
        published_at = ad.published_at
        if published_at is None:
            continue
        if window_start <= published_at <= window_end:
            result.append(ad)
    result.sort(key=lambda item: item.published_at or window_start, reverse=not oldest_first)
    return result


async def send_broadcast_step(*, bot: Bot, session: AsyncSession, state: SaleBroadcastState, force: bool = False) -> bool:
    if not state.is_active:
        return False

    now = datetime.now(_LOCAL_TZ)
    send_interval_seconds = int(getattr(state, "send_interval_seconds", SEND_INTERVAL_SECONDS) or SEND_INTERVAL_SECONDS)
    if not force and state.last_batch_at and (now - state.last_batch_at) < timedelta(seconds=send_interval_seconds):
        return False

    async def _persist_state(*, current_state: SaleBroadcastState, is_active: bool, pending_ads: list[dict], sent_olx_ids: list[str]) -> None:
        await upsert_sale_broadcast_state(
            session,
            user_id=current_state.user_id,
            is_active=is_active,
            started_at=current_state.started_at,
            window_start=current_state.window_start,
            window_end=current_state.window_end,
            last_batch_at=now,
            total_found=current_state.total_found,
            pending_ads=pending_ads,
            selected_categories=list(current_state.selected_categories or []),
            sent_olx_ids=sent_olx_ids,
            send_interval_seconds=int(getattr(current_state, "send_interval_seconds", SEND_INTERVAL_SECONDS) or SEND_INTERVAL_SECONDS),
        )

    pending_payloads, total_found = await _refresh_pending_broadcast_queue(session, state)
    state.total_found = total_found
    if not pending_payloads:
        await _persist_state(
            current_state=state,
            is_active=False,
            pending_ads=[],
            sent_olx_ids=list(state.sent_olx_ids or []),
        )
        return False

    if not pending_payloads:
        return False

    current_payload = pending_payloads[0]
    rest_payloads = pending_payloads[1:]
    sent_olx_ids = list(state.sent_olx_ids or [])
    ad = _deserialize_parsed_ad(current_payload)

    if ad.olx_id in sent_olx_ids:
        logger.info(
            "Broadcast queue dedupe skip: user_id=%s olx_id=%s already persisted as sent",
            state.user_id,
            ad.olx_id,
        )
        await _persist_state(
            current_state=state,
            is_active=bool(rest_payloads),
            pending_ads=rest_payloads,
            sent_olx_ids=sent_olx_ids,
        )
        return False

    if ad.details_loaded:
        detailed_ad = ad
    else:
        try:
            detailed_ad = await enrich_ad_with_details(ad)
        except Exception:
            logger.exception("broadcast enrich failed (user_id=%s, olx_id=%s)", state.user_id, ad.olx_id)
            detailed_ad = ad

    markup = _build_inline_link(detailed_ad)
    text = _build_html_notification(detailed_ad)

    # Crash-safe ordering: persist "sent" state before the Telegram call so a restart
    # cannot replay the same ad to the same user.
    reserved_sent_olx_ids = [*sent_olx_ids, detailed_ad.olx_id]
    await _persist_state(
        current_state=state,
        is_active=bool(rest_payloads),
        pending_ads=rest_payloads,
        sent_olx_ids=reserved_sent_olx_ids,
    )

    try:
        await _send_ad_payload_with_retry(
            bot=bot,
            chat_id=state.user_id,
            ad=detailed_ad,
            text=text,
            markup=markup,
        )
    except Exception:
        logger.exception("broadcast send failed (user_id=%s, olx_id=%s)", state.user_id, detailed_ad.olx_id)
        return False
    return True


async def _process_active_broadcasts(*, bot: Bot, session: AsyncSession) -> None:
    states = await list_active_sale_broadcast_states(session)
    for state in states:
        sent = await send_broadcast_step(bot=bot, session=session, state=state)
        if sent:
            logger.info("Broadcast step sent: user_id=%s", state.user_id)


async def run_scraper_loop(*, interval_seconds: int = SCRAPING_INTERVAL_SECONDS) -> None:
    logger.info("Scraper loop started (interval=%ss)", interval_seconds)
    await init_db()

    while True:
        try:
            logger.info("Scraper cycle started")
            await _run_interleaved_scraper_cycle(per_page_limit=60)
            logger.info("Scraper cycle finished")
        except asyncio.CancelledError:
            logger.info("Scraper loop cancelled")
            raise
        except Exception:
            logger.exception("Scraper loop failed")

        await asyncio.sleep(interval_seconds)


async def run_worker(bot: Bot, *, interval_seconds: int = WORKER_POLL_INTERVAL_SECONDS) -> None:
    logger.info("Worker started (interval=%ss)", interval_seconds)
    await init_db()

    while True:
        try:
            async with AsyncSessionLocal() as session:
                await _process_active_broadcasts(bot=bot, session=session)
        except asyncio.CancelledError:
            logger.info("Worker cancelled")
            raise
        except Exception:
            logger.exception("Worker loop failed")

        await asyncio.sleep(interval_seconds)
