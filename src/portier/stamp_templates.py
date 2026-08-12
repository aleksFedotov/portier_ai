"""Шаблоны постановки печати/факсимиле по разметке владельца (тикет 31).

Владелец рисует на образцах PDF цветные прямоугольники (Square-аннотации):
красный — печать, синий — факсимиле, фиолетовый — расшифровка. Скрипт
`.scratch/build_templates.py` переводит разметку в `stamp_templates.yaml`:
позиции хранятся как смещения (dx, dy) от текстового якоря (см. stamp.find_anchor),
поэтому шаблон не ломается при изменении длины таблиц документа.

При поступлении документа match_template подбирает шаблон по ключевым словам
в тексте; если шаблон найден — stamp.py ставит изображения точно по меткам,
а эвристики (якорные смещения, черта подписи) остаются fallback'ом для
незнакомых документов.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Метки меньше этого размера (pt) считаем случайными кликами
_MIN_MARK_SIZE = 15.0

# Эталонные цвета разметки (RGB 0..1), как в PDF-XChange
_ROLE_COLORS = {
    "stamp": (0.98, 0.19, 0.18),      # красный
    "signature": (0.32, 0.67, 0.93),  # синий
    "caption": (0.58, 0.45, 0.89),    # фиолетовый
}
_COLOR_MATCH_TOL = 0.25


@dataclass
class Mark:
    """Одна метка: смещение от якоря и размер (pt, координаты от низа страницы)."""

    role: str  # stamp | signature | caption
    dx: float
    dy: float
    w: float
    h: float


@dataclass
class Template:
    """Шаблон документа: ключевые слова для матчинга + метки от якоря."""

    name: str
    keywords: tuple[str, ...]
    marks: list[Mark] = field(default_factory=list)


def _classify_color(color) -> str | None:
    """Роль метки по цвету обводки/заливки (ближайший эталон) или None."""
    if not color or len(color) < 3:
        return None
    best_role, best_dist = None, _COLOR_MATCH_TOL
    for role, proto in _ROLE_COLORS.items():
        dist = sum((float(c) - p) ** 2 for c, p in zip(color[:3], proto)) ** 0.5
        if dist < best_dist:
            best_role, best_dist = role, dist
    return best_role


def extract_marks(data: bytes) -> list[tuple[int, str, "object"]]:
    """Извлечь цветные метки из размеченного PDF.

    Вернуть [(page_index, role, pymupdf.Rect)] в координатах pymupdf
    (y от верха страницы). Мелкие метки (<_MIN_MARK_SIZE) отбрасываются.
    """
    import pymupdf

    marks: list[tuple[int, str, object]] = []
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        for pno in range(len(doc)):
            for annot in doc[pno].annots() or []:
                if annot.type[1] != "Square":
                    continue
                colors = annot.colors or {}
                role = _classify_color(colors.get("stroke")) or _classify_color(
                    colors.get("fill")
                )
                rect = annot.rect
                if (
                    role
                    and rect.width >= _MIN_MARK_SIZE
                    and rect.height >= _MIN_MARK_SIZE
                ):
                    marks.append((pno, role, rect))
    finally:
        doc.close()
    return marks


def load_templates(path: str | Path) -> list[Template]:
    """Загрузить шаблоны из YAML; нет файла — пустой список (не ошибка)."""
    import yaml

    path = Path(path)
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as fh:
            items = yaml.safe_load(fh) or []
    except Exception:
        logger.exception("Не удалось прочитать шаблоны печатей: %s", path)
        return []
    templates = []
    for item in items:
        templates.append(
            Template(
                name=item.get("name", ""),
                keywords=tuple(item.get("keywords") or ()),
                marks=[Mark(**m) for m in item.get("marks") or []],
            )
        )
    return templates


def save_templates(templates: list[Template], path: str | Path) -> None:
    """Сохранить шаблоны в YAML (использует скрипт build_templates.py)."""
    import yaml

    items = [
        {
            "name": t.name,
            "keywords": list(t.keywords),
            "marks": [
                {"role": m.role, "dx": round(m.dx, 1), "dy": round(m.dy, 1),
                 "w": round(m.w, 1), "h": round(m.h, 1)}
                for m in t.marks
            ],
        }
        for t in templates
    ]
    path = Path(path)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(items, fh, allow_unicode=True, sort_keys=False)
    logger.info("Шаблоны печатей сохранены: %s (%d шт.)", path, len(templates))


def match_template(data: bytes, templates: list[Template]) -> Template | None:
    """Подобрать шаблон по ключевым словам в тексте документа."""
    if not templates:
        return None
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
        text = "".join(doc[pno].get_text() for pno in range(len(doc)))
        doc.close()
    except Exception:
        logger.exception("Не удалось извлечь текст PDF для матчинга шаблонов")
        return None
    for tpl in templates:
        if tpl.keywords and all(kw in text for kw in tpl.keywords):
            return tpl
    return None
