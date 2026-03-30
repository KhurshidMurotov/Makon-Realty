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

## Run admin panel

1. Заполните в `.env`:
   - `ADMIN_PANEL_ENABLED=true`
   - `ADMIN_USERNAME=...`
   - `ADMIN_PASSWORD=...`
   - `ADMIN_SESSION_SECRET=...`
   - `ADMIN_WEB_HOST=127.0.0.1`
   - `ADMIN_WEB_PORT=8000`

2. Запустите:
   - `python -m real_estate_scanner.web.main`

3. Откройте:
   - `http://127.0.0.1:8000/login`

