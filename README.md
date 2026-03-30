# Real-Estate-Scanner

Telegram bot, OLX scraper, and admin panel for managing access to the bot.

## Requirements

- Python 3.12
- PostgreSQL
- Playwright

## Setup

1. Install dependencies:
   - `pip install -e .`
2. Copy and configure env vars:
   - copy `.env.example` to `.env`
   - set `DATABASE_URL`
   - set Telegram bot token and admin panel settings if needed
3. Initialize the database:
   - `python scripts/init_db.py`
4. Install Playwright browsers if needed:
   - `python -m playwright install`

## Run

Run the bot:
- `python -m real_estate_scanner.bot.main`

Run the admin panel:
- `python -m real_estate_scanner.web.main`

Admin panel URL:
- `http://127.0.0.1:8000/login`

## Deploy to Railway

Recommended Railway layout:
- `PostgreSQL` service
- `bot` service using `Dockerfile.bot`
- `web` service using `Dockerfile.web`

Recommended steps:
1. Create a new Railway project and add a PostgreSQL database.
2. Add a `bot` service from this repo and point it to `Dockerfile.bot`.
3. Add a `web` service from this repo and point it to `Dockerfile.web`.
4. Attach a public domain only to the `web` service.

Set variables for `bot`:
- `BOT_TOKEN`
- `DATABASE_URL`

Set variables for `web`:
- `DATABASE_URL`
- `ADMIN_PANEL_ENABLED=true`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `ADMIN_SESSION_SECRET`

Notes:
- The web service automatically uses Railway `PORT` and binds to `0.0.0.0`.
- The database layer accepts Railway's default Postgres URL and normalizes it for async SQLAlchemy.
- The bot service does not need a public domain.

## Useful scripts

Initialize database schema:
- `python scripts/init_db.py`

Reset ads table:
- `python scripts/reset_ads.py`

Reset filters table:
- `python scripts/reset_db.py`

Reset broadcast state:
- `python scripts/reset_sale_broadcasts.py`

## Tests

Run tests:
- `pytest -q`
