"""Print saved meaning-writing tests; no AI or question generation is used."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

from .render import (
    APPROVED_STATES, BODY_SIZE, COLUMN_GAP, COLUMN_WIDTH, LayoutError, MARGIN,
    PAGE_HEIGHT, PAGE_WIDTH, _fonts, _stage_pdf_pair, _scope_label, _text, _wrap,
)

TABLE_WIDTH = COLUMN_WIDTH
CELL_WIDTHS = (10 * mm, 36 * mm, TABLE_WIDTH - 46 * mm)
TABLE_HEADER_HEIGHT = 8 * mm
ROW_HEIGHT = 13 * mm
CELL_PADDING = 4.0
BOTTOM = 20 * mm
CONTINUATION_TOP = PAGE_HEIGHT - 20 * mm
ANSWER_SIZES = (10.0, 9.0, 8.0)


@dataclass(frozen=True)
class WritingHeader:
    title_lines: tuple[str, ...]
    scope_lines: tuple[str, ...]
    table_top: float


@dataclass(frozen=True)
class WritingRow:
    question_id: str
    number: int
    word_lines: tuple[str, ...]
    answer_lines: tuple[str, ...]
    answer_size: float
    height: float


def _validate(worksheet: dict[str, Any]) -> list[dict[str, Any]]:
    questions = worksheet.get("questions")
    if not isinstance(questions, list) or not questions:
        raise LayoutError("출력할 뜻 쓰기 문항이 없습니다.")
    ids = set()
    for question in questions:
        if not isinstance(question, dict) or not isinstance(question.get("id"), str) or not question["id"] or question["id"] in ids:
            raise LayoutError("문항 ID가 없거나 중복되었습니다.")
        ids.add(question["id"])
        for key, label in (("word", "단어"), ("answer_text", "정답 뜻")):
            if not isinstance(question.get(key), str) or not question[key].strip():
                raise LayoutError(f"{label}이 비어 있습니다.")
    return questions


def _header_layout(worksheet: dict[str, Any], regular: str, bold: str, answer: bool) -> WritingHeader:
    title = _text(worksheet.get("title")) or "뜻 쓰기 시험"
    titles = tuple(_wrap(title, bold, 16, PAGE_WIDTH - 2 * MARGIN))
    scope = _scope_label(worksheet)
    label = "뜻 쓰기 시험 정답지" if answer else "뜻 쓰기 시험"
    scopes = tuple(_wrap((scope + " · " if scope else "") + label, regular, BODY_SIZE, PAGE_WIDTH - 2 * MARGIN))
    top = PAGE_HEIGHT - MARGIN - 42 - 20 * len(titles) - 6 - 14 * len(scopes) - 57
    if top - BOTTOM - TABLE_HEADER_HEIGHT < ROW_HEIGHT:
        raise LayoutError("시험지 제목이나 범위가 너무 깁니다. 인쇄할 제목과 범위를 줄이세요.")
    return WritingHeader(titles, scopes, top)


def _logo(pdf: canvas.Canvas, x: float, y: float, size: float) -> None:
    """Reuse the product SVG as monochrome vectors without another dependency."""
    from reportlab.graphics import renderPDF
    from reportlab.graphics.shapes import Circle, Drawing
    from reportlab.graphics.svgpath import SvgPath
    from reportlab.lib.colors import black

    package_parent = Path(__file__).resolve().parent.parent
    source = package_parent / "static" / "brand" / "logo.svg"
    if not source.is_file():
        raise LayoutError("기존 브랜드 로고 파일을 찾을 수 없습니다.")
    root = ET.parse(source).getroot()
    drawing = Drawing(200, 200)
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "path":
            drawing.add(SvgPath(element.attrib["d"], fillColor=black, strokeColor=None))
        elif tag == "circle":
            drawing.add(Circle(float(element.attrib["cx"]), float(element.attrib["cy"]), float(element.attrib["r"]), fillColor=black, strokeColor=None))
    pdf.saveState()
    pdf.translate(x, y + size)
    pdf.scale(size / 200, -size / 200)
    renderPDF.draw(drawing, pdf, 0, 0)
    pdf.restoreState()


def _draw_header(pdf: canvas.Canvas, worksheet: dict[str, Any], header: WritingHeader, regular: str, bold: str, answer: bool) -> None:
    y = PAGE_HEIGHT - MARGIN
    _logo(pdf, MARGIN, y - 25, 26)
    pdf.setFont(bold, 10)
    pdf.drawString(MARGIN + 29, y - 15, "Melody · English")
    y -= 42
    pdf.setFont(bold, 16)
    for line in header.title_lines:
        pdf.drawCentredString(PAGE_WIDTH / 2, y - 16, line)
        y -= 20
    y -= 6
    pdf.setFont(regular, BODY_SIZE)
    for line in header.scope_lines:
        pdf.drawCentredString(PAGE_WIDTH / 2, y - BODY_SIZE, line)
        y -= 14
    y -= 10
    pdf.drawString(MARGIN, y - BODY_SIZE, "이름:")
    count = len(worksheet["questions"])
    score_right = PAGE_WIDTH - MARGIN
    score_width = pdfmetrics.stringWidth(f"점수: ______ / {count}", regular, BODY_SIZE)
    # Keep the original writing space and label positions without printing a line.
    pdf.drawString(score_right - score_width, y - BODY_SIZE, "점수:")
    pdf.drawRightString(score_right, y - BODY_SIZE, f"/ {count}")
    pdf.setLineWidth(0.6)
    pdf.line(MARGIN, y - 18, PAGE_WIDTH - MARGIN, y - 18)
    pdf.drawString(MARGIN, y - 35, "정답 뜻을 확인하세요." if answer else "주어진 어휘의 뜻을 쓰시오.")


def _row(question: dict[str, Any], number: int, regular: str, bold: str, answer: bool) -> WritingRow:
    word_lines = tuple(_wrap(_text(question["word"]), bold, BODY_SIZE, CELL_WIDTHS[1] - 2 * CELL_PADDING, prefer_word_boundaries=True))
    answer_lines: tuple[str, ...] = ()
    size = BODY_SIZE
    if answer:
        text = question["answer_text"].replace("\r\n", "\n").replace("\r", "\n")
        for size in ANSWER_SIZES:
            answer_lines = tuple(_wrap(text, regular, size, CELL_WIDTHS[2] - 2 * CELL_PADDING))
            if len(answer_lines) * size * 1.3 <= ROW_HEIGHT - 2 * CELL_PADDING:
                break
    height = max(ROW_HEIGHT, len(word_lines) * BODY_SIZE * 1.3 + 2 * CELL_PADDING, len(answer_lines) * size * 1.3 + 2 * CELL_PADDING)
    return WritingRow(question["id"], number, word_lines, answer_lines, size, height)


def _paginate(rows: list[WritingRow], first_top: float) -> list[tuple[float, list[WritingRow], list[WritingRow], tuple[float, ...]]]:
    pages = []
    offset = 0
    while offset < len(rows):
        top = first_top if not pages else CONTINUATION_TOP
        capacity = top - TABLE_HEADER_HEIGHT - BOTTOM
        placed = None
        # Keep 30 on a page when possible; reduce only after measuring content.
        for count in range(min(30, len(rows) - offset), 0, -1):
            candidate = rows[offset:offset + count]
            split = (count + 1) // 2
            left, right = candidate[:split], candidate[split:]
            heights = tuple(max(left[index].height, right[index].height if index < len(right) else 0) for index in range(len(left)))
            if sum(heights) <= capacity + 0.001:
                placed = (top, left, right, heights)
                break
        if placed is None:
            if not pages and rows[offset].height + TABLE_HEADER_HEIGHT <= CONTINUATION_TOP - BOTTOM:
                # A very long header may consume a whole first page, but never
                # publish an empty cover page silently.
                raise LayoutError("첫 문항을 넣을 공간이 부족합니다. 시험지 제목이나 범위를 줄이세요.")
            raise LayoutError(f"{rows[offset].number}번 문항이 한 페이지를 초과합니다. 단어나 정답 뜻을 수정하세요.")
        pages.append(placed)
        offset += len(placed[1]) + len(placed[2])
    return pages


def _cell_text(pdf: canvas.Canvas, lines: tuple[str, ...], x: float, top: float, height: float, font: str, size: float, *, center: bool = False, width: float = 0) -> None:
    if not lines:
        return
    leading = size * 1.3
    ascent, descent = pdfmetrics.getAscentDescent(font, size)
    block_height = ascent - descent + leading * (len(lines) - 1)
    baseline = top - (height - block_height) / 2 - ascent
    pdf.setFont(font, size)
    for line in lines:
        if center:
            pdf.drawCentredString(x + width / 2, baseline, line)
        else:
            pdf.drawString(x + CELL_PADDING, baseline, line)
        baseline -= leading


def _draw_table(pdf: canvas.Canvas, rows: list[WritingRow], heights: tuple[float, ...], x: float, top: float, regular: str, bold: str, page_number: int, column: int) -> list[dict[str, Any]]:
    if not rows:
        return []
    pdf.setFillGray(0.91)
    pdf.rect(x, top - TABLE_HEADER_HEIGHT, TABLE_WIDTH, TABLE_HEADER_HEIGHT, stroke=0, fill=1)
    pdf.setFillGray(0)
    current_x = x
    for text, width in zip(("번호", "단어", "뜻"), CELL_WIDTHS):
        _cell_text(pdf, (text,), current_x, top, TABLE_HEADER_HEIGHT, bold, BODY_SIZE, center=True, width=width)
        current_x += width
    table_bottom = top - TABLE_HEADER_HEIGHT - sum(heights[:len(rows)])
    pdf.setStrokeGray(0.38)
    pdf.setLineWidth(0.5)
    pdf.rect(x, table_bottom, TABLE_WIDTH, top - table_bottom, stroke=1, fill=0)
    current_x = x
    for width in CELL_WIDTHS[:-1]:
        current_x += width
        pdf.line(current_x, top, current_x, table_bottom)
    current_y = top - TABLE_HEADER_HEIGHT
    pdf.line(x, current_y, x + TABLE_WIDTH, current_y)
    result = []
    for row, height in zip(rows, heights):
        _cell_text(pdf, (str(row.number),), x, current_y, height, regular, BODY_SIZE, center=True, width=CELL_WIDTHS[0])
        _cell_text(pdf, row.word_lines, x + CELL_WIDTHS[0], current_y, height, bold, BODY_SIZE)
        _cell_text(pdf, row.answer_lines, x + sum(CELL_WIDTHS[:2]), current_y, height, regular, row.answer_size)
        result.append({"question_id": row.question_id, "question_number": row.number, "page": page_number, "column": column,
                       "x": x, "top": current_y, "bottom": current_y - height, "content_bottom": current_y - height,
                       "content_height": height, "height": height, "width": TABLE_WIDTH, "min_font_size": row.answer_size if row.answer_lines else BODY_SIZE,
                       "word_lines": len(row.word_lines), "answer_lines": len(row.answer_lines), "answer_font_size": row.answer_size,
                       "choice_columns": 0, "choice_line_count": 0, "gap_after": 0.0})
        current_y -= height
        pdf.line(x, current_y, x + TABLE_WIDTH, current_y)
    pdf.setStrokeGray(0)
    return result


def _write_pdf(path: Path, worksheet: dict[str, Any], pages: list, header: WritingHeader, regular: str, bold: str, answer: bool) -> list[dict[str, Any]]:
    pdf = canvas.Canvas(str(path), pagesize=A4, pageCompression=1)
    pdf.setTitle(_text(worksheet.get("title")) + (" 정답지" if answer else " 문제지"))
    pdf.setAuthor("멜로디잉글리시")
    metrics = []
    for page_number, (top, left, right, heights) in enumerate(pages, 1):
        if page_number == 1:
            _draw_header(pdf, worksheet, header, regular, bold, answer)
        for column, rows in enumerate((left, right)):
            metrics.extend(_draw_table(pdf, rows, heights, MARGIN + column * (TABLE_WIDTH + COLUMN_GAP), top, regular, bold, page_number, column))
        if answer and len(pages) > 1:
            pdf.setFont(regular, 9)
            pdf.drawCentredString(PAGE_WIDTH / 2, MARGIN - 4, f"- {page_number} -")
        pdf.showPage()
    pdf.save()
    return metrics


def render_writing_worksheet(worksheet: dict[str, Any], student_path: str | Path, answer_path: str | Path, *, font_path: str | Path | None = None, bold_font_path: str | Path | None = None, draft: bool | None = None) -> dict[str, Any]:
    questions = _validate(worksheet)
    unreviewed = any(q.get("review_status") not in APPROVED_STATES | {"source_verified"} for q in questions)
    if draft is False and unreviewed:
        raise ValueError("검토하지 않은 문항이 있습니다. 검토를 완료하거나 초안으로 출력하세요.")
    regular, bold = _fonts(font_path, bold_font_path)
    student, answers = Path(student_path).resolve(), Path(answer_path).resolve()
    if student == answers:
        raise ValueError("문제지와 정답지는 서로 다른 파일이어야 합니다.")
    student_header, answer_header = (_header_layout(worksheet, regular, bold, answer) for answer in (False, True))
    student_pages = _paginate([_row(q, n, regular, bold, False) for n, q in enumerate(questions, 1)], student_header.table_top)
    answer_pages = _paginate([_row(q, n, regular, bold, True) for n, q in enumerate(questions, 1)], answer_header.table_top)
    with _stage_pdf_pair(student, answers) as (student_tmp, answer_tmp):
        student_metrics = _write_pdf(student_tmp, worksheet, student_pages, student_header, regular, bold, False)
        answer_metrics = _write_pdf(answer_tmp, worksheet, answer_pages, answer_header, regular, bold, True)
    return {"student_path": str(student), "answer_path": str(answers), "student_pages": len(student_pages), "answer_pages": len(answer_pages),
            "draft": unreviewed if draft is None else bool(draft), "layout": {"student": student_metrics, "answers": answer_metrics}}
