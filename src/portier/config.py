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

    # Тихие часы (тикет 17): в окне [START, END) почта не опрашивается —
    # ночью ничего не приходит в Telegram, утром обрабатывается накопившееся.
    # Локальное время сервера. Одинаковые значения (напр. 0 и 0) — режим выкл.
    QUIET_HOURS_START: int = 23
    QUIET_HOURS_END: int = 7

    # LLM-провайдер (тикет 07): "openai" или "deepseek".
    # Ключи храним оба — переключение одной строкой LLM_PROVIDER.
    LLM_PROVIDER: str = "openai"

    # OpenAI
    OPENAI_API_KEY: str | None = None
    OPENAI_MODEL: str = "gpt-4o-mini"

    # DeepSeek (OpenAI-совместимый API, из РФ доступен без VPN)
    DEEPSEEK_API_KEY: str | None = None
    DEEPSEEK_MODEL: str = "deepseek-chat"
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"

    @property
    def llm_model(self) -> str:
        """Модель активного провайдера."""
        if self.LLM_PROVIDER == "deepseek":
            return self.DEEPSEEK_MODEL
        return self.OPENAI_MODEL

    # Telegram (обязательны только для боевого режима, не для бэктеста)
    TELEGRAM_BOT_TOKEN: str | None = None
    TELEGRAM_CHAT_ID: int | None = None

    # Устойчивость к замедлению Telegram (тикет 16). Прокси нужен, только
    # если туннель нельзя поднять на уровне сервера: http://user:pass@host:port
    # или socks5://host:port. Пусто — прямое соединение.
    TELEGRAM_PROXY: str | None = None
    TELEGRAM_TIMEOUT: float = 60.0
    TELEGRAM_RETRY_ATTEMPTS: int = 4
    TELEGRAM_RETRY_BASE_DELAY: float = 2.0

    # Личный чат владельца (тикет 09): выжимки реестров Яндекса и уведомления
    # о документах «для ручной обработки». Если не задан — слать в основной чат.
    OWNER_CHAT_ID: int | None = None

    # Третья группа «входящие счета и важные алерты» (тикет 10).
    # Fallback: владелец → основной чат.
    INCOMING_INVOICES_CHAT_ID: int | None = None

    # Отдельный чат для ВЫСТАВЛЯЕМЫХ счетов (тикет 06): уведомления
    # invoice_required + PDF и команда /invoices. Не задан — всё в основной чат.
    INVOICE_CHAT_ID: int | None = None

    # Важные алерты → третья группа (правила как в MUTED_SENDERS).
    ALERT_RULES: list[str] = [
        "support@travelline.ru|возможный овербукинг",
        "noreply-haps@bronevik.com",
        "no-reply@gosuslugi.ru",
        "no-reply@rospotrebnadzor.ru",
        "notifier@fsa.gov.ru",
        "nadegda.ivanova88@yandex.ru",
        # Расчётный отдел TravelLine (тикет 15): все письма → третья группа;
        # письма со вложением-счётом перехватываются раньше (тикет 10) документом.
        "accounting@travelline.ru",
        # Коды входа в учётные записи (тикет 15) — владелец и гендиректор
        # должны видеть, что кто-то пытается войти.
        "info@101hotels.com|код для входа",
        "noreply@travellinemail.com|вход в учетную запись",
        "mailer@sender.ozon.ru|подтверждение учетных данных",
        "noreply@telegram.org",
        # onetwotrip глушится целиком (MUTED_SENDERS), но алерты идут раньше
        # глушения — коды входа из экстранета не потеряются.
        "extranet@onetwotrip.com|код",
    ]

    # Важные письма лично владельцу (тикет 15): правила «addr|шаблон темы»,
    # как в ALERT_RULES. Отличается от OWNER_NOTICE_SENDERS тем, что матчит
    # пару адрес+тема (101hotels шлёт и брони, и сверки с одного адреса).
    # «cверк» с латинской c — реальная опечатка в темах 101hotels.
    OWNER_NOTICE_RULES: list[str] = [
        "info@101hotels.com|сверк",
        "info@101hotels.com|cверк",
    ]

    # Исключения из общего правила входящих счетов: их счета идут
    # лично владельцу, а не в третью группу (у Купера ручная проверка доставки).
    INVOICE_OWNER_EXCEPTIONS: list[str] = [
        "info@kuper.ru",
    ]

    # Отправители, о письмах которых уведомляем владельца лично (без LLM):
    # отчёты агентов, реестры Отелло, акты сверки — обрабатываются вручную.
    OWNER_NOTICE_SENDERS: list[str] = [
        "otello@2gis.ru",
        "agentsreports@cbtc.ru",
        "agent@bronevik.com",
        "anastasiya.ryabinkina@pegast.ru",
        "e.morozova@hbpro.expert",
        "buh7@anextour.com",
        "buh@trivio.ru",
        "sverka@tutu.ru",
        "finance@101hotels.com",
        "hotels_doc@onetwotrip.com",
        "buh@ozon.travel",
        "info.russia@lindaily.com",
        "fin50@roomlink.ru",
    ]

    # База данных
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/hotel_bot.db"

    # Реквизиты отеля для счетов (тикет 11: полный банковский блок)
    HOTEL_NAME: str = ""
    HOTEL_INN: str = ""
    HOTEL_KPP: str = ""
    HOTEL_ADDRESS: str = ""
    HOTEL_PHONE: str = ""
    HOTEL_EMAIL: str = ""
    HOTEL_RS: str = ""
    HOTEL_BANK: str = ""
    HOTEL_BIK: str = ""
    HOTEL_KS: str = ""

    # Печать и факсимиле, накладываются на счёт сразу (тикет 11, директор разрешил)
    INVOICE_STAMP_PATH: str = "data/печать 2-Photoroom.png"
    INVOICE_SIGNATURE_PATH: str = "data/подпись 2-Photoroom.png"
    INVOICE_LOGO_PATH: str = "data/logo.jpg"

    # Каталог для PDF-счетов
    INVOICES_DIR: str = "data/invoices"

    # Веб-панель реестра компаний (LAN, без авторизации — ограничение MVP)
    WEB_PORT: int = 8080
    WEB_HOST: str = "0.0.0.0"

    # Файл сида компаний (если существует и таблица пуста)
    COMPANIES_SEED_FILE: str = "companies.yaml"

    # Файл сида справочника агентов (тикет 13, если существует и таблица пуста)
    AGENTS_SEED_FILE: str = "agents.yaml"

    # Чёрный список отправителей (тикет 08): "addr" или "addr|шаблон темы".
    # В .env можно переопределить JSON-массивом.
    # Перехваты тикета 10 идут РАНЬШЕ глушения: счета Купера и алерт
    # об овербукинге обрабатываются, остальное от этих адресов — глушится.
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
        "info@kuper.ru",
        "support@travelline.ru",
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
