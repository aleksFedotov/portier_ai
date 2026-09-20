"""Ретрай временных ошибок LLM: письмо остаётся PENDING и переобрабатывается,
ERROR + ⚠️ — только после исчерпания попыток или при логической ошибке."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APITimeoutError
from sqlalchemy import select

import portier.gmail_client as gc
from portier.db import get_session_factory, init_db, init_engine
from portier.models import ActionLog, ProcessedEmail


@pytest.fixture
async def _db():
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()


def _settings():
    return SimpleNamespace(
        TELEGRAM_CHAT_ID=1,
        OWNER_CHAT_ID=None,
        INCOMING_INVOICES_CHAT_ID=None,
        ALERT_RULES=[],
        REFUND_RULES=[],
        LOGIN_CODE_RULES=[],
        ADMIN_ATTENTION_RULES=[],
        OWNER_NOTICE_SENDERS=[],
        OWNER_NOTICE_RULES=[],
        MUTED_SENDERS=[],
        INCOMING_INVOICE_SENDERS=[],
    )


def _gmail():
    return SimpleNamespace(
        fetch_headers=AsyncMock(return_value={
            "internal_date": 1750000000000,
            "sender": "noreply@travellinemail.com",
            "subject": "Подтверждение бронирования №63306849. Bronevik.com",
            "date": "Sun, 14 Sep 2026 12:00:00 +0300",
            "message_id": "<msg-63306849@x>",
        }),
        fetch_body_text=AsyncMock(return_value="текст письма"),
        fetch_attachments=AsyncMock(return_value=[]),
    )


def _timeout_error() -> APITimeoutError:
    return APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1"))


async def _record() -> ProcessedEmail:
    async with get_session_factory()() as session:
        return (await session.execute(
            select(ProcessedEmail).where(ProcessedEmail.message_id == "<msg-63306849@x>")
        )).scalar_one()


async def _llm_attempts() -> int:
    async with get_session_factory()() as session:
        log = (await session.execute(
            select(ActionLog).where(ActionLog.action_type == "llm_analyze")
        )).scalar_one_or_none()
        return log.attempts if log else 0


async def test_llm_timeout_keeps_pending(_db, monkeypatch):
    """APITimeoutError → письмо остаётся PENDING, ⚠️ не шлём, попытка записана."""
    monkeypatch.setattr(gc, "analyze_body", AsyncMock(side_effect=_timeout_error()))
    notify = AsyncMock()
    monkeypatch.setattr(gc, "_notify_error", notify)

    status = await gc.process_email(_gmail(), bot=None, settings=_settings(), gmail_id="g-1")

    assert status == "PENDING"
    assert (await _record()).status == "PENDING"
    assert await _llm_attempts() == 1
    notify.assert_not_awaited()


async def test_llm_timeout_gives_up_after_max_attempts(_db, monkeypatch):
    """Исчерпали попытки → ERROR + ⚠️ админу."""
    monkeypatch.setattr(gc, "analyze_body", AsyncMock(side_effect=_timeout_error()))
    monkeypatch.setattr(gc, "MAX_LLM_ATTEMPTS", 2)
    notify = AsyncMock()
    monkeypatch.setattr(gc, "_notify_error", notify)

    assert await gc.process_email(_gmail(), bot=None, settings=_settings(), gmail_id="g-1") == "PENDING"
    assert await gc.process_email(_gmail(), bot=None, settings=_settings(), gmail_id="g-1") == "ERROR"

    record = await _record()
    assert record.status == "ERROR"
    assert "APITimeoutError" in record.error_log
    assert await _llm_attempts() == 2
    notify.assert_awaited_once()


async def test_llm_logic_error_fails_immediately(_db, monkeypatch):
    """Логическая ошибка LLM → сразу ERROR, без ретраев по циклам."""
    monkeypatch.setattr(gc, "analyze_body", AsyncMock(side_effect=ValueError("bad json")))
    notify = AsyncMock()
    monkeypatch.setattr(gc, "_notify_error", notify)

    status = await gc.process_email(_gmail(), bot=None, settings=_settings(), gmail_id="g-1")

    assert status == "ERROR"
    assert (await _record()).status == "ERROR"
    assert await _llm_attempts() == 0
    notify.assert_awaited_once()
