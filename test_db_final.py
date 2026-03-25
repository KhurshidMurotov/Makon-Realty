import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent / "src"))

from sqlalchemy import text  # noqa: E402

from real_estate_scanner.db.session import AsyncSessionLocal  # noqa: E402


async def main() -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT 1"))
        value = result.scalar_one()
        print(f"DB connection OK, SELECT 1 = {value}")


if __name__ == "__main__":
    asyncio.run(main())

