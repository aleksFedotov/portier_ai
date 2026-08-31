"""Подтверждение брони с сайта отеля с «Безналичным расчетом» от юрлица —
не invoice_required: счёт не для нас, письмо остаётся booking_confirmed."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from portier import gmail_client
from portier.config import Settings
from portier.db import get_session_factory, init_db, init_engine
from portier.models import EmailStatus, ProcessedEmail
from portier.schemas import EmailAnalysisResult, InvoiceDetails
from sqlalchemy import select

_BODY = (
    "Подтверждение бронирования\nID 20260909-7348-462731878\n"
    "TRAVELLINE\nПодтверждение №20260909-7348-462731878\n"
    "Забронировано на сайте https://likihotel.com\n"
    "Способ оплаты Безналичный расчет\n"
    "К оплате на расчетный счет 4 675 RUB\n"
    "Детали платежа\nОрганизация ООО \"СЧИТАЙ ДЕНЬГИ\"\nИНН 4345527148\n"
)


async def test_beznal_confirmation_not_invoice(monkeypatch, tmp_path):
    """LLM пометила подтверждение с безналом как invoice_required —
    код понижает до booking_confirmed: счёт и черновик не создаются."""
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()

    analyze = AsyncMock(return_value=EmailAnalysisResult(
        type="invoice_required", priority="normal",
        guest_name="Ердякова Анастасия", booking_number="20260909-7348-462731878",
        arrival_date="2026-09-09", departure_date="2026-09-10",
        action_required="Выставить счёт",
        invoice=InvoiceDetails(
            company_name="ООО «СЧИТАЙ ДЕНЬГИ»", inn="4345527148",
            amount="4675", description="Проживание",
        ),
    ))
    monkeypatch.setattr(gmail_client, "analyze_email", analyze)
    gmail = SimpleNamespace(
        fetch_headers=AsyncMock(return_value={
            "message_id": "<msg-beznal@x>",
            "sender": "TravelLine <noreply@travellinemail.com>",
            "subject": "Подтверждение бронирования №20260909-7348-462731878",
            "date": "Mon, 31 Aug 2026 15:53:00 +0300",
            "internal_date": 1756710000000,
        }),
        fetch_body_text=AsyncMock(return_value=_BODY),
        fetch_attachments=AsyncMock(return_value=[]),
    )
    bot = AsyncMock()
    settings = Settings(
        OPENAI_API_KEY="k", TELEGRAM_CHAT_ID=1,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        INVOICES_DIR=str(tmp_path),
    )
    await gmail_client.process_email(gmail, bot, settings, "gmail-id-beznal")

    async with get_session_factory()() as session:
        record = (await session.execute(select(ProcessedEmail))).scalars().one()
    assert record.status == EmailStatus.SUCCESS.value
    assert record.email_type == "booking_confirmed"
    assert record.invoice_pdf is None  # счёт не генерировался
    bot.send_message.assert_not_called()  # молча, без уведомления
