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

    # Чёрный список отправителей (тикет 08): "addr" или "addr|шаблон темы".
    # В .env можно переопределить JSON-массивом.
    # НЕ глушим (до тикета 10): info@kuper.ru (счета — владельцу),
    # support@travelline.ru («Возможный овербукинг» — важный алерт).
    MUTED_SENDERS: list[str] = [
        # реестры/отчёты, которые читаются вручную в ЛК
        "info@notify.comfortbooking.ru",
        "noreply@raiffeisen.ru",
        # TravelLine: только карты лояльности (брони с этого адреса — нужны)
        "noreply@travellinemail.com|выдана карта лояльности",
        # служебные уведомления без действий
        "service@matservice.spb.ru",
        "extranet@onetwotrip.com",
        "hotels_info@tbank.ru",
        # внутренняя переписка
        "likihotel@gmail.com",
        "jimmysonfire@gmail.com",
        # Ozon (включая ответы поддержки — решение от 07.08.2026)
        "infohotels@ozon.ru",
        # маркетинг
        "no-reply@hermitage.ru",
        "no-reply@account.2gis.com",
        "business@2gis.ru",
        "d.minaeva@spb.2gis.ru",
        "partners-hotel@tutu.ru",
        "bro@bronevik.com",
        "attention@hello.delta.ru",
        "info@e.sutochno.ru",
        "noreply@travelline.com",
        "noreply@guest.travelline",
        "email@business.yandex.ru|статистика за неделю",
        "hotelier-news@travel.yandex.ru",
        "hotelier-info@travel.yandex.ru",
        "info@mail.extranet.ostrovok.ru",
        # разовые КП и рассылки
        "info@mh78.ru",
        "726309@mail.ru",
        "manager2@okspresso.ru",
        "roman.orlov@vseinstrumenti.ru",
        "info@site.hh.ru",
        "diadoc@kontur.ru",
        "markirovka@kontur.ru",
        "accounts@kontur.ru",
        "info@zoon.ru",
        "marketing@spectrum.ru",
        "order@traveldb.io",
        "office2@ales.spb.ru",
        "spb@mservice.group",
        "inform@emails.tinkoff.ru",
        "noreply@dobrymrktrf.ru",
        "partner-info@acase.ru",
        "megakatia1@mail.ru",
        "hotelpartner@hrs.com",
    ]


def get_settings() -> Settings:
    return Settings()
