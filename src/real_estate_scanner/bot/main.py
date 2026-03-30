from __future__ import annotations

import sys
from collections import deque
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo
import asyncio
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

sys.path.append(str(Path(__file__).resolve().parents[2]))

from real_estate_scanner.config import settings
from real_estate_scanner.db.crud import (
    get_recent_ads_raw,
    is_user_bot_allowed,
    get_sale_broadcast_state,
    upsert_sale_broadcast_state,
    upsert_user,
)
from real_estate_scanner.db.init_db import init_db
from real_estate_scanner.db.session import AsyncSessionLocal
from real_estate_scanner.parser.worker import APARTMENTS_BUTTON, STOP_BUTTON, run_scraper_loop, run_worker

logger = logging.getLogger(__name__)
router = Router()

_LOCAL_TZ = ZoneInfo("Asia/Tashkent")
_scraper_task: asyncio.Task | None = None
_broadcast_starting_users: set[int] = set()

COMMERCIAL_SALE_BUTTON = "Коммерция | Продажа | Ташкент"
COMMERCIAL_RENT_BUTTON = "Коммерция | Аренда | Ташкент"

INTERVAL_30_BUTTON = "Раз в 30 секунд"
INTERVAL_60_BUTTON = "Раз в 1 минуту"
INTERVAL_120_BUTTON = "Раз в 2 минуты"
DEFAULT_SEND_INTERVAL_SECONDS = 30
BROADCAST_RECENT_SEEN_HOURS = 24

CATEGORY_ORDER = ("sale", "commercial_sale", "commercial_rent")
CATEGORY_LABELS = {
    "sale": "Квартиры | Продажа | Ташкент",
    "commercial_sale": "Коммерция | Продажа | Ташкент",
    "commercial_rent": "Коммерция | Аренда | Ташкент",
}
BUTTON_TO_CATEGORY = {
    APARTMENTS_BUTTON: "sale",
    COMMERCIAL_SALE_BUTTON: "commercial_sale",
    COMMERCIAL_RENT_BUTTON: "commercial_rent",
}
INTERVAL_BUTTONS = {
    INTERVAL_30_BUTTON: 30,
    INTERVAL_60_BUTTON: 60,
    INTERVAL_120_BUTTON: 120,
}

ACCESS_DENIED_TEXT = "Доступ к боту пока не выдан. Напишите администратору и попросите добавить ваш Telegram ID в белый список."


class BotAccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.from_user is None:
            return await handler(event, data)

        async with AsyncSessionLocal() as session:
            await upsert_user(
                session=session,
                user_id=event.from_user.id,
                username=event.from_user.username,
            )
            if not settings.ADMIN_PANEL_ENABLED:
                return await handler(event, data)

            if await is_user_bot_allowed(session, event.from_user.id):
                return await handler(event, data)

        await event.answer(ACCESS_DENIED_TEXT, reply_markup=_build_keyboard())
        return None


def _setup_logging() -> None:
    project_root = Path(__file__).resolve().parents[3]
    logs_dir = project_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "scanner.log"

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    file_handler = TimedRotatingFileHandler(
        filename=log_file,
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)


def _build_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=APARTMENTS_BUTTON)],
            [KeyboardButton(text=COMMERCIAL_SALE_BUTTON)],
            [KeyboardButton(text=COMMERCIAL_RENT_BUTTON)],
            [KeyboardButton(text=STOP_BUTTON)],
            [
                KeyboardButton(text=INTERVAL_30_BUTTON),
                KeyboardButton(text=INTERVAL_60_BUTTON),
                KeyboardButton(text=INTERVAL_120_BUTTON),
            ],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _payload_sort_key(payload: dict, fallback: datetime) -> datetime:
    for key in ("published_at", "scanned_at", "timestamp"):
        value = payload.get(key)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                continue
    return fallback


def _normalize_selected_categories(categories: list[str] | None) -> list[str]:
    selected = set(categories or [])
    return [category for category in CATEGORY_ORDER if category in selected]


def _normalize_next_category(next_category: str | None, selected_categories: list[str]) -> str | None:
    normalized_selected = _normalize_selected_categories(selected_categories)
    if not normalized_selected:
        return None
    if next_category in normalized_selected:
        return next_category
    return normalized_selected[0]


def _format_interval_label(seconds: int) -> str:
    mapping = {
        30: "30 секунд",
        60: "1 минута",
        120: "2 минуты",
    }
    return mapping.get(seconds, f"{seconds} сек.")


async def _load_category_payloads(
    *,
    category: str,
    window_start: datetime,
    window_end: datetime,
    scanned_since: datetime,
    exclude_olx_ids: set[str],
) -> list[dict]:
    async with AsyncSessionLocal() as session:
        rows = await get_recent_ads_raw(
            session,
            category=category,
            window_start=window_start,
            window_end=window_end,
            scanned_since=scanned_since,
        )

    payloads: list[dict] = []
    seen_ids = set(exclude_olx_ids)
    for payload in rows:
        olx_id = payload.get("olx_id")
        if not olx_id or olx_id in seen_ids:
            continue
        seen_ids.add(olx_id)
        prepared_payload = dict(payload)
        prepared_payload["details_loaded"] = False
        payloads.append(prepared_payload)
    payloads.sort(key=lambda item: _payload_sort_key(item, window_start))
    return payloads


async def _build_queue_for_categories(
    *,
    categories: list[str],
    sent_olx_ids: list[str],
    next_category: str | None = None,
) -> tuple[list[dict], dict[str, int], datetime, datetime, str | None]:
    window_end = datetime.now(_LOCAL_TZ)
    window_start = window_end - timedelta(days=30)
    scanned_since = window_end - timedelta(hours=BROADCAST_RECENT_SEEN_HOURS)
    payloads_by_category: dict[str, list[dict]] = {}
    counts: dict[str, int] = {}
    exclude_ids = set(sent_olx_ids or [])

    for category in _normalize_selected_categories(categories):
        payloads = await _load_category_payloads(
            category=category,
            window_start=window_start,
            window_end=window_end,
            scanned_since=scanned_since,
            exclude_olx_ids=exclude_ids,
        )
        payloads_by_category[category] = payloads
        counts[category] = len(payloads)

    selected_categories = _normalize_selected_categories(categories)
    normalized_next_category = _normalize_next_category(next_category, selected_categories)
    if normalized_next_category and normalized_next_category in selected_categories:
        start_index = selected_categories.index(normalized_next_category)
        ordered_categories = selected_categories[start_index:] + selected_categories[:start_index]
    else:
        ordered_categories = selected_categories

    queues = {category: deque(payloads_by_category.get(category, [])) for category in ordered_categories}
    queue: list[dict] = []
    while any(queues[category] for category in ordered_categories):
        for category in ordered_categories:
            if queues[category]:
                queue.append(queues[category].popleft())

    return queue, counts, window_start, window_end, normalized_next_category


async def _persist_broadcast_state(
    *,
    user_id: int,
    is_active: bool,
    pending_ads: list[dict],
    selected_categories: list[str],
    next_category: str | None,
    sent_olx_ids: list[str],
    send_interval_seconds: int,
    total_found: int,
    started_at: datetime | None,
    window_start: datetime | None,
    window_end: datetime | None,
    last_batch_at: datetime | None,
) -> None:
    async with AsyncSessionLocal() as session:
        await upsert_sale_broadcast_state(
            session,
            user_id=user_id,
            is_active=is_active,
            started_at=started_at,
            window_start=window_start,
            window_end=window_end,
            last_batch_at=last_batch_at,
            total_found=total_found,
            pending_ads=pending_ads,
            selected_categories=_normalize_selected_categories(selected_categories),
            next_category=_normalize_next_category(next_category, selected_categories),
            sent_olx_ids=sent_olx_ids,
            send_interval_seconds=send_interval_seconds,
        )


def _build_status_message(
    *,
    selected_categories: list[str],
    counts: dict[str, int],
    send_interval_seconds: int,
) -> str:
    lines = ["Активные разделы:"]
    for category in _normalize_selected_categories(selected_categories):
        lines.append(f"{CATEGORY_LABELS[category]}: {counts.get(category, 0)}")
    lines.append(f"Интервал: {_format_interval_label(send_interval_seconds)}")
    lines.append(f"Всего в очереди: {sum(counts.values())}")
    lines.append("Повторное нажатие на кнопку раздела убирает его из активных.")
    return "\n".join(lines)


async def _toggle_category_subscription(message: Message, category: str) -> None:
    user_id = message.from_user.id
    logger.info("toggle_category_subscription: user_id=%s category=%s", user_id, category)

    if user_id in _broadcast_starting_users:
        await message.answer("Подготовка очереди уже идёт. Подождите пару секунд.")
        return

    _broadcast_starting_users.add(user_id)
    try:
        async with AsyncSessionLocal() as session:
            await upsert_user(session=session, user_id=user_id, username=message.from_user.username)
            state = await get_sale_broadcast_state(session, user_id)

        selected_categories = _normalize_selected_categories(list(state.selected_categories or []) if state else [])
        next_category = _normalize_next_category(getattr(state, "next_category", None), selected_categories)
        sent_olx_ids = list(state.sent_olx_ids or []) if state else []
        send_interval_seconds = int(getattr(state, "send_interval_seconds", DEFAULT_SEND_INTERVAL_SECONDS) or DEFAULT_SEND_INTERVAL_SECONDS)

        if category in selected_categories:
            selected_categories = [item for item in selected_categories if item != category]
            action_text = f"Раздел отключён: {CATEGORY_LABELS[category]}"
        else:
            selected_categories.append(category)
            selected_categories = _normalize_selected_categories(selected_categories)
            action_text = f"Раздел добавлен: {CATEGORY_LABELS[category]}"

        if not selected_categories:
            await _persist_broadcast_state(
                user_id=user_id,
                is_active=False,
                pending_ads=[],
                selected_categories=[],
                next_category=None,
                sent_olx_ids=sent_olx_ids,
                send_interval_seconds=send_interval_seconds,
                total_found=0,
                started_at=datetime.now(_LOCAL_TZ),
                window_start=None,
                window_end=None,
                last_batch_at=None,
            )
            await message.answer("Все разделы отключены.", reply_markup=_build_keyboard())
            return

        queue, counts, window_start, window_end, next_category = await _build_queue_for_categories(
            categories=selected_categories,
            sent_olx_ids=sent_olx_ids,
            next_category=next_category,
        )
        await _persist_broadcast_state(
            user_id=user_id,
            is_active=bool(queue),
            pending_ads=queue,
            selected_categories=selected_categories,
            next_category=next_category,
            sent_olx_ids=sent_olx_ids,
            send_interval_seconds=send_interval_seconds,
            total_found=len(queue),
            started_at=datetime.now(_LOCAL_TZ),
            window_start=window_start,
            window_end=window_end,
            last_batch_at=None,
        )
        await message.answer(
            action_text
            + "\n\n"
            + _build_status_message(
                selected_categories=selected_categories,
                counts=counts,
                send_interval_seconds=send_interval_seconds,
            ),
            reply_markup=_build_keyboard(),
        )
    finally:
        _broadcast_starting_users.discard(user_id)


async def _set_interval(message: Message, seconds: int) -> None:
    user_id = message.from_user.id
    logger.info("set_interval: user_id=%s seconds=%s", user_id, seconds)

    async with AsyncSessionLocal() as session:
        await upsert_user(session=session, user_id=user_id, username=message.from_user.username)
        state = await get_sale_broadcast_state(session, user_id)

    selected_categories = _normalize_selected_categories(list(state.selected_categories or []) if state else [])
    pending_ads = list(state.pending_ads or []) if state else []
    next_category = _normalize_next_category(getattr(state, "next_category", None), selected_categories)
    sent_olx_ids = list(state.sent_olx_ids or []) if state else []
    is_active = bool(state.is_active) if state else False
    total_found = int(state.total_found or len(pending_ads)) if state else len(pending_ads)
    started_at = state.started_at if state else datetime.now(_LOCAL_TZ)
    window_start = state.window_start if state else None
    window_end = state.window_end if state else None
    last_batch_at = state.last_batch_at if state else None

    await _persist_broadcast_state(
        user_id=user_id,
        is_active=is_active,
        pending_ads=pending_ads,
        selected_categories=selected_categories,
        next_category=next_category,
        sent_olx_ids=sent_olx_ids,
        send_interval_seconds=seconds,
        total_found=total_found,
        started_at=started_at,
        window_start=window_start,
        window_end=window_end,
        last_batch_at=last_batch_at,
    )
    await message.answer(
        f"Новый интервал отправки: {_format_interval_label(seconds)}.",
        reply_markup=_build_keyboard(),
    )


async def _reset_user_history(message: Message) -> None:
    user_id = message.from_user.id
    logger.info("reset_handler: from_id=%s", user_id)

    async with AsyncSessionLocal() as session:
        await upsert_user(session=session, user_id=user_id, username=message.from_user.username)
        state = await get_sale_broadcast_state(session, user_id)

    if not state:
        await _persist_broadcast_state(
            user_id=user_id,
            is_active=False,
            pending_ads=[],
            selected_categories=[],
            next_category=None,
            sent_olx_ids=[],
            send_interval_seconds=DEFAULT_SEND_INTERVAL_SECONDS,
            total_found=0,
            started_at=datetime.now(_LOCAL_TZ),
            window_start=None,
            window_end=None,
            last_batch_at=None,
        )
        await message.answer(
            "История рассылки очищена. Теперь можно заново выбрать разделы.",
            reply_markup=_build_keyboard(),
        )
        return

    selected_categories = _normalize_selected_categories(list(state.selected_categories or []))
    send_interval_seconds = int(
        getattr(state, "send_interval_seconds", DEFAULT_SEND_INTERVAL_SECONDS) or DEFAULT_SEND_INTERVAL_SECONDS
    )

    if not selected_categories:
        await _persist_broadcast_state(
            user_id=user_id,
            is_active=False,
            pending_ads=[],
            selected_categories=[],
            next_category=None,
            sent_olx_ids=[],
            send_interval_seconds=send_interval_seconds,
            total_found=0,
            started_at=datetime.now(_LOCAL_TZ),
            window_start=None,
            window_end=None,
            last_batch_at=None,
        )
        await message.answer(
            "История отправленных объявлений очищена. Активных разделов сейчас нет.",
            reply_markup=_build_keyboard(),
        )
        return

    queue, counts, window_start, window_end, next_category = await _build_queue_for_categories(
        categories=selected_categories,
        sent_olx_ids=[],
        next_category=None,
    )
    await _persist_broadcast_state(
        user_id=user_id,
        is_active=bool(queue),
        pending_ads=queue,
        selected_categories=selected_categories,
        next_category=next_category,
        sent_olx_ids=[],
        send_interval_seconds=send_interval_seconds,
        total_found=len(queue),
        started_at=datetime.now(_LOCAL_TZ),
        window_start=window_start,
        window_end=window_end,
        last_batch_at=None,
    )
    await message.answer(
        "История отправленных объявлений очищена. Рассылка начнётся заново с самых старых.\n\n"
        + _build_status_message(
            selected_categories=selected_categories,
            counts=counts,
            send_interval_seconds=send_interval_seconds,
        ),
        reply_markup=_build_keyboard(),
    )


@router.message(Command("reset"))
async def reset_handler(message: Message) -> None:
    await _reset_user_history(message)


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    logger.info("start_handler: from_id=%s username=%s", message.from_user.id, message.from_user.username)
    async with AsyncSessionLocal() as session:
        await upsert_user(session=session, user_id=message.from_user.id, username=message.from_user.username)
    await message.answer(
        "Выберите разделы и интервал отправки. По умолчанию уведомления идут раз в 30 секунд.",
        reply_markup=_build_keyboard(),
    )


@router.message(F.text == APARTMENTS_BUTTON)
async def start_apartments_handler(message: Message) -> None:
    await _toggle_category_subscription(message, "sale")


@router.message(F.text == COMMERCIAL_SALE_BUTTON)
async def start_commercial_sale_handler(message: Message) -> None:
    await _toggle_category_subscription(message, "commercial_sale")


@router.message(F.text == COMMERCIAL_RENT_BUTTON)
async def start_commercial_rent_handler(message: Message) -> None:
    await _toggle_category_subscription(message, "commercial_rent")


@router.message(F.text.in_(set(INTERVAL_BUTTONS)))
async def interval_handler(message: Message) -> None:
    await _set_interval(message, INTERVAL_BUTTONS[message.text])


@router.message(F.text == STOP_BUTTON)
async def stop_broadcast_handler(message: Message) -> None:
    user_id = message.from_user.id
    logger.info("stop_broadcast_handler: from_id=%s", user_id)

    async with AsyncSessionLocal() as session:
        state = await get_sale_broadcast_state(session, user_id)

    if not state or not state.selected_categories:
        await message.answer("Активных разделов сейчас нет.", reply_markup=_build_keyboard())
        return

    selected_categories = _normalize_selected_categories(list(state.selected_categories or []))
    sent_olx_ids = list(state.sent_olx_ids or [])
    send_interval_seconds = int(getattr(state, "send_interval_seconds", DEFAULT_SEND_INTERVAL_SECONDS) or DEFAULT_SEND_INTERVAL_SECONDS)

    if state.is_active:
        await _persist_broadcast_state(
            user_id=user_id,
            is_active=False,
            pending_ads=list(state.pending_ads or []),
            selected_categories=selected_categories,
            next_category=_normalize_next_category(getattr(state, "next_category", None), selected_categories),
            sent_olx_ids=sent_olx_ids,
            send_interval_seconds=send_interval_seconds,
            total_found=int(state.total_found or len(state.pending_ads or [])),
            started_at=state.started_at,
            window_start=state.window_start,
            window_end=state.window_end,
            last_batch_at=state.last_batch_at,
        )
        await message.answer("Рассылка поставлена на паузу.", reply_markup=_build_keyboard())
        return

    queue, counts, window_start, window_end, next_category = await _build_queue_for_categories(
        categories=selected_categories,
        sent_olx_ids=sent_olx_ids,
        next_category=getattr(state, "next_category", None),
    )
    await _persist_broadcast_state(
        user_id=user_id,
        is_active=bool(queue),
        pending_ads=queue,
        selected_categories=selected_categories,
        next_category=next_category,
        sent_olx_ids=sent_olx_ids,
        send_interval_seconds=send_interval_seconds,
        total_found=len(queue),
        started_at=datetime.now(_LOCAL_TZ),
        window_start=window_start,
        window_end=window_end,
        last_batch_at=None,
    )
    if queue:
        await message.answer(
            "Рассылка продолжена.\n"
            + _build_status_message(
                selected_categories=selected_categories,
                counts=counts,
                send_interval_seconds=send_interval_seconds,
            ),
            reply_markup=_build_keyboard(),
        )
    else:
        await message.answer("Новых объявлений для продолжения пока нет.", reply_markup=_build_keyboard())


async def main() -> None:
    global _scraper_task
    _setup_logging()

    if not settings.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in environment (.env)")

    logger.info("Starting bot...")
    await init_db()

    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher()
    dp.message.middleware(BotAccessMiddleware())
    dp.include_router(router)

    worker_task = asyncio.create_task(run_worker(bot))
    _scraper_task = asyncio.create_task(run_scraper_loop())
    try:
        await dp.start_polling(bot)
    finally:
        if _scraper_task is not None:
            _scraper_task.cancel()
            try:
                await _scraper_task
            except asyncio.CancelledError:
                pass
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
