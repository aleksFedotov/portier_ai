"""Обработка нажатий inline-кнопок администраторами."""

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from .db import get_session_factory
from .handlers.templates import ACTION_LABELS, esc
from .models import EmailAction, ProcessedEmail

logger = logging.getLogger(__name__)

router = Router()


def parse_callback_data(data: str) -> tuple[str, int] | None:
    """Разобрать callback_data вида action:<действие>:<email_id>."""
    parts = data.split(":")
    if len(parts) == 3 and parts[0] == "action" and parts[1] in ACTION_LABELS:
        try:
            return parts[1], int(parts[2])
        except ValueError:
            return None
    return None


def remove_button(markup: InlineKeyboardMarkup | None, action: str) -> InlineKeyboardMarkup | None:
    """Убрать нажатую кнопку, остальные оставить."""
    if markup is None:
        return None
    keyboard = [
        [btn for btn in row if not btn.callback_data.startswith(f"action:{action}:")]
        for row in markup.inline_keyboard
    ]
    keyboard = [row for row in keyboard if row]
    if not keyboard:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=b.text, callback_data=b.callback_data) for b in row] for row in keyboard]
    )


@router.callback_query(F.data.startswith("action:"))
async def handle_action(callback: CallbackQuery) -> None:
    parsed = parse_callback_data(callback.data or "")
    if parsed is None:
        await callback.answer("Неизвестное действие")
        return
    action, email_id = parsed
    user = callback.from_user
    admin_name = (
        f"{user.full_name} (@{user.username})" if user and user.username
        else (user.full_name if user else "админ")
    )

    session_factory = get_session_factory()
    async with session_factory() as session:
        email = await session.get(ProcessedEmail, email_id)
        if email is None:
            await callback.answer("Письмо не найдено в базе")
            return
        existing = await session.execute(
            select(EmailAction).where(
                EmailAction.email_id == email_id, EmailAction.action == action
            )
        )
        if existing.scalar_one_or_none() is not None:
            await callback.answer("Уже отмечено")
            return
        session.add(EmailAction(email_id=email_id, action=action, admin_name=admin_name))
        await session.commit()

    label = ACTION_LABELS[action]
    new_text = (callback.message.text or callback.message.html_text or "") + (
        f"\n\n✅ Обработано админом {esc(admin_name)} ({esc(label)})"
    )
    new_markup = remove_button(callback.message.reply_markup, action)
    await callback.message.edit_text(new_text, reply_markup=new_markup)
    await callback.answer("Отмечено")
    logger.info("Действие %s по письму %s отметил %s", action, email_id, admin_name)
