from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import asdict
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from real_estate_scanner.db.crud import list_active_sale_broadcast_states, upsert_sale_broadcast_state
from real_estate_scanner.db.init_db import init_db
from real_estate_scanner.db.models import SaleBroadcastState
from real_estate_scanner.db.session import AsyncSessionLocal
from real_estate_scanner.parser.olx_client import ParsedAd, enrich_ad_with_details, fetch_ads_from_search

logger = logging.getLogger(__name__)
_LOCAL_TZ = ZoneInfo("Asia/Tashkent")


def _display_city_name(city_slug: str | None) -> str:
    mapping = {
        "tashkent": "Ташкент",
        "mirzoulugbek": "Мирзо-Улугбек",
        "yashnabadskiy": "Яшнабад",
        "yunusabadskiy": "Юнусабад",
        "chilanzarskiy": "Чиланзар",
        "yakkasarayskiy": "Яккасарай",
        "mirabadskiy": "Мирабад",
        "almazarskiy": "Алмазар",
        "uchtepinskiy": "Учтепа",
        "sergeli": "Сергели",
    }
    return mapping.get(city_slug or "", city_slug or "Ташкент")


def _format_sum_price(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " сум"


def calculate_sale_broadcast_batch_size(total_pending: int) -> int:
    if total_pending <= 0:
        return 0
    return max(1, min(10, math.ceil(total_pending * 0.15)))


def _looks_like_photo_url(url: str | None) -> bool:
    if not url:
        return False
    normalized = url.lower()
    return any(token in normalized for token in (".jpg", ".jpeg", ".png", ".webp", "/image;", "/files/"))


def _build_html_notification(ad: ParsedAd) -> str:
    rooms_value = str(ad.rooms) if ad.rooms is not None else "-"
    floor_value = str(ad.floor) if ad.floor is not None else "-"
    total_floors_value = str(ad.total_floors) if ad.total_floors is not None else "-"
    area_value = f"{ad.area:g}" if ad.area is not None else "-"
    district_name = escape(ad.district or _display_city_name(ad.city) or "Район не указан")
    description = escape((ad.description or ad.title or "").strip() or "Описание не указано")
    author = escape(ad.author_name or "Не указан")
    created_at = escape(ad.created_at_text or "Не указано")

    return "\n".join(
        [
            f"{rooms_value}/{floor_value}/{total_floors_value}, {escape(area_value)} м²",
            f"Ташкент, {district_name}",
            escape(_format_sum_price(ad.price)),
            "",
            description,
            "",
            f"От: {author}",
            f"Создано: {created_at}",
            "Источник: OLX.uz",
        ]
    )


def _build_inline_link(ad: ParsedAd) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="↗ Перейти к объявлению", url=ad.link),
            ]
        ]
    )


def _serialize_parsed_ad(ad: ParsedAd) -> dict:
    payload = asdict(ad)
    payload["published_at"] = ad.published_at.isoformat() if ad.published_at else None
    return payload


def _deserialize_parsed_ad(payload: dict) -> ParsedAd:
    published_at = payload.get("published_at")
    if isinstance(published_at, str):
        try:
            payload = dict(payload)
            payload["published_at"] = datetime.fromisoformat(published_at)
        except ValueError:
            payload = dict(payload)
            payload["published_at"] = None
    return ParsedAd(**payload)


async def build_sale_broadcast_snapshot(*, limit: int = 120) -> list[ParsedAd]:
    url = "https://www.olx.uz/nedvizhimost/kvartiry/prodazha/tashkent/"
    return await fetch_ads_from_search(url=url, ad_type="sale", city="tashkent", limit=limit)


def filter_ads_for_window(ads: list[ParsedAd], *, window_start: datetime, window_end: datetime) -> list[ParsedAd]:
    result: list[ParsedAd] = []
    seen: set[str] = set()
    for ad in ads:
        if ad.olx_id in seen:
            continue
        seen.add(ad.olx_id)
        if ad.ad_type != "sale":
            continue
        published_at = ad.published_at
        if published_at is None:
            continue
        if window_start <= published_at <= window_end:
            result.append(ad)
    result.sort(key=lambda item: item.published_at or window_start, reverse=True)
    return result


async def send_sale_broadcast_batch(*, bot: Bot, session: AsyncSession, state: SaleBroadcastState, force: bool = False) -> int:
    if not state.is_active:
        return 0

    now = datetime.now(_LOCAL_TZ)
    if not force and state.last_batch_at and (now - state.last_batch_at) < timedelta(seconds=300):
        return 0

    pending_ads = [_deserialize_parsed_ad(item) for item in list(state.pending_ads or [])]
    if not pending_ads:
        await upsert_sale_broadcast_state(
            session,
            user_id=state.user_id,
            is_active=False,
            started_at=state.started_at,
            window_start=state.window_start,
            window_end=state.window_end,
            last_batch_at=now,
            total_found=state.total_found,
            pending_ads=[],
            sent_olx_ids=list(state.sent_olx_ids or []),
        )
        return 0

    batch_size = calculate_sale_broadcast_batch_size(len(pending_ads))
    batch = pending_ads[:batch_size]
    rest = pending_ads[batch_size:]
    sent_olx_ids = list(state.sent_olx_ids or [])

    for ad in batch:
        try:
            detailed_ad = await enrich_ad_with_details(ad)
        except Exception:
            logger.exception("sale broadcast enrich failed (user_id=%s, olx_id=%s)", state.user_id, ad.olx_id)
            detailed_ad = ad

        markup = _build_inline_link(detailed_ad)
        text = _build_html_notification(detailed_ad)
        try:
            if _looks_like_photo_url(detailed_ad.image_url):
                try:
                    await bot.send_photo(
                        chat_id=state.user_id,
                        photo=detailed_ad.image_url,
                        caption=text,
                        reply_markup=markup,
                        parse_mode="HTML",
                    )
                except Exception:
                    logger.exception(
                        "sale broadcast send_photo failed (user_id=%s, olx_id=%s)",
                        state.user_id,
                        detailed_ad.olx_id,
                    )
                    await bot.send_message(
                        chat_id=state.user_id,
                        text=text,
                        reply_markup=markup,
                        parse_mode="HTML",
                    )
            else:
                await bot.send_message(
                    chat_id=state.user_id,
                    text=text,
                    reply_markup=markup,
                    parse_mode="HTML",
                )
            sent_olx_ids.append(detailed_ad.olx_id)
        except Exception:
            logger.exception("sale broadcast send failed (user_id=%s, olx_id=%s)", state.user_id, detailed_ad.olx_id)
        await asyncio.sleep(1.5)

    await upsert_sale_broadcast_state(
        session,
        user_id=state.user_id,
        is_active=bool(rest),
        started_at=state.started_at,
        window_start=state.window_start,
        window_end=state.window_end,
        last_batch_at=now,
        total_found=state.total_found,
        pending_ads=[_serialize_parsed_ad(ad) for ad in rest],
        sent_olx_ids=sent_olx_ids,
    )
    return len(batch)


async def _process_sale_broadcast_states(*, bot: Bot, session: AsyncSession) -> None:
    sale_states = await list_active_sale_broadcast_states(session)
    for state in sale_states:
        sent_count = await send_sale_broadcast_batch(bot=bot, session=session, state=state)
        if sent_count:
            logger.info("Sale broadcast batch sent: user_id=%s count=%s", state.user_id, sent_count)


async def run_worker(bot: Bot, *, interval_seconds: int = 200) -> None:
    logger.info("Worker started (interval=%ss)", interval_seconds)
    await init_db()

    while True:
        try:
            async with AsyncSessionLocal() as session:
                await _process_sale_broadcast_states(bot=bot, session=session)
        except asyncio.CancelledError:
            logger.info("Worker cancelled")
            raise
        except Exception:
            logger.exception("Worker loop failed")

        await asyncio.sleep(interval_seconds)
