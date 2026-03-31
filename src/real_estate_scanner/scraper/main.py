from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from real_estate_scanner.db.init_db import init_db
from real_estate_scanner.parser.worker import run_scraper_loop

logger = logging.getLogger(__name__)
_LOCAL_TZ = ZoneInfo("Asia/Tashkent")
BACKGROUND_TASK_RESTART_DELAY_SECONDS = 5
BACKGROUND_MONITOR_INTERVAL_SECONDS = 300


def _describe_task_state(task: asyncio.Task | None) -> str:
    if task is None:
        return "missing"
    if task.cancelled():
        return "cancelled"
    if task.done():
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return "cancelled"
        if exc is None:
            return "done"
        return f"failed:{type(exc).__name__}"
    return "running"


def _setup_logging() -> None:
    project_root = Path(__file__).resolve().parents[3]
    logs_dir = project_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "scraper.log"

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


async def _supervise_background_task(
    *,
    name: str,
    task_factory: Callable[[], Awaitable[None]],
    restart_delay_seconds: int = BACKGROUND_TASK_RESTART_DELAY_SECONDS,
) -> None:
    restart_count = 0
    while True:
        started_at = datetime.now(_LOCAL_TZ)
        logger.info("Background supervisor starting task: name=%s restart=%s", name, restart_count)
        try:
            await task_factory()
            runtime_seconds = (datetime.now(_LOCAL_TZ) - started_at).total_seconds()
            logger.error(
                "Background task exited unexpectedly: name=%s runtime=%.1fs restart_in=%ss",
                name,
                runtime_seconds,
                restart_delay_seconds,
            )
        except asyncio.CancelledError:
            logger.info("Background supervisor cancelled: name=%s", name)
            raise
        except Exception:
            logger.exception(
                "Background task crashed: name=%s restart_in=%ss",
                name,
                restart_delay_seconds,
            )

        restart_count += 1
        await asyncio.sleep(restart_delay_seconds)


async def _background_monitor_loop(*, scraper_task: asyncio.Task) -> None:
    while True:
        try:
            await asyncio.sleep(BACKGROUND_MONITOR_INTERVAL_SECONDS)
            logger.info(
                "Background monitor heartbeat: scraper=%s",
                _describe_task_state(scraper_task),
            )
        except asyncio.CancelledError:
            logger.info("Background monitor cancelled")
            raise


async def main() -> None:
    _setup_logging()
    logger.info("Starting scraper...")
    await init_db()

    scraper_task = asyncio.create_task(
        _supervise_background_task(name="scraper", task_factory=run_scraper_loop)
    )
    monitor_task = asyncio.create_task(_background_monitor_loop(scraper_task=scraper_task))
    try:
        await asyncio.gather(scraper_task, monitor_task)
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
        scraper_task.cancel()
        try:
            await scraper_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
