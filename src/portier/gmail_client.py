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
from .bot import send_document, send_notification
from .llm import analyze_email
from .incoming import DOC_SUFFIXES, is_alert, is_invoice_filename
from .models import EmailStatus, ProcessedEmail
from .muted import _extract_addr, is_muted
from .yandex_registry import is_yandex_registry

logger = logging.getLogger(__name__)


def _owner_chat(settings: Settings) -> int | None:
    """Личный чат владельца; если не настроен — основной (чтобы ничего не потерять)."""
    return settings.OWNER_CHAT_ID or settings.TELEGRAM_CHAT_ID


def _invoices_chat(settings: Settings) -> int | None:
    """Третья группа «входящие счета и алерты»; fallback: владелец → основной."""
    return settings.INCOMING_INVOICES_CHAT_ID or _owner_chat(settings)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
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
    """Поисковый запрос Gmail: письма после курсора либо за глубину backlog.

    category:primary — только вкладка «Основные»: промоакции, соцсети,
    оповещения и форумы пропускаем ещё на стороне Gmail, не тратя запросы к LLM.
    """
    if after_epoch_seconds is None:
        after_epoch_seconds = int(
            (datetime.now() - timedelta(days=backlog_days)).timestamp()
        )
    return f"after:{after_epoch_seconds} category:primary"


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

        # Важные алерты (тикет 10): текстовое уведомление в третью группу.
        # Идёт раньше глушения — овербукинг важнее рассылок TravellLine.
        if is_alert(record.sender, record.subject, settings.ALERT_RULES):
            record.email_type = "alert"
            await send_notification(
                bot, _invoices_chat(settings),
                "🚨 <b>Важное уведомление</b>\n\n"
                f"📧 От: {esc(record.sender)}\n"
                f"📌 Тема: {esc(record.subject)}",
            )
            record.status = EmailStatus.SUCCESS.value
            await session.commit()
            return

        # Входящие счета (тикет 10): вложение-счёт → документ в третью группу,
        # исключения (Купер) — лично владельцу. Тоже раньше глушения.
        if await _process_incoming_invoice(gmail, bot, settings, gmail_id, record, session):
            return

        # Чёрный список (тикет 08): молча помечаем SKIPPED, LLM не вызываем
        if is_muted(record.sender, record.subject, settings.MUTED_SENDERS):
            logger.info("Письмо %s в чёрном списке (%s) — пропуск", gmail_id, record.sender)
            record.status = EmailStatus.SKIPPED.value
            await session.commit()
            return

        # Уведомление владельцу о документах «для ручной обработки» (тикет 09)
        if _extract_addr(record.sender) in {a.lower() for a in settings.OWNER_NOTICE_SENDERS}:
            record.email_type = "owner_notice"
            await send_notification(
                bot, _owner_chat(settings),
                "📄 <b>Документ для ручной обработки</b>\n\n"
                f"📧 От: {esc(record.sender)}\n"
                f"📌 Тема: {esc(record.subject)}",
            )
            record.status = EmailStatus.SUCCESS.value
            await session.commit()
            return

        # Реестры Яндекс Путешествий — детерминированная обработка без LLM
        if is_yandex_registry(record.sender, record.subject):
            await _process_yandex_registry(gmail, bot, settings, gmail_id, record, session)
            return

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
        # Внутренний ID TravelLine парсим из тела детерминированно (не через LLM):
        # из него строится номер счёта
        from .invoices import extract_travelline_id

        result.internal_booking_id = extract_travelline_id(body_text)
        record.llm_result = result.model_dump()
        record.pii_map = mapping
        await session.commit()

        # Подтверждение новой брони без комментариев — молча фиксируем в БД,
        # в Telegram не шлём (решение пользователя 08.08.2026)
        if result.type == "booking_confirmed":
            record.status = EmailStatus.SUCCESS.value
            await session.commit()
            return

        try:
            invoice_note = await _prepare_invoice_note(
                result, settings, gmail, bot, record.sender, session
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


async def _process_incoming_invoice(
    gmail: GmailClient, bot, settings: Settings, gmail_id: str, record: ProcessedEmail, session
) -> bool:
    """Входящий счёт: вложение-счёт → документ в третью группу (Купер — владельцу).

    Возвращает True, если письмо перехвачено (конвейер дальше не идёт).
    Шлём все документы письма (счёт + детализация и пр.), если хотя бы одно
    имя файла похоже на счёт.
    """
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
        return False

    is_exception = _extract_addr(record.sender) in {
        a.lower() for a in settings.INVOICE_OWNER_EXCEPTIONS
    }
    record.email_type = "kuper_invoice" if is_exception else "incoming_invoice"
    chat_id = _owner_chat(settings) if is_exception else _invoices_chat(settings)
    caption = f"📧 От: {esc(record.sender)}\n📌 Тема: {esc(record.subject)}"

    for filename, data in attachments:
        await send_document(bot, chat_id, filename, data, caption=caption)

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

    await send_notification(
        bot, _owner_chat(settings), build_registry_notification(report)
    )
    record.status = EmailStatus.SUCCESS.value
    await session.commit()


async def _prepare_invoice_note(
    result, settings: Settings, gmail: GmailClient, bot, sender: str, session
) -> str | None:
    """Для invoice_required: реестр → PDF → файл в общий чат → текст пометки.

    Черновики в Gmail отключены (решение пользователя 08.08.2026): пока счёт
    только уходит в чат на проверку. Черновики/автоотправка — тикет 12.
    """
    if result.type != "invoice_required":
        return None
    from .drafts import (
        find_company,
        merge_invoice_data,
    )
    from .invoices import generate_invoice_pdf, invoice_missing_fields

    # Данные реестра компаний в приоритете над LLM-извлечёнными
    company = await find_company(session, sender, result.invoice)
    data = merge_invoice_data(result, sender, company)

    effective = result.model_copy(update={"invoice": data.invoice})
    pdf_path = generate_invoice_pdf(effective, settings)

    await send_document(
        bot,
        settings.TELEGRAM_CHAT_ID,
        filename=pdf_path.name,
        data=pdf_path.read_bytes(),
        caption=f"📎 Счёт для {data.to} — проверьте перед отправкой",
    )

    notes = [
        f"📎 Счёт (PDF) отправлен выше — проверьте и отправьте вручную на {data.to}",
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
