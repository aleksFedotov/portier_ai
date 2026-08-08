"""Тикет 16: retry с backoff на отправку, перезапуск polling, прокси из конфига."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from portier.bot import create_bot, polling_with_restart, send_document, send_notification


def test_create_bot_proxy_and_retry_params():
    bot = create_bot(
        "123:abc", proxy="http://user:pass@proxy:8080",
        retry_attempts=5, retry_base_delay=3.0,
    )
    assert bot.session.proxy == "http://user:pass@proxy:8080"
    assert bot.retry_attempts == 5
    assert bot.retry_base_delay == 3.0


def test_create_bot_without_proxy():
    bot = create_bot("123:abc")
    assert bot.session.proxy is None
    assert bot.retry_attempts >= 1


async def test_send_notification_retries_network_error():
    """Временный сетевой сбой → повтор, со 2-й попытки доставлено."""
    bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=[aiohttp.ClientConnectionError("boom"), None]),
        retry_attempts=3, retry_base_delay=0,
    )
    await send_notification(bot, 1, "привет")
    assert bot.send_message.await_count == 2


async def test_send_notification_gives_up_after_attempts():
    """Постоянный сбой → после исчерпания попыток исключение наружу."""
    bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=aiohttp.ClientConnectionError("boom")),
        retry_attempts=3, retry_base_delay=0,
    )
    with pytest.raises(aiohttp.ClientConnectionError):
        await send_notification(bot, 1, "привет")
    assert bot.send_message.await_count == 3


async def test_no_retry_on_logic_error():
    """Не-сетевую ошибку ретраить бессмысленно — падаем сразу."""
    bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=RuntimeError("bad request")),
        retry_attempts=3, retry_base_delay=0,
    )
    with pytest.raises(RuntimeError):
        await send_notification(bot, 1, "привет")
    assert bot.send_message.await_count == 1


async def test_send_document_retries():
    bot = SimpleNamespace(
        send_document=AsyncMock(side_effect=[TimeoutError(), None]),
        retry_attempts=2, retry_base_delay=0,
    )
    await send_document(bot, 1, "invoice.pdf", b"%PDF", caption="c")
    assert bot.send_document.await_count == 2


async def test_polling_restarts_on_network_error():
    """Обрыв polling → перезапуск, а не падение процесса."""
    dp = SimpleNamespace(
        start_polling=AsyncMock(side_effect=[aiohttp.ClientConnectionError("boom"), None])
    )
    bot = SimpleNamespace(retry_base_delay=0)
    await polling_with_restart(dp, bot)
    assert dp.start_polling.await_count == 2
