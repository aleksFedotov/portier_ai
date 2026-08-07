"""Тикет 09: личные уведомления владельцу (документы для ручной обработки)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from portier import gmail_client
from portier.config import Settings
from portier.db import get_session_factory, init_db, init_engine
from portier.models import EmailStatus, ProcessedEmail


async def _run_pipeline(monkeypatch, tmp_path, sender, subject, **settings_kwargs):
    """Прогнать process_email на моках, вернуть (bot_mock, record, analyze_mock)."""
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()

    analyze = AsyncMock()
    monkeypatch.setattr(gmail_client, "analyze_email", analyze)
    gmail = SimpleNamespace(
        fetch_headers=AsyncMock(return_value={
            "message_id": "<msg-1@x>", "sender": sender,
            "subject": subject, "date": "Mon, 1 Sep 2026 10:00:00 +0300",
            "internal_date": 1756710000000,
        }),
        fetch_body_text=AsyncMock(return_value="Текст письма."),
    )
    bot = AsyncMock()
    settings = Settings(
        OPENAI_API_KEY="k", TELEGRAM_CHAT_ID=111,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        INVOICES_DIR=str(tmp_path), **settings_kwargs,
    )
    await gmail_client.process_email(gmail, bot, settings, "gmail-id-1")
    async with get_session_factory()() as session:
        record = (await session.execute(select(ProcessedEmail))).scalars().one()
    return bot, record, analyze


async def test_owner_notice_goes_to_owner_chat(monkeypatch, tmp_path):
    bot, record, analyze = await _run_pipeline(
        monkeypatch, tmp_path,
        "Отелло <otello@2gis.ru>", "Реестр бронирований", OWNER_CHAT_ID=999,
    )
    assert record.status == EmailStatus.SUCCESS.value
    assert record.email_type == "owner_notice"
    analyze.assert_not_awaited()  # без LLM
    bot.send_message.assert_awaited_once()
    args, kwargs = bot.send_message.await_args
    chat_id = kwargs.get("chat_id", args[0] if args else None)
    assert chat_id == 999


async def test_owner_notice_fallback_to_main_chat(monkeypatch, tmp_path):
    """OWNER_CHAT_ID не задан → уведомление уходит в основной чат."""
    bot, record, _ = await _run_pipeline(
        monkeypatch, tmp_path,
        "Отелло <otello@2gis.ru>", "Акт сверки",
    )
    assert record.status == EmailStatus.SUCCESS.value
    args, kwargs = bot.send_message.await_args
    chat_id = kwargs.get("chat_id", args[0] if args else None)
    assert chat_id == 111


async def test_regular_sender_not_affected(monkeypatch, tmp_path):
    """Отправитель не из списка — обычный конвейер (LLM вызывается)."""
    from portier.schemas import EmailAnalysisResult

    result = EmailAnalysisResult(
        type="guest_message", priority="normal", summary="тест",
        action_required="ответить",
        guest_name=None, booking_number=None, amount=None,
    )
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()
    analyze = AsyncMock(return_value=result)
    monkeypatch.setattr(gmail_client, "analyze_email", analyze)
    gmail = SimpleNamespace(
        fetch_headers=AsyncMock(return_value={
            "message_id": "<msg-1@x>", "sender": "Гость <guest@mail.ru>",
            "subject": "Вопрос", "date": "Mon, 1 Sep 2026 10:00:00 +0300",
            "internal_date": 1756710000000,
        }),
        fetch_body_text=AsyncMock(return_value="Текст письма."),
    )
    bot = AsyncMock()
    settings = Settings(
        OPENAI_API_KEY="k", TELEGRAM_CHAT_ID=111,
        DATABASE_URL="sqlite+aiosqlite:///:memory:", INVOICES_DIR=str(tmp_path),
    )
    await gmail_client.process_email(gmail, bot, settings, "gmail-id-1")
    analyze.assert_awaited_once()
