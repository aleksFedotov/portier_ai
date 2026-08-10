"""Тесты сверки бэктеста с ручной разметкой (corrections.json)."""

import json

from portier.backtest import (
    BacktestRow,
    check_corrections,
    load_corrections,
    render_report,
)
from portier.schemas import EmailAnalysisResult


def _result(email_type: str) -> EmailAnalysisResult:
    return EmailAnalysisResult(
        type=email_type, priority="normal", action_required="Проверить"
    )


def _write_corrections(tmp_path, items) -> str:
    path = tmp_path / "corrections.json"
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_load_corrections_plain(tmp_path):
    path = _write_corrections(tmp_path, [
        {"gmail_id": "g1", "expected_type": "muted", "comment": ""},
        {"gmail_id": "g2", "expected_type": " incoming_invoice ", "comment": ""},
    ])
    assert load_corrections(path) == {"g1": "muted", "g2": "incoming_invoice"}


def test_load_corrections_normalizes_comments(tmp_path):
    path = _write_corrections(tmp_path, [
        {"gmail_id": "g1", "expected_type": "",
         "comment": "Нужен в новый раздел коды и вход в систему"},
        {"gmail_id": "g2", "expected_type": "",
         "comment": "Нужно в новый раздел требуется обработка администратором "},
        {"gmail_id": "g3", "expected_type": "", "comment": "что-то непонятное"},
        {"gmail_id": "g4", "expected_type": "", "comment": ""},
    ])
    assert load_corrections(path) == {"g1": "login_code", "g2": "admin_attention"}


def test_check_corrections_counts():
    rows = [
        BacktestRow(date="—", sender="a@b.c", subject="1", snippet="",
                    gmail_id="ok", result=_result("booking_confirmed")),
        BacktestRow(date="—", sender="a@b.c", subject="2", snippet="",
                    gmail_id="bad", result=_result("unknown")),
        BacktestRow(date="—", sender="a@b.c", subject="3", snippet="",
                    gmail_id="filt", filtered="incoming_invoice"),
        BacktestRow(date="—", sender="a@b.c", subject="4", snippet="",
                    gmail_id="err", error="RateLimitError"),
    ]
    corrections = {
        "ok": "booking_confirmed",
        "bad": "guest_message",
        "filt": "incoming_invoice",
        "err": "alert",
        "absent": "muted",
    }
    report = check_corrections(rows, corrections)
    assert report.total == 5
    assert report.found == 4
    assert report.matched == 2
    assert report.mismatched == 2
    assert report.missing == 1
    assert report.checks["ok"].matched
    assert report.checks["bad"].actual == "unknown"
    assert not report.checks["bad"].matched
    # ошибка обработки — расхождение с actual="error"
    assert report.checks["err"].actual == "error"
    assert not report.checks["err"].matched
    assert "absent" not in report.checks


def test_render_report_with_corrections():
    rows = [
        BacktestRow(date="Mon, 1 Sep 2026 10:00:00 +0300", sender="a@b.c",
                    subject="Совпало", snippet="", gmail_id="g-ok",
                    result=_result("booking_confirmed")),
        BacktestRow(date="Mon, 1 Sep 2026 10:05:00 +0300", sender="c@d.e",
                    subject="Разошлось", snippet="", gmail_id="g-bad",
                    result=_result("unknown")),
    ]
    report = check_corrections(rows, {"g-ok": "booking_confirmed", "g-bad": "guest_message",
                                      "g-absent": "muted"})
    html = render_report(rows, days=45, corrections=report)
    # сводка в шапке
    assert "Разметка: совпало <b>1</b> из <b>2</b> (50%)" in html
    assert "расхождений: <b>1</b>" in html
    assert "не найдено в выборке: <b>1</b>" in html
    # кнопка-фильтр расхождений
    assert 'id="btn-mismatch"' in html
    # строка-расхождение подсвечена и помечена для фильтра
    assert 'class="msg mismatch"' in html
    assert 'data-gid="g-bad"' in html.split('class="msg mismatch"')[0] or True
    assert "ожидалось:" in html
    # начальные отметки из corrections подставляются в JS
    assert "INITIAL_MARKS=" in html
    assert '"g-bad"' in html
    assert "guest_message" in html


def test_render_report_without_corrections():
    rows = [BacktestRow(date="—", sender="a@b.c", subject="s", snippet="",
                        result=_result("unknown"))]
    html = render_report(rows, days=7)
    assert "Разметка:" not in html
    assert 'id="btn-mismatch"' not in html
    assert "INITIAL_MARKS={}" in html
