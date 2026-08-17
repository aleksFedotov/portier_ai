"""Тест команды /ping: бот отвечает «pong»."""

from unittest.mock import AsyncMock

import portier.handlers.ping as ping


async def test_ping_answers_pong():
    message = AsyncMock()
    await ping.cmd_ping(message)
    message.answer.assert_awaited_once_with("pong")
