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
    get_recent_ads_raw,
    get_sale_broadcast_state,
    get_user_notifications_enabled,
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
COMMERCIAL_BUTTON = "\u041a\u043e\u043c\u043c\u0435\u0440\u0446\u0438\u044f | \u041f\u0440\u043e\u0434\u0430\u0436\u0430 \u0438 \u0410\u0440\u0435\u043d\u0434\u0430 | \u0422\u0430\u0448\u043a\u0435\u043d\u0442"
STOP_BUTTON = "\u0421\u0442\u043e\u043f"

APARTMENTS_SALE_URL = "https://www.olx.uz/nedvizhimost/kvartiry/prodazha/tashkent/?currency=UYE"
COMMERCIAL_SALE_URL = "https://www.olx.uz/nedvizhimost/kommercheskie-pomeshcheniya/prodazha/tashkent/?currency=UYE"
COMMERCIAL_RENT_URL = "https://www.olx.uz/nedvizhimost/kommercheskie-pomeshcheniya/arenda/tashkent/?currency=UYE"
SEND_INTERVAL_SECONDS = 1
MAX_SEND_RETRIES = 3
MAX_CAPTION = 500
MAX_BUCKET_PAGE_LIMIT = 100
KNOWN_BUCKET_STOP_STREAK = 40
SALE_BUCKET_MAX_PRICE = 5_000_000
COMMERCIAL_RENT_BUCKET_MAX_PRICE = 20_000
DEEP_SCAN_START_PAGE = 25
DEEP_SCAN_WINDOW_PAGES = 1
DEEP_SCAN_PAGE_DELAY_SECONDS = 3
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
    created_at = escape(ad.created_at_text or "\u041d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e")
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
    if text:
        short_text = smart_truncate(text, MAX_CAPTION)
        if len(text) <= MAX_CAPTION:
            full_text = ""
        else:
            full_text = text
    else:
        short_text = ""
        full_text = ""

    photo_urls = _get_valid_photo_urls(ad)
    if photo_urls:
        primary_photo_url = photo_urls[-1]
        extra_photo_urls = photo_urls[:-1]

        if extra_photo_urls:
            for start in range(0, len(extra_photo_urls), 10):
                chunk = extra_photo_urls[start : start + 10]
                media = [InputMediaPhoto(media=url) for url in chunk]
                await bot.send_media_group(chat_id=chat_id, media=media)

        await bot.send_photo(
            chat_id=chat_id,
            photo=primary_photo_url,
            caption=short_text,
            reply_markup=markup,
        )

        if full_text and len(full_text) > 30:
            await bot.send_message(
                chat_id=chat_id,
                text=full_text,
            )
        return

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=markup,
    )


def _get_retry_count(payload: dict) -> int:
    try:
        return max(0, int(payload.get("retry_count", 0)))
    except (TypeError, ValueError):
        return 0


def _set_retry_count(payload: dict, retry_count: int) -> dict:
    updated = dict(payload)
    updated["retry_count"] = retry_count
    return updated


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


async def build_apartments_sale_snapshot(*, limit: int = 200) -> list[ParsedAd]:
    window_end = datetime.now(_LOCAL_TZ)
    window_start = window_end - timedelta(days=30)
    async with AsyncSessionLocal() as session:
        cached_payloads = await get_recent_ads_raw(
            session,
            category="sale",
            window_start=window_start,
            window_end=window_end,
        )
    cached_ads = [_deserialize_db_parsed_ad(payload) for payload in cached_payloads]
    cached_ids = {ad.olx_id for ad in cached_ads}
    fresh_ads = await _collect_ads_for_price_buckets(
        url=APARTMENTS_SALE_URL,
        ad_type="sale",
        city="tashkent",
        buckets=iter_sale_price_buckets(),
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

    sale_ads = await _collect_ads_for_price_buckets(
        url=COMMERCIAL_SALE_URL,
        ad_type="commercial_sale",
        city="tashkent",
        buckets=iter_sale_price_buckets(),
        window_start=window_start,
        window_end=window_end,
        per_page_limit=min(limit_per_feed, 60),
        known_olx_ids={ad.olx_id for ad in cached_sale_ads},
    )
    rent_ads = await _collect_ads_for_price_buckets(
        url=COMMERCIAL_RENT_URL,
        ad_type="commercial_rent",
        city="tashkent",
        buckets=iter_commercial_rent_price_buckets(),
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
    if not force and state.last_batch_at and (now - state.last_batch_at) < timedelta(seconds=SEND_INTERVAL_SECONDS):
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
            sent_olx_ids=sent_olx_ids,
        )

    pending_payloads = [dict(item) for item in list(state.pending_ads or [])]
    if not pending_payloads:
        await _persist_state(
            current_state=state,
            is_active=False,
            pending_ads=[],
            sent_olx_ids=list(state.sent_olx_ids or []),
        )
        return False

    skipped_existing = False
    while pending_payloads:
        candidate_payload = pending_payloads[0]
        candidate_ad = _deserialize_parsed_ad(candidate_payload)
        if candidate_ad.details_loaded:
            break
        if not await ad_exists(session, candidate_ad.olx_id):
            break
        skipped_existing = True
        logger.info("Skipping cached ad before enrich (user_id=%s, olx_id=%s)", state.user_id, candidate_ad.olx_id)
        pending_payloads = pending_payloads[1:]

    if skipped_existing:
        await _persist_state(
            current_state=state,
            is_active=bool(pending_payloads),
            pending_ads=pending_payloads,
            sent_olx_ids=list(state.sent_olx_ids or []),
        )

    if not pending_payloads:
        return False

    current_payload = pending_payloads[0]
    rest_payloads = pending_payloads[1:]
    sent_olx_ids = list(state.sent_olx_ids or [])
    retry_count = _get_retry_count(current_payload)
    ad = _deserialize_parsed_ad(current_payload)

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
    success = False
    notifications_enabled = await get_user_notifications_enabled(session, state.user_id)
    if is_initial_scan or not notifications_enabled:
        success = True
        sent_olx_ids.append(detailed_ad.olx_id)
        if is_initial_scan:
            logger.info(
                "Silent initial scan processing (user_id=%s, olx_id=%s)",
                state.user_id,
                detailed_ad.olx_id,
            )
        else:
            logger.info(
                "Notifications disabled; marking processed without send (user_id=%s, olx_id=%s)",
                state.user_id,
                detailed_ad.olx_id,
            )
    else:
        try:
            await _send_ad_payload_with_retry(
                bot=bot,
                chat_id=state.user_id,
                ad=detailed_ad,
                text=text,
                markup=markup,
            )
            success = True
            sent_olx_ids.append(detailed_ad.olx_id)
        except Exception:
            logger.exception("broadcast send failed (user_id=%s, olx_id=%s)", state.user_id, detailed_ad.olx_id)

    latest_state = await get_sale_broadcast_state(session, state.user_id)
    if latest_state is not None and not latest_state.is_active:
        logger.info("Broadcast state already stopped by user; not advancing queue (user_id=%s)", state.user_id)
        await _persist_state(
            current_state=latest_state,
            is_active=False,
            pending_ads=list(latest_state.pending_ads or []),
            sent_olx_ids=list(latest_state.sent_olx_ids or sent_olx_ids),
        )
        return False

    if not success:
        retry_count += 1
        if retry_count >= MAX_SEND_RETRIES:
            logger.warning(
                "Broadcast send retry exceeded; skipping ad (user_id=%s, olx_id=%s, retries=%s)",
                state.user_id,
                detailed_ad.olx_id,
                retry_count,
            )
            await _persist_state(
                current_state=state,
                is_active=bool(rest_payloads),
                pending_ads=rest_payloads,
                sent_olx_ids=sent_olx_ids,
            )
        else:
            current_retry_payload = _set_retry_count(_serialize_parsed_ad(detailed_ad), retry_count)
            await _persist_state(
                current_state=state,
                is_active=True,
                pending_ads=[current_retry_payload, *rest_payloads],
                sent_olx_ids=sent_olx_ids,
            )
        return False

    await _persist_state(
        current_state=state,
        is_active=bool(rest_payloads),
        pending_ads=rest_payloads,
        sent_olx_ids=sent_olx_ids,
    )
    return True


async def _process_active_broadcasts(*, bot: Bot, session: AsyncSession) -> None:
    states = await list_active_sale_broadcast_states(session)
    for state in states:
        sent = await send_broadcast_step(bot=bot, session=session, state=state)
        if sent:
            logger.info("Broadcast step sent: user_id=%s", state.user_id)


async def run_worker(bot: Bot, *, interval_seconds: int = SEND_INTERVAL_SECONDS) -> None:
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
