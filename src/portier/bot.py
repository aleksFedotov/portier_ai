"""Telegram-бот: отправка уведомлений и диспетчер aiogram."""

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup

logger = logging.getLogger(__name__)


def create_bot(token: str) -> Bot:
    """Создать бота с HTML parse_mode по умолчанию."""
    return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def create_dispatcher() -> Dispatcher:
    from .callbacks import router as callbacks_router

    dp = Dispatcher()
    dp.include_router(callbacks_router)
    return dp


async def send_notification(
    bot: Bot,
    chat_id: int,
    text: str,
    buttons: InlineKeyboardMarkup | None = None,
) -> None:
    """Отправить уведомление в группу администраторов."""
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=buttons)
