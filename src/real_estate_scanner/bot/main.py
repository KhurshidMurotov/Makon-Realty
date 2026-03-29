from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

import asyncio
import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from real_estate_scanner.config import settings
from real_estate_scanner.db.crud import (
    get_sale_broadcast_state,
    get_user_notifications_enabled,
    toggle_user_notifications_enabled,
    upsert_sale_broadcast_state,
    upsert_user,
)
from real_estate_scanner.db.init_db import init_db
from real_estate_scanner.db.session import AsyncSessionLocal
from real_estate_scanner.parser.worker import (
    APARTMENTS_BUTTON,
    COMMERCIAL_BUTTON,
    SEND_INTERVAL_SECONDS,
    STOP_BUTTON,
    build_apartments_sale_snapshot,
    build_commercial_snapshot,
    filter_ads_for_window,
    is_initial_scan_active,
    run_worker,
)

logger = logging.getLogger(__name__)
_LOCAL_TZ = ZoneInfo("Asia/Tashkent")
_broadcast_starting_users: set[int] = set()
_broadcast_stop_requested_users: set[int] = set()
NOTIFICATIONS_OFF_BUTTON = "🔇 Выключить уведомления"
NOTIFICATIONS_ON_BUTTON = "🔔 Включить уведомления"

router = Router()


def _serialize_ads(ads):
    return [
        {
            **asdict(ad),
            "published_at": ad.published_at.isoformat() if ad.published_at else None,
        }
        for ad in ads
    ]


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


async def _should_abort_start(user_id: int) -> bool:
    return user_id in _broadcast_stop_requested_users


def _notifications_button_text(enabled: bool) -> str:
    return NOTIFICATIONS_OFF_BUTTON if enabled else NOTIFICATIONS_ON_BUTTON


def _build_keyboard(*, notifications_enabled: bool) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=APARTMENTS_BUTTON)],
            [KeyboardButton(text=COMMERCIAL_BUTTON)],
            [KeyboardButton(text=STOP_BUTTON)],
            [KeyboardButton(text=_notifications_button_text(notifications_enabled))],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


async def _replace_user_broadcast(
    *,
    user_id: int,
    total_found: int,
    pending_ads: list[dict],
    window_start: datetime,
    window_end: datetime,
) -> None:
    async with AsyncSessionLocal() as session:
        await upsert_sale_broadcast_state(
            session,
            user_id=user_id,
            is_active=bool(pending_ads),
            started_at=window_end,
            window_start=window_start,
            window_end=window_end,
            last_batch_at=None,
            total_found=total_found,
            pending_ads=pending_ads,
            sent_olx_ids=[],
        )


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    logger.info("start_handler: from_id=%s username=%s", message.from_user.id, message.from_user.username)
    async with AsyncSessionLocal() as session:
        await upsert_user(session=session, user_id=message.from_user.id, username=message.from_user.username)
        notifications_enabled = await get_user_notifications_enabled(session, message.from_user.id)

    keyboard = _build_keyboard(notifications_enabled=notifications_enabled)
    await message.answer(
        "\u0414\u043e\u0441\u0442\u0443\u043f\u043d\u044b \u043a\u043d\u043e\u043f\u043a\u0438: \u043a\u0432\u0430\u0440\u0442\u0438\u0440\u044b \u043d\u0430 \u043f\u0440\u043e\u0434\u0430\u0436\u0443, \u043a\u043e\u043c\u043c\u0435\u0440\u0446\u0438\u044f \u043f\u0440\u043e\u0434\u0430\u0436\u0430+\u0430\u0440\u0435\u043d\u0434\u0430, \u0441\u0442\u043e\u043f \u0438 \u043f\u0435\u0440\u0435\u043a\u043b\u044e\u0447\u0430\u0442\u0435\u043b\u044c \u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u0439.",
        reply_markup=keyboard,
    )


@router.message(F.text == APARTMENTS_BUTTON)
async def start_apartments_handler(message: Message) -> None:
    user_id = message.from_user.id
    logger.info("start_apartments_handler: from_id=%s", user_id)
    was_initial_scan = is_initial_scan_active()

    if user_id in _broadcast_starting_users:
        await message.answer("Сбор уже идёт. Подождите текущий запуск.")
        return

    _broadcast_stop_requested_users.discard(user_id)
    _broadcast_starting_users.add(user_id)
    try:
        await message.answer("Собираю продажи квартир по Ташкенту за последний месяц.")

        async with AsyncSessionLocal() as session:
            await upsert_user(session=session, user_id=user_id, username=message.from_user.username)

        snapshot = await build_apartments_sale_snapshot(limit=240)
        window_end = datetime.now(_LOCAL_TZ)
        window_start = window_end - timedelta(days=30)
        filtered_ads = filter_ads_for_window(snapshot, window_start=window_start, window_end=window_end, oldest_first=True)

        if await _should_abort_start(user_id):
            await message.answer("Запуск отменён: рассылка была остановлена до завершения сбора.")
            return

        await message.answer(
            f"Квартиры | Продажа | Ташкент\n"
            f"За последний месяц найдено: {len(filtered_ads)}\n"
            f"Отправка пойдёт по 1 объявлению каждые {SEND_INTERVAL_SECONDS} секунд, от самого старого к новому."
        )

        if was_initial_scan:
            await message.answer("Первый круг завершён. База наполнена. Включаю режим уведомлений.")
            return

        await _replace_user_broadcast(
            user_id=user_id,
            total_found=len(filtered_ads),
            pending_ads=_serialize_ads(filtered_ads),
            window_start=window_start,
            window_end=window_end,
        )
    finally:
        _broadcast_starting_users.discard(user_id)


@router.message(F.text == COMMERCIAL_BUTTON)
async def start_commercial_handler(message: Message) -> None:
    user_id = message.from_user.id
    logger.info("start_commercial_handler: from_id=%s", user_id)
    was_initial_scan = is_initial_scan_active()

    if user_id in _broadcast_starting_users:
        await message.answer("Сбор уже идёт. Подождите текущий запуск.")
        return

    _broadcast_stop_requested_users.discard(user_id)
    _broadcast_starting_users.add(user_id)
    try:
        await message.answer("Собираю коммерческие помещения по Ташкенту за последний месяц: отдельно продажу и аренду.")

        async with AsyncSessionLocal() as session:
            await upsert_user(session=session, user_id=user_id, username=message.from_user.username)

        sale_snapshot, rent_snapshot = await build_commercial_snapshot(limit_per_feed=240)
        window_end = datetime.now(_LOCAL_TZ)
        window_start = window_end - timedelta(days=30)
        sale_ads = filter_ads_for_window(sale_snapshot, window_start=window_start, window_end=window_end, oldest_first=True)
        rent_ads = filter_ads_for_window(rent_snapshot, window_start=window_start, window_end=window_end, oldest_first=True)
        combined_ads = sorted(
            [*sale_ads, *rent_ads],
            key=lambda item: item.published_at or window_start,
        )

        if await _should_abort_start(user_id):
            await message.answer("Запуск отменён: рассылка была остановлена до завершения сбора.")
            return

        await message.answer(
            f"Коммерция | Ташкент\n"
            f"Продажа за месяц: {len(sale_ads)}\n"
            f"Аренда за месяц: {len(rent_ads)}\n"
            f"Всего к отправке: {len(combined_ads)}\n"
            f"Отправка пойдёт по 1 объявлению каждые {SEND_INTERVAL_SECONDS} секунд, от самого старого к новому."
        )

        if was_initial_scan:
            await message.answer("Первый круг завершён. База наполнена. Включаю режим уведомлений.")
            return

        await _replace_user_broadcast(
            user_id=user_id,
            total_found=len(combined_ads),
            pending_ads=_serialize_ads(combined_ads),
            window_start=window_start,
            window_end=window_end,
        )
    finally:
        _broadcast_starting_users.discard(user_id)


@router.message(F.text == STOP_BUTTON)
async def stop_broadcast_handler(message: Message) -> None:
    user_id = message.from_user.id
    _broadcast_starting_users.discard(user_id)
    _broadcast_stop_requested_users.add(user_id)

    async with AsyncSessionLocal() as session:
        state = await get_sale_broadcast_state(session, user_id)
        if not state or not state.is_active:
            await message.answer("Активной рассылки сейчас нет.")
            return
        await upsert_sale_broadcast_state(
            session,
            user_id=user_id,
            is_active=False,
            started_at=state.started_at,
            window_start=state.window_start,
            window_end=state.window_end,
            last_batch_at=state.last_batch_at,
            total_found=state.total_found,
            pending_ads=[],
            sent_olx_ids=list(state.sent_olx_ids or []),
        )
    await message.answer("Рассылка остановлена.")


@router.message(F.text.in_({NOTIFICATIONS_OFF_BUTTON, NOTIFICATIONS_ON_BUTTON}))
async def toggle_notifications_handler(message: Message) -> None:
    user_id = message.from_user.id
    async with AsyncSessionLocal() as session:
        await upsert_user(session=session, user_id=user_id, username=message.from_user.username)
        notifications_enabled = await toggle_user_notifications_enabled(session, user_id)

    keyboard = _build_keyboard(notifications_enabled=notifications_enabled)
    if notifications_enabled:
        await message.answer(
            "Уведомления включены. Вы будете получать новые объекты по мере их обработки.",
            reply_markup=keyboard,
        )
    else:
        await message.answer(
            "Уведомления отключены. Сбор базы продолжается в фоновом режиме.",
            reply_markup=keyboard,
        )


async def main() -> None:
    _setup_logging()

    if not settings.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in environment (.env)")

    logger.info("Starting bot...")
    await init_db()

    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    worker_task = asyncio.create_task(run_worker(bot))
    try:
        await dp.start_polling(bot)
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
