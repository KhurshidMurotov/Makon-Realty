from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

import asyncio
import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from real_estate_scanner.config import settings
from real_estate_scanner.db.crud import get_sale_broadcast_state, upsert_sale_broadcast_state, upsert_user
from real_estate_scanner.db.init_db import init_db
from real_estate_scanner.db.session import AsyncSessionLocal
from real_estate_scanner.parser.worker import (
    SALE_BROADCAST_BUTTON,
    STOP_BUTTON,
    build_sale_broadcast_snapshot,
    calculate_sale_broadcast_batch_size,
    filter_ads_for_window,
    run_worker,
    send_sale_broadcast_batch,
)

logger = logging.getLogger(__name__)
_LOCAL_TZ = ZoneInfo("Asia/Tashkent")
_sale_broadcast_starting_users: set[int] = set()

router = Router()


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    logger.info("start_handler: from_id=%s username=%s", message.from_user.id, message.from_user.username)

    async with AsyncSessionLocal() as session:
        state = await get_sale_broadcast_state(session, message.from_user.id)
    if state and state.is_active:
        return

    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=SALE_BROADCAST_BUTTON), KeyboardButton(text=STOP_BUTTON)]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )
    await message.answer(
        "Доступны только два режима: массовая отправка всех квартир на продажу по Ташкенту и остановка текущей рассылки.",
        reply_markup=keyboard,
    )


@router.message(F.text == SALE_BROADCAST_BUTTON)
async def start_sale_broadcast_handler(message: Message) -> None:
    user_id = message.from_user.id
    bot = message.bot
    logger.info("start_sale_broadcast_handler: from_id=%s", user_id)

    if user_id in _sale_broadcast_starting_users:
        await message.answer("Рассылка уже собирается. Подождите текущий запуск.")
        return

    async with AsyncSessionLocal() as session:
        await upsert_user(session=session, user_id=user_id, username=message.from_user.username)
        state = await get_sale_broadcast_state(session, user_id)
        if state and state.is_active:
            await message.answer("Рассылка уже активна. Нажмите Стоп, чтобы запустить её заново.")
            return

    _sale_broadcast_starting_users.add(user_id)
    try:
        await message.answer("Собираю продажи квартир по Ташкенту за последние 7 суток. Это может занять немного времени.")
        snapshot = await build_sale_broadcast_snapshot(limit=120)
        window_end = datetime.now(_LOCAL_TZ)
        window_start = window_end - timedelta(days=7)
        filtered_ads = filter_ads_for_window(snapshot, window_start=window_start, window_end=window_end)

        async with AsyncSessionLocal() as session:
            await upsert_sale_broadcast_state(
                session,
                user_id=user_id,
                is_active=bool(filtered_ads),
                started_at=window_end,
                window_start=window_start,
                window_end=window_end,
                last_batch_at=None,
                total_found=len(filtered_ads),
                pending_ads=[] if not filtered_ads else [asdict(ad) | {"published_at": ad.published_at.isoformat() if ad.published_at else None} for ad in filtered_ads],
                sent_olx_ids=[],
            )
            available_count = len(filtered_ads)
            batch_count = calculate_sale_broadcast_batch_size(available_count)
            remaining_count = max(0, available_count - batch_count)
            await message.answer(
                f"Всего найдено: {available_count}, сейчас отправлю: {batch_count}, осталось: {remaining_count}"
            )
            if filtered_ads:
                state = await get_sale_broadcast_state(session, user_id)
                if state:
                    sent_now = await send_sale_broadcast_batch(bot=bot, session=session, state=state, force=True)
                    if sent_now:
                        await message.answer(f"Первая партия отправлена: {sent_now} объявлений.")
    finally:
        _sale_broadcast_starting_users.discard(user_id)


@router.message(F.text == STOP_BUTTON)
async def stop_sale_broadcast_handler(message: Message) -> None:
    user_id = message.from_user.id
    _sale_broadcast_starting_users.discard(user_id)

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


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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
