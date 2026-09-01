# -*- coding: utf-8 -*-
"""Тесты шаблонов постановки печати по разметке владельца (stamp_templates.py).

Образцы в test_data/ размечены цветными Square-аннотациями:
красный — печать, синий — факсимиле, фиолетовый — расшифровка.
"""

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from reportlab.pdfgen import canvas

from portier.stamp import stamp_pdf
from portier.stamp_templates import (
    Mark, Template, extract_marks, load_templates, match_template, save_templates,
)

TEST_DATA = Path("test_data")
UPD_TWO_SETS = TEST_DATA / "Универсальный передаточный документ (9).pdf"
AKT_NO_CAPTION = TEST_DATA / "Акт сверки взаиморасчетов № 50351 от 30 июля 2026 г (1).pdf"


def test_extract_marks_roles_and_sizes():
    """В УПД (9): 1 печать, 2 факсимиле, 2 расшифровки; мелочь отфильтрована."""
    marks = extract_marks(UPD_TWO_SETS.read_bytes())
    roles = sorted(r for _, r, _ in marks)
    assert roles == ["caption", "caption", "signature", "signature", "stamp"]
    for _, _, rect in marks:
        assert rect.width >= 15 and rect.height >= 15


def test_extract_marks_no_caption_when_not_marked():
    """В акте сверки 50351 размечены только печать и подпись."""
    roles = sorted(r for _, r, _ in extract_marks(AKT_NO_CAPTION.read_bytes()))
    assert roles == ["signature", "stamp"]


def test_templates_roundtrip(tmp_path):
    tpls = [Template(
        name="t1", keywords=("Акт сверки", "Онлайн Инновации"),
        marks=[Mark("stamp", 1.0, 2.0, 60.0, 60.0),
               Mark("caption", -10.0, 30.0, 120.0, 19.0)],
    )]
    out = tmp_path / "tpl.yaml"
    save_templates(tpls, out)
    loaded = load_templates(out)
    assert loaded[0].name == "t1"
    assert loaded[0].keywords == ("Акт сверки", "Онлайн Инновации")
    assert loaded[0].marks[1].role == "caption"
    assert loaded[0].marks[1].w == 120.0


def test_load_templates_missing_file(tmp_path):
    assert load_templates(tmp_path / "nope.yaml") == []


def _blank_pdf(text: str = "") -> bytes:
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    if text:
        pdf.drawString(50, 700, text)
    pdf.save()
    return buf.getvalue()


def test_match_template_by_keywords():
    tpls = [
        Template("a", ("Акт сверки", "Онлайн Инновации")),
        Template("b", ("Универсальный", "БРОНЕВИК")),
    ]
    assert match_template(UPD_TWO_SETS.read_bytes(), tpls).name == "b"
    assert match_template(AKT_NO_CAPTION.read_bytes(), tpls).name == "a"
    assert match_template(_blank_pdf(), tpls) is None


def test_stamp_pdf_uses_repo_template():
    """Реальный образец матчится с stamp_templates.yaml и рисует все метки."""
    settings = SimpleNamespace(
        INVOICE_STAMP_PATH="data/печать 2-Photoroom.png",
        INVOICE_SIGNATURE_PATH="data/подпись 2-Photoroom.png",
        SIGNATURE_CAPTION="Генеральный директор Кузин А. С.",
        STAMP_TEMPLATES_FILE="stamp_templates.yaml",
    )
    out = stamp_pdf(UPD_TWO_SETS.read_bytes(), settings)
    assert out.startswith(b"%PDF")

    import pymupdf
    doc = pymupdf.open(stream=out, filetype="pdf")
    text = doc[-1].get_text()
    assert "Кузин А. С." in text  # расшифровка попала на страницу
    # Два набора подписей → две расшифровки
    assert text.count("Кузин А. С.") == 2


def test_stamp_pdf_without_templates_file_still_works(tmp_path):
    """Без файла шаблонов — старая цепочка эвристик, без падений."""
    settings = SimpleNamespace(
        INVOICE_STAMP_PATH="",
        INVOICE_SIGNATURE_PATH="",
        SIGNATURE_CAPTION="",
        STAMP_TEMPLATES_FILE=str(tmp_path / "absent.yaml"),
    )
    out = stamp_pdf(_blank_pdf("____________________"), settings)
    assert out.startswith(b"%PDF")


def _two_page_pdf() -> bytes:
    """Стр. 1 — блок подписей (якорь «ООО "Орон"»), стр. 2 — без якорей."""
    from portier.invoices import _register_fonts

    font = _register_fonts()["regular"]
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    pdf.setFont(font, 10)
    pdf.drawString(400, 200, 'ООО "Орон" (ИНН 7841046976)')
    pdf.showPage()
    pdf.drawString(50, 700, "Приложение без подписей")
    pdf.save()
    return buf.getvalue()


def test_stamp_pdf_targets_anchor_page_not_last():
    """Блок подписей на стр. 1 из 2 (отчёт Броневика 08.2026): печать и
    расшифровка ставятся туда, а не в правый нижний угол последней страницы."""
    settings = SimpleNamespace(
        INVOICE_STAMP_PATH="data/печать 2-Photoroom.png",
        INVOICE_SIGNATURE_PATH="data/подпись 2-Photoroom.png",
        SIGNATURE_CAPTION="Генеральный директор Кузин А. С.",
        STAMP_TEMPLATES_FILE="stamp_templates.yaml",
    )
    out = stamp_pdf(_two_page_pdf(), settings)

    import pymupdf
    doc = pymupdf.open(stream=out, filetype="pdf")
    assert len(doc) == 2
    assert "Кузин А. С." in doc[0].get_text()  # расшифровка на стр. 1
    assert doc[0].get_images()  # печать/факсимиле на стр. 1
    assert "Кузин А. С." not in doc[1].get_text()
    assert not doc[1].get_images()  # последняя страница не тронута
