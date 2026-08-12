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


# GmailClient для колбэков создаётся лениво (та же авторизация token.json,
# что и у почтового цикла) — тикет 31: кнопка «🖋 Печать».
_gmail = None


def _get_gmail():
    global _gmail
    if _gmail is None:
        from .config import get_settings
        from .gmail_client import GmailClient

        _gmail = GmailClient(get_settings())
    return _gmail


async def stamp_and_draft(email: ProcessedEmail) -> str:
    """«🖋 Печать» (тикет 31): подписать PDF-вложения, черновик-ответ в Gmail.

    Возвращает текст результата для карточки. Исключения уходят наружу —
    вызывающий код решает, записывать ли действие (при сбое не записываем,
    чтобы повторное нажатие сработало).
    """
    from .config import get_settings
    from .drafts import build_reply_mime, parse_sender_email
    from .stamp import stamp_pdf

    if not email.gmail_id:
        raise ValueError("у письма нет gmail_id (старое письмо до тикета 31)")

    gmail = _get_gmail()
    settings = get_settings()
    attachments = await gmail.fetch_attachments(email.gmail_id, ".pdf")
    if not attachments:
        raise ValueError("в письме нет PDF-вложений")

    stamped = [(name, stamp_pdf(data, settings)) for name, data in attachments]
    body = (
        "Добрый день!\n\nПодписанные документы во вложении.\n\n"
        "С уважением, администрация отеля"
    )
    raw = build_reply_mime(
        to=parse_sender_email(email.sender),
        subject=email.subject,
        in_reply_to=email.message_id,
        body_text=body,
        attachments=stamped,
    )
    thread_id = await gmail.fetch_thread_id(email.gmail_id)
    await gmail.create_draft(raw, thread_id=thread_id)
    return (
        f"🖋 Подписано ({len(stamped)} шт.), черновик-ответ сохранён в Gmail — "
        "проверьте и отправьте вручную"
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

        # Тикет 31: «🖋 Печать» — сначала внешняя работа (подпись + черновик),
        # действие записываем только при успехе: при сбое кнопка остаётся живой.
        stamp_note = ""
        if action == "notice_stamp":
            await callback.answer("Подписываю…")
            try:
                stamp_note = await stamp_and_draft(email)
            except Exception as exc:
                logger.exception("«🖋 Печать» по письму %s не удалась", email_id)
                await callback.answer(f"⚠️ {exc}", show_alert=True)
                return
        session.add(EmailAction(email_id=email_id, action=action, admin_name=admin_name))
        await session.commit()

    label = ACTION_LABELS[action]
    new_text = (callback.message.text or callback.message.html_text or "") + (
        f"\n\n✅ Обработано админом {esc(admin_name)} ({esc(label)})"
    )
    if stamp_note:
        new_text += f"\n{esc(stamp_note)}"
    # «Понятно» и «🖋 Печать» взаимоисключающие: снимаем обе (тикет 31)
    new_markup = remove_button(callback.message.reply_markup, action)
    if action in ("notice_ok", "notice_stamp"):
        other = "notice_stamp" if action == "notice_ok" else "notice_ok"
        new_markup = remove_button(new_markup, other)
    await callback.message.edit_text(new_text, reply_markup=new_markup)
    await callback.answer("Отмечено")
    logger.info("Действие %s по письму %s отметил %s", action, email_id, admin_name)
