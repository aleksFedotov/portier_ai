"""Чёрный список отправителей (тикет 08).

Правило — строка вида "addr" или "addr|шаблон темы":
- "addr" — глушим все письма от этого адреса;
- "addr|шаблон" — глушим только письма, чья тема содержит шаблон
  (регистронезависимо). Нужно для адресов, с которых идут и нужные
  письма, и мусор (например, noreply@travellinemail.com: брони — нужны,
  «Выдана карта лояльности» — мусор).
"""

import re

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _extract_addr(sender: str) -> str:
    m = _EMAIL_RE.search(sender or "")
    return m.group(0).lower() if m else (sender or "").strip().lower()


def parse_rule(rule: str) -> tuple[str, str | None]:
    """Разобрать правило на (адрес, шаблон темы | None)."""
    addr, sep, pattern = rule.partition("|")
    return addr.strip().lower(), (pattern.strip().lower() if sep else None)


def is_muted(sender: str, subject: str, rules: list[str]) -> bool:
    """True, если письмо попадает под хотя бы одно правило чёрного списка."""
    addr = _extract_addr(sender)
    subj = (subject or "").lower()
    for rule in rules:
        rule_addr, pattern = parse_rule(rule)
        if not rule_addr or addr != rule_addr:
            continue
        if pattern is None or pattern in subj:
            return True
    return False
