from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    # Async SQLAlchemy URL (e.g. postgresql+asyncpg://user:pass@host:5432/dbname)
    DATABASE_URL: str

    # Telegram bot token (used in later modules)
    BOT_TOKEN: str | None = None

    # Used in later modules for URL building
    OLX_BASE_URL: str = "https://www.olx.uz"

    # Telegram Mini App URL (must be public HTTPS for Telegram)
    MINI_APP_URL: str = "https://example.com/real-estate-scanner/webapp/index.html"


settings = Settings()

