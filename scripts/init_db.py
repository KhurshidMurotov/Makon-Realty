import asyncio
import sys
from pathlib import Path

# Allow running script directly: `python scripts/init_db.py`
sys.path.append(str(Path(__file__).resolve().parent.parent / "src"))

from real_estate_scanner.db.init_db import init_db  # noqa: E402


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


if __name__ == "__main__":
    print("Initializing database schema (CREATE TABLE)...")
    asyncio.run(init_db())
    print("Database schema initialized.")

