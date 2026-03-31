from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from html import escape
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from sqlalchemy.ext.asyncio import AsyncSession

from real_estate_scanner.db.crud import (
    ad_scanned_within_hours,
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
    fetch_ads_from_search_detailed,
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
SCRAPING_INTERVAL_SECONDS = 10 * 60
BROADCAST_RECENT_SEEN_HOURS = 24
PAGE_SCAN_MAX_PAGE = 25
PAGE_SCAN_MIN_PAGE = 1
PAGE_SCAN_DELAY_RANGE_SECONDS = (7.5, 10.5)
FEED_SWITCH_DELAY_RANGE_SECONDS = (11.0, 15.0)
WORKER_HEARTBEAT_INTERVAL_SECONDS = 60
WORKER_STEP_TIMEOUT_SECONDS = 180
SCRAPER_CYCLE_TIMEOUT_SECONDS = 90 * 60
is_initial_scan = True


@dataclass(slots=True)
class ScraperCycleStats:
    pages_attempted: int = 0
    pages_with_ads: int = 0
    empty_pages: int = 0
    anti_bot_pages: int = 0
    ads_found: int = 0
    ads_saved: int = 0


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
    if normalized.startswith("data:"):
        return False
    if any(
        token in normalized
        for token in (
            "arrow",
            "icon",
            "sprite",
            "logo",
            "avatar",
            "placeholder",
            "user-no-photo",
            "no_photo",
        )
    ):
        return False
    return any(token in normalized for token in (".jpg", ".jpeg", ".png", ".webp", "/image;", "/files/"))


def _photo_identity(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


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
    result: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if not _looks_like_photo_url(url):
            continue
        identity = _photo_identity(url)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(url)
    return result


def _has_meaningful_description(text: str | None) -> bool:
    cleaned = clean_text(text or "")
    if not cleaned:
        return False
    lowered = cleaned.casefold()
    blocked_tokens = ("подписаться", "subscribe", "посмотреть номер", "показать телефон")
    return len(cleaned) >= 80 and not any(token in lowered for token in blocked_tokens)


def _detail_enrichment_is_usable(listing_ad: ParsedAd, detailed_ad: ParsedAd) -> bool:
    listing_photo_count = len(_get_valid_photo_urls(listing_ad))
    detail_photo_count = len(_get_valid_photo_urls(detailed_ad))

    improved_fields = any(
        (
            listing_ad.rooms is None and detailed_ad.rooms is not None,
            listing_ad.area is None and detailed_ad.area is not None,
            listing_ad.floor is None and detailed_ad.floor is not None,
            listing_ad.total_floors is None and detailed_ad.total_floors is not None,
            not listing_ad.author_name and bool(detailed_ad.author_name),
            not listing_ad.owner_type and bool(detailed_ad.owner_type),
            not listing_ad.seller_phone and bool(detailed_ad.seller_phone),
            not listing_ad.published_at and bool(detailed_ad.published_at),
            not listing_ad.district and bool(detailed_ad.district),
            detail_photo_count > listing_photo_count,
            _has_meaningful_description(detailed_ad.description) and detailed_ad.description != listing_ad.description,
        )
    )
    if improved_fields:
        return True

    has_structure = any(
        value is not None for value in (detailed_ad.rooms, detailed_ad.area, detailed_ad.floor, detailed_ad.total_floors)
    )
    has_contacts = bool(detailed_ad.author_name or detailed_ad.owner_type or detailed_ad.seller_phone)
    has_enough_photos = detail_photo_count >= 1
    return has_enough_photos and (has_structure or has_contacts)


async def _persist_enriched_ad(session: AsyncSession, ad: ParsedAd) -> None:
    await upsert_scanned_ad(
        session,
        olx_id=ad.olx_id,
        title=ad.title,
        price=ad.price,
        currency="UYE",
        published_at=ad.published_at,
        url=ad.link,
        category=ad.ad_type,
        raw_details=_serialize_parsed_ad(ad),
        image_url=ad.image_url,
    )


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
                try:
                    await bot.send_media_group(chat_id=chat_id, media=media)
                except TelegramBadRequest:
                    logger.warning(
                        "Telegram media_group failed, falling back to single photos: chat_id=%s olx_id=%s chunk_size=%s",
                        chat_id,
                        ad.olx_id,
                        len(chunk),
                    )
                    for url in chunk:
                        try:
                            await bot.send_photo(chat_id=chat_id, photo=url)
                        except TelegramBadRequest:
                            logger.warning(
                                "Telegram single photo skipped after fallback: chat_id=%s olx_id=%s url=%s",
                                chat_id,
                                ad.olx_id,
                                url,
                            )
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
    stats: ScraperCycleStats | None = None,
) -> list[ParsedAd]:
    collected: list[ParsedAd] = []
    page_ads, diagnostics = await fetch_ads_from_search_detailed(
        url=url,
        ad_type=ad_type,
        city=city,
        limit=per_page_limit,
        page_number=page_number,
    )
    if stats is not None:
        stats.pages_attempted += 1
        stats.ads_found += len(page_ads)
        if diagnostics.blocked:
            stats.anti_bot_pages += 1
        if page_ads:
            stats.pages_with_ads += 1
        else:
            stats.empty_pages += 1
    if not page_ads:
        logger.info("OLX feed page returned no ads: ad_type=%s page=%s", ad_type, page_number)
        await _sleep_scraper_delay(PAGE_SCAN_DELAY_RANGE_SECONDS)
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
    if stats is not None:
        stats.ads_saved += page_saved_count
    await _sleep_scraper_delay(PAGE_SCAN_DELAY_RANGE_SECONDS)
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

    await _sleep_scraper_delay(FEED_SWITCH_DELAY_RANGE_SECONDS)
    return collected


async def _run_interleaved_scraper_cycle(*, per_page_limit: int = 60) -> None:
    window_end = datetime.now(_LOCAL_TZ)
    window_start = window_end - timedelta(days=30)
    deleted_count = await _cleanup_expired_db_ads()
    stats = ScraperCycleStats()

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
                    stats=stats,
                )
                if index < len(active_feeds):
                    await _sleep_scraper_delay(FEED_SWITCH_DELAY_RANGE_SECONDS)

    if is_initial_scan:
        finish_initial_scan()
    logger.info(
        "[SCRAPER] cycle summary: pages_attempted=%s pages_with_ads=%s empty_pages=%s anti_bot_pages=%s ads_found=%s ads_saved=%s deleted_old=%s",
        stats.pages_attempted,
        stats.pages_with_ads,
        stats.empty_pages,
        stats.anti_bot_pages,
        stats.ads_found,
        stats.ads_saved,
        deleted_count,
    )


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


def _normalize_next_broadcast_category(next_category: str | None, selected_categories: list[str]) -> str | None:
    normalized = _normalize_broadcast_categories(selected_categories)
    if not normalized:
        return None
    if next_category in normalized:
        return next_category
    return normalized[0]


def _get_next_broadcast_category_after(current_category: str | None, selected_categories: list[str]) -> str | None:
    normalized = _normalize_broadcast_categories(selected_categories)
    if not normalized:
        return None
    if current_category not in normalized:
        return normalized[0]
    current_index = normalized.index(current_category)
    return normalized[(current_index + 1) % len(normalized)]


def _interleave_broadcast_payloads(
    payloads_by_category: dict[str, list[dict]],
    *,
    selected_categories: list[str],
    start_category: str | None,
) -> list[dict]:
    category_order = _normalize_broadcast_categories(selected_categories)
    if start_category in category_order:
        start_index = category_order.index(start_category)
        category_order = category_order[start_index:] + category_order[:start_index]
    queues = {category: list(payloads_by_category.get(category, [])) for category in category_order}
    merged: list[dict] = []
    while any(queues.get(category) for category in category_order):
        for category in category_order:
            if queues[category]:
                merged.append(queues[category].pop(0))
    return merged


async def _refresh_pending_broadcast_queue(
    session: AsyncSession,
    state: SaleBroadcastState,
) -> tuple[list[dict], int, str | None]:
    window_end = datetime.now(_LOCAL_TZ)
    window_start = window_end - timedelta(days=30)
    scanned_since = window_end - timedelta(hours=BROADCAST_RECENT_SEEN_HOURS)
    sent_ids = set(state.sent_olx_ids or [])
    payloads_by_category: dict[str, list[dict]] = {}
    selected_categories = _normalize_broadcast_categories(list(state.selected_categories or []))
    next_category = _normalize_next_broadcast_category(getattr(state, "next_category", None), selected_categories)
    existing_queue = list(state.pending_ads or [])
    existing_order: list[str] = []
    existing_meta_by_id: dict[str, dict] = {}

    for payload in existing_queue:
        olx_id = payload.get("olx_id")
        if not olx_id or olx_id in sent_ids or olx_id in existing_meta_by_id:
            continue
        existing_order.append(olx_id)
        existing_meta_by_id[olx_id] = dict(payload)

    fresh_payloads_by_id: dict[str, dict] = {}

    for category in selected_categories:
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
            fresh_payloads_by_id[olx_id] = prepared_payload
        category_payloads.sort(key=lambda item: _broadcast_payload_sort_key(item, window_start))
        payloads_by_category[category] = category_payloads

    preserved_queue: list[dict] = []
    preserved_ids: set[str] = set()
    for olx_id in existing_order:
        fresh_payload = fresh_payloads_by_id.get(olx_id)
        if not fresh_payload:
            continue
        merged_payload = dict(fresh_payload)
        existing_payload = existing_meta_by_id.get(olx_id, {})
        for key in ("detail_retry_count",):
            if key in existing_payload:
                merged_payload[key] = existing_payload[key]
        preserved_queue.append(merged_payload)
        preserved_ids.add(olx_id)

    remaining_payloads_by_category: dict[str, list[dict]] = {}
    for category, payloads in payloads_by_category.items():
        remaining_payloads_by_category[category] = [
            payload for payload in payloads if payload.get("olx_id") not in preserved_ids
        ]

    refreshed_queue = preserved_queue + _interleave_broadcast_payloads(
        remaining_payloads_by_category,
        selected_categories=selected_categories,
        start_category=next_category,
    )
    return refreshed_queue, len(refreshed_queue), next_category


def _pick_jittered_delay(delay_range: tuple[float, float]) -> float:
    low, high = delay_range
    if high <= low:
        return low
    return random.uniform(low, high)


async def _sleep_scraper_delay(delay_range: tuple[float, float]) -> None:
    await asyncio.sleep(_pick_jittered_delay(delay_range))


async def send_broadcast_step(*, bot: Bot, session: AsyncSession, state: SaleBroadcastState, force: bool = False) -> bool:
    latest_state = await get_sale_broadcast_state(session, state.user_id) or state
    state = latest_state

    if not state.is_active:
        return False

    now = datetime.now(_LOCAL_TZ)
    send_interval_seconds = int(getattr(state, "send_interval_seconds", SEND_INTERVAL_SECONDS) or SEND_INTERVAL_SECONDS)
    if not force and state.last_batch_at and (now - state.last_batch_at) < timedelta(seconds=send_interval_seconds):
        return False

    async def _persist_state(
        *,
        current_state: SaleBroadcastState,
        is_active: bool,
        pending_ads: list[dict],
        sent_olx_ids: list[str],
        next_category: str | None,
    ) -> None:
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
            next_category=_normalize_next_broadcast_category(next_category, list(current_state.selected_categories or [])),
            sent_olx_ids=sent_olx_ids,
            send_interval_seconds=int(getattr(current_state, "send_interval_seconds", SEND_INTERVAL_SECONDS) or SEND_INTERVAL_SECONDS),
        )

    pending_payloads, total_found, next_category = await _refresh_pending_broadcast_queue(session, state)
    state.total_found = total_found
    if not pending_payloads:
        await _persist_state(
            current_state=state,
            is_active=False,
            pending_ads=[],
            sent_olx_ids=list(state.sent_olx_ids or []),
            next_category=next_category,
        )
        return False

    if not pending_payloads:
        return False

    sent_olx_ids = list(state.sent_olx_ids or [])
    deferred_payloads: list[dict] = []
    current_payload: dict | None = None
    detailed_ad: ParsedAd | None = None

    while pending_payloads:
        current_payload = pending_payloads[0]
        rest_payloads = pending_payloads[1:]
        ad = _deserialize_parsed_ad(current_payload)

        if ad.olx_id in sent_olx_ids:
            logger.info(
                "Broadcast queue dedupe skip: user_id=%s olx_id=%s already persisted as sent",
                state.user_id,
                ad.olx_id,
            )
            pending_payloads = rest_payloads
            continue

        if ad.details_loaded:
            candidate_ad = ad
        else:
            try:
                candidate_ad = await enrich_ad_with_details(ad)
            except Exception:
                logger.exception("broadcast enrich failed (user_id=%s, olx_id=%s)", state.user_id, ad.olx_id)
                candidate_ad = ad

        if not _detail_enrichment_is_usable(ad, candidate_ad):
            retry_count = int(current_payload.get("detail_retry_count", 0) or 0) + 1
            deferred_payload = dict(current_payload)
            deferred_payload["detail_retry_count"] = retry_count
            deferred_payload["details_loaded"] = False
            deferred_payloads.append(deferred_payload)
            logger.warning(
                "Broadcast detail defer: user_id=%s olx_id=%s retry=%s photos=%s rooms=%s area=%s floor=%s total_floors=%s author=%s owner_type=%s",
                state.user_id,
                ad.olx_id,
                retry_count,
                len(_get_valid_photo_urls(candidate_ad)),
                candidate_ad.rooms,
                candidate_ad.area,
                candidate_ad.floor,
                candidate_ad.total_floors,
                bool(candidate_ad.author_name),
                bool(candidate_ad.owner_type),
            )
            pending_payloads = rest_payloads
            continue

        detailed_ad = candidate_ad
        try:
            await _persist_enriched_ad(session, detailed_ad)
        except Exception:
            logger.exception(
                "broadcast enrich persist failed (user_id=%s, olx_id=%s)",
                state.user_id,
                detailed_ad.olx_id,
            )
        pending_payloads = rest_payloads
        break

    if detailed_ad is None or current_payload is None:
        await _persist_state(
            current_state=state,
            is_active=bool(deferred_payloads),
            pending_ads=deferred_payloads,
            sent_olx_ids=sent_olx_ids,
            next_category=next_category,
        )
        return False

    live_state = await get_sale_broadcast_state(session, state.user_id) or state
    live_categories = _normalize_broadcast_categories(list(live_state.selected_categories or []))
    if not live_state.is_active or detailed_ad.ad_type not in live_categories:
        logger.info(
            "Broadcast send skipped by live subscription state: user_id=%s olx_id=%s ad_type=%s live_categories=%s",
            state.user_id,
            detailed_ad.olx_id,
            detailed_ad.ad_type,
            live_categories,
        )
        return False

    markup = _build_inline_link(detailed_ad)
    text = _build_html_notification(detailed_ad)

    # Crash-safe ordering: persist "sent" state before the Telegram call so a restart
    # cannot replay the same ad to the same user.
    reserved_sent_olx_ids = [*sent_olx_ids, detailed_ad.olx_id]
    reserved_next_category = _get_next_broadcast_category_after(
        detailed_ad.ad_type,
        list(state.selected_categories or []),
    )
    await _persist_state(
        current_state=state,
        is_active=bool(pending_payloads or deferred_payloads),
        pending_ads=[*pending_payloads, *deferred_payloads],
        sent_olx_ids=reserved_sent_olx_ids,
        next_category=reserved_next_category,
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


async def _process_active_broadcasts(*, bot: Bot, session: AsyncSession) -> tuple[int, int]:
    states = await list_active_sale_broadcast_states(session)
    sent_count = 0
    for state in states:
        sent = await send_broadcast_step(bot=bot, session=session, state=state)
        if sent:
            sent_count += 1
            logger.info("Broadcast step sent: user_id=%s", state.user_id)
    return len(states), sent_count


async def run_scraper_loop(*, interval_seconds: int = SCRAPING_INTERVAL_SECONDS) -> None:
    logger.info("Scraper loop started (interval=%ss)", interval_seconds)
    await init_db()

    cycle_number = 0
    while True:
        cycle_number += 1
        cycle_started_at = datetime.now(_LOCAL_TZ)
        try:
            logger.info("Scraper cycle started: cycle=%s", cycle_number)
            await asyncio.wait_for(
                _run_interleaved_scraper_cycle(per_page_limit=60),
                timeout=SCRAPER_CYCLE_TIMEOUT_SECONDS,
            )
            duration_seconds = (datetime.now(_LOCAL_TZ) - cycle_started_at).total_seconds()
            logger.info(
                "Scraper cycle finished: cycle=%s duration=%.1fs next_run_in=%ss",
                cycle_number,
                duration_seconds,
                interval_seconds,
            )
        except asyncio.CancelledError:
            logger.info("Scraper loop cancelled")
            raise
        except asyncio.TimeoutError:
            duration_seconds = (datetime.now(_LOCAL_TZ) - cycle_started_at).total_seconds()
            logger.error(
                "Scraper cycle timeout: cycle=%s duration=%.1fs timeout=%ss",
                cycle_number,
                duration_seconds,
                SCRAPER_CYCLE_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("Scraper loop failed (cycle=%s)", cycle_number)

        await asyncio.sleep(interval_seconds)


async def run_worker(bot: Bot, *, interval_seconds: int = WORKER_POLL_INTERVAL_SECONDS) -> None:
    logger.info("Worker started (interval=%ss)", interval_seconds)
    await init_db()

    last_heartbeat_at = datetime.now(_LOCAL_TZ)
    tick_number = 0
    while True:
        tick_number += 1
        try:
            async with AsyncSessionLocal() as session:
                active_states, sent_count = await asyncio.wait_for(
                    _process_active_broadcasts(bot=bot, session=session),
                    timeout=WORKER_STEP_TIMEOUT_SECONDS,
                )
            now = datetime.now(_LOCAL_TZ)
            if sent_count or (now - last_heartbeat_at).total_seconds() >= WORKER_HEARTBEAT_INTERVAL_SECONDS:
                logger.info(
                    "Worker heartbeat: tick=%s active_states=%s sent=%s poll_interval=%ss",
                    tick_number,
                    active_states,
                    sent_count,
                    interval_seconds,
                )
                last_heartbeat_at = now
        except asyncio.CancelledError:
            logger.info("Worker cancelled")
            raise
        except asyncio.TimeoutError:
            logger.error(
                "Worker loop timeout: tick=%s timeout=%ss",
                tick_number,
                WORKER_STEP_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("Worker loop failed (tick=%s)", tick_number)

        await asyncio.sleep(interval_seconds)
