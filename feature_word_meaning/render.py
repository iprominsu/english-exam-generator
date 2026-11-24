"""Print a saved word/meaning worksheet without changing its choices or answers.

Only saved content is rendered: this module never generates or reviews questions.
Korean TrueType fonts are required. Set MELODY_PDF_FONT and
MELODY_PDF_BOLD_FONT for a server deployment (for example Noto Sans KR TTF).
"""
from __future__ import annotations

import os
import logging
import re
import shutil
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import combinations, combinations_with_replacement
from pathlib import Path
from typing import Any

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 12 * mm
COLUMN_GAP = 9 * mm
COLUMN_WIDTH = (PAGE_WIDTH - 2 * MARGIN - COLUMN_GAP) / 2
HEADER_CELL_WIDTHS = (33 * mm, 120 * mm, 33 * mm)
HEADER_MIN_HEIGHT = 10 * mm
HEADER_BODY_GAP = 39.0
BODY_TOP = PAGE_HEIGHT - MARGIN - HEADER_MIN_HEIGHT - HEADER_BODY_GAP
BODY_BOTTOM = 21 * mm
FOOTER_RULE_Y = BODY_BOTTOM - 6 * mm
DIVIDER_RULE_GAP = 7.0
BODY_HEIGHT = BODY_TOP - BODY_BOTTOM
CONTINUATION_BODY_TOP = PAGE_HEIGHT - 20 * mm
BODY_SIZE = 10.0
LINE_HEIGHT = 16.0
WORD_SIZE = BODY_SIZE
WORD_LINE_HEIGHT = 17.0
QUESTION_GAP = 18.0
MIN_QUESTION_GAP = 4.0
OVERFLOW_QUESTION_GAP = 8.0
OPTION_SPACES = "  "
APPROVED_STATES = {"approved", "ai_reviewed", "human_reviewed"}
_FONT_LOCK = threading.Lock()


class LayoutError(ValueError):
    """A saved question cannot be printed legibly in the selected page format."""


def _publish_pdf_pair(staging: list[Path], destinations: list[Path]) -> None:
    """Restore the prior local pair if publishing either staged PDF fails.

    The files are replaced individually. This is rollback protection for local
    output files, not a transaction for multiple Drive uploads.
    """
    backups: list[tuple[Path, Path | None]] = []
    published: list[tuple[Path, Path | None]] = []
    preserve_backups = False
    try:
        for destination in destinations:
            backup = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.rollback") if destination.exists() else None
            backups.append((destination, backup))
            if backup is not None:
                shutil.copy2(destination, backup)
        for source, (destination, backup) in zip(staging, backups):
            os.replace(source, destination)
            published.append((destination, backup))
    except Exception as error:
        rollback_failed = False
        for destination, backup in reversed(published):
            try:
                if backup is None:
                    destination.unlink(missing_ok=True)
                else:
                    os.replace(backup, destination)
            except OSError:
                rollback_failed = True
        if rollback_failed:
            preserve_backups = True
            raise OSError("PDF 저장과 이전 파일 복구를 완료하지 못했습니다. 복구용 임시본을 보존했습니다.") from error
        raise
    finally:
        if not preserve_backups:
            for _, backup in backups:
                if backup is not None:
                    try:
                        backup.unlink(missing_ok=True)
                    except OSError as error:
                        logging.getLogger(__name__).warning("PDF rollback copy cleanup unavailable (%s)", type(error).__name__)


@contextmanager
def _stage_pdf_pair(student: Path, answers: Path):
    """두 임시 PDF 작성을 모두 마치면 함께 발행하고 임시 파일을 정리한다."""
    suffix = "." + uuid.uuid4().hex + ".tmp"
    destinations = [student, answers]
    staging = [path.with_name(path.name + suffix) for path in destinations]
    try:
        for path in destinations:
            path.parent.mkdir(parents=True, exist_ok=True)
        yield staging
        _publish_pdf_pair(staging, destinations)
    finally:
        for path in staging:
            path.unlink(missing_ok=True)


@dataclass(frozen=True)
class Line:
    text: str
    x: float
    offset: float
    font: str
    size: float
    option_number: int | None = None
    option_width: float = 0.0


@dataclass(frozen=True)
class Block:
    question_id: str
    question_number: int
    lines: tuple[Line, ...]
    height: float
    word_indent: float = 0.0
    word_lines: int = 1
    choice_columns: int = 0
    choice_line_count: int = 0

    @property
    def content_height(self) -> float:
        """The final text line box, excluding the legacy trailing spacing."""
        return self.height - QUESTION_GAP


@dataclass(frozen=True)
class Header:
    title_lines: tuple[str, ...]
    scope_lines: tuple[str, ...]
    scope_y: float
    body_top: float
    box_height: float = HEADER_MIN_HEIGHT


def _fonts(font_path: str | Path | None, bold_font_path: str | Path | None) -> tuple[str, str]:
    regular = font_path or os.environ.get("MELODY_PDF_FONT")
    bold = bold_font_path or os.environ.get("MELODY_PDF_BOLD_FONT")
    if not regular:
        for candidate in (
            "C:/Windows/Fonts/malgun.ttf",
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansKR-Regular.ttf",
        ):
            if Path(candidate).is_file():
                regular = candidate
                break
    if not regular or not Path(regular).is_file():
        raise LayoutError("한국어 PDF 글꼴이 없습니다. MELODY_PDF_FONT에 한국어 TrueType 글꼴 경로를 설정하세요.")
    if not bold:
        regular_path = Path(regular)
        candidates = (
            regular_path.with_name("malgunbd.ttf"),
            regular_path.with_name("NanumGothicBold.ttf"),
            regular_path.with_name(regular_path.name.replace("Regular", "Bold")),
        )
        bold = next((p for p in candidates if p.is_file() and p != regular_path), regular)
    if not Path(bold).is_file():
        raise LayoutError("MELODY_PDF_BOLD_FONT의 한국어 굵은 글꼴 파일을 찾을 수 없습니다.")
    import hashlib

    names = []
    with _FONT_LOCK:
        for role, path in (("regular", regular), ("bold", bold)):
            digest = hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()[:16]
            name = f"Melody-{role}-{digest}"
            if name not in pdfmetrics.getRegisteredFontNames():
                try:
                    pdfmetrics.registerFont(TTFont(name, str(path)))
                except Exception as exc:
                    raise LayoutError(f"PDF용 한국어 TrueType 글꼴을 읽을 수 없습니다: {Path(path).name}") from exc
            names.append(name)
    return names[0], names[1]


def _text(value: Any) -> str:
    return re.sub(r"[\t\r ]+", " ", str(value or "")).strip()


def _wrap(text: str, font: str, size: float, width: float, *, prefer_word_boundaries: bool = False) -> list[str]:
    """Wrap Korean or English text, preserving explicit lines and word breaks."""
    glyphs = pdfmetrics.getFont(font).face.charToGlyph
    missing = sorted({char for char in text if not char.isspace() and not glyphs.get(ord(char), 0)})
    if missing:
        displayed = ", ".join(f"U+{ord(char):04X}" for char in missing[:8])
        raise LayoutError(f"PDF 글꼴에서 표시할 수 없는 문자가 있습니다 ({displayed}). 문항 또는 한국어 글꼴을 확인하세요.")
    result: list[str] = []
    for paragraph in text.split("\n"):
        remaining = paragraph.strip()
        if not remaining:
            result.append("")
            continue
        while remaining:
            if pdfmetrics.stringWidth(remaining, font, size) <= width:
                result.append(remaining)
                break
            low, high = 1, len(remaining)
            while low < high:
                middle = (low + high + 1) // 2
                if pdfmetrics.stringWidth(remaining[:middle], font, size) <= width:
                    low = middle
                else:
                    high = middle - 1
            if pdfmetrics.stringWidth(remaining[:low], font, size) > width:
                raise LayoutError("한 글자가 인쇄 폭을 초과합니다. 글꼴과 문항 내용을 확인하세요.")
            end = low
            # Prefer an English word boundary without sacrificing most of a line.
            last_space = remaining.rfind(" ", 0, low + 1)
            keep_word = False
            if prefer_word_boundaries and last_space > 0:
                # Word cells may move a whole fitting word to the next line.
                # A single oversized word still uses the existing hard wrap.
                next_word = remaining[last_space + 1:].split(" ", 1)[0]
                keep_word = bool(next_word) and pdfmetrics.stringWidth(next_word, font, size) <= width
            if last_space >= max(1, int(low * 0.45)) or keep_word:
                end = last_space
            result.append(remaining[:end].rstrip())
            remaining = remaining[end:].lstrip()
    return result


def _circled(index: int, font: str | None = None) -> str:
    if 1 <= index <= 20:
        code = 0x2460 + index - 1
        if font is None or pdfmetrics.getFont(font).face.charToGlyph.get(code, 0):
            return chr(code)
    # Both PDFs use text labels; fall back to plain text for a number which
    # has no circled glyph in the selected font.
    return f"({index})"


def _validate(worksheet: dict[str, Any]) -> list[dict[str, Any]]:
    questions = worksheet.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("인쇄할 문항이 없습니다.")
    seen: set[str] = set()
    for number, question in enumerate(questions, 1):
        qid = str(question.get("id", number))
        if qid in seen:
            raise ValueError(f"중복 문항 ID: {qid}")
        seen.add(qid)
        if not _text(question.get("word")):
            raise ValueError(f"{number}번 문항의 영단어가 비어 있습니다.")
        choices = question.get("choices")
        if not isinstance(choices, list) or len(choices) < 2:
            raise ValueError(f"{number}번 문항에는 두 개 이상의 보기가 필요합니다.")
        ids = [str(choice.get("id", "")) for choice in choices]
        if any(not choice_id for choice_id in ids) or len(ids) != len(set(ids)):
            raise ValueError(f"{number}번 문항의 보기 ID가 비어 있거나 중복되었습니다.")
        if any(not _text(choice.get("text")) for choice in choices):
            raise ValueError(f"{number}번 문항의 보기 내용이 비어 있습니다.")
        correct_ids = question.get("correct_choice_ids")
        if not isinstance(correct_ids, (list, set, tuple)) or not correct_ids:
            raise ValueError(f"{number}번 문항의 정답이 없습니다.")
        if any(str(choice_id) not in ids for choice_id in correct_ids):
            raise ValueError(f"{number}번 문항의 정답 ID가 보기에 없습니다.")
        if len(set(map(str, correct_ids))) != len(correct_ids):
            raise ValueError(f"{number}번 문항의 정답 ID가 중복되었습니다.")
    return questions


def _unit_label(question: dict[str, Any]) -> str:
    value = question.get("source_unit_labels") or question.get("unit_label")
    if isinstance(value, list):
        return ", ".join(dict.fromkeys(_display_scope_label(_text(part)) for part in value if _text(part)))
    return _display_scope_label(_text(value))


def _display_scope_label(value: str) -> str:
    for pages, suffix in ((r"41\s*(?:쪽|번)?\s*[-~–—]\s*50\s*(?:쪽|번)?", "a"), (r"51\s*(?:쪽|번)?\s*[-~–—]\s*60\s*(?:쪽|번)?", "b")):
        value = re.sub(r"11\s*강\s*" + pages, "11강(" + suffix + ")", value)
    return value


def _scope_label(worksheet: dict[str, Any]) -> str:
    selected = set(worksheet.get("selected_unit_ids", []))
    labels = [_display_scope_label(_text(u.get("label"))) for u in worksheet.get("selected_units", []) if not selected or u.get("id") in selected]
    if not labels:
        raw = _display_scope_label(_text(worksheet.get("range_label")))
        labels = re.split(r"\s*[·,]\s*", raw) if raw else [_unit_label(q) for q in worksheet.get("questions", [])]
    labels = list(dict.fromkeys(label for label in labels if label))
    # Compact consecutive lessons while retaining explicit subdivisions and names.
    result = []
    index = 0
    while index < len(labels):
        day = re.fullmatch(r"DAY\s*(\d+)", labels[index], re.IGNORECASE)
        if day:
            end = index
            while end + 1 < len(labels):
                following = re.fullmatch(r"DAY\s*(\d+)", labels[end + 1], re.IGNORECASE)
                if not following or int(following[1]) != int(day[1]) + end + 1 - index:
                    break
                end += 1
            first = int(day[1])
            result.append(f"DAY {first}~{first + end - index}" if end > index else f"DAY {first}")
            index = end + 1
            continue
        match = re.fullmatch(r"(\d+)강(.*)", labels[index])
        end = index
        if match and not match[2].strip().startswith("("):
            while end + 1 < len(labels):
                following = re.fullmatch(r"(\d+)강(.*)", labels[end + 1])
                if not following or following[2] != match[2] or int(following[1]) != int(match[1]) + end + 1 - index:
                    break
                end += 1
        result.append(f"{match[1]}~{int(match[1]) + end - index}강{match[2]}" if end > index else labels[index])
        index = end + 1
    return " · ".join(result)


def _common_unit_label(questions: list[dict[str, Any]]) -> str:
    labels = [_unit_label(question) for question in questions]
    return labels[0] if labels and labels[0] and all(label == labels[0] for label in labels) else ""


def _answer_lines(correct: list[tuple[int, dict[str, Any]]], regular: str) -> list[str]:
    """Keep each answer number attached to its meaning when breaking lines."""
    result: list[str] = []
    current = ""
    for index, choice in correct:
        pair = _circled(index, regular) + OPTION_SPACES + _text(choice["text"])
        # Validate every pair, including those which fit beside another answer.
        wrapped = _wrap(pair, regular, BODY_SIZE, COLUMN_WIDTH)
        combined = current + " · " + pair if current else pair
        if len(wrapped) == 1 and pdfmetrics.stringWidth(combined, regular, BODY_SIZE) <= COLUMN_WIDTH:
            current = combined
            continue
        if current:
            result.append(current)
        result.extend(wrapped[:-1])
        current = wrapped[-1]
    if current:
        result.append(current)
    return result


def _choice_rows(widths: list[float], texts: list[str], width: float, gap: float) -> tuple[int, ...] | None:
    """Choose the requested five-choice pattern, preserving answer order."""
    if len(widths) != 5:
        return None

    def row_widths(pattern: tuple[int, ...]) -> list[float] | None:
        start = 0
        result = []
        for count in pattern:
            end = start + count
            if any("\n" in text for text in texts[start:end]):
                return None
            used = sum(widths[start:end]) + gap * (count - 1)
            if used > width:
                return None
            result.append(used)
            start = end
        return result

    for pattern in ((3, 2), (2, 3), (4, 1), (2, 2, 1)):
        if row_widths(pattern) is not None:
            return pattern
    for rows in (2, 3):
        candidates = []
        for cuts in combinations(range(1, 5), rows - 1):
            bounds = (0, *cuts, 5)
            pattern = tuple(bounds[i + 1] - bounds[i] for i in range(rows))
            used = row_widths(pattern)
            if used is not None:
                candidates.append(((max(used) - min(used), tuple(-n for n in pattern)), pattern))
        if candidates:
            return min(candidates)[1]
    return None


def _block(
    question: dict[str, Any], number: int, regular: str, bold: str, answer: bool,
    body_height: float = BODY_HEIGHT, *, word_indent: float | None = None,
    show_unit_label: bool = True,
) -> Block:
    lines: list[Line] = []
    offset = 0.0
    number_text = f"{number}."
    word_indent = word_indent if word_indent is not None else pdfmetrics.stringWidth(number_text + " ", bold, WORD_SIZE)
    lines.append(Line(number_text, 0, 0, bold, WORD_SIZE))
    word_lines = _wrap(_text(question["word"]), bold, WORD_SIZE, COLUMN_WIDTH - word_indent)
    for line in word_lines:
        lines.append(Line(line, word_indent, offset, bold, WORD_SIZE))
        offset += WORD_LINE_HEIGHT
    offset += 2
    choice_columns = 0
    choice_line_count = 0
    if answer:
        correct_ids = set(map(str, question["correct_choice_ids"]))
        correct = [(index, choice) for index, choice in enumerate(question["choices"], 1) if str(choice["id"]) in correct_ids]
        for line in _answer_lines(correct, regular):
            lines.append(Line(line, 0, offset, regular, BODY_SIZE))
            offset += LINE_HEIGHT
            choice_line_count += 1
        label = _unit_label(question) if show_unit_label else ""
        if label:
            for line in _wrap("범위: " + label, regular, BODY_SIZE, COLUMN_WIDTH):
                lines.append(Line(line, 0, offset, regular, BODY_SIZE))
                offset += LINE_HEIGHT
    else:
        # Pack as many complete choices as fit. Move a choice to the next row
        # before wrapping; only a choice wider than a full column may wrap.
        choices = question["choices"]
        option_gap = 12.0
        labels = [_circled(index, regular) for index in range(1, len(choices) + 1)]
        option_indent = max(pdfmetrics.stringWidth(label + OPTION_SPACES, regular, BODY_SIZE) for label in labels)
        rendered = [_text(choice["text"]) for choice in choices]
        wrapped_choices = [_wrap(text, regular, BODY_SIZE, COLUMN_WIDTH - option_indent) for text in rendered]
        widths = [option_indent + pdfmetrics.stringWidth(text, regular, BODY_SIZE) for text in rendered]
        pattern = _choice_rows(widths, rendered, COLUMN_WIDTH, option_gap)
        breaks = {sum(pattern[:i]) for i in range(1, len(pattern))} if pattern else set()
        choice_columns = 1
        row_x, row_count = 0.0, 0
        for index, text in enumerate(rendered):
            oversized = "\n" in text or widths[index] > COLUMN_WIDTH
            if row_count and (index in breaks or oversized or row_x + widths[index] > COLUMN_WIDTH):
                offset += LINE_HEIGHT
                choice_line_count += 1
                row_x, row_count = 0.0, 0
            if oversized:
                wrapped = wrapped_choices[index]
                lines.append(Line(wrapped[0], 0, offset, regular, BODY_SIZE, index + 1, option_indent))
                offset += LINE_HEIGHT
                choice_line_count += 1
                for line in wrapped[1:]:
                    lines.append(Line(line, option_indent, offset, regular, BODY_SIZE))
                    offset += LINE_HEIGHT
                    choice_line_count += 1
            else:
                lines.append(Line(text, row_x, offset, regular, BODY_SIZE, index + 1, option_indent))
                row_x += widths[index] + option_gap
                row_count += 1
                choice_columns = max(choice_columns, row_count)
        if row_count:
            offset += LINE_HEIGHT
            choice_line_count += 1
    height = offset + QUESTION_GAP
    if offset > body_height:
        raise LayoutError(f"{number}번 '{_text(question['word'])[:60]}' 문항이 한 단의 높이를 초과합니다. 뜻 보기 내용을 줄이거나 문항을 나누세요. (최소 본문 {BODY_SIZE:g}pt)")
    return Block(str(question.get("id", number)), number, tuple(lines), height, word_indent, len(word_lines), choice_columns, choice_line_count)


def _paginate(blocks: list[Block], body_top: float = BODY_TOP) -> list[list[tuple[int, float, Block]]]:
    """Prefer one or two balanced pages, then continue without a page limit."""
    if not blocks:
        return []
    count = len(blocks)
    prefix = [0.0]
    for block in blocks:
        prefix.append(prefix[-1] + block.content_height)
    tolerance = 1e-7

    for page_count in (1, 2):
        column_count = page_count * 2
        tops = [body_top, body_top] + ([CONTINUATION_BODY_TOP] * 2 if page_count == 2 else [])
        capacities = [top - BODY_BOTTOM for top in tops]
        # Skip impossible one/two-page partitions before exploring them.
        minimum_total = prefix[-1] + MIN_QUESTION_GAP * max(0, count - column_count)
        if minimum_total > sum(capacities) + tolerance:
            continue
        best_score: tuple[Any, ...] | None = None
        best_boundaries: tuple[int, ...] | None = None
        for cuts in combinations_with_replacement(range(1, count + 1), column_count - 1):
            boundaries = (0, *cuts, count)
            counts = [boundaries[index + 1] - boundaries[index] for index in range(column_count)]
            # Repeated cuts allow an empty column only when needed, for example
            # a single question or a tall question on the continuation page.
            heights = [prefix[boundaries[index + 1]] - prefix[boundaries[index]] for index in range(column_count)]
            if any(height + MIN_QUESTION_GAP * max(0, size - 1) > capacity + tolerance for height, size, capacity in zip(heights, counts, capacities)):
                continue
            gaps = [(capacity - height) / (size - 1) for height, size, capacity in zip(heights, counts, capacities) if size > 1]
            mean_gap = sum(gaps) / len(gaps) if gaps else 0.0
            fill_ratios = [height / capacity for height, size, capacity in zip(heights, counts, capacities) if size]
            mean_fill = sum(fill_ratios) / len(fill_ratios)
            score = (
                sum((size * column_count - count) ** 2 for size in counts),
                round(sum((gap - mean_gap) ** 2 for gap in gaps), 9),
                round(sum((ratio - mean_fill) ** 2 for ratio in fill_ratios), 12),
                tuple(-size for size in counts),
                cuts,
            )
            if best_score is None or score < best_score:
                best_score, best_boundaries = score, boundaries
        if best_boundaries is None:
            continue
        pages: list[list[tuple[int, float, Block]]] = [[] for _ in range(page_count)]
        for index in range(column_count):
            start, end = best_boundaries[index:index + 2]
            size = end - start
            if not size:
                continue
            content_height = prefix[end] - prefix[start]
            gap = (capacities[index] - content_height) / (size - 1) if size > 1 else 0.0
            for offset, block in enumerate(blocks[start:end]):
                top = tops[index] - (prefix[start + offset] - prefix[start]) - offset * gap
                if size > 1 and offset == size - 1:
                    top = BODY_BOTTOM + block.content_height
                pages[index // 2].append((index % 2, top, block))
        return pages
    # Keep complete questions and reading order, adding continuation columns
    # as needed. Greedy filling minimizes the number of overflow pages without
    # extending the combinatorial partition search to three or more pages.
    columns: list[tuple[float, int, int]] = []
    cursor = 0
    while cursor < count:
        top = body_top if len(columns) < 2 else CONTINUATION_BODY_TOP
        capacity = top - BODY_BOTTOM
        start = cursor
        used = 0.0
        column_gap = OVERFLOW_QUESTION_GAP if len(columns) >= 4 else MIN_QUESTION_GAP
        while cursor < count:
            needed = blocks[cursor].content_height + (column_gap if cursor > start else 0.0)
            if used + needed > capacity + tolerance:
                break
            used += needed
            cursor += 1
        if cursor == start and len(columns) >= 2:
            raise LayoutError(f"{blocks[cursor].question_number}번 문항이 한 단의 높이를 초과합니다. 뜻 보기 내용을 줄이거나 문항을 나누세요.")
        columns.append((top, start, cursor))
    pages = [[] for _ in range((len(columns) + 1) // 2)]
    for index, (top, start, end) in enumerate(columns):
        size = end - start
        if not size:
            continue
        content_height = prefix[end] - prefix[start]
        gap = OVERFLOW_QUESTION_GAP if index >= 4 else ((top - BODY_BOTTOM - content_height) / (size - 1) if size > 1 else 0.0)
        for offset, block in enumerate(blocks[start:end]):
            position = top - (prefix[start + offset] - prefix[start]) - offset * gap
            if index < 4 and size > 1 and offset == size - 1:
                position = BODY_BOTTOM + block.content_height
            pages[index // 2].append((index % 2, position, block))
    return pages


def _centered_text_y(bottom: float, height: float, font: str, size: float, count: int = 1, leading: float = 0.0) -> float:
    ascent, descent = pdfmetrics.getAscentDescent(font, size)
    return bottom + height / 2 + (count - 1) * leading / 2 - (ascent + descent) / 2


def _header_layout(worksheet: dict[str, Any], regular: str, bold: str, answer: bool) -> Header:
    title = _text(worksheet.get("title")) or "단어-뜻 시험"
    title_lines = _wrap(title, bold, 14, HEADER_CELL_WIDTHS[1] - 16)
    scope = _scope_label(worksheet)
    scope_lines = _wrap(scope, regular, BODY_SIZE, HEADER_CELL_WIDTHS[0] - 12)
    box_height = max(HEADER_MIN_HEIGHT, 14 + 18 * (len(title_lines) - 1) + 8, BODY_SIZE + 13 * (len(scope_lines) - 1) + 10)
    box_bottom = PAGE_HEIGHT - MARGIN - box_height
    scope_y = _centered_text_y(box_bottom, box_height, regular, BODY_SIZE, len(scope_lines), 13)
    body_top = box_bottom - HEADER_BODY_GAP
    if body_top - BODY_BOTTOM < 200:
        raise LayoutError("시험지 제목이 너무 길거나 범위 이름이 너무 깁니다. 인쇄할 제목과 범위를 줄이세요.")
    return Header(tuple(title_lines), tuple(scope_lines), scope_y, body_top, box_height)


def _header(pdf: canvas.Canvas, header: Header, regular: str, bold: str, answer: bool, draft: bool, *, first_page: bool = True) -> None:
    if not first_page:
        pdf.setLineWidth(0.5)
        pdf.line(PAGE_WIDTH / 2, CONTINUATION_BODY_TOP, PAGE_WIDTH / 2, FOOTER_RULE_Y + DIVIDER_RULE_GAP)
        return
    box_top = PAGE_HEIGHT - MARGIN
    box_bottom = box_top - header.box_height
    scope_width, title_width, name_width = HEADER_CELL_WIDTHS
    pdf.setLineWidth(0.6)
    pdf.rect(MARGIN, box_bottom, sum(HEADER_CELL_WIDTHS), header.box_height)
    pdf.setLineWidth(0.5)
    for x in (MARGIN + scope_width, MARGIN + scope_width + title_width):
        pdf.line(x, box_bottom, x, box_top)
    y = _centered_text_y(box_bottom, header.box_height, bold, 14, len(header.title_lines), 18)
    for line in header.title_lines:
        pdf.setFont(bold, 14)
        pdf.drawCentredString(MARGIN + scope_width + title_width / 2, y, line)
        y -= 18
    y = header.scope_y
    pdf.setFont(regular, BODY_SIZE)
    for line in header.scope_lines:
        pdf.drawCentredString(MARGIN + scope_width / 2, y, line)
        y -= 13
    name_x = MARGIN + scope_width + title_width
    name_font = bold if answer else regular
    name_y = _centered_text_y(box_bottom, header.box_height, name_font, BODY_SIZE)
    pdf.setFont(name_font, BODY_SIZE)
    if answer:
        pdf.setFont(bold, BODY_SIZE)
        pdf.drawCentredString(name_x + name_width / 2, name_y, "정답지")
    else:
        pdf.drawString(name_x + 8, name_y, "이름 :")
    pdf.setFont(regular, BODY_SIZE)
    instruction = "정답 번호 전체와 뜻을 확인하세요." if answer else "■ 주어진 어휘의 뜻을 모두 고르시오."
    pdf.drawString(MARGIN, header.body_top + 20, instruction)
    pdf.setLineWidth(0.5)
    pdf.line(MARGIN, header.body_top + 11, PAGE_WIDTH - MARGIN, header.body_top + 11)
    pdf.line(PAGE_WIDTH / 2, header.body_top + 11 - DIVIDER_RULE_GAP, PAGE_WIDTH / 2, FOOTER_RULE_Y + DIVIDER_RULE_GAP)


def _write_pdf(path: Path, worksheet: dict[str, Any], pages: list[list[tuple[int, float, Block]]], header: Header, regular: str, bold: str, answer: bool, draft: bool) -> list[dict[str, Any]]:
    pdf = canvas.Canvas(str(path), pagesize=A4, pageCompression=1)
    pdf.setTitle(_text(worksheet.get("title")) + (" 정답지" if answer else " 문제지"))
    pdf.setAuthor("멜로디잉글리시")
    metrics: list[dict[str, Any]] = []
    for page_number, placements in enumerate(pages, 1):
        _header(pdf, header, regular, bold, answer, draft, first_page=page_number == 1)
        for placement_index, (column, top, block) in enumerate(placements):
            x = MARGIN + column * (COLUMN_WIDTH + COLUMN_GAP)
            for line in block.lines:
                pdf.setFont(line.font, line.size)
                baseline = top - line.offset - line.size
                if line.option_number is not None:
                    pdf.drawString(x + line.x, baseline, _circled(line.option_number, line.font))
                pdf.drawString(x + line.x + line.option_width, baseline, line.text)
            content_bottom = top - block.content_height
            following = placements[placement_index + 1] if placement_index + 1 < len(placements) else None
            gap_after = content_bottom - following[1] if following and following[0] == column else 0.0
            metrics.append({
                "question_id": block.question_id,
                "question_number": block.question_number,
                "page": page_number,
                "column": column,
                "x": x,
                "top": top,
                "bottom": content_bottom,
                "content_bottom": content_bottom,
                "content_height": block.content_height,
                "gap_after": gap_after,
                "width": COLUMN_WIDTH,
                "min_font_size": min(line.size for line in block.lines),
                "max_line_width": max(line.x + line.option_width + pdfmetrics.stringWidth(line.text, line.font, line.size) for line in block.lines),
                "word_indent": block.word_indent,
                "word_lines": block.word_lines,
                "choice_columns": block.choice_columns,
                "choice_line_count": block.choice_line_count,
                "option_number_size": BODY_SIZE if block.choice_columns else None,
                "option_number_style": "text" if block.choice_columns else None,
                "option_circle_diameter": None,
            })
        pdf.setLineWidth(0.5)
        pdf.line(MARGIN, FOOTER_RULE_Y, PAGE_WIDTH - MARGIN, FOOTER_RULE_Y)
        pdf.setFont(regular, 9)
        pdf.drawCentredString(PAGE_WIDTH / 2, MARGIN - 4, f"- {page_number} -")
        pdf.showPage()
    pdf.save()
    return metrics


def render_worksheet(
    worksheet: dict[str, Any],
    student_path: str | Path,
    answer_path: str | Path,
    *,
    font_path: str | Path | None = None,
    bold_font_path: str | Path | None = None,
    draft: bool | None = None,
) -> dict[str, Any]:
    """Render student and teacher PDFs from a stable saved snapshot.

    ``draft=None`` automatically tracks worksheets containing unreviewed content
    in the returned metadata, without printing a review label or watermark.
    ``draft=False`` requires every question to be approved, AI reviewed, or human
    reviewed. Returned geometry can be used to audit pagination without OCR.
    """
    if worksheet.get("worksheet_kind") == "meaning_writing_test":
        from .writing_render import render_writing_worksheet
        return render_writing_worksheet(worksheet, student_path, answer_path, font_path=font_path, bold_font_path=bold_font_path, draft=draft)
    questions = _validate(worksheet)
    unreviewed = any(question.get("review_status") not in APPROVED_STATES for question in questions)
    if draft is False and unreviewed:
        raise ValueError("검토하지 않은 문항이 있습니다. 검토를 완료하거나 초안으로 출력하세요.")
    is_draft = unreviewed if draft is None else bool(draft)
    regular, bold = _fonts(font_path, bold_font_path)
    student = Path(student_path).resolve()
    answers = Path(answer_path).resolve()
    if student == answers:
        raise ValueError("문제지와 정답지는 서로 다른 파일이어야 합니다.")
    # Validate headers before opening output files.
    student_header = _header_layout(worksheet, regular, bold, False)
    answer_header = _header_layout(worksheet, regular, bold, True)
    show_unit_labels = not bool(_common_unit_label(questions))
    student_capacity = max(student_header.body_top, CONTINUATION_BODY_TOP) - BODY_BOTTOM
    answer_capacity = max(answer_header.body_top, CONTINUATION_BODY_TOP) - BODY_BOTTOM
    student_pages = _paginate([
        _block(q, n, regular, bold, False, student_capacity)
        for n, q in enumerate(questions, 1)
    ], student_header.body_top)
    answer_pages = _paginate([
        _block(q, n, regular, bold, True, answer_capacity, show_unit_label=show_unit_labels)
        for n, q in enumerate(questions, 1)
    ], answer_header.body_top)
    with _stage_pdf_pair(student, answers) as (student_tmp, answer_tmp):
        student_metrics = _write_pdf(student_tmp, worksheet, student_pages, student_header, regular, bold, False, is_draft)
        answer_metrics = _write_pdf(answer_tmp, worksheet, answer_pages, answer_header, regular, bold, True, is_draft)
    return {
        "student_path": str(student),
        "answer_path": str(answers),
        "student_pages": len(student_pages),
        "answer_pages": len(answer_pages),
        "draft": is_draft,
        "layout": {"student": student_metrics, "answers": answer_metrics},
    }
