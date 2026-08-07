"""Тикет 10: входящие счета (документ в 3-ю группу / Купер владельцу) и алерты."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from portier import gmail_client
from portier.config import Settings
from portier.db import get_session_factory, init_db, init_engine
from portier.incoming import is_alert, is_invoice_filename
from portier.models import EmailStatus, ProcessedEmail

# ---------- юнит ----------


def test_is_invoice_filename():
    assert is_invoice_filename("Счет-03-080726222843.pdf")
    assert is_invoice_filename("Счет на оплату № 453 от 27 июля 2026 г.xls")
    assert is_invoice_filename("Счёт_№301700_от_31.07.2026.xlsx")
    assert is_invoice_filename("Invoice_123.pdf")
    assert not is_invoice_filename("Детализация по договору 78-2233.pdf")
    assert not is_invoice_filename("Счет.txt")  # не документ
    assert not is_invoice_filename("")


def test_is_alert():
    rules = ["support@travelline.ru|возможный овербукинг", "no-reply@gosuslugi.ru"]
    assert is_alert("TL <support@travelline.ru>", "TravelLine. Возможный овербукинг", rules)
    assert not is_alert("TL <support@travelline.ru>", "Мы на экваторе с TL: Metasearch", rules)
    assert is_alert("Госуслуги <no-reply@gosuslugi.ru>", "Любая тема", rules)


# ---------- конвейер ----------


async def _run_pipeline(monkeypatch, tmp_path, sender, subject, attachments=(), **kw):
    """Прогнать process_email на моках, вернуть (bot, record, analyze)."""
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
        fetch_attachments=AsyncMock(return_value=list(attachments)),
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


def _doc_chat_id(bot):
    args, kwargs = bot.send_document.await_args
    return kwargs.get("chat_id", args[0] if args else None)


async def test_invoice_goes_to_third_group(monkeypatch, tmp_path):
    bot, record, analyze = await _run_pipeline(
        monkeypatch, tmp_path, "DELTA <cc@delta.ru>", "Пополните счет… DELTA",
        attachments=[("Счет-03-1.pdf", b"%PDF"), ("Детализация.pdf", b"%PDF2")],
        INCOMING_INVOICES_CHAT_ID=555,
    )
    assert record.status == EmailStatus.SUCCESS.value
    assert record.email_type == "incoming_invoice"
    analyze.assert_not_awaited()
    assert bot.send_document.await_count == 2  # счёт + детализация
    assert _doc_chat_id(bot) == 555


async def test_kuper_invoice_goes_to_owner(monkeypatch, tmp_path):
    bot, record, analyze = await _run_pipeline(
        monkeypatch, tmp_path, "Купер <info@kuper.ru>", "Счёт на оплату в магазине METRO",
        attachments=[("Счёт на оплату № 1.pdf", b"%PDF")],
        INCOMING_INVOICES_CHAT_ID=555, OWNER_CHAT_ID=999,
    )
    assert record.email_type == "kuper_invoice"
    analyze.assert_not_awaited()
    assert _doc_chat_id(bot) == 999


async def test_invoice_fallback_chat(monkeypatch, tmp_path):
    """Ни INCOMING_INVOICES_CHAT_ID, ни OWNER_CHAT_ID не заданы → основной чат."""
    bot, record, _ = await _run_pipeline(
        monkeypatch, tmp_path, "DELTA <cc@delta.ru>", "Пополните счет",
        attachments=[("Счет-03-1.pdf", b"%PDF")],
        INCOMING_INVOICES_CHAT_ID=None, OWNER_CHAT_ID=None,
    )
    assert _doc_chat_id(bot) == 111


async def test_alert_goes_to_third_group(monkeypatch, tmp_path):
    bot, record, analyze = await _run_pipeline(
        monkeypatch, tmp_path,
        "TL <support@travelline.ru>", "TravelLine. Возможный овербукинг",
        INCOMING_INVOICES_CHAT_ID=555,
    )
    assert record.email_type == "alert"
    analyze.assert_not_awaited()
    args, kwargs = bot.send_message.await_args
    assert kwargs.get("chat_id", args[0] if args else None) == 555


async def test_no_invoice_attachments_fall_through(monkeypatch, tmp_path):
    """Нет вложений-счетов → конвейер идёт дальше (письмо Купера без счёта глушится)."""
    bot, record, analyze = await _run_pipeline(
        monkeypatch, tmp_path, "Купер <info@kuper.ru>", "Ваш заказ в магазине Лента",
        attachments=[],
    )
    assert record.status == EmailStatus.SKIPPED.value  # в чёрном списке
    analyze.assert_not_awaited()
    bot.send_document.assert_not_called()
