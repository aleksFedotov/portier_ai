"""Тикет 15: маршрутизация уведомлений.

- unknown / booking_modified / booking_cancelled / payment_received /
  review_notification / booking_comment — молча в БД, без Telegram;
- booking_confirmed с комментарием гостя — уведомление в основную группу;
- коды входа (Extranet/TravelLine/Ozon/Telegram) и accounting@travelline.ru —
  в третью группу; сверка 101hotels — лично владельцу.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from portier import gmail_client
from portier.config import Settings
from portier.db import get_session_factory, init_db, init_engine
from portier.models import EmailStatus, ProcessedEmail
from portier.schemas import EmailAnalysisResult


async def _run_pipeline(monkeypatch, tmp_path, sender, subject, llm_type=None, **kw):
    """Прогнать process_email на моках, вернуть (bot, record, analyze)."""
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()

    analyze = AsyncMock()
    if llm_type:
        analyze.return_value = EmailAnalysisResult(
            type=llm_type, priority="normal", action_required="—",
            booking_number="20260807-7348-456714535",
            comment_details=kw.pop("comment_details", None),
        )
    monkeypatch.setattr(gmail_client, "analyze_email", analyze)
    gmail = SimpleNamespace(
        fetch_headers=AsyncMock(return_value={
            "message_id": "<msg-1@x>", "sender": sender,
            "subject": subject, "date": "Mon, 1 Sep 2026 10:00:00 +0300",
            "internal_date": 1756710000000,
        }),
        fetch_body_text=AsyncMock(return_value="Текст письма."),
        fetch_attachments=AsyncMock(return_value=[]),
    )
    bot = AsyncMock()
    settings = Settings(
        OPENAI_API_KEY="k", TELEGRAM_CHAT_ID=111,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        INVOICES_DIR=str(tmp_path), **kw,
    )
    await gmail_client.process_email(gmail, bot, settings, "gmail-id-1")
    async with get_session_factory()() as session:
        record = (await session.execute(select(ProcessedEmail))).scalars().one()
    return bot, record, analyze


def _msg_chat_id(bot):
    args, kwargs = bot.send_message.await_args
    return kwargs.get("chat_id", args[0] if args else None)


# ---------- молчаливые типы ----------


@pytest.mark.parametrize("llm_type", [
    "unknown", "booking_modified", "booking_cancelled",
    "payment_received", "review_notification", "booking_comment",
])
async def test_silent_types_no_telegram(monkeypatch, tmp_path, llm_type):
    """Эти типы фиксируем в БД, в основную группу не шлём."""
    bot, record, _ = await _run_pipeline(
        monkeypatch, tmp_path,
        "TravelLine <noreply@travellinemail.com>", "Какая-то тема",
        llm_type=llm_type,
    )
    assert record.status == EmailStatus.SUCCESS.value
    assert record.email_type == llm_type
    bot.send_message.assert_not_called()


async def test_booking_confirmed_with_comment_notified(monkeypatch, tmp_path):
    """Комментарий гостя берём из подтверждения TravelLine → основная группа."""
    bot, record, _ = await _run_pipeline(
        monkeypatch, tmp_path,
        "TravelLine <noreply@travellinemail.com>",
        "Подтверждение бронирования №20260807-7348-456714535",
        llm_type="booking_confirmed",
        comment_details="Просит тихий номер и ранний заезд",
    )
    assert record.status == EmailStatus.SUCCESS.value
    assert record.email_type == "booking_confirmed"
    assert _msg_chat_id(bot) == 111
    text = bot.send_message.await_args.kwargs.get("text", "")
    assert "тихий номер" in text


async def test_booking_confirmed_without_comment_silent(monkeypatch, tmp_path):
    bot, record, _ = await _run_pipeline(
        monkeypatch, tmp_path,
        "TravelLine <noreply@travellinemail.com>",
        "Подтверждение бронирования №20260807-7348-456714535",
        llm_type="booking_confirmed",
    )
    assert record.status == EmailStatus.SUCCESS.value
    bot.send_message.assert_not_called()


# ---------- коды входа и расчётный отдел → третья группа ----------


@pytest.mark.parametrize("sender,subject,expected_type", [
    ("info@101hotels.com <info@101hotels.com>", "Код для входа в Extranet", "login_code"),
    ("TravelLine <noreply@travellinemail.com>", "Вход в учетную запись TravelLine", "login_code"),
    ("Ozon <mailer@sender.ozon.ru>", "Подтверждение учетных данных Ozon", "login_code"),
    ("Telegram <noreply@telegram.org>", "Ваш код - 403611", "login_code"),
    ("Расчетный отдел TravelLine <accounting@travelline.ru>",
     "Продление подписки на компоненты TravelLine", "incoming_invoice"),
])
async def test_security_alerts_to_third_group(monkeypatch, tmp_path, sender, subject, expected_type):
    bot, record, analyze = await _run_pipeline(
        monkeypatch, tmp_path, sender, subject,
        INCOMING_INVOICES_CHAT_ID=555,
    )
    assert record.email_type == expected_type
    analyze.assert_not_awaited()  # детерминированно, без LLM
    assert _msg_chat_id(bot) == 555


# ---------- сверка 101hotels → лично владельцу ----------


@pytest.mark.parametrize("subject", [
    "Войдите в Систему! Cверка за июль 2026 до 10 августа!",  # латинская C
    "Истекает срок оплаты по сверке за июль 2026!",  # кириллица
])
async def test_sverka_goes_to_owner(monkeypatch, tmp_path, subject):
    bot, record, analyze = await _run_pipeline(
        monkeypatch, tmp_path,
        "info@101hotels.com <info@101hotels.com>", subject,
        OWNER_CHAT_ID=999, INCOMING_INVOICES_CHAT_ID=555,
    )
    assert record.email_type == "owner_notice"
    analyze.assert_not_awaited()
    assert _msg_chat_id(bot) == 999


async def test_101hotels_booking_not_owner_notice(monkeypatch, tmp_path):
    """Брони с того же адреса 101hotels — обычный конвейер, не владельцу."""
    bot, record, analyze = await _run_pipeline(
        monkeypatch, tmp_path,
        "info@101hotels.com <info@101hotels.com>", "Новое бронирование. Отель Liki Loft",
        llm_type="booking_confirmed",
        OWNER_CHAT_ID=999,
    )
    analyze.assert_awaited()
    assert record.email_type == "booking_confirmed"
    bot.send_message.assert_not_called()  # booking_confirmed без комментария — тишина
