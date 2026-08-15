"""Gmail API клиент и конвейер обработки писем.

Опрос по курсору (internalDate последнего обработанного письма): список новых
писем → заголовки → дедуп по message_id в БД → тело только новых → конвейер
(очистка → PII-mask → LLM → unmask → роутер). Критерий «обработано» — метка
Gmail `portier-processed` (тикет 14): выбираем письма без неё, после обработки
вешаем метку и помечаем письмо прочитанным. При ошибке обработки метку НЕ
вешаем — письмо попадёт в повторную обработку.
"""

import asyncio
import base64
import email.utils
import logging
import os
import re
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from .cleaner import clean_email
from .config import Settings
from .db import get_session_factory
from .handlers.router import route_notification
from .handlers.templates import _row, esc
from .bot import RETRYABLE_ERRORS, send_document, send_notification
from .llm import analyze_email
from .incoming import DOC_SUFFIXES, is_alert, is_invoice_filename, is_own_address
from .models import ActionLog, ActionLogStatus, EmailStatus, ProcessedEmail
from .muted import _extract_addr, is_muted
from .yandex_registry import is_yandex_registry

logger = logging.getLogger(__name__)


def _owner_chat(settings: Settings) -> int | None:
    """Личный чат владельца; если не настроен — основной (чтобы ничего не потерять)."""
    return settings.OWNER_CHAT_ID or settings.TELEGRAM_CHAT_ID


def _invoices_chat(settings: Settings) -> int | None:
    """Третья группа «входящие счета и алерты»; fallback: владелец → основной."""
    return settings.INCOMING_INVOICES_CHAT_ID or _owner_chat(settings)


# Типы, которые фиксируем в БД без уведомления в Telegram (тикет 15).
# Исключение — booking_confirmed с комментарием гостя: комментарий нужен
# администраторам (отдельные письма-комментарии дублируются и молчат).
# review_notification убран 13.08.2026 (решение владельца): отзывы (2ГИС,
# Яндекс.Бизнес) показываем администраторам карточкой в основной группе.
SILENT_TYPES = frozenset({
    "booking_confirmed",
    "booking_comment",
    "booking_modified",
    "booking_cancelled",
    "payment_received",
    "unknown",
})

# Тикет 25: ответ в треде (Re:/Fwd:) — живая переписка человека, а не
# письмо-заявка канала. Автосчета из таких писем не выставляем (решение
# владельца 10.08.2026): invoice_required и booking_comment из ответов
# понижаем до guest_message — карточка уходит админам, PDF не генерится.
_REPLY_SUBJECT_RE = re.compile(r"^\s*(re(\[\d+\])?|fwd?)\s*:", re.IGNORECASE)
_REPLY_DOWNGRADE_TYPES = frozenset({"invoice_required", "booking_comment"})

# Агенты, которым тема черновика счёта — только «#<номер брони>» (без
# текстового префикса). Решение владельца: Броневику — просто #62158429.
_SUBJECT_NUMBER_ONLY_AGENTS = ("bronevik", "броневик")

# Сервисные «комментарии» каналов — это контакты гостя, а не запрос к отелю
# («For contacting the guest please dial: +… (verification code: …)»).
# Такие уведомления в чат не шлём, письмо просто фиксируем в БД
# (решение владельца).
_SERVICE_COMMENT_RE = re.compile(
    r"for contacting the guest|verification code|please dial", re.IGNORECASE
)
_SERVICE_COMMENT_TYPES = frozenset(
    {"booking_comment", "guest_message", "booking_confirmed"}
)

# Брони Яндекс Путешествий приходят письмами «изменение бронирования», а не
# «новое бронирование» — напоминание «отредактируйте бронь» (тикет 30) шлём
# им и по booking_modified (решение владельца 11.08.2026).
_EDIT_NOTICE_ON_MODIFIED_AGENTS = ("яндекс путешествия", "yandex travel")


def _is_reply_subject(subject: str) -> bool:
    """Тема — ответ/пересылка в треде: «Re:», «Re[2]:», «Fwd:», «FW: …»."""
    return bool(_REPLY_SUBJECT_RE.match(subject or ""))

# Метка «письмо обработано ботом» (тикет 14). Выборка строится по ней,
# а не по непрочитанности: прочитанное сотрудником письмо всё равно обработается.
PROCESSED_LABEL = "portier-processed"

# Статус-сигнал для check_once: письмо уже обработано ранее (дедуп по БД) —
# метку вешаем, чтобы Gmail больше его не возвращал.
ALREADY_PROCESSED = "already_processed"

# Статусы, при которых check_once вешает метку PROCESSED_LABEL на письмо.
LABEL_OK_STATUSES = frozenset({
    EmailStatus.SUCCESS.value, EmailStatus.SKIPPED.value, ALREADY_PROCESSED,
})

# Тикет 26: сколько раз переотправляем счёт при сетевом сбое Telegram /
# Gmail (черновик, тикет 27), прежде чем сдаться (ERROR + алерт админам).
# Попытка = цикл опроса, внутри неё ещё 4 retry (тикет 16).
MAX_INVOICE_ACTION_ATTEMPTS = 10

# Действия счёта, чьи попытки суммируются для лимита MAX_INVOICE_ACTION_ATTEMPTS.
INVOICE_ACTION_TYPES = ("invoice_gmail_draft",)


def _is_transient_gmail_error(exc: Exception) -> bool:
    """Временный сбой Gmail API (сеть, 5xx, 429) — стоит повторить позже."""
    try:
        from googleapiclient.errors import HttpError
    except ImportError:  # pragma: no cover
        HttpError = ()
    if isinstance(exc, HttpError):
        return exc.resp.status in (429, 500, 502, 503, 504)
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
    # Напоминания о задачах из Google Календаря (calendar_tasks.py)
    "https://www.googleapis.com/auth/calendar.readonly",
]

_HEADER_NAMES = ["Message-ID", "From", "Subject", "Date"]


class GmailAuthError(RuntimeError):
    """Нет валидного OAuth-токена Gmail."""


def get_credentials(settings: Settings):
    """Загрузить/обновить OAuth-токен из кэша. Бросает GmailAuthError."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_file = settings.GOOGLE_TOKEN_FILE
    creds = None
    if os.path.exists(token_file):
        # Scope'ы не передаём: refresh пойдёт со scope'ами, выданными при
        # авторизации. Иначе расширение SCOPES (например, +calendar) ломает
        # refresh старого токена (invalid_scope) и роняет весь бот.
        creds = Credentials.from_authorized_user_file(token_file)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_credentials(creds, token_file)
        except Exception:
            logger.warning("Не удалось обновить OAuth-токен", exc_info=True)
            creds = None
    if not creds or not creds.valid:
        raise GmailAuthError(
            f"Нет валидного токена Gmail ({token_file}).\n"
            "Выполните первичную авторизацию командой:\n"
            "  python -m portier.gmail_auth\n"
            "Она откроет ссылку авторизации Google в браузере; после подтверждения "
            f"токен будет сохранён в {token_file} и переиспользован."
        )
    return creds


def _save_credentials(creds, token_file: str) -> None:
    os.makedirs(os.path.dirname(token_file) or ".", exist_ok=True)
    with open(token_file, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())


def parse_headers(payload: dict, internal_date: int | None = None) -> dict:
    """Разобрать заголовки из payload сообщения Gmail API."""
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    return {
        "message_id": (headers.get("message-id") or "").strip() or None,
        "sender": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "internal_date": internal_date,
        "attachments": _attachment_names(payload),
    }


def _attachment_names(payload: dict) -> list[str]:
    """Имена файлов-вложений из структуры письма (без скачивания содержимого)."""
    return [
        part["filename"]
        for part in _walk_parts(payload)
        if part.get("filename")
    ]


def _decode_part(part: dict) -> str | None:
    data = (part.get("body") or {}).get("data")
    if not data:
        return None
    raw = base64.urlsafe_b64decode(data.encode("ascii"))
    return raw.decode("utf-8", errors="replace")


def _walk_parts(payload: dict):
    yield payload
    for part in payload.get("parts", []) or []:
        yield from _walk_parts(part)


def extract_body_from_payload(payload: dict) -> str:
    """Извлечь текст из payload Gmail: предпочитаем text/plain, иначе text/html."""
    plain, html_part = None, None
    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        if mime == "text/plain" and plain is None:
            plain = _decode_part(part)
        elif mime == "text/html" and html_part is None:
            html_part = _decode_part(part)
    if plain:
        return clean_email(plain)
    if html_part:
        return clean_email(html_part)
    return ""


def build_query(after_epoch_seconds: int | None, backlog_days: int) -> str:
    """Поисковый запрос Gmail: письма после курсора либо за глубину backlog.

    Минусуем только явный мусор (промоакции, соцсети, форумы), а НЕ
    ограничиваемся category:primary (тикет 26): Gmail может перенести
    письмо канала во вкладку «Оповещения» (CATEGORY_UPDATES) задним числом,
    и тогда оно выпадает из выборки — живой случай с подтверждением Bronevik.
    -label:portier-processed — без метки «обработано» (тикет 14): прочитанность
    письма значения не имеет, повторно обработанные не приходят.
    """
    if after_epoch_seconds is None:
        after_epoch_seconds = int(
            (datetime.now() - timedelta(days=backlog_days)).timestamp()
        )
    return (
        f"after:{after_epoch_seconds} "
        f"-category:promotions -category:social -category:forums "
        f"-label:{PROCESSED_LABEL}"
    )


def in_quiet_hours(now: datetime, start: int, end: int) -> bool:
    """Тихие часы (тикет 17): окно [start, end) по локальному времени.

    Окно через полночь (23 → 7): тихо, если час >= start ИЛИ < end.
    start == end — режим выключен.
    """
    if start == end:
        return False
    hour = now.hour
    if start < end:  # дневное окно, напр. 12–18
        return start <= hour < end
    return hour >= start or hour < end


class GmailClient:
    """Тонкая обёртка над Gmail API (sync SDK гоняется через asyncio.to_thread)."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._service = None
        self._processed_label_id: str | None = None

    def _build(self):
        from googleapiclient.discovery import build

        creds = get_credentials(self._settings)
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    async def connect(self) -> None:
        self._service = await asyncio.to_thread(self._build)
        logger.info("Gmail API: авторизация успешна")

    async def close(self) -> None:
        if self._service is not None:
            try:
                await asyncio.to_thread(self._service.close)
            except Exception:
                logger.warning("Gmail: ошибка при закрытии соединения", exc_info=True)
            self._service = None

    async def _ensure(self):
        if self._service is None:
            await self.connect()
        return self._service

    async def list_new_message_ids(
        self, after_epoch_seconds: int | None, backlog_days: int
    ) -> list[str]:
        """ID новых писем (от старых к новым) по курсору или глубине backlog."""
        service = await self._ensure()
        query = build_query(after_epoch_seconds, backlog_days)

        def _list_all() -> list[str]:
            ids: list[str] = []
            page_token = None
            while True:
                resp = (
                    service.users()
                    .messages()
                    .list(userId="me", q=query, pageToken=page_token)
                    .execute()
                )
                ids.extend(m["id"] for m in resp.get("messages", []))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    return ids

        ids = await asyncio.to_thread(_list_all)
        ids.reverse()  # Gmail отдаёт от новых — обрабатываем от старых
        return ids

    async def fetch_headers(self, gmail_id: str) -> dict:
        """Только заголовки письма (format=metadata)."""
        service = await self._ensure()

        def _get():
            return (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=gmail_id,
                    format="metadata",
                    metadataHeaders=_HEADER_NAMES,
                )
                .execute()
            )

        msg = await asyncio.to_thread(_get)
        internal = msg.get("internalDate")
        return parse_headers(
            msg.get("payload", {}), int(internal) if internal else None
        )

    async def fetch_body_text(self, gmail_id: str) -> str:
        """Полное тело письма (format=full), очищенное до текста."""
        service = await self._ensure()

        def _get():
            return (
                service.users()
                .messages()
                .get(userId="me", id=gmail_id, format="full")
                .execute()
            )

        msg = await asyncio.to_thread(_get)
        return extract_body_from_payload(msg.get("payload", {}))

    async def fetch_attachments(
        self, gmail_id: str, suffix: str | tuple[str, ...] = ".xlsx"
    ) -> list[tuple[str, bytes]]:
        """Скачать вложения письма с заданным расширением(ями): [(имя файла, байты)]."""
        service = await self._ensure()

        def _get():
            return (
                service.users()
                .messages()
                .get(userId="me", id=gmail_id, format="full")
                .execute()
            )

        msg = await asyncio.to_thread(_get)
        suffixes = (suffix,) if isinstance(suffix, str) else suffix
        result: list[tuple[str, bytes]] = []
        for part in _walk_parts(msg.get("payload", {})):
            filename = part.get("filename") or ""
            if not filename.lower().endswith(suffixes):
                continue
            att_id = part.get("body", {}).get("attachmentId")
            if not att_id:
                continue

            def _download(att_id=att_id):
                return (
                    service.users()
                    .messages()
                    .attachments()
                    .get(userId="me", messageId=gmail_id, id=att_id)
                    .execute()
                )

            att = await asyncio.to_thread(_download)
            result.append((filename, base64.urlsafe_b64decode(att["data"])))
        return result

    async def create_draft(self, raw_message: str, thread_id: str | None = None) -> str:
        """Создать черновик в Gmail (users.drafts.create), вернуть ID черновика.

        thread_id — положить черновик ответом в существующий тред (тикет 31).
        """
        service = await self._ensure()

        def _create():
            message = {"raw": raw_message}
            if thread_id:
                message["threadId"] = thread_id
            return (
                service.users()
                .drafts()
                .create(userId="me", body={"message": message})
                .execute()
            )

        draft = await asyncio.to_thread(_create)
        logger.info("Черновик создан в Gmail: %s", draft.get("id"))
        return draft["id"]

    async def fetch_thread_id(self, gmail_id: str) -> str | None:
        """threadId письма — чтобы черновик-ответ лёг в тот же тред (тикет 31)."""
        service = await self._ensure()

        def _get():
            return (
                service.users()
                .messages()
                .get(userId="me", id=gmail_id, format="metadata")
                .execute()
            )

        msg = await asyncio.to_thread(_get)
        return msg.get("threadId")

    async def _processed_label(self) -> str:
        """ID метки portier-processed; метка создаётся при первом обращении."""
        if self._processed_label_id:
            return self._processed_label_id
        service = await self._ensure()

        def _find_or_create() -> str:
            labels = (
                service.users().labels().list(userId="me").execute().get("labels", [])
            )
            for label in labels:
                if label.get("name") == PROCESSED_LABEL:
                    return label["id"]
            created = (
                service.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": PROCESSED_LABEL,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                .execute()
            )
            logger.info("Создана метка Gmail %s", PROCESSED_LABEL)
            return created["id"]

        self._processed_label_id = await asyncio.to_thread(_find_or_create)
        return self._processed_label_id

    async def mark_processed(self, gmail_id: str) -> None:
        """Повесить метку portier-processed. Письмо остаётся непрочитанным (тикет 20):
        двойная проверка — программа обработала, администратор видит письмо сам."""
        label_id = await self._processed_label()
        service = await self._ensure()

        def _modify():
            return (
                service.users()
                .messages()
                .modify(
                    userId="me",
                    id=gmail_id,
                    body={"addLabelIds": [label_id]},
                )
                .execute()
            )

        await asyncio.to_thread(_modify)
        logger.info("Письмо %s: метка %s (остаётся непрочитанным)", gmail_id, PROCESSED_LABEL)


async def get_last_uid(session) -> int | None:
    """Курсор: максимальный internalDate (мс) обработанного письма (None — база пуста)."""
    result = await session.execute(select(func.max(ProcessedEmail.uid)))
    return result.scalar_one_or_none()


async def get_oldest_pending_uid(session) -> int | None:
    """Uid самого старого PENDING-письма (тикет 26).

    Зависшее PENDING-письмо старше основного курсора иначе никогда не вернётся
    из Gmail-выборки: окно after: двигается вперёд, а метки portier-processed
    на нём нет — окно должно его захватывать.
    """
    result = await session.execute(
        select(func.min(ProcessedEmail.uid)).where(
            ProcessedEmail.status == EmailStatus.PENDING.value
        )
    )
    return result.scalar_one_or_none()


async def is_processed(session, message_id: str) -> bool:
    """Дедупликация по message_id. Обработанными считаются SUCCESS/ERROR/SKIPPED:
    зависшие PENDING (падение процесса до LLM) обрабатываются повторно.
    Fallback по uid намеренно не используется: uid = internalDate (мс) может
    совпадать у разных писем.
    """
    result = await session.execute(
        select(ProcessedEmail.id).where(
            ProcessedEmail.message_id == message_id,
            ProcessedEmail.status.in_([
                EmailStatus.SUCCESS.value, EmailStatus.ERROR.value, EmailStatus.SKIPPED.value,
            ]),
        )
    )
    return result.scalar_one_or_none() is not None


def make_message_id(headers: dict, gmail_id: str) -> str:
    """Message-ID письма; при отсутствии — синтетический из internalDate и ID Gmail."""
    if headers["message_id"]:
        return headers["message_id"]
    uid = headers["internal_date"] or 0
    return f"gmail-{uid}-{gmail_id}"


def cursor_after_seconds(last_uid: int | None) -> int | None:
    """Курсор для запроса after: с запасом 2 секунды (у Gmail секундная
    гранулярность, а uid хранит миллисекунды). Повторы покрывает дедуп."""
    if not last_uid:
        return None
    return max(last_uid // 1000 - 2, 0)


async def _run_action(session, email_id: int, action_type: str, fn) -> bool:
    """Идемпотентное внешнее действие (тикет 23).

    Ключ идемпотентности — (email_id, action_type): если SUCCESS-лог уже есть,
    fn не выполняется (действие не дублируется при повторной обработке PENDING)
    и возвращается False. Иначе выполняем fn(), пишем SUCCESS-лог; при
    исключении — FAILED-лог с текстом ошибки и пробрасываем исключение дальше
    (письмо остаётся PENDING и будет обработано повторно).

    fn — callable без аргументов, возвращающий awaitable.
    """
    log = (await session.execute(
        select(ActionLog).where(
            ActionLog.email_id == email_id,
            ActionLog.action_type == action_type,
        )
    )).scalar_one_or_none()
    if log is not None and log.status == ActionLogStatus.SUCCESS.value:
        logger.info("Действие %s по письму %s уже выполнено — пропуск", action_type, email_id)
        return False
    try:
        await fn()
    except Exception as exc:
        if log is None:
            log = ActionLog(email_id=email_id, action_type=action_type)
            session.add(log)
        else:
            log.attempts += 1
        log.status = ActionLogStatus.FAILED.value
        log.error_message = f"{type(exc).__name__}: {exc}"
        await session.commit()
        raise
    if log is None:
        log = ActionLog(email_id=email_id, action_type=action_type)
        session.add(log)
    else:
        log.attempts += 1
    log.status = ActionLogStatus.SUCCESS.value
    log.error_message = None
    await session.commit()
    return True


async def _notify_error(bot, chat_id: int, sender: str, subject: str, error: str) -> None:
    """Предупредить администраторов о письме, которое не удалось обработать."""
    from .bot import send_notification

    text = (
        "⚠️ <b>Не удалось обработать письмо</b>\n\n"
        f"📧 От: {esc(sender)}\n"
        f"📌 Тема: {esc(subject)}\n"
        f"🛠 Ошибка: {esc(error)}"
    )
    try:
        await send_notification(bot, chat_id, text[:4096])
    except Exception:
        logger.error("Не удалось отправить уведомление об ошибке", exc_info=True)


async def analyze_body(
    settings: Settings, sender: str, subject: str, body_text: str
) -> tuple:
    """Маскирование PII → LLM → обратная подстановка.

    Возвращает (EmailAnalysisResult с полными данными, карта PII).
    LLM получает только замаскированный текст.
    """
    from .pii import _Masker, mask_pii, unmask_pii

    # Общий masker: одно значение → один токен во всех полях письма
    masker = _Masker()
    masked_body, _ = mask_pii(body_text, masker)
    masked_sender, _ = mask_pii(sender, masker)
    masked_subject, _ = mask_pii(subject, masker)
    # getattr: в тестах settings собирается SimpleNamespace-ом без llm_model/LLM_PROVIDER
    model = getattr(settings, "llm_model", None) or settings.OPENAI_MODEL
    json_mode = getattr(settings, "LLM_PROVIDER", "openai") == "deepseek"
    result = await analyze_email(
        _llm_client, model,
        sender=masked_sender, subject=masked_subject, body=masked_body,
        json_mode=json_mode,
    )
    _unmask_result(result, masker.mapping)
    return result, masker.mapping


def _unmask_result(result, mapping: dict) -> None:
    """Обратная подстановка PII во все строковые поля результата (включая invoice)."""
    from .pii import unmask_pii

    for field, value in result.model_dump().items():
        if isinstance(value, str):
            setattr(result, field, unmask_pii(value, mapping))
    invoice = getattr(result, "invoice", None)  # блок появляется в тикете 02
    if invoice is not None:
        for field, value in invoice.model_dump().items():
            if isinstance(value, str):
                setattr(invoice, field, unmask_pii(value, mapping))


async def process_email(gmail: GmailClient, bot, settings: Settings, gmail_id: str) -> str:
    """Полный конвейер одного письма: заголовки → дедуп → тело → LLM → Telegram.

    Возвращает финальный статус (SUCCESS/ERROR/SKIPPED или ALREADY_PROCESSED
    при дедупе) — по нему check_once решает, вешать ли метку PROCESSED_LABEL.
    """
    session_factory = get_session_factory()
    headers = await gmail.fetch_headers(gmail_id)
    uid = headers["internal_date"] or 0
    message_id = make_message_id(headers, gmail_id)

    async with session_factory() as session:
        if await is_processed(session, message_id):
            logger.info("Письмо %s уже обработано, пропуск", gmail_id)
            return ALREADY_PROCESSED

        body_text = await gmail.fetch_body_text(gmail_id)

        # Повторная обработка зависшего PENDING (тикет 23): запись с этим
        # message_id уже есть — обновляем её, а не создаём новую (message_id
        # уникален, вставка упала бы на IntegrityError). Вместе с журналом
        # action_logs это убирает дубли отправок в Telegram.
        record = (await session.execute(
            select(ProcessedEmail).where(ProcessedEmail.message_id == message_id)
        )).scalar_one_or_none()
        if record is not None:
            record.uid = uid
            record.gmail_id = gmail_id
            record.sender = headers["sender"]
            record.subject = headers["subject"]
            record.received_at = _parse_date(headers["date"])
            record.processed_at = datetime.utcnow()
            record.raw_payload = body_text
            record.error_log = None
            await session.commit()
        else:
            record = ProcessedEmail(
                message_id=message_id,
                uid=uid,
                gmail_id=gmail_id,
                sender=headers["sender"],
                subject=headers["subject"],
                received_at=_parse_date(headers["date"]),
                processed_at=datetime.utcnow(),
                raw_payload=body_text,
                status=EmailStatus.PENDING.value,
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)

        # Важные алерты (тикет 10): текстовое уведомление в третью группу.
        # Идёт раньше глушения — овербукинг важнее рассылок TravellLine.
        if is_alert(record.sender, record.subject, settings.ALERT_RULES):
            record.email_type = "alert"
            text = (
                "🚨 <b>Важное уведомление</b>\n\n"
                f"📧 От: {esc(record.sender)}\n"
                f"📌 Тема: {esc(record.subject)}"
            )
            await _run_action(
                session, record.id, "alert_notice",
                lambda: send_notification(bot, _invoices_chat(settings), text),
            )
            record.status = EmailStatus.SUCCESS.value
            await session.commit()
            return record.status

        # Входящие счета (тикет 10): вложение-счёт → документ в третью группу,
        # исключения (Купер) — лично владельцу. Тоже раньше глушения.
        if await _process_incoming_invoice(gmail, bot, settings, gmail_id, record, session):
            return record.status

        # Коды входа в учётные записи (тикет 19): текстовое уведомление
        # в третью группу. Раньше глушения — accounts@kontur.ru заглушён,
        # но «Вход в сервис» владелец должен видеть.
        if is_alert(record.sender, record.subject, settings.LOGIN_CODE_RULES):
            record.email_type = "login_code"
            text = (
                "🔑 <b>Код / вход в учётную запись</b>\n\n"
                f"📧 От: {esc(record.sender)}\n"
                f"📌 Тема: {esc(record.subject)}"
            )
            await _run_action(
                session, record.id, "login_notice",
                lambda: send_notification(bot, _invoices_chat(settings), text),
            )
            record.status = EmailStatus.SUCCESS.value
            await session.commit()
            return record.status

        # Требуется обработка администратором (тикет 19): заявки HBConnect,
        # незавершённые брони, подтверждения выезда и бронирований Островка →
        # основная группа. Раньше глушения и LLM.
        if is_alert(record.sender, record.subject, settings.ADMIN_ATTENTION_RULES):
            record.email_type = "admin_attention"
            text = (
                "🛎 <b>Требуется обработка администратором</b>\n\n"
                f"📧 От: {esc(record.sender)}\n"
                f"📌 Тема: {esc(record.subject)}"
            )
            await _run_action(
                session, record.id, "admin_attention_notice",
                lambda: send_notification(bot, settings.TELEGRAM_CHAT_ID, text),
            )
            record.status = EmailStatus.SUCCESS.value
            await session.commit()
            return record.status

        # Уведомление владельцу о документах «для ручной обработки» (тикет 09)
        # и о важных письмах по паре адрес+тема (тикет 15: сверка 101hotels).
        # Раньше глушения (тикет 19): у Купера и МатСервиса адрес заглушён
        # целиком, но счета и акты сверки владелец должен видеть.
        if _extract_addr(record.sender) in {a.lower() for a in settings.OWNER_NOTICE_SENDERS} \
                or is_alert(record.sender, record.subject, settings.OWNER_NOTICE_RULES):
            record.email_type = "owner_notice"
            text = (
                "📄 <b>Документ для ручной обработки</b>\n\n"
                f"📧 От: {esc(record.sender)}\n"
                f"📌 Тема: {esc(record.subject)}"
            )
            # Тикет 31: «Понятно» — просто закрыть; «🖋 Печать» — подписать
            # PDF-вложения и положить черновик-ответ в Gmail.
            from aiogram.types import InlineKeyboardMarkup

            buttons = InlineKeyboardMarkup(
                inline_keyboard=[_row("notice_ok", "notice_stamp", email_id=record.id)]
            )
            await _run_action(
                session, record.id, "owner_notice",
                lambda: send_notification(bot, _owner_chat(settings), text, buttons),
            )
            record.status = EmailStatus.SUCCESS.value
            await session.commit()
            return record.status

        # Чёрный список (тикет 08): молча помечаем SKIPPED, LLM не вызываем
        if is_muted(record.sender, record.subject, settings.MUTED_SENDERS):
            logger.info("Письмо %s в чёрном списке (%s) — пропуск", gmail_id, record.sender)
            record.status = EmailStatus.SKIPPED.value
            await session.commit()
            return record.status

        # Реестры Яндекс Путешествий — детерминированная обработка без LLM
        if is_yandex_registry(record.sender, record.subject):
            await _process_yandex_registry(gmail, bot, settings, gmail_id, record, session)
            return record.status

        if record.llm_result:
            # Повторная обработка PENDING (тикет 23): LLM уже отработала —
            # берём сохранённый результат, не тратя повторный вызов.
            from .schemas import EmailAnalysisResult

            result = EmailAnalysisResult.model_validate(record.llm_result)
            mapping = record.pii_map or {}
        else:
            try:
                result, mapping = await analyze_body(settings, record.sender, record.subject, body_text)
            except Exception as exc:
                logger.exception("LLM не смогла обработать письмо %s", gmail_id)
                record.status = EmailStatus.ERROR.value
                record.error_log = f"{type(exc).__name__}: {exc}"
                await session.commit()
                await _notify_error(bot, settings.TELEGRAM_CHAT_ID, record.sender, record.subject, str(exc))
                return record.status

        # Справочник агентов (тикет 13): подтверждение брони от агента с
        # invoice_on_booking → счёт. Правило работает от справочника, не от LLM.
        from .agents import match_agent
        from .schemas import InvoiceDetails

        agent = await match_agent(session, record.subject, result.channel_name)
        # Тикет 30: запоминаем, что это подтверждение брони от агента, —
        # по нему админу уходит напоминание «отредактируйте бронирование»
        is_agent_booking = result.type == "booking_confirmed"
        if agent and result.type == "booking_confirmed" and agent.invoice_on_booking:
            inv = result.invoice or InvoiceDetails()
            inv.company_name = agent.payer_name or inv.company_name
            inv.arrival_date = inv.arrival_date or result.arrival_date
            inv.departure_date = inv.departure_date or result.departure_date
            result.invoice = inv
            result.type = "invoice_required"

        # Тикет 25: ответы в треде — не автосчёт и не «комментарий к брони».
        if _is_reply_subject(record.subject) and result.type in _REPLY_DOWNGRADE_TYPES:
            logger.info(
                "Письмо %s — ответ в треде (%s): тип %s понижен до guest_message",
                gmail_id, record.subject, result.type,
            )
            result.type = "guest_message"

        record.email_type = result.type
        # Внутренний ID TravelLine парсим из тела детерминированно (не через LLM):
        # из него строится номер счёта
        from .invoices import extract_travelline_id

        result.internal_booking_id = extract_travelline_id(body_text)
        record.llm_result = result.model_dump()
        record.pii_map = mapping
        await session.commit()

        # Тикет 30: у агента есть инструкция по редактированию брони —
        # напоминание администратору в основную группу (до проверки на
        # молчаливые типы: booking_confirmed сам по себе не уведомляет).
        # Яндекс Путешествия присылают брони как «изменение» — им напоминание
        # идёт и по booking_modified.
        edit_notice_type = is_agent_booking or (
            result.type == "booking_modified"
            and agent is not None
            and any(a in agent.name.lower() for a in _EDIT_NOTICE_ON_MODIFIED_AGENTS)
        )
        if agent is not None and agent.edit_note and edit_notice_type:
            notice = _build_agent_edit_notice(record, result, agent)
            await _run_action(
                session, record.id, "agent_edit_notice",
                lambda: send_notification(bot, settings.TELEGRAM_CHAT_ID, notice),
            )

        # Заезд сегодня/завтра (решение владельца 11.08.2026): уведомление
        # админам — до проверки молчаливых типов (booking_confirmed сам по
        # себе в чат не идёт). is_agent_booking — исходный тип
        # booking_confirmed, до возможной конвертации в invoice_required.
        # 14.08.2026: брони Яндекс Путешествий приходят как booking_modified —
        # им напоминание о заезде тоже нужно (владелец поймал пропуск).
        if is_agent_booking or result.type == "booking_modified":
            checkin_notice = _build_checkin_notice(result)
            if checkin_notice:
                await _run_action(
                    session, record.id, "checkin_today_notice",
                    lambda: send_notification(
                        bot, settings.TELEGRAM_CHAT_ID, checkin_notice
                    ),
                )

        # Молчаливые типы (тикет 15): SUCCESS в БД, в Telegram не шлём.
        # Подтверждение брони с комментарием гостя — исключение: отдельные
        # письма-комментарии дублируются (до 3 раз), поэтому комментарий
        # показываем администраторам из самого подтверждения.
        # Сервисные комментарии каналов («свяжитесь с гостем: +7…,
        # verification code…») — не запрос: уведомление не шлём.
        service_comment = (
            result.type in _SERVICE_COMMENT_TYPES
            and bool(_SERVICE_COMMENT_RE.search(body_text or ""))
        )
        if service_comment:
            logger.info(
                "Письмо %s — сервисный комментарий канала (контакты гостя), "
                "уведомление не шлём", gmail_id,
            )
        if service_comment or (
            result.type in SILENT_TYPES and not (
                result.type == "booking_confirmed" and result.comment_details
            )
        ):
            record.status = EmailStatus.SUCCESS.value
            await session.commit()
            return record.status

        try:
            invoice_note = await _prepare_invoice_note(
                result, settings, gmail, bot, record, session, agent=agent
            )
        except Exception as exc:
            # Тикет 26/27: сетевой сбой Telegram или Gmail (черновик) — не
            # ERROR, а пауза. Письмо остаётся PENDING: следующий цикл
            # доотправит счёт сам (LLM не вызовется — тикет 23). Сдаёмся
            # после MAX_INVOICE_ACTION_ATTEMPTS (попытки обоих действий счёта).
            if isinstance(exc, RETRYABLE_ERRORS) or _is_transient_gmail_error(exc):
                attempts = (await session.execute(
                    select(func.coalesce(func.sum(ActionLog.attempts), 0)).where(
                        ActionLog.email_id == record.id,
                        ActionLog.action_type.in_(INVOICE_ACTION_TYPES),
                    )
                )).scalar() or 0
                if attempts < MAX_INVOICE_ACTION_ATTEMPTS:
                    logger.warning(
                        "Счёт по письму %s не отправлен (%s), попытка %d/%d — "
                        "остаётся PENDING, повторим в следующем цикле",
                        gmail_id, exc, attempts, MAX_INVOICE_ACTION_ATTEMPTS,
                    )
                    return record.status  # PENDING
            # Логическая ошибка или исчерпаны попытки: ERROR в БД + ⚠️ админу
            logger.exception("Не удалось подготовить счёт по письму %s", gmail_id)
            record.status = EmailStatus.ERROR.value
            record.error_log = f"{type(exc).__name__}: {exc}"
            await session.commit()
            await _notify_error(bot, settings.TELEGRAM_CHAT_ID, record.sender, record.subject, str(exc))
            return record.status

        # Тикет 22: запросы счетов (invoice_required) — в основную группу
        # (решение владельца 10.08.2026), как и остальные типы; группа счетов
        # остаётся для ВХОДЯЩИХ счетов на оплату.
        chat_id = settings.TELEGRAM_CHAT_ID
        sent: dict = {}

        async def _send_card():
            sent["message"] = await route_notification(
                bot, chat_id, result,
                email_id=record.id, sender=record.sender, subject=record.subject,
                body_text=body_text, invoice_note=invoice_note,
            )

        await _run_action(session, record.id, "notify_card", _send_card)
        # При пропуске (повторная обработка) message=None — карточка уже в чате,
        # invoice_message_id был сохранён при первой отправке.
        message = sent.get("message")

        # Тикет 18/22: запоминаем карточку счёта, чтобы удалить её после закрытия.
        message_id = getattr(message, "message_id", None)
        if (
            result.type == "invoice_required"
            and isinstance(message_id, int)
        ):
            record.invoice_message_id = message_id
            record.invoice_chat_id = chat_id

        # SUCCESS ставим только после успешной отправки в Telegram:
        # если отправка упала, письмо остаётся PENDING и будет обработано повторно
        record.status = EmailStatus.SUCCESS.value
        await session.commit()
        return record.status


async def _process_incoming_invoice(
    gmail: GmailClient, bot, settings: Settings, gmail_id: str, record: ProcessedEmail, session
) -> bool:
    """Входящий счёт: вложение-счёт → документ в третью группу (Купер — владельцу).

    Возвращает True, если письмо перехвачено (конвейер дальше не идёт).
    Шлём все документы письма (счёт + детализация и пр.), если хотя бы одно
    имя файла похоже на счёт.
    """
    # Собственные исходящие письма (ответы с чужими счетами) — не входящие
    # счета: пропускаем перехват, дальше письмо уйдёт в глушение.
    if is_own_address(record.sender, settings):
        return False

    try:
        attachments = await gmail.fetch_attachments(gmail_id, DOC_SUFFIXES)
    except Exception as exc:
        logger.exception("Не удалось получить вложения письма %s", gmail_id)
        record.status = EmailStatus.ERROR.value
        record.error_log = f"{type(exc).__name__}: {exc}"
        await session.commit()
        await _notify_error(bot, settings.TELEGRAM_CHAT_ID, record.sender, record.subject, str(exc))
        return True

    if not any(is_invoice_filename(name) for name, _ in attachments):
        # Тикет 33: запрос на возврат денежных средств (Комфорт Букинг и др.)
        # — все вложения в группу входящих счетов; без вложений — текстом.
        # Раньше чёрного списка: notify.comfortbooking.ru заглушён целиком.
        if is_alert(record.sender, record.subject, settings.REFUND_RULES):
            record.email_type = "refund_request"
            chat_id = _invoices_chat(settings)
            caption = (
                "💸 <b>Запрос на возврат</b>\n\n"
                f"📧 От: {esc(record.sender)}\n"
                f"📌 Тема: {esc(record.subject)}"
            )
            if attachments:
                for filename, data in attachments:
                    await _run_action(
                        session, record.id, f"refund_docs:{filename}",
                        lambda f=filename, d=data: send_document(
                            bot, chat_id, f, d, caption=caption
                        ),
                    )
            else:
                await _run_action(
                    session, record.id, "refund_notice",
                    lambda: send_notification(bot, chat_id, caption),
                )
            record.status = EmailStatus.SUCCESS.value
            await session.commit()
            return True
        # Тикет 19: известные отправители входящих счетов без узнаваемого
        # вложения (охрана, ККТ, хозтовары) — текстовое уведомление в третью
        # группу, исключения (Купер) — лично владельцу.
        if _extract_addr(record.sender) in {
            a.lower() for a in settings.INCOMING_INVOICE_SENDERS
        }:
            record.email_type = "incoming_invoice"
            text = (
                "🧾 <b>Входящий счёт</b>\n\n"
                f"📧 От: {esc(record.sender)}\n"
                f"📌 Тема: {esc(record.subject)}"
            )
            await _run_action(
                session, record.id, "incoming_invoice_docs",
                lambda: send_notification(bot, _invoices_chat(settings), text),
            )
            record.status = EmailStatus.SUCCESS.value
            await session.commit()
            return True
        return False

    is_exception = _extract_addr(record.sender) in {
        a.lower() for a in settings.INVOICE_OWNER_EXCEPTIONS
    }
    record.email_type = "kuper_invoice" if is_exception else "incoming_invoice"
    chat_id = _owner_chat(settings) if is_exception else _invoices_chat(settings)
    caption = f"📧 От: {esc(record.sender)}\n📌 Тема: {esc(record.subject)}"

    for filename, data in attachments:
        # Идемпотентность по каждому файлу (тикет 23): при падении посреди
        # цикла повторная обработка дошлёт только неотправленные вложения.
        await _run_action(
            session, record.id, f"incoming_invoice_docs:{filename}",
            lambda f=filename, d=data: send_document(bot, chat_id, f, d, caption=caption),
        )

    record.status = EmailStatus.SUCCESS.value
    await session.commit()
    return True


async def _process_yandex_registry(
    gmail: GmailClient, bot, settings: Settings, gmail_id: str, record: ProcessedEmail, session
) -> None:
    """Реестр Яндекс Путешествий: xlsx-вложение → карточка в Telegram. Без LLM.

    Пустой реестр (нет броней) — молча помечаем SUCCESS, уведомление не шлём.
    """
    from .yandex_registry import (
        build_registry_notification,
        parse_payment_order,
        parse_registry,
    )

    record.email_type = "yandex_registry"
    try:
        attachments = await gmail.fetch_attachments(gmail_id, ".xlsx")
        payment_order, payment_date = parse_payment_order(record.subject)
        reports = [
            parse_registry(data, payment_order=payment_order, payment_date=payment_date)
            for _, data in attachments
        ]
        bookings = [b for rep in reports for b in rep.bookings]
    except Exception as exc:
        logger.exception("Не удалось разобрать реестр по письму %s", gmail_id)
        record.status = EmailStatus.ERROR.value
        record.error_log = f"{type(exc).__name__}: {exc}"
        await session.commit()
        await _notify_error(bot, settings.TELEGRAM_CHAT_ID, record.sender, record.subject, str(exc))
        return

    if not bookings:
        logger.info("Пустой реестр по письму %s — пропуск без уведомления", gmail_id)
        record.status = EmailStatus.SUCCESS.value
        await session.commit()
        return

    report = reports[0]
    if len(reports) > 1:  # несколько xlsx — объединяем в одну карточку
        report.bookings = bookings
        report.commission = sum(rep.commission for rep in reports)
    record.llm_result = {
        "payment_order": report.payment_order,
        "payment_date": report.payment_date,
        "bookings": [b.__dict__ for b in report.bookings],
        "total": report.total,
    }
    await session.commit()

    await _run_action(
        session, record.id, "yandex_registry_notice",
        lambda: send_notification(bot, _owner_chat(settings), build_registry_notification(report)),
    )
    record.status = EmailStatus.SUCCESS.value
    await session.commit()


def _build_agent_edit_notice(record: ProcessedEmail, result, agent) -> str:
    """Текст напоминания админу «отредактируйте бронирование» (тикет 30)."""
    lines = [
        "✏️ <b>Отредактируйте бронирование</b>",
        "",
        f"📌 {esc(record.subject)}",
    ]
    if result.booking_number:
        lines.append(f"🔖 Бронь № {esc(result.booking_number)}")
    if result.arrival_date or result.departure_date:
        lines.append(f"📅 {result.arrival_date or '—'} — {result.departure_date or '—'}")
    lines += ["", f"💰 <b>{esc(agent.name)}</b>: {esc(agent.edit_note)}"]
    return "\n".join(lines)


def _parse_booking_date(value: str | None) -> date | None:
    """Дата заезда/выезда из LLM: ISO (2026-08-12) или ДД.ММ.ГГГГ."""
    if not value:
        return None
    value = value.strip()[:10]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _build_checkin_notice(result) -> str | None:
    """Уведомление админам о заезде сегодня/завтра с планом действий.

    Решение владельца 11.08.2026:
    - заезд сегодня → сообщить админам; бронь на 1 ночь → добавить в план
      уборки выезд на завтра; больше ночей → текущую уборку номера;
      гостей > 2 → подготовить номер;
    - заезд завтра → добавить номер в заезды на завтра и узнать время приезда.
    None, если заезд не сегодня/завтра или дату не распарсить.
    """
    arrival = _parse_booking_date(result.arrival_date)
    if arrival is None:
        return None
    today = date.today()
    is_today = arrival == today
    if not is_today and arrival != today + timedelta(days=1):
        return None
    departure = _parse_booking_date(result.departure_date)
    nights = max((departure - arrival).days, 1) if departure else 1
    lines = [
        "🏨 <b>Сегодня новый заезд!</b>" if is_today
        else "🗓 <b>Завтра новый заезд!</b>",
        "",
    ]
    if result.booking_number:
        lines.append(f"🔖 Бронь № {esc(result.booking_number)}")
    if result.guest_name:
        lines.append(f"👤 Гость: {esc(result.guest_name)}")
    lines.append(
        f"📅 {result.arrival_date} — {result.departure_date or '—'} ({nights} ноч.)"
    )
    lines.append("")
    if is_today:
        if nights <= 1:
            lines.append("🧹 Бронь на одну ночь — добавьте в план уборки выезд на завтра.")
        else:
            lines.append("🧹 Добавьте текущую уборку этого номера в план уборки.")
    else:
        lines.append("📋 Добавьте номер в заезды на завтра и узнайте у гостя время приезда.")
    guests = getattr(result, "guests_count", None)
    if guests and guests > 2:
        lines.append(f"🛏 Подготовьте номер на {guests} человек.")
    return "\n".join(lines)


async def _prepare_invoice_note(
    result, settings: Settings, gmail: GmailClient, bot, record, session,
    agent=None,
) -> str | None:
    """Для invoice_required: реестр → PDF на диск → черновик в Gmail → пометки.

    Тикет 27: каждый счёт сохраняется черновиком в Gmail (кому — email
    заказчика из реестра/агента) — страховка от потери счёта и готовое письмо
    для ручной отправки. Автоотправка черновиков — тикет 12.
    agent — запись справочника агентов (тикет 13): подставляет email для счёта
    и пометку о корректировке цены.
    Путь к PDF сохраняется в record.invoice_pdf (тикет 06: команда /invoices).
    В основную группу PDF не шлём (решение владельца от 15.08.2026).
    """
    if result.type != "invoice_required":
        return None
    from .drafts import (
        build_draft_body,
        build_draft_mime,
        find_company,
        merge_invoice_data,
    )
    from .invoices import generate_invoice_pdf, invoice_missing_fields

    sender = record.sender
    # Данные реестра компаний в приоритете над LLM-извлечёнными
    company = await find_company(session, sender, result.invoice)
    data = merge_invoice_data(result, sender, company)
    if agent is not None and agent.invoice_email:
        data.to = agent.invoice_email

    # Скидка/комиссия агента из справочника (тикет 13): процент из price_note
    # применяется к сумме счёта («-15% ко всем дням» → сумма × 0,85).
    price_percent = None
    if agent is not None:
        from .agents import apply_price_percent, parse_price_percent

        price_percent = parse_price_percent(agent.price_note)
        if price_percent is not None:
            data.invoice.amount = apply_price_percent(
                data.invoice.amount, price_percent
            )

    effective = result.model_copy(update={"invoice": data.invoice})
    pdf_path = generate_invoice_pdf(effective, settings)
    record.invoice_pdf = str(pdf_path)
    await session.commit()

    # PDF в основную группу не отправляем (решение владельца от 15.08.2026):
    # счёт доступен через /invoices и лежит в черновике Gmail ниже.
    # Черновик в Gmail (тикет 27). MIME детерминирован — пересобираем при
    # повторах; внешнее действие (create_draft) под идемпотентностью.
    # В теме — #<номер брони канала> (Ostrovok/Bronevik/…), чтобы черновики
    # было легко искать по брони (тикет 27, доп. от 10.08.2026).
    # Броневику — только номер брони без префикса (решение владельца).
    subject = data.subject
    if result.booking_number:
        if agent is not None and any(
            a in agent.name.lower() for a in _SUBJECT_NUMBER_ONLY_AGENTS
        ):
            subject = f"#{result.booking_number}"
        else:
            subject = f"{subject} #{result.booking_number}"
    raw = build_draft_mime(
        data.to, subject,
        build_draft_body(data, settings.HOTEL_NAME), pdf_path,
    )
    await _run_action(
        session, record.id, "invoice_gmail_draft",
        lambda: gmail.create_draft(raw),
    )

    notes = [
        f"✉️ Черновик со счётом сохранён в Gmail (кому: {data.to})",
    ]
    if agent is not None and agent.price_note:
        note = f"💰 Агент {agent.name}: {agent.price_note}"
        if price_percent is not None:
            note += " — применено к сумме счёта"
        notes.append(note)
    missing = invoice_missing_fields(data.invoice)
    if missing:
        notes.append(f"⚠️ Проверьте данные: не заполнены {', '.join(missing)}")
    if company is None:
        notes.append("🆕 Новая компания — добавьте в реестр")
    return "\n".join(notes)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return parsed.replace(tzinfo=None) if parsed else None
    except (TypeError, ValueError):
        return None


# Клиент OpenAI создаётся один раз на процесс
_llm_client = None


def set_llm_client(client) -> None:
    global _llm_client
    _llm_client = client


async def check_once(gmail: GmailClient, bot, settings: Settings) -> None:
    """Один цикл опроса почты."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        last_uid = await get_last_uid(session)
        pending_uid = await get_oldest_pending_uid(session)

    after = cursor_after_seconds(last_uid)
    if pending_uid:
        # Тикет 26: окно опускаем до самого старого PENDING — иначе зависшие
        # письма (сетевой сбой и т.п.) навсегда выпадают из выборки Gmail.
        pending_after = cursor_after_seconds(pending_uid)
        after = pending_after if after is None else min(after, pending_after)
    ids = await gmail.list_new_message_ids(after, settings.BACKLOG_DAYS)
    logger.info("Найдено новых писем: %d (курсор: %s)", len(ids), last_uid)

    for gmail_id in ids:
        try:
            status = await process_email(gmail, bot, settings, gmail_id)
        except Exception:
            # Ошибка одного письма не останавливает очередь; метку не вешаем —
            # письмо придёт на повторную обработку
            logger.exception("Ошибка обработки письма %s", gmail_id)
            continue
        # Тикет 14: метка portier-processed + «прочитано» — только при успехе
        # (SUCCESS/SKIPPED/дедуп). При ERROR метка не ставится.
        if status in LABEL_OK_STATUSES:
            try:
                await gmail.mark_processed(gmail_id)
            except Exception:
                logger.warning(
                    "Не удалось повесить метку %s на письмо %s",
                    PROCESSED_LABEL, gmail_id, exc_info=True,
                )


async def gmail_loop(settings: Settings, bot) -> None:
    """Бесконечный цикл опроса почты каждые CHECK_INTERVAL_SECONDS."""
    gmail = GmailClient(settings)
    await gmail.connect()  # GmailAuthError всплывёт на старте с инструкцией
    while True:
        try:
            if in_quiet_hours(
                datetime.now(), settings.QUIET_HOURS_START, settings.QUIET_HOURS_END
            ):
                # Тикет 17: ночью не опрашиваем — письма остаются без метки
                # и обработаются утром первым же циклом после конца окна
                logger.info(
                    "Тихие часы (%02d:00–%02d:00) — опрос пропущен",
                    settings.QUIET_HOURS_START, settings.QUIET_HOURS_END,
                )
            else:
                await check_once(gmail, bot, settings)
        except GmailAuthError:
            # Токен протух/отозван посреди работы — почтовый цикл останавливаем,
            # сервис продолжит отвечать в Telegram, в логе будет инструкция
            logger.exception("OAuth-токен Gmail недействителен, почтовый цикл остановлен")
            raise
        except Exception:
            logger.exception("Ошибка цикла опроса, переподключение")
            await gmail.close()
        await asyncio.sleep(settings.CHECK_INTERVAL_SECONDS)
