"""Тикет 10: входящие счета и важные алерты.

Два детерминированных перехвата до LLM:
1. Алерты (овербукинг, некорректные цены, госорганы) → текстовое
   уведомление в третью группу.
2. Письма с вложением-счётом (имя файла содержит «счет»/«invoice» и пр.,
   расширения pdf/xls/xlsx) → документ(ы) в третью группу.
   Исключение — Купер: счета идут лично владельцу.
"""

import re

from .muted import _extract_addr, addr_matches, parse_rule

_INVOICE_WORD_RE = re.compile(r"сч[её]т|invoice|schet", re.IGNORECASE)
_DOC_SUFFIXES = (".pdf", ".xls", ".xlsx")

DOC_SUFFIXES = _DOC_SUFFIXES


def is_invoice_filename(filename: str) -> bool:
    """Имя файла похоже на счёт: «Счет-03-….pdf», «Счёт на оплату № ….xls»."""
    name = (filename or "").lower()
    return name.endswith(_DOC_SUFFIXES) and bool(_INVOICE_WORD_RE.search(name))


def is_own_address(sender: str, settings) -> bool:
    """Отправитель — собственный адрес отеля (наши исходящие письма).

    getattr с дефолтом: в тестах settings часто SimpleNamespace без этого
    поля — тогда считаем, что собственных адресов нет.
    """
    addrs = getattr(settings, "OWN_EMAIL_ADDRESSES", None) or []
    return _extract_addr(sender) in {a.lower() for a in addrs}


def is_alert(sender: str, subject: str, rules: list[str]) -> bool:
    """True, если письмо — важный алерт (правила как в чёрном списке:
    «addr» или «addr|шаблон темы»)."""
    addr = _extract_addr(sender)
    subj = (subject or "").lower()
    for rule in rules:
        rule_addr, pattern = parse_rule(rule)
        if not rule_addr or not addr_matches(rule_addr, addr):
            continue
        if pattern is None or pattern in subj:
            return True
    return False
