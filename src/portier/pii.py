"""Маскирование персональных данных (PII) перед отправкой текста в LLM."""

import re
from typing import Optional

# Телефоны: +7/8, со скобками, пробелами и дефисами
_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\w)"
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

# Даты рождения (тикет 22): все даты в сегменте строки после ключевого слова
# «рождени…». Общие даты НЕ маскируем — даты заезда/выезда нужны для счетов.
_BIRTHDATE_SEGMENT_RE = re.compile(r"рождени[яе][^\n]*", re.IGNORECASE)
_DATE_RE = re.compile(r"\d{1,2}\.\d{1,2}\.\d{4}")

_names_extractor = None


def _get_names_extractor():
    """Ленивая инициализация NamesExtractor (правиловый, без нейросетей)."""
    global _names_extractor
    if _names_extractor is None:
        from natasha import MorphVocab, NamesExtractor

        _names_extractor = NamesExtractor(MorphVocab())
    return _names_extractor


class _Masker:
    """Накапливает токены с дедупликацией одинаковых значений."""

    def __init__(self) -> None:
        self.mapping: dict[str, str] = {}  # токен -> оригинал
        self._counters = {"PHONE": 0, "EMAIL": 0, "GUEST": 0, "BDATE": 0}

    def token_for(self, kind: str, value: str) -> str:
        for token, original in self.mapping.items():
            if original == value:
                return token
        self._counters[kind] += 1
        token = f"[{kind}_{self._counters[kind]}]"
        self.mapping[token] = value
        return token


def _find_name_spans(text: str) -> list[tuple[int, int]]:
    """Span'ы русских ФИО через natasha. Пересечения разрешаются в пользу длинного.

    Фильтр шума: обязательно имя (fact.first), все слова span'а с заглавной буквы.
    """
    extractor = _get_names_extractor()
    try:
        matches = list(extractor(text))
    except Exception:
        return []
    candidates = []
    for m in matches:
        if m.fact.first is None:
            continue
        if not all(word[:1].isupper() for word in text[m.start : m.stop].split()):
            continue
        candidates.append((m.start, m.stop))
    candidates.sort(key=lambda s: s[1] - s[0], reverse=True)
    taken: list[tuple[int, int]] = []
    for start, stop in candidates:
        if any(start < t_stop and stop > t_start for t_start, t_stop in taken):
            continue
        taken.append((start, stop))
    return taken


def mask_pii(text: str, masker: _Masker | None = None) -> tuple[str, dict[str, str]]:
    """Замаскировать PII. Возвращает (текст с токенами, карта токен→оригинал).

    Телефоны → [PHONE_N], email → [EMAIL_N], ФИО → [GUEST_N],
    даты рождения → [BDATE_N] (тикет 22).
    Одинаковые значения получают один и тот же токен. Если передать общий
    masker (например, для sender/subject/body одного письма), нумерация
    токенов и дедупликация значений сквозные между вызовами.
    """
    if masker is None:
        masker = _Masker()

    def sub_phone(m: re.Match) -> str:
        return masker.token_for("PHONE", m.group(0))

    def sub_email(m: re.Match) -> str:
        return masker.token_for("EMAIL", m.group(0))

    def sub_birthdate(m: re.Match) -> str:
        # В сегменте «…рождени… <перечисление>» маскируем все даты дд.мм.гггг
        return _DATE_RE.sub(lambda d: masker.token_for("BDATE", d.group(0)), m.group(0))

    masked = _EMAIL_RE.sub(sub_email, text)
    masked = _PHONE_RE.sub(sub_phone, masked)
    masked = _BIRTHDATE_SEGMENT_RE.sub(sub_birthdate, masked)

    # Значения извлекаем до замен, чтобы замены не сдвигали индексы span'ов
    values = [masked[start:stop] for start, stop in _find_name_spans(masked)]
    for value in sorted(set(values), key=len, reverse=True):
        if value not in masked:  # поглощено более длинной заменой
            continue
        token = masker.token_for("GUEST", value)
        masked = masked.replace(value, token)

    return masked, masker.mapping


def unmask_pii(text: Optional[str], mapping: dict[str, str]) -> Optional[str]:
    """Обратная подстановка токенов в текст."""
    if not text:
        return text
    for token, original in mapping.items():
        text = text.replace(token, original)
    return text
