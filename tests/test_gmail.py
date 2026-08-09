"""Тесты Gmail-клиента: запрос, разбор заголовков, извлечение тела (без сети)."""

import base64

import pytest

from portier.gmail_client import (
    GmailAuthError,
    build_query,
    extract_body_from_payload,
    get_credentials,
    parse_headers,
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_build_query_backlog():
    query = build_query(None, 7)
    assert query.startswith("after:")
    assert query.endswith(" category:primary -label:portier-processed")
    assert int(query.split(":")[1].split()[0]) > 0


def test_build_query_cursor():
    assert build_query(1700000000, 7) == (
        "after:1700000000 category:primary -label:portier-processed"
    )


def test_parse_headers():
    payload = {
        "headers": [
            {"name": "Message-ID", "value": "<abc@mail.gmail.com>"},
            {"name": "From", "value": "Гость <guest@mail.ru>"},
            {"name": "Subject", "value": "Бронь 123"},
            {"name": "Date", "value": "Mon, 1 Sep 2026 10:00:00 +0300"},
        ]
    }
    parsed = parse_headers(payload, internal_date=1756710000000)
    assert parsed["message_id"] == "<abc@mail.gmail.com>"
    assert parsed["sender"] == "Гость <guest@mail.ru>"
    assert parsed["subject"] == "Бронь 123"
    assert parsed["internal_date"] == 1756710000000


def test_parse_headers_missing_message_id():
    assert parse_headers({"headers": []})["message_id"] is None


def test_extract_body_plain_preferred():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/html", "body": {"data": _b64("<p>HTML <b>вариант</b></p>")}},
            {"mimeType": "text/plain", "body": {"data": _b64("Обычный текст")}},
        ],
    }
    assert extract_body_from_payload(payload) == "Обычный текст"


def test_extract_body_html_fallback():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": _b64("<p>Только HTML</p>")}},
        ],
    }
    assert extract_body_from_payload(payload) == "Только HTML"


def test_extract_body_single_part():
    payload = {"mimeType": "text/plain", "body": {"data": _b64("Одно тело")}}
    assert extract_body_from_payload(payload) == "Одно тело"


def test_get_credentials_without_token(tmp_path, monkeypatch):
    """Без token.json — понятная ошибка с инструкцией авторизации."""
    class _S:
        GOOGLE_TOKEN_FILE = str(tmp_path / "token.json")

    with pytest.raises(GmailAuthError) as exc:
        get_credentials(_S())
    assert "gmail_auth" in str(exc.value)


# --- Тикет 14: метка portier-processed вешается только при успехе ---

from types import SimpleNamespace
from unittest.mock import AsyncMock

import portier.gmail_client as gc
from portier.db import init_db, init_engine


@pytest.fixture
async def _db():
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()


async def test_check_once_labels_only_ok_statuses(_db, monkeypatch):
    """SUCCESS/SKIPPED/дедуп → метка; ERROR и исключение → без метки."""
    statuses = {
        "g-ok": "SUCCESS",
        "g-muted": "SKIPPED",
        "g-dup": gc.ALREADY_PROCESSED,
        "g-err": "ERROR",
    }

    async def fake_process(gmail, bot, settings, gmail_id):
        if gmail_id == "g-crash":
            raise RuntimeError("boom")
        return statuses[gmail_id]

    monkeypatch.setattr(gc, "process_email", fake_process)
    gmail = SimpleNamespace(
        list_new_message_ids=AsyncMock(
            return_value=["g-ok", "g-muted", "g-dup", "g-err", "g-crash"]
        ),
        mark_processed=AsyncMock(),
    )
    settings = SimpleNamespace(BACKLOG_DAYS=7)

    await gc.check_once(gmail, bot=None, settings=settings)

    labeled = sorted(c.args[0] for c in gmail.mark_processed.call_args_list)
    assert labeled == ["g-dup", "g-muted", "g-ok"]


async def test_check_once_label_failure_does_not_stop_queue(_db, monkeypatch):
    """Сбой mark_processed не роняет цикл — следующие письма обрабатываются."""
    seen = []

    async def fake_process(gmail, bot, settings, gmail_id):
        seen.append(gmail_id)
        return "SUCCESS"

    monkeypatch.setattr(gc, "process_email", fake_process)
    gmail = SimpleNamespace(
        list_new_message_ids=AsyncMock(return_value=["g-1", "g-2"]),
        mark_processed=AsyncMock(side_effect=RuntimeError("gmail down")),
    )
    settings = SimpleNamespace(BACKLOG_DAYS=7)

    await gc.check_once(gmail, bot=None, settings=settings)

    assert seen == ["g-1", "g-2"]
    assert gmail.mark_processed.call_count == 2


# --- Тикет 17: тихие часы ---

from datetime import datetime as _dt

from portier.gmail_client import in_quiet_hours


def test_quiet_hours_overnight_window():
    assert in_quiet_hours(_dt(2026, 8, 9, 23, 0), 23, 7) is True
    assert in_quiet_hours(_dt(2026, 8, 9, 3, 30), 23, 7) is True
    assert in_quiet_hours(_dt(2026, 8, 9, 6, 59), 23, 7) is True
    assert in_quiet_hours(_dt(2026, 8, 9, 7, 0), 23, 7) is False
    assert in_quiet_hours(_dt(2026, 8, 9, 22, 59), 23, 7) is False
    assert in_quiet_hours(_dt(2026, 8, 9, 12, 0), 23, 7) is False


def test_quiet_hours_day_window_and_disabled():
    assert in_quiet_hours(_dt(2026, 8, 9, 15, 0), 12, 18) is True
    assert in_quiet_hours(_dt(2026, 8, 9, 9, 0), 12, 18) is False
    # start == end — режим выключен, всегда False
    assert in_quiet_hours(_dt(2026, 8, 9, 3, 0), 0, 0) is False
