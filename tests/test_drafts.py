"""Тесты черновиков счетов: мэтчинг компаний, MIME, сквозной сценарий на моках."""

import base64
import email
import email.policy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from portier import gmail_client
from portier.db import init_db, init_engine, get_session_factory
from portier.drafts import (
    DEFAULT_SUBJECT,
    build_draft_mime,
    find_company,
    merge_invoice_data,
    parse_sender_email,
)
from portier.models import Base, Company, ProcessedEmail
from portier.schemas import EmailAnalysisResult, InvoiceDetails
from sqlalchemy import select


# ---------- фикстуры ----------

@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _settings(tmp_path):
    return SimpleNamespace(
        OPENAI_MODEL="gpt-4o-mini",
        TELEGRAM_CHAT_ID=-100123,
        HOTEL_NAME="Мини-отель «Тест»",
        HOTEL_INN="7701234567",
        HOTEL_DETAILS="р/с 4070281...",
        INVOICES_DIR=str(tmp_path / "invoices"),
        MUTED_SENDERS=[],
        OWNER_CHAT_ID=None,
        OWNER_NOTICE_SENDERS=[],
        OWNER_NOTICE_RULES=[],
        ALERT_RULES=[],
        LOGIN_CODE_RULES=[],
        ADMIN_ATTENTION_RULES=[],
        INVOICE_OWNER_EXCEPTIONS=[],
        INCOMING_INVOICE_SENDERS=[],
        INCOMING_INVOICES_CHAT_ID=None,
    )


def _invoice_result(**invoice_kwargs) -> EmailAnalysisResult:
    return EmailAnalysisResult(
        type="invoice_required",
        priority="normal",
        action_required="Выставить счёт",
        comment_details="Просят счёт для компании",
        invoice=InvoiceDetails(**invoice_kwargs),
    )


def _parse_mime(raw_b64: str):
    return email.message_from_bytes(
        base64.urlsafe_b64decode(raw_b64.encode()), policy=email.policy.default
    )


# ---------- мэтчинг и сведение данных ----------

def test_parse_sender_email():
    assert parse_sender_email("Иван <ivan@romashka.ru>") == "ivan@romashka.ru"
    assert parse_sender_email("ivan@romashka.ru") == "ivan@romashka.ru"


async def test_find_company_by_inn(session_factory):
    async with session_factory() as session:
        session.add(Company(name="ООО «Ромашка»", inn="7712345678"))
        await session.commit()
        found = await find_company(session, "x@other.ru", InvoiceDetails(inn="7712345678"))
        assert found is not None and found.name == "ООО «Ромашка»"


async def test_find_company_by_domain(session_factory):
    async with session_factory() as session:
        session.add(Company(name="ООО «Домен»", inn="", email="buh@romashka.ru"))
        await session.commit()
        found = await find_company(session, "manager@romashka.ru", None)
        assert found is not None and found.name == "ООО «Домен»"


async def test_find_company_by_name(session_factory):
    async with session_factory() as session:
        session.add(Company(name="ООО «Василёк»", inn=""))
        await session.commit()
        found = await find_company(
            session, "x@y.ru", InvoiceDetails(company_name="ооо «василёк»")
        )
        assert found is not None


async def test_find_company_no_match(session_factory):
    async with session_factory() as session:
        session.add(Company(name="ООО «Другое»", inn="111", email="a@other.ru"))
        await session.commit()
        assert await find_company(session, "x@unknown.ru", InvoiceDetails(inn="222")) is None


def test_merge_registry_priority():
    company = Company(
        name="ООО «Реестр»", inn="999", email="buh@reestr.ru",
        subject_template="Счёт за отель",
    )
    result = _invoice_result(company_name="LLM Название", inn="111", amount="5000")
    data = merge_invoice_data(result, "guest@mail.ru", company)
    assert data.invoice.company_name == "ООО «Реестр»"
    assert data.invoice.inn == "999"
    assert data.invoice.amount == "5000"  # сумма — из письма
    assert data.to == "buh@reestr.ru"
    assert data.subject == "Счёт за отель"


def test_merge_unknown_company():
    result = _invoice_result(company_name="LLM Название", inn="111")
    data = merge_invoice_data(result, "Бух <buh@newco.ru>", None)
    assert data.to == "buh@newco.ru"
    assert data.subject == DEFAULT_SUBJECT
    assert data.invoice.company_name == "LLM Название"


# ---------- MIME ----------

def test_build_draft_mime(tmp_path):
    pdf = tmp_path / "invoice_test.pdf"
    pdf.write_bytes(b"%PDF-1.3 fake")
    raw = build_draft_mime("buh@romashka.ru", "Счёт на оплату проживания", "Текст письма", pdf)
    msg = _parse_mime(raw)
    assert msg["To"] == "buh@romashka.ru"
    assert msg["Subject"] == "Счёт на оплату проживания"
    assert msg.is_multipart()
    parts = list(msg.iter_parts())
    assert parts[0].get_content_type() == "text/plain"
    assert "Текст письма" in parts[0].get_content()
    assert parts[1].get_content_type() == "application/pdf"
    assert parts[1].get_filename() == "invoice_test.pdf"
    assert parts[1].get_content() == b"%PDF-1.3 fake"


# ---------- сквозной сценарий ----------

async def _run_pipeline(monkeypatch, tmp_path, result, *, send_document_fails=False, companies=()):
    """Прогнать process_email на моках, вернуть (gmail_mock, bot_mock)."""
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()

    async with get_session_factory()() as session:
        for company in companies:
            session.add(company)
        await session.commit()

    monkeypatch.setattr(
        gmail_client, "analyze_email",
        AsyncMock(return_value=result),
    )
    gmail = SimpleNamespace(
        fetch_headers=AsyncMock(return_value={
            "message_id": "<msg-1@x>", "sender": "Бух <buh@romashka.ru>",
            "subject": "Нужен счёт", "date": "Mon, 1 Sep 2026 10:00:00 +0300",
            "internal_date": 1756710000000,
        }),
        fetch_body_text=AsyncMock(return_value="Просим выставить счёт на проживание."),
        fetch_attachments=AsyncMock(return_value=[]),
        create_draft=AsyncMock(return_value="draft-42"),
    )
    bot = AsyncMock()
    if send_document_fails:
        bot.send_document.side_effect = RuntimeError("Telegram недоступен")
    await gmail_client.process_email(gmail, bot, _settings(tmp_path), "gmail-id-1")
    return gmail, bot


async def _get_record():
    async with get_session_factory()() as session:
        rows = (await session.execute(select(ProcessedEmail))).scalars().all()
    assert len(rows) == 1
    return rows[0]


async def test_invoice_pipeline_happy_path(monkeypatch, tmp_path):
    company = Company(
        name="ООО «Ромашка»", inn="7712345678", email="buh@romashka.ru",
        subject_template="Счёт за проживание в отеле",
    )
    result = _invoice_result(
        company_name="Ромашка", inn="7712345678", amount="15000 руб.",
        arrival_date="2026-09-01", departure_date="2026-09-05",
    )
    # Тикет 28: в теме черновика — #<номер брони канала>
    result = result.model_copy(update={"booking_number": "1206313115"})
    gmail, bot = await _run_pipeline(
        monkeypatch, tmp_path, result, companies=[company]
    )

    # Тикет 27: черновик со счётом создаётся в Gmail (адресат — из реестра),
    # PDF уходит документом в общий чат
    gmail.create_draft.assert_awaited_once()
    raw = gmail.create_draft.await_args.args[0]
    import base64
    import email
    from email.header import decode_header

    message = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert message["To"] == "buh@romashka.ru"
    subject = str(decode_header(message["Subject"])[0][0], "utf-8")
    assert subject == "Счёт за проживание в отеле #1206313115"  # реестр + #бронь
    bot.send_document.assert_awaited_once()
    doc_kwargs = bot.send_document.await_args.kwargs
    assert doc_kwargs["chat_id"] == -100123
    assert doc_kwargs["document"].filename.endswith(".pdf")
    assert "buh@romashka.ru" in doc_kwargs["caption"]

    # PDF лежит в INVOICES_DIR
    pdfs = list((tmp_path / "invoices").glob("*.pdf"))
    assert len(pdfs) == 1

    # Telegram-уведомление
    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.kwargs["text"]
    assert "Счёт готов" in text
    assert "buh@romashka.ru" in text
    assert "Новая компания" not in text
    buttons = bot.send_message.await_args.kwargs["reply_markup"]
    assert buttons.inline_keyboard[0][0].callback_data.startswith("action:invoice_sent:")

    # Запись в БД — SUCCESS, путь к PDF сохранён (тикет 06: команда /invoices)
    record = await _get_record()
    assert record.status == "SUCCESS"
    assert record.email_type == "invoice_required"
    assert record.invoice_pdf and record.invoice_pdf.endswith(".pdf")
    from pathlib import Path

    assert Path(record.invoice_pdf).exists()


async def test_invoice_pipeline_unknown_company(monkeypatch, tmp_path):
    result = _invoice_result(company_name="ООО «Новое»", amount="9000 руб.")
    gmail, bot = await _run_pipeline(monkeypatch, tmp_path, result)

    # Тикет 27: черновик создаётся и для новой компании (адресат — отправитель);
    # PDF уходит в чат
    gmail.create_draft.assert_awaited_once()
    bot.send_document.assert_awaited_once()
    assert "buh@romashka.ru" in bot.send_document.await_args.kwargs["caption"]

    text = bot.send_message.await_args.kwargs["text"]
    assert "Новая компания — добавьте в реестр" in text


async def test_invoice_pipeline_send_document_failure(monkeypatch, tmp_path):
    """Сбой отправки PDF в чат: письмо помечается ERROR, админу уходит ⚠️."""
    result = _invoice_result(company_name="ООО «Сбой»", amount="1000")
    gmail, bot = await _run_pipeline(
        monkeypatch, tmp_path, result, send_document_fails=True
    )

    record = await _get_record()
    assert record.status == "ERROR"

    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.kwargs["text"]
    assert "Не удалось обработать письмо" in text
