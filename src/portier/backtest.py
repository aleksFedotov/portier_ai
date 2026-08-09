"""Бэктест: прогон конвейера по старой почте без Telegram и прод-БД.

Перед LLM применяются те же детерминированные пред-фильтры, что в живом
конвейере (входящие счета, чёрный список, уведомления владельцу, реестры
Яндекса) — перехваченные письма помечаются псевдо-типом и в LLM не идут.

Отчёт — интерактивный HTML: фильтры по типам, поиск, разметка неверных
строк с выгрузкой corrections.json. Сырые результаты прогона кэшируются
(JSON), отчёт можно пересобрать из кэша без повторного прогона LLM.

Использование:
  python -m portier.backtest --days 45 --output report.html
  python -m portier.backtest --from-cache --output report.html
"""

import argparse
import asyncio
import html
import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import get_settings
from .gmail_client import GmailClient, analyze_body, set_llm_client
from .incoming import is_alert, is_invoice_filename
from .llm import create_llm_client
from .muted import _extract_addr, is_muted
from .schemas import EmailAnalysisResult
from .yandex_registry import is_yandex_registry

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
    # Псевдо-типы детерминированных пред-фильтров (до LLM)
    "muted": "#e9ecef",
    "owner_notice": "#fff3cd",
    "yandex_registry": "#cfe2ff",
    "incoming_invoice": "#e2d9f3",
    "kuper_invoice": "#e2d9f3",
    "alert": "#f5c6cb",
    "login_code": "#d1ecf1",
    "admin_attention": "#ffc107",
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
    # Псевдо-типы детерминированных пред-фильтров (до LLM)
    "muted": "Чёрный список",
    "owner_notice": "Уведомление владельцу",
    "yandex_registry": "Реестр Яндекса",
    "incoming_invoice": "Входящий счёт",
    "kuper_invoice": "Счёт Купера (владельцу)",
    "alert": "Важный алерт",
    "login_code": "Код / вход в систему",
    "admin_attention": "Требуется обработка администратором",
}

# Типы, которые можно выбрать в разметке «должно быть»
_MARKABLE_TYPES = [
    "booking_comment", "guest_message", "invoice_required", "booking_modified",
    "booking_cancelled", "payment_received", "payment_failed",
    "review_notification", "booking_confirmed", "unknown",
    "muted", "owner_notice", "yandex_registry", "incoming_invoice",
    "alert", "login_code", "admin_attention",
]

_SNIPPET_LEN = 300
_DEFAULT_CACHE = "data/backtest_cache.json"


@dataclass
class BacktestRow:
    """Результат обработки одного письма в бэктесте."""

    date: str
    sender: str
    subject: str
    snippet: str
    gmail_id: str = ""
    result: Optional[EmailAnalysisResult] = None
    error: Optional[str] = None
    filtered: Optional[str] = None  # псевдо-тип пред-фильтра (письмо не пошло в LLM)


def apply_prefilters(settings, sender: str, subject: str, attachments: list[str]) -> Optional[str]:
    """Детерминированные перехваты живого конвейера, в том же порядке (gmail_client).

    Возвращает псевдо-тип, если письмо перехватывается до LLM, иначе None.
    """
    addr = _extract_addr(sender)
    # 1. Важные алерты (овербукинг, госорганы, расчётный отдел TravelLine)
    if is_alert(sender, subject, settings.ALERT_RULES):
        return "alert"
    # 2. Входящие счета: по вложению, затем по известным отправителям
    if any(is_invoice_filename(name) for name in attachments):
        is_exception = addr in {a.lower() for a in settings.INVOICE_OWNER_EXCEPTIONS}
        return "kuper_invoice" if is_exception else "incoming_invoice"
    if addr in {a.lower() for a in settings.INCOMING_INVOICE_SENDERS}:
        return "incoming_invoice"
    # 3. Коды входа в учётные записи
    if is_alert(sender, subject, settings.LOGIN_CODE_RULES):
        return "login_code"
    # 4. Требуется обработка администратором
    if is_alert(sender, subject, settings.ADMIN_ATTENTION_RULES):
        return "admin_attention"
    # 5. Уведомление владельцу (раньше чёрного списка: Купер, МатСервис)
    if addr in {a.lower() for a in settings.OWNER_NOTICE_SENDERS} \
            or is_alert(sender, subject, settings.OWNER_NOTICE_RULES):
        return "owner_notice"
    # 6. Чёрный список
    if is_muted(sender, subject, settings.MUTED_SENDERS):
        return "muted"
    # 7. Реестры Яндекс Путешествий
    if is_yandex_registry(sender, subject):
        return "yandex_registry"
    return None


def esc(value: object) -> str:
    return html.escape("—" if value is None else str(value), quote=True)


# --- Кэш сырых результатов прогона -----------------------------------------


def save_cache(rows: list[BacktestRow], path: str) -> None:
    """Сохранить сырые результаты прогона, чтобы пересобирать отчёт без LLM."""
    data = [
        {
            "gmail_id": r.gmail_id,
            "date": r.date,
            "sender": r.sender,
            "subject": r.subject,
            "snippet": r.snippet,
            "filtered": r.filtered,
            "error": r.error,
            "result": r.result.model_dump() if r.result else None,
        }
        for r in rows
    ]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("Кэш результатов записан: %s (%d писем)", path, len(rows))


def load_cache(path: str) -> list[BacktestRow]:
    """Прочитать кэш сырых результатов прогона."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        BacktestRow(
            gmail_id=d.get("gmail_id", ""),
            date=d["date"],
            sender=d["sender"],
            subject=d["subject"],
            snippet=d.get("snippet", ""),
            filtered=d.get("filtered"),
            error=d.get("error"),
            result=EmailAnalysisResult(**d["result"]) if d.get("result") else None,
        )
        for d in data
    ]


def reapply_filters(rows: list[BacktestRow], settings) -> None:
    """Пере-применить пред-фильтры к кэшированным строкам (после смены правил).

    Письма, перехваченные новыми правилами, получают псевдо-тип даже если
    раньше дошли до LLM; обратная ситуация (правила только добавляются)
    в расчёт не берётся.
    """
    for r in rows:
        if r.error:
            continue
        pseudo = apply_prefilters(settings, r.sender, r.subject, [])
        if pseudo:
            r.filtered = pseudo


# --- HTML-отчёт -------------------------------------------------------------

_REPORT_CSS = """
body{font-family:sans-serif;margin:24px;color:#212529}
#toolbar{position:sticky;top:0;background:#fff;padding:8px 0;border-bottom:2px solid #343a40;z-index:10}
.chip{margin:2px;padding:4px 10px;border:1px solid #999;border-radius:12px;cursor:pointer;font-size:13px}
.chip.active{outline:3px solid #343a40}
#search{margin-left:12px;padding:4px 8px;width:260px}
button.action{margin-left:8px;padding:4px 10px;cursor:pointer}
table{border-collapse:collapse;width:100%;font-size:14px;margin-top:8px}
th{padding:6px 10px;border:1px solid #ccc;background:#343a40;color:#fff;position:sticky;top:44px}
td{padding:6px 10px;border:1px solid #ccc;vertical-align:top}
tr.day-sep td{background:#dee2e6;font-weight:bold;cursor:pointer}
tr.marked{outline:3px solid #dc3545}
.gid{color:#6c757d;font-size:11px}
select.expect{max-width:170px}
input.note{width:150px;margin-top:2px}
"""

_REPORT_JS = """
const LS_KEY='portierBacktestMarks';
let marks={};try{marks=JSON.parse(localStorage.getItem(LS_KEY)||'{}')}catch(e){marks={}}
let activeType=null;
const rows=[...document.querySelectorAll('tr.msg')];
const byId={};rows.forEach(r=>byId[r.dataset.gid]=r);

function saveMarks(){localStorage.setItem(LS_KEY,JSON.stringify(marks));updateCount();}
function updateCount(){document.getElementById('marked-count').textContent=Object.keys(marks).length;}

function applyMark(gid){
  const tr=byId[gid];if(!tr)return;
  const m=marks[gid];
  tr.classList.toggle('marked',!!m);
  tr.querySelector('select.expect').value=m?(m.expected||''):'';
  tr.querySelector('input.note').value=m?(m.comment||''):'';
}
function setMark(gid,expected,comment){
  if(!expected&&!comment){delete marks[gid];}else{marks[gid]={expected:expected,comment:comment};}
  saveMarks();applyMark(gid);
}
document.querySelectorAll('select.expect').forEach(s=>{
  s.addEventListener('change',()=>{
    const gid=s.closest('tr').dataset.gid;
    const note=s.closest('td').querySelector('input.note').value;
    setMark(gid,s.value,note);
  });
});
document.querySelectorAll('input.note').forEach(n=>{
  n.addEventListener('change',()=>{
    const gid=n.closest('tr').dataset.gid;
    const sel=n.closest('td').querySelector('select.expect').value;
    setMark(gid,sel,n.value);
  });
});

function applyFilter(){
  const q=document.getElementById('search').value.trim().toLowerCase();
  rows.forEach(r=>{
    const okType=!activeType||r.dataset.type===activeType;
    const okSearch=!q||r.dataset.search.includes(q);
    r.style.display=(okType&&okSearch)?'':'none';
  });
  document.querySelectorAll('tr.day-sep').forEach(sep=>{
    let el=sep.nextElementSibling,any=false;
    while(el&&!el.classList.contains('day-sep')){
      if(el.classList.contains('msg')&&el.style.display!=='none'){any=true;break;}
      el=el.nextElementSibling;
    }
    sep.style.display=any?'':'none';
  });
}
document.querySelectorAll('.chip').forEach(c=>{
  c.addEventListener('click',()=>{
    const t=c.dataset.type;
    activeType=(activeType===t)?null:t;
    document.querySelectorAll('.chip').forEach(x=>x.classList.toggle('active',x.dataset.type===activeType));
    applyFilter();
  });
});
document.getElementById('btn-unknown').addEventListener('click',()=>{
  activeType='unknown';
  document.querySelectorAll('.chip').forEach(x=>x.classList.toggle('active',x.dataset.type==='unknown'));
  applyFilter();
});
document.getElementById('btn-reset').addEventListener('click',()=>{
  activeType=null;document.getElementById('search').value='';
  document.querySelectorAll('.chip').forEach(x=>x.classList.remove('active'));
  applyFilter();
});
document.getElementById('search').addEventListener('input',applyFilter);
document.querySelectorAll('tr.day-sep').forEach(sep=>{
  sep.addEventListener('click',()=>{
    let el=sep.nextElementSibling;const hide=sep.dataset.collapsed!=='1';
    sep.dataset.collapsed=hide?'1':'0';
    while(el&&!el.classList.contains('day-sep')){
      if(el.classList.contains('msg'))el.style.visibility=hide?'collapse':'visible';
      el=el.nextElementSibling;
    }
  });
});

function exportData(){
  return Object.entries(marks).map(([gid,m])=>{
    const tr=byId[gid];
    return {gmail_id:gid,date:tr?tr.dataset.date:'',sender:tr?tr.dataset.sender:'',
      subject:tr?tr.dataset.subject:'',current_type:tr?tr.dataset.type:'',
      expected_type:m.expected||'',comment:m.comment||''};
  });
}
document.getElementById('btn-export').addEventListener('click',()=>{
  const blob=new Blob([JSON.stringify(exportData(),null,1)],{type:'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='corrections.json';a.click();URL.revokeObjectURL(a.href);
});
document.getElementById('btn-copy').addEventListener('click',()=>{
  const lines=exportData().map(d=>
    d.gmail_id+' | '+d.current_type+' -> '+(d.expected_type||'?')+' | '+d.subject+(d.comment?' | '+d.comment:''));
  navigator.clipboard.writeText(lines.join('\\n'));
  document.getElementById('btn-copy').textContent='Скопировано: '+lines.length;
});

Object.keys(marks).forEach(applyMark);
updateCount();
"""


def _mark_cell() -> str:
    options = '<option value="">— верно —</option>' + "".join(
        f'<option value="{esc(t)}">{esc(_TYPE_LABELS.get(t, t))}</option>'
        for t in _MARKABLE_TYPES
    )
    return (
        '<td class="mark">'
        f'<select class="expect">{options}</select><br>'
        '<input class="note" placeholder="комментарий">'
        "</td>"
    )


def render_report(rows: list[BacktestRow], days: int) -> str:
    """Собрать интерактивный HTML-отчёт (инлайн JS, все данные экранированы)."""
    total = len(rows)
    errors = sum(1 for r in rows if r.error)
    filtered = sum(1 for r in rows if r.filtered)
    counts = Counter(r.filtered or r.result.type for r in rows if r.filtered or r.result)
    unknown = counts.get("unknown", 0)
    llm_rows = total - errors - filtered  # письма, реально дошедшие до LLM
    unknown_pct = f"{unknown / llm_rows * 100:.0f}%" if llm_rows else "—"

    chips = "".join(
        f'<button class="chip" data-type="{esc(t)}" '
        f'style="background:{_TYPE_COLORS.get(t, "#ffffff")}">'
        f"{esc(_TYPE_LABELS.get(t, t))}: <b>{n}</b></button>"
        for t, n in counts.most_common()
    )

    table_rows: list[str] = []
    prev_day: Optional[str] = None
    for r in rows:
        day = r.date[:16] if r.date and r.date != "—" else "—"
        if day != prev_day:
            prev_day = day
            table_rows.append(
                f'<tr class="day-sep"><td colspan="8">▾ {esc(day)}</td></tr>'
            )
        gid = esc(r.gmail_id)
        date_cell = f"{esc(r.date)}<br><span class='gid'>id: {gid}</span>"
        if r.error:
            rtype = "error"
            color = "#f8d7da"
            cells = [date_cell, esc(r.sender), esc(r.subject), "ОШИБКА", "—",
                     "", f"<code>{esc(r.error)}</code>"]
        elif r.filtered:
            rtype = r.filtered
            color = _TYPE_COLORS.get(r.filtered, "#ffffff")
            cells = [date_cell, esc(r.sender), esc(r.subject),
                     esc(_TYPE_LABELS.get(r.filtered, r.filtered)), "—",
                     "перехвачено до LLM", esc(r.snippet)]
        else:
            res = r.result
            rtype = res.type
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
            cells = [date_cell, esc(r.sender), esc(r.subject),
                     esc(_TYPE_LABELS.get(res.type, res.type)), esc(res.priority),
                     fields or "—", esc(r.snippet)]
        tds = "".join(f"<td>{c}</td>" for c in cells)
        search = esc(f"{r.sender} {r.subject}".lower())
        table_rows.append(
            f'<tr class="msg" data-type="{esc(rtype)}" data-gid="{gid}" '
            f'data-date="{esc(r.date)}" data-sender="{esc(r.sender)}" '
            f'data-subject="{esc(r.subject)}" data-search="{search}" '
            f'style="background:{color}">{tds}{_mark_cell()}</tr>'
        )

    headers = "".join(
        f"<th>{h}</th>"
        for h in ("Дата", "Отправитель", "Тема", "Тип", "Приоритет",
                  "Извлечённые поля", "Фрагмент текста", "Разметка")
    )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="ru"><head><meta charset="utf-8">\n'
        f"<title>Бэктест Portier AI за {days} дн.</title>\n"
        f"<style>{_REPORT_CSS}</style></head><body>\n"
        "<h1>Бэктест Portier AI</h1>\n"
        f"<p>Период: последние {esc(days)} дн. · Всего писем: <b>{total}</b> · "
        f"Перехвачено фильтрами (без LLM): <b>{filtered}</b> · "
        f"Ошибок LLM: <b>{errors}</b> · Не распознано: <b>{unknown}</b> ({unknown_pct})</p>\n"
        '<div id="toolbar">\n'
        f"<div>{chips}"
        '<button class="chip" id="btn-unknown">Только нераспознанные</button>'
        '<button class="chip" id="btn-reset">Сбросить</button>'
        '<input id="search" placeholder="поиск по отправителю/теме"></div>\n'
        '<div style="margin-top:6px">Отмечено неверных: <b id="marked-count">0</b>'
        '<button class="action" id="btn-export">Скачать отметки (JSON)</button>'
        '<button class="action" id="btn-copy">Скопировать списком</button>'
        '<span class="gid"> Отметки хранятся в браузере (localStorage) — не теряются при перезагрузке.</span></div>\n'
        "</div>\n"
        f"<table><tr>{headers}</tr>{''.join(table_rows)}</table>\n"
        f"<script>{_REPORT_JS}</script>\n"
        "</body></html>"
    )


async def run_backtest(days: int, output: str, cache: str = _DEFAULT_CACHE) -> None:
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
            except Exception as exc:
                logger.exception("Не удалось загрузить письмо %s", gmail_id)
                rows.append(BacktestRow(date="—", sender="—", subject=f"ID {gmail_id}",
                                        snippet="", gmail_id=gmail_id, error=str(exc)))
                continue
            row = BacktestRow(
                date=headers["date"] or "—",
                sender=headers["sender"],
                subject=headers["subject"],
                snippet="",
                gmail_id=gmail_id,
            )
            # Те же детерминированные перехваты, что в живом конвейере, —
            # такие письма в LLM не идут и тело не скачивается
            pseudo = apply_prefilters(
                settings, headers["sender"], headers["subject"],
                headers.get("attachments") or [],
            )
            if pseudo:
                row.filtered = pseudo
                rows.append(row)
                continue
            try:
                body_text = await imap.fetch_body_text(gmail_id)
            except Exception as exc:
                logger.exception("Не удалось загрузить тело письма %s", gmail_id)
                row.error = f"{type(exc).__name__}: {exc}"
                rows.append(row)
                continue
            row.snippet = body_text[:_SNIPPET_LEN]
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

    save_cache(rows, cache)
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
    parser.add_argument("--cache", default=_DEFAULT_CACHE, help="путь к кэшу результатов")
    parser.add_argument(
        "--from-cache", action="store_true",
        help="не ходить в почту/LLM, пересобрать отчёт из кэша",
    )
    parser.add_argument(
        "--reapply-filters", action="store_true",
        help="с --from-cache: пере-применить пред-фильтры к кэшу (после смены правил)",
    )
    args = parser.parse_args()
    started = datetime.now()
    if args.from_cache:
        rows = load_cache(args.cache)
        if args.reapply_filters:
            settings = get_settings()
            reapply_filters(rows, settings)
            save_cache(rows, args.cache)
            logger.info("Пред-фильтры пере-применены, кэш обновлён")
        report = render_report(rows, args.days)
        Path(args.output).write_text(report, encoding="utf-8")
        logger.info("Отчёт из кэша записан: %s (%d писем)", args.output, len(rows))
    else:
        asyncio.run(run_backtest(args.days, args.output, args.cache))
    logger.info("Готово за %s", datetime.now() - started)


if __name__ == "__main__":
    main()
