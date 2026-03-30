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
