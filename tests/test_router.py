"""Тесты диспетчера уведомлений (мок бота)."""

from unittest.mock import AsyncMock

import pytest

from portier.handlers.router import route_notification
from portier.schemas import EmailAnalysisResult

EXPECTED_BUTTONS = {
    "booking_comment": ["recorded_in_pms", "replied_to_guest"],
    "guest_message": ["replied_to_guest"],
    "invoice_required": ["invoice_sent"],
    "booking_modified": ["recorded_in_pms"],
    "booking_cancelled": ["recorded_in_pms"],
    "payment_failed": [],
    "payment_received": [],
    "review_notification": [],
    "unknown": [],
}


@pytest.mark.parametrize("email_type,expected", EXPECTED_BUTTONS.items())
async def test_router_sends_correct_buttons(email_type, expected):
    bot = AsyncMock()
    result = EmailAnalysisResult(
        type=email_type, priority="normal", action_required="Проверить"
    )
    await route_notification(
        bot, -100123, result,
        email_id=42, sender="guest@example.com", subject="Тема", body_text="Текст",
    )

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == -100123
    assert kwargs["text"]

    markup = kwargs["reply_markup"]
    actions = [] if markup is None else [
        btn.callback_data.split(":")[1]
        for row in markup.inline_keyboard
        for btn in row
    ]
    assert actions == expected

    if markup is not None:
        # callback_data вида action:<действие>:<email_id>
        first = markup.inline_keyboard[0][0].callback_data
        assert first.endswith(":42")
