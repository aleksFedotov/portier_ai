"""Тесты генерации HTML-отчёта бэктеста."""

from types import SimpleNamespace

from portier.backtest import BacktestRow, apply_prefilters, load_cache, render_report, save_cache
from portier.schemas import EmailAnalysisResult

_SETTINGS = SimpleNamespace(
    MUTED_SENDERS=["spam@example.com", "noreply@travellinemail.com|карта лояльности"],
    OWNER_NOTICE_SENDERS=["director@agency.ru"],
    OWNER_NOTICE_RULES=["101hotels@example.com|сверка"],
    INVOICE_OWNER_EXCEPTIONS=["kuper@example.com"],
)


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
    assert "45" in report
    # без внешних ресурсов (JS только инлайн)
    assert "<script src" not in report
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


def test_prefilters_yandex_registry():
    pseudo = apply_prefilters(
        _SETTINGS,
        '"Яндекс Путешествия" <hotels@travel.yandex.ru>',
        "Реестр бронирований по платежному поручению 87653 от 24.06.2026",
        ["reestr.xlsx"],
    )
    assert pseudo == "yandex_registry"


def test_prefilters_muted():
    assert apply_prefilters(_SETTINGS, "Spam <spam@example.com>", "Любая тема", []) == "muted"
    # правило с шаблоном темы: глушим только совпадение
    assert apply_prefilters(
        _SETTINGS, "noreply@travellinemail.com", "Выдана карта лояльности", []
    ) == "muted"
    assert apply_prefilters(
        _SETTINGS, "noreply@travellinemail.com", "Подтверждение бронирования", []
    ) is None


def test_prefilters_owner_notice():
    assert apply_prefilters(_SETTINGS, "director@agency.ru", "Документы", []) == "owner_notice"
    assert apply_prefilters(
        _SETTINGS, "101hotels@example.com", "Сверка за июль 2026", []
    ) == "owner_notice"


def test_prefilters_incoming_invoice():
    assert apply_prefilters(
        _SETTINGS, "someone@firma.ru", "Счёт", ["Счет-03-777.pdf"]
    ) == "incoming_invoice"
    # исключение (Купер) — отдельный псевдо-тип
    assert apply_prefilters(
        _SETTINGS, "Kuper <kuper@example.com>", "Счёт", ["invoice_01.pdf"]
    ) == "kuper_invoice"
    # вложение без «счёта» в имени — не перехват
    assert apply_prefilters(_SETTINGS, "someone@firma.ru", "Фото", ["img.pdf"]) is None


def test_prefilters_priority_order():
    # входящий счёт проверяется раньше чёрного списка
    assert apply_prefilters(
        _SETTINGS, "spam@example.com", "Счёт", ["Счет на оплату.xlsx"]
    ) == "incoming_invoice"


def test_report_filtered_rows():
    rows = [
        BacktestRow(
            date="Thu, 25 Jun 2026 18:52:05 +0300",
            sender="hotels@travel.yandex.ru",
            subject="Реестр бронирований по платежному поручению 87653",
            snippet="",
            filtered="yandex_registry",
        ),
        BacktestRow(
            date="Fri, 26 Jun 2026 10:00:00 +0300",
            sender="guest@mail.ru",
            subject="Вопрос",
            snippet="когда заезд",
            result=_result("unknown"),
        ),
    ]
    report = render_report(rows, days=45)
    assert "Реестр Яндекса" in report
    assert "Перехвачено фильтрами (без LLM): <b>1</b>" in report
    # процент нераспознанных считается только от писем, дошедших до LLM: 1 из 1
    assert "Не распознано: <b>1</b> (100%)" in report


def test_report_interactive_markup():
    rows = _rows()
    rows[0].gmail_id = "abc123"
    report = render_report(rows, days=45)
    # строка с data-атрибутами для фильтров и разметки
    assert 'data-gid="abc123"' in report
    assert 'data-type="booking_comment"' in report
    assert "data-search=" in report
    # панель фильтров, разметка и выгрузка
    assert 'id="toolbar"' in report
    assert 'id="btn-unknown"' in report
    assert 'id="search"' in report
    assert '<select class="expect">' in report
    assert 'id="btn-export"' in report
    assert "localStorage" in report


def test_cache_roundtrip(tmp_path):
    rows = _rows()
    rows[0].gmail_id = "gid-1"
    rows.append(BacktestRow(
        date="Fri, 5 Sep 2026 10:00:00 +0300",
        sender="hotels@travel.yandex.ru",
        subject="Реестр бронирований",
        snippet="",
        gmail_id="gid-2",
        filtered="yandex_registry",
    ))
    cache = tmp_path / "cache.json"
    save_cache(rows, str(cache))
    loaded = load_cache(str(cache))
    assert len(loaded) == len(rows)
    assert loaded[0].gmail_id == "gid-1"
    assert loaded[0].result.type == "booking_comment"
    assert loaded[0].result.guest_name == "Иван Петров"
    assert loaded[1].result is not None and loaded[1].error is None
    assert loaded[3].error is not None
    assert loaded[4].filtered == "yandex_registry"
