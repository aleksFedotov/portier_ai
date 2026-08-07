"""Тикет 08: чёрный список отправителей (модуль muted + конвейер)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from portier import gmail_client
from portier.config import Settings
from portier.db import get_session_factory, init_db, init_engine
from portier.models import EmailStatus, ProcessedEmail
from portier.muted import is_muted, parse_rule

# ---------- parse_rule / is_muted ----------


def test_parse_rule_plain():
    assert parse_rule("Spam <Spammer@X.ru>") == ("spam <spammer@x.ru>", None)


def test_parse_rule_with_subject():
    assert parse_rule("a@b.ru|Карта Лояльности") == ("a@b.ru", "карта лояльности")


def test_is_muted_plain_sender():
    rules = ["info@notify.comfortbooking.ru"]
    assert is_muted('"Комфорт Букинг" <info@notify.comfortbooking.ru>', "Реестр №1", rules)
    assert not is_muted("other@comfortbooking.ru", "Реестр №1", rules)


def test_is_muted_subject_pattern():
    rules = ["noreply@travellinemail.com|выдана карта лояльности"]
    sender = "TravelLine <noreply@travellinemail.com>"
    assert is_muted(sender, "Выдана карта лояльности № 01016900000815", rules)
    # бронирование с того же адреса — НЕ глушится
    assert not is_muted(sender, "Подтверждение бронирования №20260720-7348-444660748", rules)


def test_is_muted_case_insensitive():
    assert is_muted("NEWS <News@X.RU>", "тема", ["news@x.ru"])
    assert is_muted("a@b.ru", "СТАТИСТИКА ЗА НЕДЕЛЮ для отеля", ["a@b.ru|статистика за неделю"])


def test_default_rules_do_not_mute_bookings():
    """Страховка: дефолтный чёрный список не цепляет рабочие письма."""
    settings = Settings(OPENAI_API_KEY="k")
    assert not is_muted("TravelLine <noreply@travellinemail.com>", "Подтверждение бронирования №1", settings.MUTED_SENDERS)
    assert not is_muted('"Яндекс" <hotels@travel.yandex.ru>', "Реестр бронирований по платежному поручению 1", settings.MUTED_SENDERS)
    assert not is_muted("info@kuper.ru", "Счёт на оплату в магазине METRO", settings.MUTED_SENDERS)
    assert is_muted("TravelLine <noreply@travellinemail.com>", "Выдана карта лояльности № 1", settings.MUTED_SENDERS)


# ---------- конвейер ----------


async def _run_pipeline(monkeypatch, tmp_path, sender, subject):
    """Прогнать process_email на моках, вернуть (gmail_mock, bot_mock, record)."""
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
        OPENAI_API_KEY="k", TELEGRAM_CHAT_ID=1,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        INVOICES_DIR=str(tmp_path),
    )
    await gmail_client.process_email(gmail, bot, settings, "gmail-id-1")
    async with get_session_factory()() as session:
        record = (await session.execute(select(ProcessedEmail))).scalars().one()
    return gmail, bot, record, analyze


async def test_pipeline_muted_sender_skipped(monkeypatch, tmp_path):
    gmail, bot, record, analyze = await _run_pipeline(
        monkeypatch, tmp_path,
        '"Комфорт Букинг" <info@notify.comfortbooking.ru>', "Реестр №02624 …",
    )
    assert record.status == EmailStatus.SKIPPED.value
    analyze.assert_not_awaited()  # LLM не вызывалась
    bot.send_message.assert_not_called()  # в Telegram ничего не ушло


async def test_pipeline_muted_only_by_subject(monkeypatch, tmp_path):
    """Карта лояльности глушится, подтверждение брони с того же адреса — нет."""
    _, _, record, analyze = await _run_pipeline(
        monkeypatch, tmp_path,
        "TravelLine <noreply@travellinemail.com>", "Выдана карта лояльности № 123",
    )
    assert record.status == EmailStatus.SKIPPED.value
    analyze.assert_not_awaited()
