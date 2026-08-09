"""Тесты шаблонов уведомлений."""

import pytest

from portier.handlers.templates import ACTION_LABELS, build_notification
from portier.schemas import EmailAnalysisResult


def _result(email_type: str, **kwargs) -> EmailAnalysisResult:
    return EmailAnalysisResult(
        type=email_type, priority="normal", action_required="Проверить", **kwargs
    )


def _render(result: EmailAnalysisResult, body_text: str = "Текст письма"):
    return build_notification(
        result, email_id=1, sender="guest@example.com", subject="Тема", body_text=body_text
    )


def _actions(markup) -> list[str]:
    if markup is None:
        return []
    return [
        btn.callback_data.split(":")[1]
        for row in markup.inline_keyboard
        for btn in row
    ]


@pytest.mark.parametrize("email_type", [
    "booking_comment", "guest_message", "invoice_required", "booking_modified",
    "booking_cancelled", "payment_received", "payment_failed",
    "review_notification", "unknown",
])
def test_every_type_renders(email_type):
    text, _ = _render(_result(email_type))
    assert text
    assert len(text) <= 4096


def test_booking_comment_buttons():
    _, markup = _render(_result("booking_comment", booking_number="77"))
    assert _actions(markup) == ["recorded_in_pms", "replied_to_guest"]


def test_guest_message_button():
    _, markup = _render(_result("guest_message"))
    assert _actions(markup) == ["replied_to_guest"]


def test_invoice_button():
    _, markup = _render(_result("invoice_required"))
    assert _actions(markup) == ["invoice_sent", "invoice_paid"]


def test_no_button_types():
    for t in ("payment_received", "review_notification", "unknown"):
        _, markup = _render(_result(t))
        assert markup is None, t


def test_unknown_without_buttons():
    text, markup = _render(_result("unknown"), body_text="Какой-то текст")
    assert markup is None
    assert "guest@example.com" in text
    assert "Какой-то текст" in text


def test_html_escaping():
    result = _result("booking_comment", guest_name="<script>alert(1)</script>")
    text, _ = _render(result)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_long_text_truncated():
    text, _ = _render(_result("unknown"), body_text="x" * 10000)
    assert len(text) <= 4096
    assert "обрезан" in text


def test_action_labels_complete():
    assert set(ACTION_LABELS) == {
        "recorded_in_pms", "replied_to_guest", "invoice_sent", "invoice_paid",
    }
