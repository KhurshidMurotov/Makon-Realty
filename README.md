# Real-Estate-Scanner

## Requirements

- Python 3.12
- PostgreSQL
- Playwright (для скрейпинга OLX)

## Setup

1. Установите зависимости:
   - `pip install -e .`

2. Настройте `.env` (создайте из `.env.example` и укажите `DATABASE_URL`).

3. Инициализация БД:
   - `python scripts/init_db.py`

## Tests

Run tests:
- `pytest -q`

Если Playwright ещё не установлен в систему:
- `python -m playwright install`

## Run bot

Из корня проекта:
- `python -m real_estate_scanner.bot.main`

