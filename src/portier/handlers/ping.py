"""Команда /ping: живость бота. Отвечает «pong» в любом чате."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()


@router.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    await message.answer("pong")
