from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        extra="ignore",
    )

    # Async SQLAlchemy URL (e.g. postgresql+asyncpg://user:pass@host:5432/dbname)
    DATABASE_URL: str

    # Telegram bot token (used in later modules)
    BOT_TOKEN: str | None = None

    # Used in later modules for URL building
    OLX_BASE_URL: str = "https://www.olx.uz"

    # Telegram Mini App URL (must be public HTTPS for Telegram)
    MINI_APP_URL: str = "https://khurshidmurotov.github.io/Real-Estate-Scanner/"

    # If OLX price is in UZS (сум), but user entered price as USD in the Mini App,
    # we can try a fallback matching with currency conversion.
    # Conversion is only used if the first matching attempt returns no users.
    USD_TO_SUM_RATE: float = 12000.0


settings = Settings()

