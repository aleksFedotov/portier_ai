"""Telegram-бот: отправка уведомлений и диспетчер aiogram."""

import asyncio
import logging

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.types import InlineKeyboardMarkup

logger = logging.getLogger(__name__)

# Сетевые сбои, при которых отправку имеет смысл повторить (тикет 16:
# замедление Telegram в РФ). Логические ошибки (TelegramBadRequest и пр.)
# не ретраим — повтор их не исправит.
RETRYABLE_ERRORS = (
    TelegramNetworkError,
    TelegramServerError,
    aiohttp.ClientError,
    asyncio.TimeoutError,
)


def create_bot(
    token: str,
    proxy: str | None = None,
    timeout: float = 60.0,
    retry_attempts: int = 4,
    retry_base_delay: float = 2.0,
) -> Bot:
    """Создать бота с HTML parse_mode, таймаутами и опциональным прокси.

    Параметры retry кладём на инстанс бота, чтобы не таскать settings
    по всем местам отправки (тикет 16).
    """
    session = AiohttpSession(proxy=proxy, timeout=timeout)
    bot = Bot(
        token=token, session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    bot.retry_attempts = max(int(retry_attempts), 1)
    bot.retry_base_delay = float(retry_base_delay)
    return bot


def create_dispatcher() -> Dispatcher:
    from .callbacks import router as callbacks_router

    dp = Dispatcher()
    dp.include_router(callbacks_router)
    return dp


async def _send_with_retry(bot: Bot, what: str, send) -> None:
    """Отправить с retry по экспоненциальному backoff при сетевых сбоях.

    send — фабрика корутины (одна корутина на попытку). После исчерпания
    попыток исключение уходит наружу: письмо останется PENDING и будет
    обработано повторно конвейером.
    """
    # У настоящего Bot параметры кладёт create_bot; в тестах bot — AsyncMock,
    # там retry отключён (attempts=1).
    attempts = getattr(bot, "retry_attempts", 1)
    if not isinstance(attempts, int):
        attempts = 1
    base_delay = getattr(bot, "retry_base_delay", 2.0)
    if not isinstance(base_delay, (int, float)):
        base_delay = 2.0

    for attempt in range(1, attempts + 1):
        try:
            await send()
            return
        except RETRYABLE_ERRORS as exc:
            if attempt == attempts:
                raise
            delay = base_delay * 2 ** (attempt - 1)
            logger.warning(
                "%s: сетевая ошибка (%s), попытка %d/%d, повтор через %.0f с",
                what, exc, attempt, attempts, delay,
            )
            await asyncio.sleep(delay)


async def send_notification(
    bot: Bot,
    chat_id: int,
    text: str,
    buttons: InlineKeyboardMarkup | None = None,
) -> None:
    """Отправить уведомление в группу администраторов."""
    await _send_with_retry(
        bot, "send_notification",
        lambda: bot.send_message(chat_id=chat_id, text=text, reply_markup=buttons),
    )


async def send_document(
    bot: Bot,
    chat_id: int,
    filename: str,
    data: bytes,
    caption: str | None = None,
) -> None:
    """Отправить файл как документ (тикет 10: входящие счета)."""
    from aiogram.types import BufferedInputFile

    await _send_with_retry(
        bot, f"send_document({filename})",
        lambda: bot.send_document(
            chat_id=chat_id,
            document=BufferedInputFile(data, filename=filename),
            caption=caption,
        ),
    )


async def polling_with_restart(dp: Dispatcher, bot: Bot) -> None:
    """Polling с перезапуском при сетевых сбоях (тикет 16).

    Обрыв соединения с api.telegram.org не должен ронять процесс:
    ждём и запускаем polling заново.
    """
    base_delay = getattr(bot, "retry_base_delay", 2.0)
    if not isinstance(base_delay, (int, float)):
        base_delay = 2.0
    delay = base_delay
    while True:
        try:
            await dp.start_polling(bot)
            return  # штатная остановка (сигнал завершения)
        except RETRYABLE_ERRORS as exc:
            logger.warning(
                "polling: сетевая ошибка (%s), перезапуск через %.0f с", exc, delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)  # потолок 5 минут
