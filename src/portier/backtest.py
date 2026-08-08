"""Бэктест: прогон конвейера по старой почте без Telegram и прод-БД.

Использование: python -m portier.backtest --days 45 --output report.html
"""

import argparse
import asyncio
import html
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import get_settings
from .gmail_client import GmailClient, analyze_body, set_llm_client
from .llm import create_llm_client
from .schemas import EmailAnalysisResult

logger = logging.getLogger(__name__)

# Подсветка строк таблицы по типу письма
_TYPE_COLORS = {
    "booking_comment": "#fff3cd",
    "guest_message": "#d1ecf1",
    "invoice_required": "#e2d9f3",
    "booking_modified": "#ffe8cc",
    "booking_cancelled": "#f8d7da",
    "payment_received": "#d4edda",
    "payment_failed": "#f5c6cb",
    "review_notification": "#e2e3e5",
    "booking_confirmed": "#eaf4fb",
    "unknown": "#f0f0f0",
}

_TYPE_LABELS = {
    "booking_comment": "Комментарий к брони",
    "guest_message": "Сообщение гостя",
    "invoice_required": "Запрос счета",
    "booking_modified": "Изменение брони",
    "booking_cancelled": "Отмена брони",
    "payment_received": "Оплата получена",
    "payment_failed": "Ошибка оплаты",
    "review_notification": "Отзыв",
    "booking_confirmed": "Новая бронь (молча)",
    "unknown": "Не распознано",
}

_SNIPPET_LEN = 300


@dataclass
class BacktestRow:
    """Результат обработки одного письма в бэктесте."""

    date: str
    sender: str
    subject: str
    snippet: str
    result: Optional[EmailAnalysisResult] = None
    error: Optional[str] = None


def esc(value: object) -> str:
    return html.escape("—" if value is None else str(value))


def render_report(rows: list[BacktestRow], days: int) -> str:
    """Собрать HTML-отчёт (инлайн-стили, все данные экранированы)."""
    total = len(rows)
    errors = sum(1 for r in rows if r.error)
    counts = Counter(r.result.type for r in rows if r.result)
    unknown = counts.get("unknown", 0)
    unknown_pct = f"{unknown / (total - errors) * 100:.0f}%" if total - errors else "—"

    summary_items = "".join(
        f"<li>{esc(_TYPE_LABELS.get(t, t))}: <b>{n}</b></li>"
        for t, n in counts.most_common()
    )

    table_rows = []
    for r in rows:
        if r.error or r.result is None:
            color = "#f8d7da"
            cells = [esc(r.date), esc(r.sender), esc(r.subject), "ОШИБКА", "—",
                     "", f"<code>{esc(r.error)}</code>"]
        else:
            res = r.result
            color = _TYPE_COLORS.get(res.type, "#ffffff")
            fields = "; ".join(
                f"{label}: {esc(value)}"
                for label, value in (
                    ("Гость", res.guest_name),
                    ("Заезд", res.arrival_date),
                    ("Выезд", res.departure_date),
                    ("Бронь", res.booking_number),
                    ("Канал", res.channel_name),
                    ("Детали", res.comment_details),
                )
                if value
            )
            cells = [esc(r.date), esc(r.sender), esc(r.subject),
                     esc(_TYPE_LABELS.get(res.type, res.type)), esc(res.priority),
                     fields or "—", esc(r.snippet)]
        tds = "".join(f'<td style="padding:6px 10px;border:1px solid #ccc;vertical-align:top">{c}</td>' for c in cells)
        table_rows.append(f'<tr style="background:{color}">{tds}</tr>')

    headers = "".join(
        f'<th style="padding:6px 10px;border:1px solid #ccc;background:#343a40;color:#fff">{h}</th>'
        for h in ("Дата", "Отправитель", "Тема", "Тип", "Приоритет", "Извлечённые поля", "Фрагмент текста")
    )

    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Бэктест Portier AI за {days} дн.</title></head>
<body style="font-family:sans-serif;margin:24px;color:#212529">
<h1>Бэктест Portier AI</h1>
<p>Период: последние {esc(days)} дн. · Всего писем: <b>{total}</b> ·
Ошибок LLM: <b>{errors}</b> · Не распознано: <b>{unknown}</b> ({unknown_pct})</p>
<h2>Сводка по типам</h2>
<ul>{summary_items}</ul>
<h2>Письма</h2>
<table style="border-collapse:collapse;width:100%;font-size:14px">
<tr>{headers}</tr>
{''.join(table_rows)}
</table>
</body></html>"""


async def run_backtest(days: int, output: str) -> None:
    """Прогнать конвейер по всем письмам за период и записать HTML-отчёт."""
    settings = get_settings()
    set_llm_client(create_llm_client(settings))

    imap = GmailClient(settings)
    await imap.connect()
    try:
        ids = await imap.list_new_message_ids(None, days)
        logger.info("Найдено писем за %d дн.: %d", days, len(ids))

        rows: list[BacktestRow] = []
        for gmail_id in ids:
            try:
                headers = await imap.fetch_headers(gmail_id)
                body_text = await imap.fetch_body_text(gmail_id)
            except Exception as exc:
                logger.exception("Не удалось загрузить письмо %s", gmail_id)
                rows.append(BacktestRow(date="—", sender="—", subject=f"ID {gmail_id}",
                                        snippet="", error=str(exc)))
                continue
            row = BacktestRow(
                date=headers["date"] or "—",
                sender=headers["sender"],
                subject=headers["subject"],
                snippet=body_text[:_SNIPPET_LEN],
            )
            try:
                result, _mapping = await analyze_body(
                    settings, headers["sender"], headers["subject"], body_text
                )
                row.result = result
            except Exception as exc:
                logger.exception("LLM не смогла обработать письмо %s", gmail_id)
                row.error = f"{type(exc).__name__}: {exc}"
            rows.append(row)
    finally:
        await imap.close()

    report = render_report(rows, days)
    Path(output).write_text(report, encoding="utf-8")
    logger.info("Отчёт записан: %s (%d писем)", output, len(rows))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(
        description="Бэктест Portier AI: классификация старой почты с HTML-отчётом"
    )
    parser.add_argument("--days", type=int, default=30, help="глубина выборки в днях")
    parser.add_argument("--output", default="report.html", help="путь к HTML-отчёту")
    args = parser.parse_args()
    started = datetime.now()
    asyncio.run(run_backtest(args.days, args.output))
    logger.info("Готово за %s", datetime.now() - started)


if __name__ == "__main__":
    main()
