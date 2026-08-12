"""Тесты пересылки счёта от владельца в группу входящих счетов."""

from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import portier.handlers.owner_invoice as oi
from portier.models import Base, ProcessedEmail

OWNER_ID = 111
INVOICES_ID = -100777


@pytest.fixture
async def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(oi, "_owner_chat_id", lambda: OWNER_ID)
    monkeypatch.setattr(oi, "_invoices_chat_id", lambda: INVOICES_ID)
    monkeypatch.setattr(
        "portier.config.get_settings",
        lambda: type("S", (), {"INVOICES_DIR": str(tmp_path)})(),
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("portier.db.get_session_factory", lambda: factory)
    yield factory, tmp_path
    await engine.dispose()


def _message(chat_id=OWNER_ID, caption=None):
    message = AsyncMock()
    message.chat.id = chat_id
    message.message_id = 42
    message.caption = caption
    message.document.file_id = "file123"
    message.document.file_name = "schet.pdf"
    message.document.file_unique_id = "uniq1"
    message.bot = AsyncMock()
    message.bot.download.return_value = BytesIO(b"%PDF owner")
    return message


async def _saved_record(factory):
    async with factory() as session:
        return (await session.execute(select(ProcessedEmail))).scalar_one()


async def test_forwards_document_with_caption(_env):
    message = _message(caption="Счёт за охрану, июль")
    await oi.forward_owner_invoice(message)

    kwargs = message.bot.send_document.await_args.kwargs
    assert kwargs["chat_id"] == INVOICES_ID
    assert kwargs["document"] == "file123"
    assert kwargs["caption"] == "🧾 Счёт от участника\n📝 Счёт за охрану, июль"
    message.answer.assert_awaited_once()


async def test_registered_as_unpaid_with_paid_button(_env):
    """Счёт пишется в реестр как неоплаченный, на документе — кнопка «Оплачен»."""
    factory, tmp_path = _env
    message = _message(caption="счет метро 08.07.2026")
    await oi.forward_owner_invoice(message)

    record = await _saved_record(factory)
    assert record.email_type == "owner_invoice"
    assert record.subject == "счет метро 08.07.2026"
    assert record.invoice_pdf.endswith("owner_uniq1_schet.pdf")
    assert record.llm_result["invoice"]["company_name"] == "счет метро 08.07.2026"
    # PDF сохранён на диск — /invoices выдаст его файлом
    assert (tmp_path / "owner_uniq1_schet.pdf").read_bytes() == b"%PDF owner"

    markup = message.bot.send_document.await_args.kwargs["reply_markup"]
    (button,) = [btn for row in markup.inline_keyboard for btn in row]
    assert button.callback_data == f"action:invoice_paid:{record.id}"


async def test_forwards_document_without_caption(_env):
    message = _message()
    await oi.forward_owner_invoice(message)

    kwargs = message.bot.send_document.await_args.kwargs
    assert kwargs["chat_id"] == INVOICES_ID
    assert kwargs["caption"] == "🧾 Счёт от участника"


async def test_caption_is_escaped(_env):
    message = _message(caption="счёт <b>срочно</b> & оплатить")
    await oi.forward_owner_invoice(message)

    caption = message.bot.send_document.await_args.kwargs["caption"]
    assert "<b>" not in caption
    assert "&lt;b&gt;" in caption and "&amp;" in caption


async def test_ignores_other_chats(_env):
    message = _message(chat_id=999)
    await oi.forward_owner_invoice(message)

    message.bot.send_document.assert_not_awaited()
    message.bot.download.assert_not_awaited()
    message.answer.assert_not_awaited()
