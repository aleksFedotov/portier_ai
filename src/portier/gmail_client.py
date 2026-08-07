"""Gmail API клиент и конвейер обработки писем.

Опрос по курсору (internalDate последнего обработанного письма): список новых
писем → заголовки → дедуп по message_id в БД → тело только новых → конвейер
(очистка → PII-mask → LLM → unmask → роутер). Метки прочитанности не меняются.
"""

import asyncio
import base64
import email.utils
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import func, select

from .cleaner import clean_email
from .config import Settings
from .db import get_session_factory
from .handlers.router import route_notification
from .handlers.templates import esc
from .llm import analyze_email
from .models import EmailStatus, ProcessedEmail

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
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
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
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
    }


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
    """Поисковый запрос Gmail: письма после курсора либо за глубину backlog."""
    if after_epoch_seconds is None:
        after_epoch_seconds = int(
            (datetime.now() - timedelta(days=backlog_days)).timestamp()
        )
    return f"after:{after_epoch_seconds}"


class GmailClient:
    """Тонкая обёртка над Gmail API (sync SDK гоняется через asyncio.to_thread)."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._service = None

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

    async def create_draft(self, raw_message: str) -> str:
        """Создать черновик в Gmail (users.drafts.create), вернуть ID черновика."""
        service = await self._ensure()

        def _create():
            return (
                service.users()
                .drafts()
                .create(userId="me", body={"message": {"raw": raw_message}})
                .execute()
            )

        draft = await asyncio.to_thread(_create)
        logger.info("Черновик создан в Gmail: %s", draft.get("id"))
        return draft["id"]


async def get_last_uid(session) -> int | None:
    """Курсор: максимальный internalDate (мс) обработанного письма (None — база пуста)."""
    result = await session.execute(select(func.max(ProcessedEmail.uid)))
    return result.scalar_one_or_none()


async def is_processed(session, message_id: str) -> bool:
    """Дедупликация по message_id. Обработанными считаются только SUCCESS/ERROR:
    зависшие PENDING (падение процесса до LLM) обрабатываются повторно.
    Fallback по uid намеренно не используется: uid = internalDate (мс) может
    совпадать у разных писем.
    """
    result = await session.execute(
        select(ProcessedEmail.id).where(
            ProcessedEmail.message_id == message_id,
            ProcessedEmail.status.in_([EmailStatus.SUCCESS.value, EmailStatus.ERROR.value]),
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
    result = await analyze_email(
        _llm_client, settings.OPENAI_MODEL,
        sender=masked_sender, subject=masked_subject, body=masked_body,
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


async def process_email(gmail: GmailClient, bot, settings: Settings, gmail_id: str) -> None:
    """Полный конвейер одного письма: заголовки → дедуп → тело → LLM → Telegram."""
    session_factory = get_session_factory()
    headers = await gmail.fetch_headers(gmail_id)
    uid = headers["internal_date"] or 0
    message_id = make_message_id(headers, gmail_id)

    async with session_factory() as session:
        if await is_processed(session, message_id):
            logger.info("Письмо %s уже обработано, пропуск", gmail_id)
            return

        body_text = await gmail.fetch_body_text(gmail_id)

        record = ProcessedEmail(
            message_id=message_id,
            uid=uid,
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

        try:
            result, mapping = await analyze_body(settings, record.sender, record.subject, body_text)
        except Exception as exc:
            logger.exception("LLM не смогла обработать письмо %s", gmail_id)
            record.status = EmailStatus.ERROR.value
            record.error_log = f"{type(exc).__name__}: {exc}"
            await session.commit()
            await _notify_error(bot, settings.TELEGRAM_CHAT_ID, record.sender, record.subject, str(exc))
            return

        record.email_type = result.type
        record.llm_result = result.model_dump()
        record.pii_map = mapping
        await session.commit()

        try:
            invoice_note = await _prepare_invoice_note(
                result, settings, gmail, record.sender, session
            )
        except Exception as exc:
            # Сбой PDF/черновика: ERROR в БД + ⚠️ админу, очередь не останавливается
            logger.exception("Не удалось подготовить счёт по письму %s", gmail_id)
            record.status = EmailStatus.ERROR.value
            record.error_log = f"{type(exc).__name__}: {exc}"
            await session.commit()
            await _notify_error(bot, settings.TELEGRAM_CHAT_ID, record.sender, record.subject, str(exc))
            return

        await route_notification(
            bot, settings.TELEGRAM_CHAT_ID, result,
            email_id=record.id, sender=record.sender, subject=record.subject,
            body_text=body_text, invoice_note=invoice_note,
        )

        # SUCCESS ставим только после успешной отправки в Telegram:
        # если отправка упала, письмо остаётся PENDING и будет обработано повторно
        record.status = EmailStatus.SUCCESS.value
        await session.commit()


async def _prepare_invoice_note(
    result, settings: Settings, gmail: GmailClient, sender: str, session
) -> str | None:
    """Для invoice_required: реестр → PDF → черновик в Gmail → текст пометки."""
    if result.type != "invoice_required":
        return None
    from .drafts import (
        build_draft_body,
        build_draft_mime,
        find_company,
        merge_invoice_data,
    )
    from .invoices import generate_invoice_pdf, invoice_missing_fields

    # Данные реестра компаний в приоритете над LLM-извлечёнными
    company = await find_company(session, sender, result.invoice)
    data = merge_invoice_data(result, sender, company)

    effective = result.model_copy(update={"invoice": data.invoice})
    pdf_path = generate_invoice_pdf(effective, settings)

    raw = build_draft_mime(
        data.to, data.subject, build_draft_body(data, settings.HOTEL_NAME), pdf_path
    )
    await gmail.create_draft(raw)

    notes = [
        f"✉️ Черновик создан в Gmail (кому: {data.to}) — проверьте и отправьте вручную",
        f"📎 Счёт: {pdf_path}",
    ]
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

    after = cursor_after_seconds(last_uid)
    ids = await gmail.list_new_message_ids(after, settings.BACKLOG_DAYS)
    logger.info("Найдено новых писем: %d (курсор: %s)", len(ids), last_uid)

    for gmail_id in ids:
        try:
            await process_email(gmail, bot, settings, gmail_id)
        except Exception:
            # Ошибка одного письма не останавливает очередь
            logger.exception("Ошибка обработки письма %s", gmail_id)


async def gmail_loop(settings: Settings, bot) -> None:
    """Бесконечный цикл опроса почты каждые CHECK_INTERVAL_SECONDS."""
    gmail = GmailClient(settings)
    await gmail.connect()  # GmailAuthError всплывёт на старте с инструкцией
    while True:
        try:
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
