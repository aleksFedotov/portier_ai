"""Тикет 08: чёрный список отправителей (модуль muted + конвейер)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from portier import gmail_client
from portier.config import Settings
from portier.db import get_session_factory, init_db, init_engine
from portier.models import EmailStatus, ProcessedEmail
from portier.muted import addr_matches, is_muted, parse_rule

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


def test_is_muted_yo_e_equivalent():
    """«ё» и «е» в теме/шаблоне равнозначны (Суточно.ру пишет «внёс»)."""
    rules = ["info@sutochno.ru|гость внес предоплату"]
    assert is_muted("Суточно.ру <info@sutochno.ru>", "Гость внёс предоплату за бронь", rules)
    assert is_muted("Суточно.ру <info@sutochno.ru>", "Гость внес предоплату за бронь", rules)


def test_default_rules_do_not_mute_bookings():
    """Страховка: дефолтный чёрный список не цепляет рабочие письма."""
    settings = Settings(OPENAI_API_KEY="k")
    assert not is_muted("TravelLine <noreply@travellinemail.com>", "Подтверждение бронирования №1", settings.MUTED_SENDERS)
    assert not is_muted('"Яндекс" <hotels@travel.yandex.ru>', "Реестр бронирований по платежному поручению 1", settings.MUTED_SENDERS)
    assert is_muted("TravelLine <noreply@travellinemail.com>", "Выдана карта лояльности № 1", settings.MUTED_SENDERS)
    # Купер глушится по умолчанию — его счета перехватывает тикет 10 раньше
    assert is_muted("info@kuper.ru", "Ваш заказ в магазине Лента", settings.MUTED_SENDERS)


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


# ---------- доменные правила (тикет 19) ----------


def test_addr_matches_domain_rule():
    assert addr_matches("@v2.hbconnect.ru", "o_1631426.spb@v2.hbconnect.ru")
    assert not addr_matches("@v2.hbconnect.ru", "other@hbconnect.ru")
    assert addr_matches("a@b.ru", "a@b.ru")
    assert not addr_matches("a@b.ru", "x@b.ru")


def test_is_muted_domain_rule():
    rules = ["@bronevik.com|напоминание о заезде гостей"]
    assert is_muted(
        "Bronevik <l.vlasova@bronevik.com>",
        "Bronevik.com: напоминание о заезде гостей 24.07.2026", rules,
    )
    assert not is_muted("Bronevik <l.vlasova@bronevik.com>", "Вопрос по оплате", rules)


async def test_pipeline_owner_notice_before_muted(monkeypatch, tmp_path):
    """Купер заглушён целиком, но «Счёт на оплату» уходит владельцу (тикет 19)."""
    _, bot, record, analyze = await _run_pipeline(
        monkeypatch, tmp_path,
        '"Купер" <info@kuper.ru>', "Счёт на оплату в магазине METRO",
    )
    assert record.email_type == "owner_notice"
    assert record.status == EmailStatus.SUCCESS.value
    analyze.assert_not_awaited()
    bot.send_message.assert_called_once()  # уведомление владельцу ушло


async def test_pipeline_login_code(monkeypatch, tmp_path):
    """Контур заглушён целиком, но «Вход в сервис» — код в третью группу."""
    _, bot, record, analyze = await _run_pipeline(
        monkeypatch, tmp_path,
        '"Контур" <accounts@kontur.ru>', "Вход в сервис",
    )
    assert record.email_type == "login_code"
    analyze.assert_not_awaited()
    bot.send_message.assert_called_once()


async def test_pipeline_admin_attention(monkeypatch, tmp_path):
    """Заявка HBConnect — в основную группу, без LLM."""
    _, bot, record, analyze = await _run_pipeline(
        monkeypatch, tmp_path,
        '"HBConnect - Спб" <o_1631426.spb@v2.hbconnect.ru>',
        "Заявка на бронирование #1631426",
    )
    assert record.email_type == "admin_attention"
    analyze.assert_not_awaited()
    bot.send_message.assert_called_once()


async def test_pipeline_travelline_digest_muted(monkeypatch, tmp_path):
    """Ежедневный дайджест «Уведомление о бронированиях» глушится (тикет 19)."""
    _, _, record, analyze = await _run_pipeline(
        monkeypatch, tmp_path,
        "TravelLine <noreply@travellinemail.com>", "Уведомление о бронированиях",
    )
    assert record.status == EmailStatus.SKIPPED.value
    analyze.assert_not_awaited()


# ---------- тикет 21: хвост unknown после бэктеста 10.08.2026 ----------


def test_ticket21_muted_rules():
    """Новые глушения: дайджест Platform, закупки, Google-рассылки, баунсы."""
    settings = Settings(OPENAI_API_KEY="k")
    rules = settings.MUTED_SENDERS
    assert is_muted("TravelLine <noreply@travellinemail.com>", "Аналитический отчет TravelLine: Platform для сайта likihotel.com", rules)
    assert is_muted("Суточно.ру <info@sutochno.ru>", "На ваш баланс поступили средства", rules)
    assert is_muted("Суточно.ру <info@sutochno.ru>", "🔑 Тук-тук! Кажется, вы не завершили бронирование", rules)
    assert is_muted("Dobry.market@multonpartners.com", "Мы получили ваш заказ № 1", rules)
    assert is_muted("KDV Online <info@kdvonline.ru>", "Заказ #RB1308240B передан на доставку", rules)
    assert is_muted("Google <google-noreply@google.com>", "Мы обновляем Условия использования", rules)
    assert is_muted("Google Developers <googledevelopers-noreply@google.com>", "[Action Advised] Manage your unused OAuth clients", rules)
    assert is_muted("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "Delivery Status Notification (Failure)", rules)
    # Яндекс: сервисные рассылки поддержки (переход на УПД) — не счёт
    assert is_muted('Компания Яндекс <info-noreply@support.yandex.ru>', "Переход на использование УПД с 01.08.26г.", rules)


def test_bronevik_invoice_request_muted():
    """«Просьба выставить счет за услугу #…» от billing@bronevik.online — глушим
    (решение владельца 31.08.2026): счёт по таким письмам не выставляем.
    Заявки агентов с того же адреса — не трогаем."""
    settings = Settings(OPENAI_API_KEY="k")
    rules = settings.MUTED_SENDERS
    sender = "Bronevik.com <billing@bronevik.online>"
    assert is_muted(sender, "Просьба выставить счет за услугу #62759589", rules)
    assert not is_muted(sender, "Вопрос по оплате услуги #62759589", rules)


async def test_pipeline_bronevik_invoice_request_muted(monkeypatch, tmp_path):
    _, bot, record, analyze = await _run_pipeline(
        monkeypatch, tmp_path,
        "Bronevik.com <billing@bronevik.online>",
        "Просьба выставить счет за услугу #62759589",
    )
    assert record.status == EmailStatus.SKIPPED.value
    analyze.assert_not_awaited()
    bot.send_message.assert_not_called()


def test_2gis_review_not_muted():
    """2ГИС «клиент ждёт ответа на отзыв» обрабатываем как отзыв, не глушим
    (решение владельца 13.08.2026)."""
    settings = Settings(OPENAI_API_KEY="k")
    assert not is_muted(
        "2ГИС для бизнеса <noreply@account.2gis.com>",
        "Ваш клиент ждёт ответа на отзыв",
        settings.MUTED_SENDERS,
    )


async def test_pipeline_ostrovok_sverka_owner(monkeypatch, tmp_path):
    """«Сверка началась» с экстранет-адреса Островка — владельцу (тикет 21)."""
    _, bot, record, analyze = await _run_pipeline(
        monkeypatch, tmp_path,
        '"Островок! Экстранет" <hotels@account.extranet.ostrovok.ru>', "Сверка началась",
    )
    assert record.email_type == "owner_notice"
    analyze.assert_not_awaited()
    bot.send_message.assert_called_once()


async def test_pipeline_kdv_invoice_owner_not_muted(monkeypatch, tmp_path):
    """Счёт KDV перехватывается владельцем раньше глушения заказов (тикет 21)."""
    _, bot, record, analyze = await _run_pipeline(
        monkeypatch, tmp_path,
        "KDV Online <info@kdvonline.ru>", "Счет на оплату заказа #RB1308240B",
    )
    assert record.email_type == "owner_notice"
    analyze.assert_not_awaited()
    bot.send_message.assert_called_once()


async def test_pipeline_google_security_alert_owner(monkeypatch, tmp_path):
    """Оповещение безопасности Google — владельцу (тикет 21)."""
    _, bot, record, analyze = await _run_pipeline(
        monkeypatch, tmp_path,
        "Google <no-reply@accounts.google.com>", "Оповещение системы безопасности",
    )
    assert record.email_type == "owner_notice"
    analyze.assert_not_awaited()
    bot.send_message.assert_called_once()
