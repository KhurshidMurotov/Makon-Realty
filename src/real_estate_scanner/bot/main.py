from __future__ import annotations

import sys
from pathlib import Path

# Allow running bot from project root without PYTHONPATH
sys.path.append(str(Path(__file__).resolve().parents[2]))

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, WebAppInfo
from sqlalchemy.ext.asyncio import AsyncSession

from real_estate_scanner.bot.schemas import FilterSchema
from real_estate_scanner.config import settings
from real_estate_scanner.db.crud import get_sale_broadcast_state, save_filter, upsert_sale_broadcast_state, upsert_user
from real_estate_scanner.db.init_db import init_db
from real_estate_scanner.db.session import AsyncSessionLocal
from real_estate_scanner.parser.worker import (
    SALE_BROADCAST_BUTTON,
    STOP_BUTTON,
    build_sale_broadcast_snapshot,
    filter_ads_for_window,
    run_worker,
    send_sale_broadcast_batch,
)

logger = logging.getLogger(__name__)
_LOCAL_TZ = ZoneInfo("Asia/Tashkent")
LEGACY_FILTER_BUTTON = "Подобрать недвижимость"

router = Router()


async def get_db_session() -> AsyncSession:
    # Helper to keep type check happy. Session is a context manager when used directly.
    return AsyncSessionLocal()


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    logger.info("start_handler: from_id=%s username=%s", message.from_user.id, message.from_user.username)

    webapp_url = settings.MINI_APP_URL
    async with AsyncSessionLocal() as session:
        state = await get_sale_broadcast_state(session, message.from_user.id)
    if state and state.is_active:
        return

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=LEGACY_FILTER_BUTTON, web_app=WebAppInfo(url=webapp_url))],
            [KeyboardButton(text=SALE_BROADCAST_BUTTON), KeyboardButton(text=STOP_BUTTON)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )

    await message.answer(
        "Выберите режим: старый фильтр или автоматическая отправка всех продаж квартир по Ташкенту за последние 7 суток.",
        reply_markup=keyboard,
    )


@router.message(F.web_app_data)
async def webapp_data_handler(message: Message) -> None:
    logger.info("webapp_data_handler: from_id=%s", message.from_user.id)

    try:
        payload = json.loads(message.web_app_data.data)
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
    except Exception:
        logger.exception("webapp_data_handler: cannot parse web_app_data json")
        await message.answer("Ошибка: неверный формат данных. Попробуйте ещё раз.")
        return

    filter_data: Any = payload.get("filter")
    if filter_data is None:
        await message.answer("Ошибка: отсутствует `filter` в данных.")
        return

    try:
        filter_schema = FilterSchema.model_validate(filter_data)
    except Exception:
        logger.exception("webapp_data_handler: filter validation failed")
        await message.answer("Ошибка: фильтр заполнен некорректно. Проверьте поля и повторите.")
        return

    # Save to DB
    try:
        user_id = message.from_user.id
        logger.info("ПОЛУЧЕН НОВЫЙ ФИЛЬТР: Тип=%s, Районы=%s", filter_schema.type, filter_schema.cities)
        async with AsyncSessionLocal() as session:
            await upsert_user(session=session, user_id=user_id, username=message.from_user.username)

            filter_data = filter_schema.model_dump()
            # Put `name` into additional_params so DB always has a JSONB payload for future fields.
            additional_params = dict(filter_data.pop("additional_params", {}))
            name = filter_data.pop("name", None)
            if name:
                additional_params["name"] = name

            filter_data["additional_params"] = additional_params

            await save_filter(session=session, user_id=user_id, filter_data=filter_data)

        logger.info("webapp_data_handler: filter saved (user_id=%s)", user_id)
    except Exception:
        logger.exception("webapp_data_handler: db save failed")
        await message.answer("Ошибка: не удалось сохранить фильтр. Попробуйте позже.")
        return

    await message.answer(f"✅ Мониторинг запущен! Ищу: {filter_schema.type}")


@router.message(F.text == SALE_BROADCAST_BUTTON)
async def start_sale_broadcast_handler(message: Message) -> None:
    user_id = message.from_user.id
    bot = message.bot
    async with AsyncSessionLocal() as session:
        await upsert_user(session=session, user_id=user_id, username=message.from_user.username)
        state = await get_sale_broadcast_state(session, user_id)
        if state and state.is_active:
            return

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
        await message.answer(f"Доступно квартир к отправке: {available_count}")
        if filtered_ads:
            state = await get_sale_broadcast_state(session, user_id)
            if state:
                sent_now = await send_sale_broadcast_batch(bot=bot, session=session, state=state, force=True)
                if sent_now:
                    await message.answer(f"Первая партия отправлена: {sent_now} объявлений.")


@router.message(F.text == STOP_BUTTON)
async def stop_sale_broadcast_handler(message: Message) -> None:
    user_id = message.from_user.id
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
        except Exception:
            # Ignore cancellation errors on shutdown.
            pass


if __name__ == "__main__":
    asyncio.run(main())

