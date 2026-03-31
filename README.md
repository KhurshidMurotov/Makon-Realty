# Real-Estate-Scanner

Telegram bot, OLX scraper, and admin panel for managing access to the bot.

Recommended production layout:
- `Railway PostgreSQL`
- `Railway bot`
- `Railway admin web`
- local Windows `scraper` running from home IP

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

Run the scraper locally:
- `python -m real_estate_scanner.scraper.main`

Admin panel URL:
- `http://127.0.0.1:8000/login`

## Deploy to Railway

Recommended Railway layout:
- `PostgreSQL` service
- `bot` service using `Dockerfile.bot`
- `web` service using `Dockerfile.web`

Production flow:
- local scraper writes to Railway PostgreSQL
- Railway bot reads from Railway PostgreSQL and sends to Telegram
- Railway web reads and updates state in the same Railway PostgreSQL

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

Set variables for local `scraper`:
- `DATABASE_URL`
- `OLX_HEADLESS=true`
- optional `OLX_PROXY_SERVER`
- optional `OLX_PROXY_USERNAME`
- optional `OLX_PROXY_PASSWORD`

Notes:
- The web service automatically uses Railway `PORT` and binds to `0.0.0.0`.
- The database layer accepts Railway's default Postgres URL and normalizes it for async SQLAlchemy.
- The bot service does not need a public domain.
- The bot service no longer runs OLX scraping itself.
- The scraper does not need `BOT_TOKEN`.

## Windows scraper service

Recommended local runtime for scraper:
- install Python 3.12
- clone the repo on your home PC
- configure `.env` with Railway production `DATABASE_URL`
- install dependencies:
  - `pip install -e .`
  - `python -m playwright install chromium`
- run or install as a Windows service:
  - `python -m real_estate_scanner.scraper.main`

Recommended service wrapper:
- `NSSM`
- service command: `python.exe -m real_estate_scanner.scraper.main`
- working directory: repo root
- startup: `Automatic`
- recovery: restart on failure
- disable Windows sleep/hibernation if scraper must run overnight

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
