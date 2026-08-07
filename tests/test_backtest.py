"""Тесты генерации HTML-отчёта бэктеста."""

from portier.backtest import BacktestRow, render_report
from portier.schemas import EmailAnalysisResult


def _result(email_type: str, **kwargs) -> EmailAnalysisResult:
    return EmailAnalysisResult(
        type=email_type, priority="normal", action_required="Проверить", **kwargs
    )


def _rows() -> list[BacktestRow]:
    return [
        BacktestRow(
            date="Mon, 1 Sep 2026 10:00:00 +0300",
            sender="noreply@ostrovok.ru",
            subject="Комментарий к бронированию 123",
            snippet="Гость просит поздний заезд",
            result=_result("booking_comment", guest_name="Иван Петров", booking_number="123"),
        ),
        BacktestRow(
            date="Tue, 2 Sep 2026 11:00:00 +0300",
            sender="guest@mail.ru",
            subject="Вопрос",
            snippet="когда заезд",
            result=_result("unknown"),
        ),
        BacktestRow(
            date="Wed, 3 Sep 2026 12:00:00 +0300",
            sender="pay@bank.ru",
            subject="Оплата",
            snippet="оплата прошла",
            result=_result("payment_received"),
        ),
        BacktestRow(
            date="Thu, 4 Sep 2026 13:00:00 +0300",
            sender="bad@example.com",
            subject="Странное письмо",
            snippet="...",
            error="RateLimitError: превышен лимит",
        ),
    ]


def test_report_structure():
    report = render_report(_rows(), days=45)
    assert "<!DOCTYPE html>" in report
    assert '<html lang="ru">' in report
    assert "Сводка по типам" in report
    assert "45" in report
    # без внешних ресурсов
    assert "<script" not in report
    assert "<link" not in report


def test_report_summary_counts():
    report = render_report(_rows(), days=45)
    assert "Всего писем: <b>4</b>" in report
    assert "Ошибок LLM: <b>1</b>" in report
    assert "Не распознано: <b>1</b>" in report
    # 1 unknown из 3 успешных = 33%
    assert "33%" in report
    assert "Комментарий к брони" in report
    assert "Оплата получена" in report


def test_report_escapes_data():
    row = BacktestRow(
        date="—", sender="a@b.c", subject="<script>alert(1)</script>",
        snippet="<b>жирный</b>", result=_result("unknown", guest_name="<img src=x>"),
    )
    report = render_report([row], days=7)
    assert "<script>alert(1)</script>" not in report
    assert "&lt;script&gt;" in report
    assert "&lt;img src=x&gt;" in report


def test_report_error_row():
    report = render_report(_rows(), days=45)
    assert "ОШИБКА" in report
    assert "RateLimitError" in report


def test_empty_report():
    report = render_report([], days=30)
    assert "Всего писем: <b>0</b>" in report
