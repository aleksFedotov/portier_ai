"""Наложение печати и факсимиле на готовый PDF (тикет 31).

Входящие документы (счета-фактуры, акты сверки) приходят чужими PDF —
перегенерировать их нельзя, поэтому reportlab'ом рисуем overlay-страницу
с печатью/подписью и сливаем её с последней страницей документа через
pypdf (там обычно место подписей). Расшифровка (caption) — опционально,
добавляется настройкой SIGNATURE_CAPTION.

Место подписи ищем автоматически, по цепочке fallback:
0. Шаблон из stamp_templates.yaml (разметка владельца цветными рамками на
   образцах, см. stamp_templates.py): документ матчится по ключевым словам,
   позиции печати/факсимиле/расшифровки берутся как смещения от якоря.
1. Текстовый якорь (способ 1): слова-маркеры зоны подписи принципала
   («М.П.», «Генеральный директор», «ООО "ОРОН"» и т.п., см. ANCHOR_WORDS) —
   берётся самое правое вхождение в нижней половине страницы.
2. Черта подписи (способ 3): текстовая (длинная строка подчёркиваний «____»)
   или векторная (тонкий широкий прямоугольник/горизонтальная линия в content
   stream). Берём самую правую черту в нижней части страницы.
3. Fallback на фиксированную позицию в правом нижнем углу (скан, растр).
"""

import io
import logging
from pathlib import Path

from .config import Settings

logger = logging.getLogger(__name__)

# Минимальная длина текстовой черты (подчёркиваний подряд)
_MIN_UNDERSCORES = 15
# Геометрия векторной черты: ширина и высота в pt
_MIN_LINE_W = 40.0
_MAX_LINE_H = 3.0

# Способ 1: текстовые якоря зоны подписи принципала, по убыванию приоритета.
ANCHOR_WORDS = (
    "М.П.",
    "Место печати",
    "Подтверждено Принципалом",
    "Генеральный директор",
)
# Наша сторона — ООО «ОРОН»: если в зоне подписей есть это слово, якорь
# выбираем в той же колонке (сторона документа бывает и левой, и правой).
# search_for регистрозависим — перечисляем варианты написания.
_COMPANY_WORDS = ("ОРОН", "Орон")
# Максимальное расстояние якоря от колонки компании, pt; дальше — чужая колонка
_COMPANY_COLUMN_MAX_DX = 160.0
# Зона подписей: нижние 70% страницы (поиск якорей выше игнорируем)
_ANCHOR_ZONE_TOP = 0.3

# Смещения печати/факсимиле от якоря, pt (1 мм = 2.835 pt)
_SIG_DX = 20.0    # факсимиле правее якоря
_SIG_DY = -25.0   # и ниже него (в зону подписи)
_STAMP_OVERLAP = 25.0  # печать внахлест на факсимиле
_STAMP_DY = 15.0  # печать чуть выше факсимиле


def find_anchor(data: bytes, page_index: int,
                page_height: float) -> tuple[float, float, str] | None:
    """Найти текстовый якорь зоны подписи на странице PDF.

    Поиск через pymupdf (search_for): в отличие от pypdf visitor_text,
    корректно даёт координаты текста внутри form XObject. Вернуть
    (x, y, слово) в координатах pypdf (y от низа страницы) или None.
    """
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
        page = doc[page_index]
        zone_top = page_height * _ANCHOR_ZONE_TOP
        # Занятые места: растровые картинки (чужие печати/подписи) на странице
        img_rects = []
        for img in page.get_images(full=True):
            img_rects.extend(page.get_image_rects(img[0]))
        company_rects = [
            r for w in _COMPANY_WORDS for r in page.search_for(w) if r.y0 > zone_top
        ]
        company = max(company_rects, key=lambda r: r.y1, default=None)
        ccx = (company.x0 + company.x1) / 2 if company is not None else None

        def occupied(r) -> bool:
            # Область, где встанут факсимиле (над/правее якоря) и печать
            spot = pymupdf.Rect(r.x0 - 20, r.y0 - 80, r.x1 + 130, r.y1 + 30)
            return any(spot.intersects(ir) for ir in img_rects)

        for word in ANCHOR_WORDS:
            rects = [r for r in page.search_for(word) if r.y0 > zone_top]
            free = [r for r in rects if not occupied(r)]
            if company is not None:
                # Своя колонка приоритетнее; свободные вне колонки — запасные
                own = [r for r in free
                       if abs((r.x0 + r.x1) / 2 - ccx) <= _COMPANY_COLUMN_MAX_DX]
                candidates = own or free
            else:
                candidates = free or rects
            if not candidates:
                continue
            if company is not None:
                # Та же колонка, что и наша компания; при равенстве — нижний
                rect = min(candidates, key=lambda r: abs((r.x0 + r.x1) / 2 - ccx) - 0.5 * r.y1)
            else:
                # Нет упоминания компании — самый правый и нижний
                rect = max(candidates, key=lambda r: r.x1 + 0.3 * r.y1)
            # Низ якоря → координата y от низа страницы
            return rect.x0, page_height - rect.y1, word
        if company is not None:
            # Якорных слов в своей колонке нет — привязываемся к названию
            # компании (под ним обычно черта подписи)
            return company.x0, page_height - company.y1, "ООО «ОРОН»"
    except Exception:
        logger.exception("Не удалось найти якоря в PDF")
    return None


def _matmul(m1, m2):
    a, b, c, d, e, f = m1
    g, h, i, j, k, l = m2
    return (a * g + c * h, b * g + d * h, a * i + c * j, b * i + d * j,
            a * k + c * l + e, b * k + d * l + f)


def _apply(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def _find_underscore_lines(page, page_height: float) -> list[tuple[float, float, float]]:
    """Текстовые черты «____»: вернуть [(x, y, width)] в нижней половине."""
    found: list[tuple[float, float, float]] = []

    def visitor(text, cm, tm, font, size):
        run = 0
        best = 0
        for ch in text:
            run = run + 1 if ch == "_" else 0
            best = max(best, run)
        if best < _MIN_UNDERSCORES:
            return
        x, y = _apply(cm, tm[4], tm[5])
        # Ширина черты: подчёркивание ~0.5em
        width = best * float(size) * 0.5
        if y < page_height / 2:
            found.append((x, y, width))

    try:
        page.extract_text(visitor_text=visitor)
    except Exception:
        logger.exception("Не удалось извлечь текстовые черты из PDF")
    return found


def _find_vector_lines(page, page_height: float, reader) -> list[tuple[float, float, float]]:
    """Векторные черты: тонкие широкие re-прямоугольники и горизонтальные m/l."""
    from pypdf.generic import ContentStream

    found: list[tuple[float, float, float]] = []
    try:
        cs = ContentStream(page.get_contents(), reader)
    except Exception:
        logger.exception("Не удалось прочитать content stream PDF")
        return found

    ctm = (1, 0, 0, 1, 0, 0)
    stack: list = []
    path_start: tuple[float, float] | None = None
    for operands, op in cs.operations:
        if op == b"q":
            stack.append(ctm)
        elif op == b"Q":
            ctm = stack.pop() if stack else ctm
        elif op == b"cm":
            ctm = _matmul(ctm, tuple(float(o) for o in operands))
        elif op == b"m":
            path_start = _apply(ctm, float(operands[0]), float(operands[1]))
        elif op == b"l" and path_start is not None:
            x2, y2 = _apply(ctm, float(operands[0]), float(operands[1]))
            if abs(y2 - path_start[1]) <= _MAX_LINE_H:
                w = abs(x2 - path_start[0])
                if w >= _MIN_LINE_W and path_start[1] < page_height / 2:
                    found.append((min(x2, path_start[0]), path_start[1], w))
            path_start = None
        elif op == b"re":
            x, y, w, h = (float(o) for o in operands)
            pts = [_apply(ctm, x, y), _apply(ctm, x + w, y),
                   _apply(ctm, x + w, y + h), _apply(ctm, x, y + h)]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            W = max(xs) - min(xs)
            H = max(ys) - min(ys)
            if W >= _MIN_LINE_W and H <= _MAX_LINE_H and min(ys) < page_height / 2:
                found.append((min(xs), min(ys), W))
    return found


def find_signature_line(reader, page) -> tuple[float, float, float] | None:
    """Найти черту подписи на странице: вернуть (x, y, width) самой правой.

    Правая черта в нижней части страницы — место подписи принципала
    (слева обычно подпись агента, уже стоящая). None, если не нашлось.
    """
    page_height = float(page.mediabox.height)
    lines = _find_underscore_lines(page, page_height)
    if not lines:
        lines = _find_vector_lines(page, page_height, reader)
    if not lines:
        return None
    # Сначала самые нижние черты (зона подписей внизу документа), среди них —
    # самая правая: там подпись принципала. Отсекает промежуточные черты
    # выше (например, подпись главбуха в УПД).
    min_y = min(ln[1] for ln in lines)
    lowest = [ln for ln in lines if ln[1] <= min_y + 60]
    return max(lowest, key=lambda ln: ln[0] + ln[2])


def _draw_caption(pdf, text: str, x: float, y: float, w: float, h: float) -> None:
    """Расшифровка подписи текстом с кириллицей, вписанная в рамку (w × h)."""
    from reportlab.pdfbase import pdfmetrics

    from .invoices import _FONTS, _register_fonts

    font = _register_fonts()["regular"]
    size = min(10.0, h * 0.7)
    while size > 5.0 and pdfmetrics.stringWidth(text, font, size) > w:
        size -= 0.5
    pdf.setFont(font, size)
    pdf.drawString(x, y + (h - size) / 2, text)


def _overlay_page(page_box, stamp_path: str, signature_path: str, caption: str,
                  line: tuple[float, float, float] | None,
                  anchor: tuple[float, float, str] | None = None,
                  placements: list | None = None) -> io.BytesIO:
    """Overlay-страница размером с целевую: печать и факсимиле у места подписи.

    placements (метки шаблона из stamp_templates.yaml) приоритетнее эвристик:
    каждая метка рисуется в позиции якорь + (dx, dy) с собственным размером.
    """
    from reportlab.pdfgen import canvas

    width = float(page_box.width)
    height = float(page_box.height)
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(width, height))

    if placements is not None and anchor is not None:
        ax, ay, _word = anchor
        for mark in placements:
            mx = max(5.0, min(ax + mark.dx, width - mark.w - 5))
            my = max(5.0, min(ay + mark.dy, height - mark.h - 5))
            if mark.role == "signature":
                path = signature_path
            elif mark.role == "stamp":
                path = stamp_path
            else:
                path = None
            if mark.role == "caption":
                if caption:
                    _draw_caption(pdf, caption, mx, my, mark.w, mark.h)
            elif path and Path(path).exists():
                pdf.drawImage(path, mx, my, width=mark.w, height=mark.h,
                              preserveAspectRatio=True, mask="auto")
            else:
                logger.warning("Картинка для метки %s не найдена: %s",
                               mark.role, path)
        pdf.showPage()  # страница есть, даже если ничего не нарисовали
        pdf.save()
        buf.seek(0)
        return buf

    sig_w, sig_h = 90, 60
    stamp_w, stamp_h = 100, 80
    if anchor is not None:
        ax, ay, _word = anchor
        # Факсимиле правее и ниже якоря, печать — внахлест правее и выше
        sig_x = ax + _SIG_DX
        sig_y = ay + _SIG_DY
        stamp_x = sig_x + sig_w - _STAMP_OVERLAP
        stamp_y = sig_y + _STAMP_DY
    elif line is not None:
        line_x, line_y, line_w = line
        # Факсимиле правее конца черты и ниже её уровня (квадрат между
        # чертой и печатью), печать — правее подписи, с клампом у края
        sig_x = line_x + line_w - 20
        sig_y = line_y - 55
        stamp_x = min(sig_x + sig_w + 10, width - stamp_w - 10)
        stamp_y = sig_y - 25
    else:
        # Fallback: правый нижний угол
        sig_x = width - sig_w - 60
        sig_y = 110
        stamp_x = sig_x + sig_w - 20
        stamp_y = sig_y - 20
    # Кламп в поля страницы
    sig_x = max(10.0, min(sig_x, width - sig_w - 10))
    sig_y = max(10.0, min(sig_y, height - sig_h - 10))
    stamp_x = max(10.0, min(stamp_x, width - stamp_w - 10))
    stamp_y = max(10.0, min(stamp_y, height - stamp_h - 10))
    if signature_path and Path(signature_path).exists():
        pdf.drawImage(
            signature_path, sig_x, sig_y, width=sig_w, height=sig_h,
            preserveAspectRatio=True, mask="auto",
        )
    else:
        logger.warning("Факсимиле не найдено: %s — рисуем без него", signature_path)
    if caption:
        _draw_caption(pdf, caption, sig_x, sig_y - 14, sig_w + 40, 12)
    if stamp_path and Path(stamp_path).exists():
        pdf.drawImage(
            stamp_path, stamp_x, stamp_y,
            width=stamp_w, height=stamp_h,
            preserveAspectRatio=True, mask="auto",
        )
    else:
        logger.warning("Печать не найдена: %s — рисуем без неё", stamp_path)

    pdf.showPage()  # страница есть, даже если ничего не нарисовали
    pdf.save()
    buf.seek(0)
    return buf


def stamp_pdf(data: bytes, settings: Settings, caption: str | None = None) -> bytes:
    """Поставить печать и факсимиле на последнюю страницу PDF, вернуть байты."""
    from pypdf import PdfReader, PdfWriter

    if caption is None:
        caption = getattr(settings, "SIGNATURE_CAPTION", "") or ""
    stamp_path = getattr(settings, "INVOICE_STAMP_PATH", "") or ""
    signature_path = getattr(settings, "INVOICE_SIGNATURE_PATH", "") or ""

    reader = PdfReader(io.BytesIO(data))
    writer = PdfWriter()
    last_index = len(reader.pages) - 1
    # Шаблон из разметки владельца (stamp_templates.yaml) — точные позиции
    from .stamp_templates import load_templates, match_template

    templates_path = getattr(settings, "STAMP_TEMPLATES_FILE", "stamp_templates.yaml")
    template = match_template(data, load_templates(templates_path))
    if template:
        logger.info("Документ совпал с шаблоном печати %r (%d меток)",
                    template.name, len(template.marks))
    for i, page in enumerate(reader.pages):
        if i == last_index:
            page_height = float(page.mediabox.height)
            anchor = find_anchor(data, i, page_height)
            placements = None
            if template and anchor:
                placements = template.marks
            elif template:
                logger.warning("Шаблон %r есть, но якорь не найден — эвристики",
                               template.name)
            line = None if anchor else find_signature_line(reader, page)
            if anchor:
                logger.info("Якорь подписи найден: %r @ x=%.0f y=%.0f", anchor[2], anchor[0], anchor[1])
            elif line:
                logger.info("Черта подписи найдена: x=%.0f y=%.0f w=%.0f", *line)
            else:
                logger.info("Ни якорь, ни черта не найдены — fallback в правый нижний угол")
            overlay = PdfReader(
                _overlay_page(page.mediabox, stamp_path, signature_path, caption,
                              line, anchor, placements)
            ).pages[0]
            page.merge_page(overlay)
        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    logger.info("Печать и факсимиле наложены (страниц: %d)", len(reader.pages))
    return out.getvalue()
