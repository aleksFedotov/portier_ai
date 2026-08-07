"""Конфигурация сервиса Portier AI из переменных окружения / .env."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Gmail API (OAuth 2.0)
    GOOGLE_CREDENTIALS_FILE: str = "credentials.json"
    GOOGLE_TOKEN_FILE: str = "data/token.json"

    # Опрос
    CHECK_INTERVAL_SECONDS: int = 180
    BACKLOG_DAYS: int = 7

    # OpenAI
    OPENAI_API_KEY: str
    OPENAI_MODEL: str = "gpt-4o-mini"

    # Telegram (обязательны только для боевого режима, не для бэктеста)
    TELEGRAM_BOT_TOKEN: str | None = None
    TELEGRAM_CHAT_ID: int | None = None

    # База данных
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/hotel_bot.db"

    # Реквизиты отеля для счетов
    HOTEL_NAME: str = ""
    HOTEL_INN: str = ""
    HOTEL_DETAILS: str = ""

    # Каталог для PDF-счетов
    INVOICES_DIR: str = "data/invoices"

    # Веб-панель реестра компаний (LAN, без авторизации — ограничение MVP)
    WEB_PORT: int = 8080
    WEB_HOST: str = "0.0.0.0"

    # Файл сида компаний (если существует и таблица пуста)
    COMPANIES_SEED_FILE: str = "companies.yaml"


def get_settings() -> Settings:
    return Settings()
