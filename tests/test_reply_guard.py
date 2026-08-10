"""Тикет 25: ответы в треде (Re:/Fwd:) — автосчёта нет, тип понижается до guest_message."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from portier import gmail_client
from portier.config import Settings
from portier.db import get_session_factory, init_db, init_engine
from portier.gmail_client import _is_reply_subject
from portier.models import EmailStatus, ProcessedEmail
from portier.schemas import EmailAnalysisResult, InvoiceDetails


# ---------- юнит: распознавание темы-ответа ----------


def test_is_reply_subject():
    assert _is_reply_subject("Re: Бронирование отеля")
    assert _is_reply_subject("Re[2]: Бронирование отеля")
    assert _is_reply_subject("re: счёт")
    assert _is_reply_subject("  RE: пробелы")
    assert _is_reply_subject("Fwd: Интересно")
    assert _is_reply_subject("FW: счёт")
    assert not _is_reply_subject("Новое бронирование № #123")
    assert not _is_reply_subject("Re без двоеточия")
    assert not _is_reply_subject("")


# ---------- конвейер: ответ в треде → guest_message, без PDF ----------


async def _run_reply_email(monkeypatch, tmp_path, llm_type: str) -> tuple:
    """Прогнать process_email на письме-ответе с заданным типом от LLM."""
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()

    analyze = AsyncMock(return_value=EmailAnalysisResult(
        type=llm_type, priority="normal",
        action_required="—",
        invoice=InvoiceDetails(amount="10 000,00", description="Стандарт"),
    ))
    monkeypatch.setattr(gmail_client, "analyze_email", analyze)
    gmail = SimpleNamespace(
        fetch_headers=AsyncMock(return_value={
            "message_id": "<msg-re@x>",
            "sender": "Илья Мартышкин <zakupkiyalagrokoll@mail.ru>",
            "subject": "Re[2]: Бронирование отеля",
            "date": "Tue, 7 Jul 2026 12:00:00 +0300",
            "internal_date": 1756710000000,
        }),
        fetch_body_text=AsyncMock(return_value="Доброго, просьба выгрузить акт."),
        fetch_attachments=AsyncMock(return_value=[]),
    )
    bot = AsyncMock()
    settings = Settings(
        OPENAI_API_KEY="k", TELEGRAM_CHAT_ID=111,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        INVOICES_DIR=str(tmp_path),
    )
    status = await gmail_client.process_email(gmail, bot, settings, "gmail-id-re")
    async with get_session_factory()() as session:
        record = (await session.execute(select(ProcessedEmail))).scalars().one()
    return status, record, bot


async def test_reply_invoice_required_downgraded(monkeypatch, tmp_path):
    """Re[2]: + invoice_required → guest_message: PDF не генерится, карточка в чат."""
    status, record, bot = await _run_reply_email(
        monkeypatch, tmp_path, "invoice_required"
    )
    assert status == EmailStatus.SUCCESS.value
    assert record.email_type == "guest_message"
    assert record.invoice_pdf is None
    bot.send_document.assert_not_called()
    assert bot.send_message.await_args.kwargs["chat_id"] == 111


async def test_reply_booking_comment_downgraded(monkeypatch, tmp_path):
    """Re: + booking_comment → guest_message (не молчит в SILENT_TYPES)."""
    status, record, bot = await _run_reply_email(
        monkeypatch, tmp_path, "booking_comment"
    )
    assert status == EmailStatus.SUCCESS.value
    assert record.email_type == "guest_message"
    assert bot.send_message.await_args.kwargs["chat_id"] == 111
