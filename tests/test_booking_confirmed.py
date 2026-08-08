"""Подтверждение новой брони без комментариев — молча в БД, без Telegram."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from portier import gmail_client
from portier.config import Settings
from portier.db import get_session_factory, init_db, init_engine
from portier.models import EmailStatus, ProcessedEmail
from portier.schemas import EmailAnalysisResult
from sqlalchemy import select


async def test_booking_confirmed_silent(monkeypatch, tmp_path):
    """booking_confirmed: SUCCESS в БД, уведомление в Telegram не отправляется."""
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()

    analyze = AsyncMock(return_value=EmailAnalysisResult(
        type="booking_confirmed", priority="normal",
        guest_name="Иван Иванов", booking_number="20260807-7348-456714535",
        arrival_date="2026-08-07", departure_date="2026-08-08",
        channel_name="TravelLine", action_required="Ничего не требуется",
    ))
    monkeypatch.setattr(gmail_client, "analyze_email", analyze)
    gmail = SimpleNamespace(
        fetch_headers=AsyncMock(return_value={
            "message_id": "<msg-1@x>",
            "sender": "TravelLine <noreply@travellinemail.com>",
            "subject": "Подтверждение бронирования №20260807-7348-456714535",
            "date": "Mon, 1 Sep 2026 10:00:00 +0300",
            "internal_date": 1756710000000,
        }),
        fetch_body_text=AsyncMock(return_value="Подтверждение бронирования."),
        fetch_attachments=AsyncMock(return_value=[]),
    )
    bot = AsyncMock()
    settings = Settings(
        OPENAI_API_KEY="k", TELEGRAM_CHAT_ID=1,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        INVOICES_DIR=str(tmp_path),
    )
    await gmail_client.process_email(gmail, bot, settings, "gmail-id-1")

    async with get_session_factory()() as session:
        record = (await session.execute(select(ProcessedEmail))).scalars().one()
    assert record.status == EmailStatus.SUCCESS.value
    assert record.email_type == "booking_confirmed"
    bot.send_message.assert_not_called()  # молча, без уведомления
