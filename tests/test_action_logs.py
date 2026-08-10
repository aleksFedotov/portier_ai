"""Тикет 23: журнал действий (action_logs) + идемпотентность повторной обработки."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from portier import gmail_client
from portier.config import Settings
from portier.db import get_session_factory, init_db, init_engine
from portier.models import ActionLog, Base, EmailStatus, ProcessedEmail
from portier.schemas import EmailAnalysisResult, InvoiceDetails


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _invoice_result() -> EmailAnalysisResult:
    return EmailAnalysisResult(
        type="invoice_required", priority="normal",
        guest_name="Иван Иванов", channel_name="Ostrovok",
        arrival_date="2026-08-30", departure_date="2026-09-03",
        action_required="Выставить счёт",
        invoice=InvoiceDetails(
            company_name="ООО «Ромашка»", amount="18 140,00",
            description="Стандарт, 2 взрослых",
        ),
    )


def _gmail() -> SimpleNamespace:
    return SimpleNamespace(
        fetch_headers=AsyncMock(return_value={
            "message_id": "<msg-1@x>",
            "sender": "Ostrovok <noreply@ostrovok.ru>",
            "subject": "Запрос счёта на бронирование №1206313115",
            "date": "Mon, 1 Sep 2026 10:00:00 +0300",
            "internal_date": 1756710000000,
        }),
        fetch_body_text=AsyncMock(return_value="Просим выставить счёт."),
        fetch_attachments=AsyncMock(return_value=[]),
    )


def _settings(tmp_path) -> Settings:
    return Settings(
        OPENAI_API_KEY="k", TELEGRAM_CHAT_ID=111,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        INVOICES_DIR=str(tmp_path),
    )


async def _records(model):
    async with get_session_factory()() as session:
        return (await session.execute(select(model))).scalars().all()


async def test_action_logged_success(monkeypatch, tmp_path):
    """Успешная отправка → SUCCESS-логи по invoice_pdf_document и notify_card."""
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()
    monkeypatch.setattr(gmail_client, "analyze_email", AsyncMock(return_value=_invoice_result()))
    bot = AsyncMock()

    status = await gmail_client.process_email(_gmail(), bot, _settings(tmp_path), "gmail-id-1")

    assert status == EmailStatus.SUCCESS.value
    logs = {l.action_type: l for l in await _records(ActionLog)}
    assert logs["invoice_pdf_document"].status == "SUCCESS"
    assert logs["notify_card"].status == "SUCCESS"
    assert logs["notify_card"].attempts == 1
    assert logs["notify_card"].error_message is None


async def test_failed_card_logged_and_email_pending(monkeypatch, tmp_path):
    """Падение route_notification → FAILED-лог с текстом ошибки, письмо PENDING."""
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()
    monkeypatch.setattr(gmail_client, "analyze_email", AsyncMock(return_value=_invoice_result()))
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("Telegram упал")

    with pytest.raises(RuntimeError):
        await gmail_client.process_email(_gmail(), bot, _settings(tmp_path), "gmail-id-1")

    record = (await _records(ProcessedEmail))[0]
    assert record.status == EmailStatus.PENDING.value
    logs = {l.action_type: l for l in await _records(ActionLog)}
    # PDF ушёл до падения карточки — его лог SUCCESS
    assert logs["invoice_pdf_document"].status == "SUCCESS"
    failed = logs["notify_card"]
    assert failed.status == "FAILED"
    assert "Telegram упал" in failed.error_message


async def test_reprocess_no_duplicates(monkeypatch, tmp_path):
    """Повторная обработка PENDING: без дублей отправок, без повторного LLM,
    без IntegrityError — статус становится SUCCESS."""
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()
    analyze = AsyncMock(return_value=_invoice_result())
    monkeypatch.setattr(gmail_client, "analyze_email", analyze)
    bot = AsyncMock()
    # Первая обработка: карточка падает после успешной отправки PDF
    bot.send_message.side_effect = RuntimeError("Telegram упал")
    with pytest.raises(RuntimeError):
        await gmail_client.process_email(_gmail(), bot, _settings(tmp_path), "gmail-id-1")
    assert bot.send_document.await_count == 1
    assert analyze.await_count == 1

    # Повторная обработка того же письма (Telegram уже доступен)
    bot.send_message.side_effect = None
    status = await gmail_client.process_email(_gmail(), bot, _settings(tmp_path), "gmail-id-1")

    assert status == EmailStatus.SUCCESS.value
    # Дубля PDF нет: send_document вызван один раз за обе обработки
    assert bot.send_document.await_count == 1
    # LLM не вызывалась повторно
    assert analyze.await_count == 1
    # Карточка доотправлена (1 падение + 1 успех)
    assert bot.send_message.await_count == 2
    # Запись одна — повторной вставки с тем же message_id не было
    records = await _records(ProcessedEmail)
    assert len(records) == 1
    assert records[0].status == EmailStatus.SUCCESS.value
    # Лог notify_card: единственный, переведён в SUCCESS, попытки посчитаны
    logs = {l.action_type: l for l in await _records(ActionLog)}
    assert logs["notify_card"].status == "SUCCESS"
    assert logs["notify_card"].attempts == 2
    assert logs["notify_card"].error_message is None
    assert logs["invoice_pdf_document"].attempts == 1
