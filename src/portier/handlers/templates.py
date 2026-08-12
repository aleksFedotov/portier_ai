"""Шаблоны уведомлений и inline-кнопок для Telegram."""

import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..schemas import EmailAnalysisResult

MAX_MESSAGE_LENGTH = 4096
_TRUNCATE_AT = 3800  # с запасом под служебную часть сообщения

# Человекочитаемые названия действий администратора
ACTION_LABELS = {
    "recorded_in_pms": "Отмечено в шахматке",
    "replied_to_guest": "Отвечено гостю",
    "invoice_sent": "Счет отправлен",
    "invoice_paid": "💰 Оплачен",
    # Тикет 31: карточка «документ для ручной обработки» владельцу
    "notice_ok": "✅ Понятно",
    "notice_stamp": "🖋 Печать",
}


def esc(value: object) -> str:
    """Экранировать данные из письма для HTML parse_mode."""
    if value is None:
        return "—"
    return html.escape(str(value))


def _truncate(text: str) -> str:
    if len(text) <= MAX_MESSAGE_LENGTH:
        return text
    return text[:_TRUNCATE_AT].rstrip() + "\n…(текст обрезан)"


def _btn(action: str, email_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=ACTION_LABELS[action], callback_data=f"action:{action}:{email_id}")


def _row(*actions: str, email_id: int) -> list[InlineKeyboardButton]:
    return [_btn(a, email_id) for a in actions]


def build_notification(
    result: EmailAnalysisResult,
    *,
    email_id: int,
    sender: str,
    subject: str,
    body_text: str,
    invoice_note: str | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Собрать текст уведомления и клавиатуру по типу письма."""
    r = result
    t = r.type

    if t == "booking_comment" or (t == "booking_confirmed" and r.comment_details):
        # booking_confirmed с комментарием гостя показываем как комментарий
        # (тикет 15): отдельные письма-комментарии дублируются и молчат.
        text = (
            f"🔔 <b>Комментарий к брони №{esc(r.booking_number)}</b>\n\n"
            f"👤 Гость: {esc(r.guest_name)}\n"
            f"📅 Заезд → выезд: {esc(r.arrival_date)} → {esc(r.departure_date)}\n"
            f"🌐 Канал: {esc(r.channel_name)}\n"
            f"💬 Детали: {esc(r.comment_details)}\n"
            f"⚡ Действие: {esc(r.action_required)}"
        )
        buttons = InlineKeyboardMarkup(
            inline_keyboard=[_row("recorded_in_pms", "replied_to_guest", email_id=email_id)]
        )
    elif t == "guest_message":
        text = (
            f"💬 <b>Сообщение в Extranet</b>\n\n"
            f"👤 Гость: {esc(r.guest_name)}\n"
            f"🌐 Канал: {esc(r.channel_name)}\n"
            f"📝 Текст: {esc(r.comment_details)}\n"
            f"⚡ Ответить в экстранете: {esc(r.action_required)}"
        )
        buttons = InlineKeyboardMarkup(inline_keyboard=[_row("replied_to_guest", email_id=email_id)])
    elif t == "invoice_required":
        inv = r.invoice
        text = (
            f"📄 <b>Счёт готов (PDF выше)</b>\n\n"
            f"📧 Отправитель: {esc(sender)}\n"
            f"🏢 Компания: {esc(inv.company_name if inv else None)}\n"
            f"💰 Сумма: {esc(inv.amount if inv else None)}\n"
            f"💬 Детали: {esc(r.comment_details)}"
        )
        if invoice_note:
            text += f"\n{esc(invoice_note)}"
        buttons = InlineKeyboardMarkup(
            inline_keyboard=[_row("invoice_sent", email_id=email_id)]
        )
    elif t == "booking_cancelled":
        text = (
            f"❌ <b>Отмена бронирования</b>\n\n"
            f"🔢 Бронь №{esc(r.booking_number)}\n"
            f"👤 Гость: {esc(r.guest_name)}\n"
            f"📅 Даты: {esc(r.arrival_date)} → {esc(r.departure_date)}\n"
            f"⚡ Освободить номер!"
        )
        buttons = InlineKeyboardMarkup(inline_keyboard=[_row("recorded_in_pms", email_id=email_id)])
    elif t == "booking_modified":
        text = (
            f"✏️ <b>Изменение брони</b>\n\n"
            f"🔢 Бронь №{esc(r.booking_number)}\n"
            f"👤 Гость: {esc(r.guest_name)}\n"
            f"📅 Даты: {esc(r.arrival_date)} → {esc(r.departure_date)}\n"
            f"💬 Детали: {esc(r.comment_details)}\n"
            f"⚡ Действие: {esc(r.action_required)}"
        )
        buttons = InlineKeyboardMarkup(inline_keyboard=[_row("recorded_in_pms", email_id=email_id)])
    elif t == "payment_received":
        text = (
            f"✅ <b>Оплата получена</b>\n\n"
            f"🔢 Бронь №{esc(r.booking_number)}\n"
            f"👤 Гость: {esc(r.guest_name)}\n"
            f"💬 Детали: {esc(r.comment_details)}"
        )
        buttons = None
    elif t == "payment_failed":
        text = (
            f"⚠️ <b>Ошибка оплаты</b>\n\n"
            f"🔢 Бронь №{esc(r.booking_number)}\n"
            f"💬 Причина: {esc(r.comment_details)}\n"
            f"⚡ Действие: {esc(r.action_required)}"
        )
        buttons = None  # по ошибке оплаты нечего отмечать в PMS
    elif t == "review_notification":
        text = (
            f"⭐ <b>Новый отзыв</b>\n\n"
            f"🌐 Платформа: {esc(r.channel_name)}\n"
            f"💬 Детали: {esc(r.comment_details)}"
        )
        buttons = None
    else:  # unknown — без кнопок
        text = (
            f"❓ <b>Нераспознанное письмо</b>\n\n"
            f"📧 От: {esc(sender)}\n"
            f"📌 Тема: {esc(subject)}\n\n"
            f"{esc(body_text)}"
        )
        buttons = None

    return _truncate(text), buttons
