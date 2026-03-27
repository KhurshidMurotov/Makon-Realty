from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from sqlalchemy.ext.asyncio import AsyncSession

from real_estate_scanner.db.crud import (
    get_sale_broadcast_state,
    list_active_sale_broadcast_states,
    upsert_sale_broadcast_state,
)
from real_estate_scanner.db.init_db import init_db
from real_estate_scanner.db.models import SaleBroadcastState
from real_estate_scanner.db.session import AsyncSessionLocal
from real_estate_scanner.parser.olx_client import ParsedAd, enrich_ad_with_details, fetch_ads_from_search

logger = logging.getLogger(__name__)
_LOCAL_TZ = ZoneInfo("Asia/Tashkent")

APARTMENTS_BUTTON = "\u041a\u0432\u0430\u0440\u0442\u0438\u0440\u044b | \u041f\u0440\u043e\u0434\u0430\u0436\u0430 | \u0422\u0430\u0448\u043a\u0435\u043d\u0442"
COMMERCIAL_BUTTON = "\u041a\u043e\u043c\u043c\u0435\u0440\u0446\u0438\u044f | \u041f\u0440\u043e\u0434\u0430\u0436\u0430 \u0438 \u0410\u0440\u0435\u043d\u0434\u0430 | \u0422\u0430\u0448\u043a\u0435\u043d\u0442"
STOP_BUTTON = "\u0421\u0442\u043e\u043f"

APARTMENTS_SALE_URL = "https://www.olx.uz/nedvizhimost/kvartiry/prodazha/tashkent/?currency=UYE"
COMMERCIAL_SALE_URL = "https://www.olx.uz/nedvizhimost/kommercheskie-pomeshcheniya/prodazha/tashkent/?currency=UYE"
COMMERCIAL_RENT_URL = "https://www.olx.uz/nedvizhimost/kommercheskie-pomeshcheniya/arenda/tashkent/?currency=UYE"
SEND_INTERVAL_SECONDS = 10
MAX_SEND_RETRIES = 3
MAX_CAPTION = 500


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


async def build_apartments_sale_snapshot(*, limit: int = 200) -> list[ParsedAd]:
    return await fetch_ads_from_search(
        url=APARTMENTS_SALE_URL,
        ad_type="sale",
        city="tashkent",
        limit=limit,
    )


async def build_commercial_snapshot(*, limit_per_feed: int = 200) -> tuple[list[ParsedAd], list[ParsedAd]]:
    sale_ads = await fetch_ads_from_search(
        url=COMMERCIAL_SALE_URL,
        ad_type="commercial_sale",
        city="tashkent",
        limit=limit_per_feed,
    )
    rent_ads = await fetch_ads_from_search(
        url=COMMERCIAL_RENT_URL,
        ad_type="commercial_rent",
        city="tashkent",
        limit=limit_per_feed,
    )
    return sale_ads, rent_ads


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

    current_payload = pending_payloads[0]
    rest_payloads = pending_payloads[1:]
    sent_olx_ids = list(state.sent_olx_ids or [])
    retry_count = _get_retry_count(current_payload)
    ad = _deserialize_parsed_ad(current_payload)

    try:
        detailed_ad = await enrich_ad_with_details(ad)
    except Exception:
        logger.exception("broadcast enrich failed (user_id=%s, olx_id=%s)", state.user_id, ad.olx_id)
        detailed_ad = ad

    markup = _build_inline_link(detailed_ad)
    text = _build_html_notification(detailed_ad)
    success = False
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
