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

    # Admin panel settings
    ADMIN_PANEL_ENABLED: bool = False
    ADMIN_USERNAME: str | None = None
    ADMIN_PASSWORD: str | None = None
    ADMIN_SESSION_SECRET: str | None = None
    ADMIN_WEB_HOST: str = "127.0.0.1"
    ADMIN_WEB_PORT: int = 8000

    # Used in later modules for URL building
    OLX_BASE_URL: str = "https://www.olx.uz"

    # Optional proxy for OLX Playwright traffic
    OLX_PROXY_SERVER: str | None = None
    OLX_PROXY_USERNAME: str | None = None
    OLX_PROXY_PASSWORD: str | None = None

    # Telegram Mini App URL (must be public HTTPS for Telegram)
    MINI_APP_URL: str = "https://khurshidmurotov.github.io/Real-Estate-Scanner/"

    # If OLX price is in UZS (сум), but user entered price as USD in the Mini App,
    # we can try a fallback matching with currency conversion.
    # Conversion is only used if the first matching attempt returns no users.
    USD_TO_SUM_RATE: float = 12000.0


settings = Settings()

