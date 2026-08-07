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
    assert int(query.split(":")[1]) > 0


def test_build_query_cursor():
    assert build_query(1700000000, 7) == "after:1700000000"


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
