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


def addr_matches(rule_addr: str, addr: str) -> bool:
    """Совпадение адреса с адресной частью правила.

    Правило "@domain.ru" матчит любой ящик домена (нужно для адресов
    вида o_<номер>.spb@v2.hbconnect.ru и bronevik.com с меняющимися
    локальными частями); обычное правило — точное совпадение.
    """
    if rule_addr.startswith("@"):
        return addr.endswith(rule_addr)
    return addr == rule_addr


def _norm(text: str) -> str:
    """Нормализация для сравнения: нижний регистр, ё → е.

    Каналы пишут темы то с «ё» («Гость внёс предоплату»), то с «е» —
    правило на одну форму не должно пропускать другую.
    """
    return (text or "").lower().replace("ё", "е")


def is_muted(sender: str, subject: str, rules: list[str]) -> bool:
    """True, если письмо попадает под хотя бы одно правило чёрного списка."""
    addr = _extract_addr(sender)
    subj = _norm(subject)
    for rule in rules:
        rule_addr, pattern = parse_rule(rule)
        if not rule_addr or not addr_matches(rule_addr, addr):
            continue
        if pattern is None or _norm(pattern) in subj:
            return True
    return False
