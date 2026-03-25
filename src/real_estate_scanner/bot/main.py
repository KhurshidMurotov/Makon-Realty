from __future__ import annotations

import sys
from pathlib import Path

# Allow running bot from project root without PYTHONPATH
sys.path.append(str(Path(__file__).resolve().parents[2]))

import asyncio
import json
import logging
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, WebAppInfo
from aiogram.utils.web_app import WebAppInitData
from sqlalchemy.ext.asyncio import AsyncSession

from real_estate_scanner.bot.schemas import FilterSchema, parse_payload_json, verify_webapp_init_data
from real_estate_scanner.config import settings
from real_estate_scanner.db.crud import save_filter, upsert_user
from real_estate_scanner.db.init_db import init_db
from real_estate_scanner.db.session import AsyncSessionLocal
from real_estate_scanner.parser.worker import run_worker

logger = logging.getLogger(__name__)

router = Router()


async def get_db_session() -> AsyncSession:
    # Helper to keep type check happy. Session is a context manager when used directly.
    return AsyncSessionLocal()


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    logger.info("start_handler: from_id=%s username=%s", message.from_user.id, message.from_user.username)

    webapp_url = settings.MINI_APP_URL
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Подобрать недвижимость", web_app=WebAppInfo(url=webapp_url))]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )

    await message.answer("Нажмите кнопку, чтобы создать фильтр и начать мониторинг OLX.", reply_markup=keyboard)


@router.message(F.web_app_data)
async def webapp_data_handler(message: Message) -> None:
    logger.info("webapp_data_handler: from_id=%s", message.from_user.id)

    raw_data_str = message.web_app_data.data
    logger.debug("webapp_data_handler: raw_web_app_data=%s", raw_data_str)

    try:
        payload = parse_payload_json(raw_data_str)
        logger.info("webapp_data_handler: json parsed")
    except Exception:
        logger.exception("webapp_data_handler: cannot parse web_app_data json")
        await message.answer("Ошибка: неверный формат данных. Попробуйте ещё раз.")
        return

    try:
        init_data_raw: str | None = payload.get("_auth")
        if not init_data_raw:
            raise ValueError("Missing _auth in payload")

        parsed_init: WebAppInitData = verify_webapp_init_data(
            bot_token=settings.BOT_TOKEN or "",
            init_data_raw=init_data_raw,
            expected_user_id=message.from_user.id,
        )
        logger.info("webapp_data_handler: initData signature ok (auth_date=%s)", parsed_init.auth_date)
    except Exception:
        logger.exception("webapp_data_handler: initData verification failed")
        await message.answer("Ошибка безопасности: данные не подтверждены Telegram. Попробуйте ещё раз.")
        return

    filter_payload: Any = payload.get("filter")
    if filter_payload is None:
        await message.answer("Ошибка: отсутствует `filter` в данных.")
        return

    try:
        filter_schema = FilterSchema.model_validate(filter_payload)
        logger.info("webapp_data_handler: filter validated")
    except Exception:
        logger.exception("webapp_data_handler: filter validation failed")
        await message.answer("Ошибка: фильтр заполнен некорректно. Проверьте поля и повторите.")
        return

    # Save to DB
    try:
        user_id = message.from_user.id
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

    await message.answer("Фильтр принят! Я начал поиск новых объявлений.")


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

