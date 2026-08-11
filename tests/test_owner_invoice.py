"""Тесты пересылки счёта от владельца в группу входящих счетов."""

from unittest.mock import AsyncMock

import pytest

import portier.handlers.owner_invoice as oi

OWNER_ID = 111
INVOICES_ID = -100777


@pytest.fixture
def _chats(monkeypatch):
    monkeypatch.setattr(oi, "_owner_chat_id", lambda: OWNER_ID)
    monkeypatch.setattr(oi, "_invoices_chat_id", lambda: INVOICES_ID)


def _message(chat_id=OWNER_ID, caption=None):
    message = AsyncMock()
    message.chat.id = chat_id
    message.caption = caption
    message.document.file_id = "file123"
    message.document.file_name = "schet.pdf"
    message.bot = AsyncMock()
    return message


async def test_forwards_document_with_caption(_chats):
    message = _message(caption="Счёт за охрану, июль")
    await oi.forward_owner_invoice(message)

    message.bot.send_document.assert_awaited_once_with(
        chat_id=INVOICES_ID,
        document="file123",
        caption="🧾 Счёт от участника\n📝 Счёт за охрану, июль",
    )
    message.answer.assert_awaited_once()


async def test_forwards_document_without_caption(_chats):
    message = _message()
    await oi.forward_owner_invoice(message)

    kwargs = message.bot.send_document.await_args.kwargs
    assert kwargs["chat_id"] == INVOICES_ID
    assert kwargs["caption"] == "🧾 Счёт от участника"


async def test_caption_is_escaped(_chats):
    message = _message(caption="счёт <b>срочно</b> & оплатить")
    await oi.forward_owner_invoice(message)

    caption = message.bot.send_document.await_args.kwargs["caption"]
    assert "<b>" not in caption
    assert "&lt;b&gt;" in caption and "&amp;" in caption


async def test_ignores_other_chats(_chats):
    message = _message(chat_id=999)
    await oi.forward_owner_invoice(message)

    message.bot.send_document.assert_not_awaited()
    message.answer.assert_not_awaited()
