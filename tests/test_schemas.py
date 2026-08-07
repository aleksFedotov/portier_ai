"""Тесты схемы результата LLM."""

import pytest
from pydantic import ValidationError

from portier.schemas import EmailAnalysisResult

ALL_TYPES = [
    "booking_comment",
    "guest_message",
    "invoice_required",
    "booking_modified",
    "booking_cancelled",
    "payment_received",
    "payment_failed",
    "review_notification",
    "unknown",
]


@pytest.mark.parametrize("email_type", ALL_TYPES)
def test_all_types_accepted(email_type):
    result = EmailAnalysisResult(type=email_type, priority="normal", action_required="Проверить")
    assert result.type == email_type
    assert result.priority == "normal"
    assert result.guest_name is None


@pytest.mark.parametrize("priority", ["low", "normal", "high", "urgent"])
def test_priorities(priority):
    result = EmailAnalysisResult(type="unknown", priority=priority, action_required="—")
    assert result.priority == priority


def test_action_required_is_mandatory():
    with pytest.raises(ValidationError):
        EmailAnalysisResult(type="unknown", priority="low")


def test_invalid_type_rejected():
    with pytest.raises(ValidationError):
        EmailAnalysisResult(type="spam", priority="low", action_required="—")


def test_full_payload():
    result = EmailAnalysisResult(
        type="booking_comment",
        priority="high",
        guest_name="Иван Петров",
        arrival_date="2026-09-01",
        departure_date="2026-09-05",
        booking_number="12345",
        channel_name="Островок",
        comment_details="Просит поздний заезд",
        action_required="Записать в PMS",
    )
    assert result.booking_number == "12345"
