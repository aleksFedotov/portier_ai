"""Очистка HTML-писем до читаемого текста."""

import re

# Подписи/подвалы, которые отсекаем из текста письма
_FOOTER_MARKERS = (
    "отправлено с",
    "sent from my",
    "get outlook for",
    "---------- forwarded message",
    "-----original message-----",
    "-----исходное сообщение-----",
    "вы писали",
)

_TAGS_TO_DROP = ("style", "script", "head", "title", "meta")


def _clean_with_selectolax(html: str) -> str:
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    for tag in _TAGS_TO_DROP:
        for node in tree.css(tag):
            node.decompose()
    body = tree.body
    return body.text(separator="\n") if body else tree.text(separator="\n")


def _clean_with_bs4(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(list(_TAGS_TO_DROP)):
        tag.decompose()
    return soup.get_text(separator="\n")


def _is_html(text: str) -> bool:
    return bool(re.search(r"<\s*(html|body|div|p|br|table|span)[^>]*>", text, re.IGNORECASE))


def _strip_footers(text: str) -> str:
    lines = text.splitlines()
    cut = len(lines)
    for i, line in enumerate(lines):
        low = line.strip().lower()
        if any(low.startswith(marker) for marker in _FOOTER_MARKERS):
            cut = i
            break
    return "\n".join(lines[:cut])


def clean_email(raw: str) -> str:
    """Очистить письмо: HTML → текст, удалить style/script и подвалы.

    Обычный текст возвращается как есть (нормализованы лишь пустые строки).
    """
    if not raw:
        return ""
    if _is_html(raw):
        try:
            text = _clean_with_selectolax(raw)
        except Exception:
            text = _clean_with_bs4(raw)
    else:
        text = raw
    text = _strip_footers(text)
    # Сжать повторные пустые строки и пробелы
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()
