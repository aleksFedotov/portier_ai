"""Тикет 15: маршрутизация уведомлений.

- unknown / booking_modified / booking_cancelled / payment_received /
  booking_comment — молча в БД, без Telegram;
- review_notification — карточка в основную группу (решение 13.08.2026);
- booking_confirmed с комментарием гостя — уведомление в основную группу;
- коды входа (Extranet/TravelLine/Ozon/Telegram) и accounting@travelline.ru —
  в третью группу; сверка 101hotels — лично владельцу.
"""

from types import SimpleNamespace
from datetime import date, timedelta
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

    body_text = kw.pop("body_text", "Текст письма.")
    analyze = AsyncMock()
    if llm_type:
        analyze.return_value = EmailAnalysisResult(
            type=llm_type, priority="normal", action_required="—",
            booking_number="20260807-7348-456714535",
            comment_details=kw.pop("comment_details", None),
            arrival_date=kw.pop("arrival_date", None),
            departure_date=kw.pop("departure_date", None),
            guests_count=kw.pop("guests_count", None),
            guest_name=kw.pop("guest_name", None),
        )
    monkeypatch.setattr(gmail_client, "analyze_email", analyze)
    gmail = SimpleNamespace(
        fetch_headers=AsyncMock(return_value={
            "message_id": "<msg-1@x>", "sender": sender,
            "subject": subject, "date": "Mon, 1 Sep 2026 10:00:00 +0300",
            "internal_date": 1756710000000,
        }),
        fetch_body_text=AsyncMock(return_value=body_text),
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
    "payment_received", "booking_comment",
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


async def test_review_notification_sent_to_main_chat(monkeypatch, tmp_path):
    """Отзыв (2ГИС/Яндекс.Бизнес) — карточка в основную группу (13.08.2026)."""
    bot, record, _ = await _run_pipeline(
        monkeypatch, tmp_path,
        "Яндекс Бизнес <email@business.yandex.ru>",
        "У вас новые отзывы для ЛиКи Лофт Отель",
        llm_type="review_notification",
    )
    assert record.status == EmailStatus.SUCCESS.value
    assert record.email_type == "review_notification"
    assert _msg_chat_id(bot) == 111


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


# ---------- сервисные комментарии каналов (контакты гостя) ----------


@pytest.mark.parametrize("llm_type", ["booking_confirmed", "guest_message", "booking_comment"])
async def test_service_comment_not_notified(monkeypatch, tmp_path, llm_type):
    """«For contacting the guest please dial… (verification code…)» — это
    контакты гостя, а не запрос: уведомление в чат не шлём."""
    bot, record, _ = await _run_pipeline(
        monkeypatch, tmp_path,
        "Acase <noreply@acase.ru>",
        "Подтверждение бронирования №14638587. Acase.ru",
        llm_type=llm_type,
        comment_details="Гость просит связаться с ней по телефону.",
        body_text=(
            "Комментарий гостя: For contacting the guest please dial: "
            "+8651368192531(verification code:42549)"
        ),
    )
    assert record.status == EmailStatus.SUCCESS.value
    bot.send_message.assert_not_called()


async def test_real_comment_still_notified(monkeypatch, tmp_path):
    """Обычный комментарий гостя (не контакты) по-прежнему уходит в чат."""
    bot, record, _ = await _run_pipeline(
        monkeypatch, tmp_path,
        "Acase <noreply@acase.ru>",
        "Подтверждение бронирования №14638587. Acase.ru",
        llm_type="booking_confirmed",
        comment_details="Просит тихий номер",
        body_text="Комментарий гостя: просим тихий номер, приедем поздно.",
    )
    assert record.status == EmailStatus.SUCCESS.value
    bot.send_message.assert_awaited_once()


# ---------- заезд сегодня: план уборки админам ----------


async def test_checkin_today_one_night(monkeypatch, tmp_path):
    """Заезд сегодня на 1 ночь → уведомление «выезд на завтра в план уборки»."""
    today = date.today()
    bot, record, _ = await _run_pipeline(
        monkeypatch, tmp_path,
        "TravelLine <noreply@travellinemail.com>",
        "Подтверждение бронирования №20260807-7348-456714535",
        llm_type="booking_confirmed",
        arrival_date=today.isoformat(),
        departure_date=(today + timedelta(days=1)).isoformat(),
        guest_name="ИВАНОВ ИВАН",
    )
    assert record.status == EmailStatus.SUCCESS.value
    bot.send_message.assert_awaited_once()
    assert _msg_chat_id(bot) == 111
    text = bot.send_message.await_args.kwargs.get("text", "")
    assert "Сегодня новый заезд" in text
    assert "выезд на завтра" in text
    assert "текущую уборку" not in text


async def test_checkin_today_multi_night_many_guests(monkeypatch, tmp_path):
    """Заезд сегодня на 3 ночи, 4 гостя → текущая уборка + подготовить номер."""
    today = date.today()
    bot, record, _ = await _run_pipeline(
        monkeypatch, tmp_path,
        "TravelLine <noreply@travellinemail.com>",
        "Подтверждение бронирования №20260807-7348-456714535",
        llm_type="booking_confirmed",
        arrival_date=today.strftime("%d.%m.%Y"),
        departure_date=(today + timedelta(days=3)).strftime("%d.%m.%Y"),
        guests_count=4,
    )
    text = bot.send_message.await_args.kwargs.get("text", "")
    assert "текущую уборку" in text
    assert "на 4 человек" in text
    assert "выезд на завтра" not in text


async def test_checkin_tomorrow_notified(monkeypatch, tmp_path):
    """Заезд завтра → «добавьте в заезды на завтра и узнайте время приезда»."""
    tomorrow = date.today() + timedelta(days=1)
    bot, record, _ = await _run_pipeline(
        monkeypatch, tmp_path,
        "TravelLine <noreply@travellinemail.com>",
        "Подтверждение бронирования №20260807-7348-456714535",
        llm_type="booking_confirmed",
        arrival_date=tomorrow.isoformat(),
        departure_date=(tomorrow + timedelta(days=2)).isoformat(),
        guest_name="ПЕТРОВ ПЁТР",
    )
    assert record.status == EmailStatus.SUCCESS.value
    bot.send_message.assert_awaited_once()
    assert _msg_chat_id(bot) == 111
    text = bot.send_message.await_args.kwargs.get("text", "")
    assert "Завтра новый заезд" in text
    assert "заезды на завтра" in text
    assert "время приезда" in text


async def test_checkin_not_today_silent(monkeypatch, tmp_path):
    """Заезд позже завтра — уведомления о заезде нет (молчаливый тип)."""
    later = date.today() + timedelta(days=2)
    bot, record, _ = await _run_pipeline(
        monkeypatch, tmp_path,
        "TravelLine <noreply@travellinemail.com>",
        "Подтверждение бронирования №20260807-7348-456714535",
        llm_type="booking_confirmed",
        arrival_date=later.isoformat(),
        departure_date=(later + timedelta(days=1)).isoformat(),
    )
    bot.send_message.assert_not_called()


async def test_checkin_notice_for_booking_modified(monkeypatch, tmp_path):
    """Брони Яндекса приходят как booking_modified — заезд сегодня
    всё равно должен уведомить админов (решение владельца 14.08.2026)."""
    today = date.today()
    bot, record, _ = await _run_pipeline(
        monkeypatch, tmp_path,
        "TravelLine <noreply@travellinemail.com>",
        "Изменение бронирования № YA-6097-9993-4907:0. Яндекс Путешествия",
        llm_type="booking_modified",
        arrival_date=today.isoformat(),
        departure_date=(today + timedelta(days=1)).isoformat(),
    )
    assert record.email_type == "booking_modified"
    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.kwargs.get("text", "")
    assert "Сегодня новый заезд" in text
    assert "выезд на завтра" in text


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
