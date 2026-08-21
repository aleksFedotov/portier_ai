"""Тесты черновиков счетов: мэтчинг компаний, MIME, сквозной сценарий на моках."""

import base64
import email
import email.policy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from portier import gmail_client
from portier.db import init_db, init_engine, get_session_factory
from portier.drafts import (
    DEFAULT_SUBJECT,
    build_draft_mime,
    find_booking_facts,
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
        REFUND_RULES=[],
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

async def _run_pipeline(monkeypatch, tmp_path, result, *, companies=(), prior=()):
    """Прогнать process_email на моках, вернуть (gmail_mock, bot_mock)."""
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()

    async with get_session_factory()() as session:
        for company in companies:
            session.add(company)
        for record in prior:
            session.add(record)
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

    # Тикет 27: черновик со счётом создаётся в Gmail (адресат — из реестра);
    # PDF в чат не отправляется (решение владельца от 15.08.2026)
    gmail.create_draft.assert_awaited_once()
    raw = gmail.create_draft.await_args.args[0]
    import base64
    import email
    from email.header import decode_header

    message = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert message["To"] == "buh@romashka.ru"
    subject = str(decode_header(message["Subject"])[0][0], "utf-8")
    assert subject == "Счёт за проживание в отеле #1206313115"  # реестр + #бронь
    bot.send_document.assert_not_called()

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
    # PDF в чат не отправляется
    gmail.create_draft.assert_awaited_once()
    bot.send_document.assert_not_called()

    text = bot.send_message.await_args.kwargs["text"]
    assert "Новая компания — добавьте в реестр" in text



# ---------- дозаполнение счёта из прошлых писем по брони ----------


def _email_record(message_id, booking_number, *, amount=None, internal_id=None,
                  processed_at=datetime(2026, 8, 10, 12, 0, 0)):
    """Прошлое письмо по брони (подтверждение TravelLine и т.п.)."""
    return ProcessedEmail(
        message_id=message_id,
        uid=1,
        gmail_id="",
        sender="noreply@travellinemail.com",
        subject=f"Подтверждение бронирования №{booking_number}",
        processed_at=processed_at,
        email_type="booking_confirmed",
        raw_payload="",
        llm_result={
            "booking_number": booking_number,
            "internal_booking_id": internal_id,
            "invoice": {"amount": amount},
        },
        status="SUCCESS",
    )


async def test_find_booking_facts(session_factory):
    async with session_factory() as session:
        session.add(_email_record(
            "<c1@x>", "62107414", amount="14227 RUB",
            internal_id="20260819-7348-457341008",
        ))
        await session.commit()
        facts = await find_booking_facts(session, "62107414")
        assert facts == {
            "amount": "14227 RUB",
            "internal_booking_id": "20260819-7348-457341008",
        }


async def test_find_booking_facts_latest_wins(session_factory):
    """Свежая цена (изменение брони) важнее старой; поля собираются по отдельности."""
    async with session_factory() as session:
        session.add(_email_record(
            "<c1@x>", "62107414", amount="14227 RUB",
            internal_id="20260819-7348-457341008",
            processed_at=datetime(2026, 8, 10),
        ))
        session.add(_email_record(
            "<c2@x>", "62107414", amount="15000 RUB",
            processed_at=datetime(2026, 8, 15),
        ))
        await session.commit()
        facts = await find_booking_facts(session, "62107414")
        assert facts["amount"] == "15000 RUB"
        assert facts["internal_booking_id"] == "20260819-7348-457341008"


async def test_find_booking_facts_excludes_current(session_factory):
    """Текущее письмо (без суммы) не должно быть источником данных."""
    async with session_factory() as session:
        current = _email_record("<cur@x>", "62107414")
        session.add(current)
        await session.commit()
        assert await find_booking_facts(session, "62107414", exclude_id=current.id) is None


async def test_find_booking_facts_no_match(session_factory):
    async with session_factory() as session:
        session.add(_email_record("<c1@x>", "999", amount="100"))
        await session.commit()
        assert await find_booking_facts(session, "62107414") is None
        assert await find_booking_facts(session, "") is None


async def test_invoice_pipeline_amount_from_previous_email(monkeypatch, tmp_path):
    """Заявка Броневика без цены: сумма и ID счёта берутся из подтверждения брони."""
    from portier import invoices

    captured = {}
    real_generate = invoices.generate_invoice_pdf

    def spy(result_arg, settings_arg):
        captured["result"] = result_arg
        return real_generate(result_arg, settings_arg)

    monkeypatch.setattr(invoices, "generate_invoice_pdf", spy)

    prior = _email_record(
        "<confirm@x>", "62107414", amount="14227 RUB",
        internal_id="20260819-7348-457341008",
    )
    result = _invoice_result(
        company_name="ООО «Компания Броневик»",
        arrival_date="2026-08-19", departure_date="2026-08-22",
    )
    result = result.model_copy(update={"booking_number": "62107414"})
    await _run_pipeline(monkeypatch, tmp_path, result, prior=[prior])

    used = captured["result"]
    assert used.invoice.amount == "14227 RUB"
    assert used.internal_booking_id == "20260819-7348-457341008"
