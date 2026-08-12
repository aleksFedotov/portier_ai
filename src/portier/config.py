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

    # Автоудаление закрытых карточек в чате счетов (тикет 18): карточка
    # удаляется через столько часов после того, как нажаты обе кнопки
    # («Счёт отправлен» и «Оплачен»). PDF-документы не трогаем.
    INVOICE_CARD_TTL_HOURS: int = 24

    # Важные алерты → третья группа (правила как в MUTED_SENDERS).
    ALERT_RULES: list[str] = [
        "support@travelline.ru|возможный овербукинг",
        "noreply-haps@bronevik.com",
        "no-reply@gosuslugi.ru",
        "no-reply@rospotrebnadzor.ru",
        "notifier@fsa.gov.ru",
        "nadegda.ivanova88@yandex.ru",
        # onetwotrip глушится целиком (MUTED_SENDERS), но алерты идут раньше
        # глушения — коды входа из экстранета не потеряются.
    ]

    # Важные письма лично владельцу (тикет 15): правила «addr|шаблон темы»,
    # как в ALERT_RULES. Отличается от OWNER_NOTICE_SENDERS тем, что матчит
    # пару адрес+тема (101hotels шлёт и брони, и сверки с одного адреса).
    # «cверк» с латинской c — реальная опечатка в темах 101hotels.
    # Проверка идёт РАНЬШЕ чёрного списка: у Купера и МатСервиса весь адрес
    # заглушён, но счета/акты сверки от них владелец должен видеть.
    OWNER_NOTICE_RULES: list[str] = [
        "info@101hotels.com|сверк",
        "info@101hotels.com|cверк",
        # Тикет 19 (по corrections.json): сверки и счета от заглушённых адресов
        "hotels@info.mail.emergingtravel.com|сверка началась",
        "hotels@travel.yandex.ru|реестр завершенных бронирований",
        "info@kuper.ru|счёт на оплату",
        "service@matservice.spb.ru|акт сверки",
        "spbzavtrak@gmail.com|сверка",
        "MorozovaAD@cbtc.ru|закрывающ",
        # Тикет 21 (разбор хвоста unknown после бэктеста 10.08.2026):
        # сверка Островка с экстранет-адреса (раньше правило было только
        # для emergingtravel и не матчилось)
        "hotels@account.extranet.ostrovok.ru|сверка началась",
        # счёт KDV — только ссылка на сайт, без вложения: просто сообщение
        # владельцу, в группу счетов не отправляем
        "info@kdvonline.ru|счет на оплату",
        # безопасность почтового ящика — владельцу
        "no-reply@accounts.google.com|оповещение системы безопасности",
    ]

    # Исключения из общего правила входящих счетов: их счета идут
    # лично владельцу, а не в третью группу (у Купера ручная проверка доставки).
    INVOICE_OWNER_EXCEPTIONS: list[str] = [
        "info@kuper.ru",
    ]

    # Собственные адреса отеля: наши исходящие письма (ответы со счетами,
    # которые мы пересылаем другим компаниям) — не входящие счета, в третью
    # группу не шлём.
    OWN_EMAIL_ADDRESSES: list[str] = [
        "likihotel@gmail.com",
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
        # Тикет 19: закрывающие документы от контрагентов — владельцу
        "1c.erp@alean.ru",
    ]

    # Тикет 19: коды входа в учётные записи → третья группа (владелец и
    # гендиректор видят, что кто-то пытается войти). Отдельно от ALERT_RULES,
    # чтобы в БД и отчётах был свой тип login_code. Раньше чёрного списка:
    # accounts@kontur.ru заглушён целиком, но «Вход в сервис» важен.
    LOGIN_CODE_RULES: list[str] = [
        "hotels@account.extranet.ostrovok.ru|код",
        "hotels@info.mail.emergingtravel.com|код",
        "notification@info.mail.emergingtravel.com|код",
        "notification@account.extranet.ostrovok.ru|код",
        "accounts@kontur.ru|вход",
        # перенесено из ALERT_RULES (тикет 19): всё про коды/вход — сюда
        "info@101hotels.com|код для входа",
        "noreply@travellinemail.com|вход в учетную запись",
        "mailer@sender.ozon.ru|подтверждение учетных данных",
        "noreply@telegram.org",
        "extranet@onetwotrip.com|код",
    ]

    # Тикет 19: письма, требующие ручной обработки администратором → основная
    # группа с высоким приоритетом. Раньше чёрного списка и LLM.
    ADMIN_ATTENTION_RULES: list[str] = [
        "@v2.hbconnect.ru|заявка на бронирование",
        "noreply@travellinemail.com|незавершенная бронь",
        "@bronevik.com|подтвердите выезд",
        "@info.mail.emergingtravel.com|подтвердите бронирование",
        "@account.extranet.ostrovok.ru|подтвердите бронирование",
    ]

    # Тикет 19: отправители входящих счетов без узнаваемого вложения
    # (охрана, ККТ, хозтовары) → текстовое уведомление в третью группу,
    # как и письма с вложением-счётом (тикет 10).
    INCOMING_INVOICE_SENDERS: list[str] = [
        "cc@delta.ru",
        "noreply@delta.ru",
        "informer@delta.ru",
        "smartsoft.spb@yandex.ru",
        "kofeman.spb@mail.ru",
        "zakaz7@cosmipro.ru",
        "no-reply@lindaily.novoline.spb.ru",
        # Расчётный отдел TravelLine (тикет 15/19): счета за подписку →
        # третья группа; письма со вложением-счётом перехватываются документом.
        "accounting@travelline.ru",
    ]

    # Тикет 33: запросы на возврат денежных средств → группа входящих счетов
    # (PDF-вложения пересылаются документом). Раньше чёрного списка:
    # notify.comfortbooking.ru заглушён целиком, но возвраты важны.
    REFUND_RULES: list[str] = [
        "@notify.comfortbooking.ru|возврат",
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
    # Расшифровка подписи (ФИО) для наложения на входящие документы (тикет 31).
    # Ставится на фиолетовые метки шаблонов и под факсимиле в эвристике.
    SIGNATURE_CAPTION: str = "Генеральный директор Кузин А. С."
    # Файл шаблонов постановки печати/факсимиле (разметка владельца, тикет 31)
    STAMP_TEMPLATES_FILE: str = "stamp_templates.yaml"

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
        # Яндекс: сервисные рассылки поддержки («Переход на УПД с 01.08.26» и
        # т.п.) — LLM принимал их за запрос счёта (решение владельца 11.08.2026)
        "info-noreply@support.yandex.ru",
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
        # Тикет 19 (по corrections.json): дубли и напоминания каналов бронирования,
        # не требующие действий
        # ежедневный дайджест TravelLine и анкета гостя (отзывы не рассылаем)
        "noreply@travellinemail.com|уведомление о бронированиях",
        "noreply@travellinemail.com|гость заполнил анкету",
        # Островок: дубли подтверждений и предоплат (новые брони приходят от TravelLine)
        "hotels@info.mail.emergingtravel.com|новое бронирование",
        "hotels@account.extranet.ostrovok.ru|новое бронирование",
        "hotels@info.mail.emergingtravel.com|внесена предоплата",
        "hotels@account.extranet.ostrovok.ru|внесена предоплата",
        # Т-Банк: напоминания о заездах и повторные уведомления
        "hotels_partners@tbank.ru",
        # Суточно.ру: напоминания о заездах/выездах, предоплаты, просьбы об отзыве
        "info@sutochno.ru|напоминаем: завтра приезжают гости",
        "info@sutochno.ru|гость выезжает завтра",
        "info@sutochno.ru|гость внес предоплату",
        "info@sutochno.ru|оставить отзыв о госте",
        # Броневик: напоминания о заездах и дубли заявок
        "@bronevik.com|напоминание о заезде гостей",
        "billing@bronevik.online|заявка",
        # прочие дубли подтверждений от каналов
        "@acase.ru|бронирование",
        "24help@mail.personvip.com|подтвердите детали бронирования",
        # автоматическая переписка контрагентов без действий
        "SamoilovaSP@cbtc.ru",
        "reservation@cbtc.ru",
        "1c_mail@cbtc.ru",
        "priemspb@pegast.ru",
        "hotline88007007777@multonpartners.ru",
        "apartrent@list.ru",
        # отзывы/жалобы 2ГИС и отчёты рассылок — читаются в ЛК
        "reviews@2gis.ru",
        "uk.2gis.support@2gis.ru",
        "noreply@guest.travelline-mail.com",
        # Тикет 21 (разбор хвоста unknown после бэктеста 10.08.2026):
        # аналитический дайджест TravelLine Platform (брони с этого адреса — нужны)
        "noreply@travellinemail.com|аналитический отчет",
        # Суточно.ру: пополнения баланса и брошенные бронирования
        "info@sutochno.ru|на ваш баланс поступили средства",
        "info@sutochno.ru|не завершили бронирование",
        # закупки отеля: подтверждения заказов/доставки (счёт KDV — владельцу,
        # перехватывается OWNER_NOTICE_RULES раньше глушения)
        "dobry.market@multonpartners.com",
        "info@kdvonline.ru",
        # Google: условия использования и советы Developers
        "google-noreply@google.com",
        "googledevelopers-noreply@google.com",
        # 2ГИС «клиент ждёт ответа на отзыв»: реальный адрес noreply@
        # (в списке был вариант no-reply@ — не матчился)
        "noreply@account.2gis.com",
        # баунсы почты
        "mailer-daemon@googlemail.com",
    ]


def get_settings() -> Settings:
    return Settings()
