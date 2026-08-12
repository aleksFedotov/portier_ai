"""Тесты команды /invoices (тикет 06): фильтр оплаты, период, выдача PDF."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import portier.handlers.invoices_cmd as cmd
from portier.db import init_db, init_engine
from portier.models import Base, EmailAction, ProcessedEmail

CHAT_ID = -100555


@pytest.fixture
async def _db(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    init_engine("sqlite+aiosqlite:///:memory:")
    await init_db()
    monkeypatch.setattr(cmd, "get_session_factory", lambda: factory)
    monkeypatch.setattr(cmd, "_invoice_chat_id", lambda: CHAT_ID)
    yield factory, tmp_path
    await engine.dispose()


def _record(tmp_path, tag: str, days_old: int) -> ProcessedEmail:
    pdf = tmp_path / f"invoice_{tag}.pdf"
    pdf.write_bytes(b"%PDF fake")
    return ProcessedEmail(
        message_id=f"<m{tag}@x>", uid=days_old, sender="a@b.c", subject="Счёт",
        raw_payload="", status="SUCCESS", email_type="invoice_required",
        processed_at=datetime.utcnow() - timedelta(days=days_old),
        invoice_pdf=str(pdf),
        llm_result={"invoice": {"company_name": "ООО «Ромашка»", "amount": "15000"}},
    )


def _message(chat_id=CHAT_ID):
    message = AsyncMock()
    message.chat.id = chat_id
    return message


def _callback(data: str, chat_id=CHAT_ID):
    callback = AsyncMock()
    callback.data = data
    callback.message.chat.id = chat_id
    callback.bot = AsyncMock()
    return callback


# ---------- разбор callback_data ----------

def test_parse_period():
    assert cmd.parse_period("inv:all:1") == ("all", 1)
    assert cmd.parse_period("inv:due:30") == ("due", 30)
    assert cmd.parse_period("inv:all:abc") is None
    assert cmd.parse_period("inv:xxx:7") is None
    assert cmd.parse_period("inv:7") is None
    assert cmd.parse_period(None) is None


# ---------- шаг 1: фильтр ----------

async def test_cmd_invoices_shows_scope_buttons(_db):
    message = _message()
    await cmd.cmd_invoices(message)
    message.answer.assert_awaited_once()
    markup = message.answer.await_args.kwargs["reply_markup"]
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert callbacks == ["inv-scope:all", "inv-scope:due"]


async def test_cmd_invoices_ignores_other_chat(_db):
    message = _message(chat_id=-999)
    await cmd.cmd_invoices(message)
    message.answer.assert_not_awaited()


async def test_cmd_invoices_silent_without_chat_id(_db, monkeypatch):
    monkeypatch.setattr(cmd, "_invoice_chat_id", lambda: None)
    message = _message()
    await cmd.cmd_invoices(message)
    message.answer.assert_not_awaited()


# ---------- шаг 2: период ----------

async def test_scope_shows_period_buttons(_db):
    callback = _callback("inv-scope:due")
    await cmd.handle_scope(callback)
    markup = callback.message.answer.await_args.kwargs["reply_markup"]
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert callbacks == ["inv:due:1", "inv:due:7", "inv:due:30"]


# ---------- выдача PDF ----------

async def test_period_sends_pdfs_in_range(_db):
    factory, tmp_path = _db
    async with factory() as session:
        session.add(_record(tmp_path, "a", 0))   # сегодня — входит в «7 дней»
        session.add(_record(tmp_path, "b", 3))   # входит
        session.add(_record(tmp_path, "c", 10))  # старше 7 дней — нет
        await session.commit()

    callback = _callback("inv:all:7")
    await cmd.handle_period(callback)

    assert callback.bot.send_document.await_count == 2
    sent_files = sorted(
        c.kwargs["document"].filename
        for c in callback.bot.send_document.await_args_list
    )
    assert sent_files == ["invoice_a.pdf", "invoice_b.pdf"]
    caption = callback.bot.send_document.await_args_list[0].kwargs["caption"]
    assert "ООО «Ромашка»" in caption and "15000" in caption
    assert "⏳ Не оплачен" in caption


async def test_due_scope_skips_paid(_db):
    """«Только неоплаченные»: оплаченный счёт не выдаётся, в «all» — с ✅."""
    factory, tmp_path = _db
    async with factory() as session:
        paid = _record(tmp_path, "paid", 0)
        unpaid = _record(tmp_path, "unpaid", 0)
        session.add_all([paid, unpaid])
        await session.commit()
        session.add(EmailAction(email_id=paid.id, action="invoice_paid", admin_name="x"))
        await session.commit()

    callback = _callback("inv:due:7")
    await cmd.handle_period(callback)
    assert callback.bot.send_document.await_count == 1
    doc = callback.bot.send_document.await_args.kwargs["document"]
    assert doc.filename == "invoice_unpaid.pdf"

    callback = _callback("inv:all:7")
    await cmd.handle_period(callback)
    assert callback.bot.send_document.await_count == 2
    captions = {
        c.kwargs["document"].filename: c.kwargs["caption"]
        for c in callback.bot.send_document.await_args_list
    }
    assert "✅ Оплачен" in captions["invoice_paid.pdf"]
    assert "⏳ Не оплачен" in captions["invoice_unpaid.pdf"]


async def test_period_empty_answers_text(_db):
    callback = _callback("inv:all:1")
    await cmd.handle_period(callback)
    callback.bot.send_document.assert_not_awaited()
    assert "Счетов за период нет" in callback.message.answer.await_args.args[0]


async def test_due_empty_answers_text(_db):
    callback = _callback("inv:due:1")
    await cmd.handle_period(callback)
    assert "Неоплаченных счетов за период нет" in callback.message.answer.await_args.args[0]


async def test_period_ignores_other_chat(_db):
    factory, tmp_path = _db
    async with factory() as session:
        session.add(_record(tmp_path, "a", 0))
        await session.commit()

    callback = _callback("inv:all:30", chat_id=-999)
    await cmd.handle_period(callback)

    callback.bot.send_document.assert_not_awaited()


async def test_owner_invoices_included(_db):
    """Счета, присланные владельцем (owner_invoice), выдаются наравне с почтовыми."""
    factory, tmp_path = _db
    owner_rec = _record(tmp_path, "owner", 0)
    owner_rec.email_type = "owner_invoice"
    async with factory() as session:
        session.add(owner_rec)
        await session.commit()

    callback = _callback("inv:due:7")
    await cmd.handle_period(callback)

    assert callback.bot.send_document.await_count == 1
    doc = callback.bot.send_document.await_args.kwargs["document"]
    assert doc.filename == "invoice_owner.pdf"
