"""Position-aware PDF vocabulary imports with explicit review states.

No AI is used to alter source vocabulary. Coordinates are PDF points, origin at
the top left. OCR engines implement ``recognize(image_path)`` and return TSV-style
token dictionaries (text, left, top, width, height, confidence).
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Callable

import pdfplumber


SCHEMA_VERSION = 1
_KOREAN = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")
_NUMBERED = re.compile(r"^\s*(\d+)[.)]?\s+(.+?)\s*$", re.S)
_HEADING = re.compile(
    r"^(?:\d+\s*강(?:\s*\d+\s*번(?:\s*[-–~,·]\s*\d+\s*번)*)?|"
    r"(?:DAY|Day|day)\s*\d+|(?:mini\s*)?모의고사\s*\d+\s*회)\s*$",
    re.I,
)
_HEADERS = {"word", "meaning", "vocabulary", "단어", "영단어", "뜻", "의미", "번호", "no", "no."}


class PDFImportError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class PDFImportCancelled(PDFImportError):
    def __init__(self):
        super().__init__("cancelled", "PDF 가져오기가 취소되었습니다.")


class TesseractOCR:
    """Tesseract adapter; only writes the rendered page inside project .local."""

    def __init__(self, executable: str | None = None, languages: str = "kor+eng", timeout: int = 180):
        self.executable = executable or os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
        self.languages = languages
        self.timeout = timeout

    def recognize(self, image_path: Path) -> list[dict]:
        if not self.executable:
            raise PDFImportError("ocr_unavailable", "스캔 PDF에는 한국어·영어 Tesseract OCR이 필요합니다.")
        try:
            result = subprocess.run(
                [self.executable, str(image_path), "stdout", "--oem", "1", "-l", self.languages, "tsv"],
                check=True, capture_output=True, text=True, encoding="utf-8", timeout=self.timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PDFImportError("ocr_failed", "OCR 실행에 실패했습니다. 실행 파일과 kor/eng 언어 데이터를 확인하세요.") from exc
        tokens = []
        for row in csv.DictReader(io.StringIO(result.stdout), delimiter="\t"):
            if not row.get("text", "").strip():
                continue
            try:
                tokens.append({"text": row["text"], "left": float(row["left"]), "top": float(row["top"]),
                               "width": float(row["width"]), "height": float(row["height"]),
                               "confidence": float(row["conf"])})
            except (KeyError, ValueError) as exc:
                raise PDFImportError("ocr_invalid_output", "OCR 위치 정보가 올바르지 않습니다.") from exc
        return tokens


class TesseractJsOCR:
    """Project-local WebAssembly OCR fallback for machines without Tesseract."""

    def __init__(self, node: str | None = None, timeout: int = 300):
        self.project_root = Path(__file__).resolve().parents[1]
        self.node = node or os.environ.get("TESSERACT_JS_NODE") or shutil.which("node")
        self.timeout = timeout
        self.worker_script = Path(__file__).with_name("ocr_worker.cjs")
        self.data_path = self.project_root / ".local" / "ocr" / "tessdata"

    @property
    def available(self) -> bool:
        package = self.project_root / ".local" / "ocr" / "node_modules" / "tesseract.js"
        return bool(self.node and self.worker_script.is_file() and package.is_dir()
                    and all((self.data_path / f"{lang}.traineddata.gz").is_file()
                            or (self.data_path / f"{lang}.traineddata").is_file() for lang in ("kor", "eng")))

    def recognize(self, image_path: Path) -> list[dict]:
        if not self.available:
            raise PDFImportError("ocr_unavailable", "프로젝트의 한국어·영어 OCR 설치가 필요합니다.")
        try:
            result = subprocess.run([self.node, str(self.worker_script), str(image_path)],
                                    cwd=self.project_root, check=True, capture_output=True,
                                    text=True, encoding="utf-8", timeout=self.timeout,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError) as exc:
            raise PDFImportError("ocr_failed", "프로젝트 OCR 실행에 실패했습니다. 설치 상태와 언어 데이터를 확인하세요.") from exc
        tokens = []
        for row in csv.DictReader(io.StringIO(result.stdout), delimiter="\t"):
            if not row.get("text", "").strip():
                continue
            try:
                tokens.append({"text": row["text"], "left": float(row["left"]), "top": float(row["top"]),
                               "width": float(row["width"]), "height": float(row["height"]),
                               "confidence": float(row["conf"])})
            except (KeyError, ValueError) as exc:
                raise PDFImportError("ocr_invalid_output", "OCR 위치 정보가 올바르지 않습니다.") from exc
        return tokens


def _default_ocr_engine():
    native = TesseractOCR()
    if native.executable:
        return native
    local = TesseractJsOCR()
    return local if local.available else native


def _stable_id(prefix: str, *parts) -> str:
    return prefix + hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:24]


def _normalize(raw: str) -> str:
    # Preserve punctuation and letters. A line break is whitespace, never a
    # license to guess a missing letter or remove a lexical space.
    return re.sub(r"\s+", " ", raw).strip()


def _lines(words: list[dict], tolerance: float = 4) -> list[list[dict]]:
    groups: list[list[dict]] = []
    for word in sorted(words, key=lambda w: ((w["top"] + w["bottom"]) / 2, w["x0"])):
        center = (word["top"] + word["bottom"]) / 2
        if groups and abs(center - sum((w["top"] + w["bottom"]) / 2 for w in groups[-1]) / len(groups[-1])) <= tolerance:
            groups[-1].append(word)
        else:
            groups.append([word])
    return [sorted(group, key=lambda w: w["x0"]) for group in groups]


def _word_text(words: list[dict]) -> str:
    return "\n".join(" ".join(w["text"] for w in line) for line in _lines(words))


def _heading(words: list[dict], cutoff: float) -> str | None:
    for line in _lines([w for w in words if w["bottom"] <= cutoff]):
        text = _normalize(" ".join(w["text"] for w in line))
        # A distant name field can share the heading baseline.
        text = re.split(r"\s+Name\s*:", text, flags=re.I)[0].strip()
        if _HEADING.fullmatch(text):
            return text
    return None


def _bbox_union(boxes) -> list[float]:
    boxes = [box for box in boxes if box]
    return [round(min(b[0] for b in boxes), 3), round(min(b[1] for b in boxes), 3),
            round(max(b[2] for b in boxes), 3), round(max(b[3] for b in boxes), 3)]


def _table_rows(page) -> tuple[list[dict], float | None]:
    """Read each word/meaning cell pair once, including row-spanned cells."""
    result, seen = [], set()
    cutoff = None
    for table in sorted(page.find_tables(), key=lambda t: (t.bbox[1], t.bbox[0])):
        extracted = table.extract(x_tolerance=2, y_tolerance=3)
        separate_number = any(values and len(values) % 3 == 0 and values[0]
                              and re.fullmatch(r"\s*\d+[.)]?\s*", values[0]) for values in extracted)
        for values, row in zip(extracted, table.rows):
            if not values:
                continue
            # Three-column tables use separate number, word and meaning cells.
            size = 3 if separate_number else 2
            if len(values) % size:
                continue
            for start in range(0, len(values), size):
                word_index = start + (1 if size == 3 else 0)
                mean_index = start + size - 1
                key = tuple(row.cells[word_index] or ())
                if not key or key in seen:
                    continue
                seen.add(key)
                raw_word, raw_meaning = values[word_index] or "", values[mean_index] or ""
                if not raw_word.strip() and not raw_meaning.strip():
                    continue
                if _normalize(raw_word).lower() in _HEADERS:
                    continue
                number = None
                if size == 3:
                    number_text = values[start] or ""
                    if re.fullmatch(r"\s*\d+[.)]?\s*", number_text):
                        number = int(re.sub(r"\D", "", number_text))
                else:
                    match = _NUMBERED.match(raw_word)
                    if match:
                        number, raw_word = int(match[1]), match[2]
                    elif re.fullmatch(r"\s*\d+[.)]?\s*", raw_word):
                        number, raw_word = int(re.sub(r"\D", "", raw_word)), ""
                # Do not interpret arbitrary unrelated tables as a bank.
                if number is None and not re.search(r"[A-Za-z]", raw_word):
                    continue
                bbox = _bbox_union(row.cells[start:start + size])
                cutoff = min(cutoff, bbox[1]) if cutoff is not None else bbox[1]
                result.append({"source_number": number, "word_raw": raw_word,
                               "meaning_raw": raw_meaning, "bbox": bbox, "issues": []})
    result.sort(key=lambda row: (row["source_number"] if row["source_number"] is not None else 10**9,
                                 row["bbox"][0], row["bbox"][1]))
    return result, cutoff


def _position_rows(words: list[dict], page_width: float, *, ocr: bool = False) -> tuple[list[dict], float | None]:
    """Conservative fallback for unruled or OCR pages: keep rows for review."""
    anchors = [w for w in words if re.fullmatch(r"\d+[.)]?", w["text"])]
    # Page numbers at the footer and numbers in meanings are not row starts.
    clusters: list[list[dict]] = []
    for anchor in sorted(anchors, key=lambda w: w["x0"]):
        if clusters and abs(anchor["x0"] - sum(w["x0"] for w in clusters[-1]) / len(clusters[-1])) < 8:
            clusters[-1].append(anchor)
        else:
            clusters.append([anchor])
    # Retain only leftmost starts in each page half. Their rows still require
    # review, since numeric content or missing OCR anchors can be ambiguous.
    selected = []
    for half in [0, 1]:
        candidates = [c for c in clusters if int(c[0]["x0"] >= page_width / 2) == half]
        if candidates:
            selected.append(min(candidates, key=lambda c: sum(w["x0"] for w in c) / len(c)))
    rows = []
    for column_index, column in enumerate(selected):
        x0 = min(w["x0"] for w in column)
        right = min((min(w["x0"] for w in c) for c in selected if min(w["x0"] for w in c) > x0 + 8), default=page_width)
        column.sort(key=lambda w: w["top"])
        for index, anchor in enumerate(column):
            height = anchor["bottom"] - anchor["top"]
            top = anchor["top"] - max(3, height * .5)
            bottom = column[index + 1]["top"] - max(3, height * .5) if index + 1 < len(column) else anchor["bottom"] + 45
            tokens = [w for w in words if w is not anchor and w["x0"] >= x0 and w["x1"] <= right
                      and top <= (w["top"] + w["bottom"]) / 2 < bottom]
            first_line = _lines(tokens)[0] if tokens else []
            korean = [w for w in first_line if _KOREAN.search(w["text"])]
            if not korean:
                continue
            divide = min(w["x0"] for w in korean)
            english = [w for w in tokens if w["x0"] < divide]
            meanings = [w for w in tokens if w["x0"] >= divide]
            if not english:
                continue
            boxes = [(w["x0"], w["top"], w["x1"], w["bottom"]) for w in tokens + [anchor]]
            confidence = min((w.get("confidence", 100) for w in tokens + [anchor]), default=0)
            rows.append({"source_number": int(re.sub(r"\D", "", anchor["text"])),
                         "word_raw": _word_text(english), "meaning_raw": _word_text(meanings),
                         "bbox": _bbox_union(boxes), "issues": ["ocr_layout_review" if ocr else "unruled_layout_review"],
                         "ocr_confidence": confidence if ocr else None})
    rows.sort(key=lambda r: (r["source_number"], r["bbox"][0]))
    return rows, min((row["bbox"][1] for row in rows), default=None)


def _row_issues(row: dict) -> list[str]:
    issues = list(row.get("issues", []))
    word, meaning = _normalize(row["word_raw"]), _normalize(row["meaning_raw"])
    if not word:
        issues.append("empty_word")
    elif not re.search(r"[A-Za-z]", word):
        issues.append("word_not_english")
    if not meaning:
        issues.append("empty_meaning")
    if not _KOREAN.search(meaning):
        issues.append("meaning_not_korean")
    if "\ufffd" in word + meaning or "(cid:" in word + meaning:
        issues.append("broken_characters")
    if _KOREAN.search(word):
        issues.append("word_contains_korean")
    if row["source_number"] is None:
        issues.append("missing_source_number")
    # A line wrapping inside a Korean word cannot be safely repaired from text
    # alone. Keep the original line break and request source comparison.
    if re.search(r"[가-힣]\s*\n\s*[가-힣]", row["meaning_raw"]):
        issues.append("korean_wrap_review")
    if row.get("ocr_confidence") is not None and row["ocr_confidence"] < 85:
        issues.append("low_ocr_confidence")
    return list(dict.fromkeys(issues))


def _unparsed_vocabulary_text(words: list[dict], rows: list[dict], page_height: float) -> bool:
    """Flag vocabulary candidates outside recognized table cells.

    A partially recognized page cannot be claimed complete. Chapter/name text
    above the first vocabulary row is not body content; page-number labels and
    column labels alone are not another vocabulary pair. No bottom margin is
    discarded, because a real vocabulary row may be printed close to the edge.
    """
    boxes = [row["bbox"] for row in rows]
    first_top = min(box[1] for box in boxes)
    unclaimed = [word for word in words
        if first_top <= (word["top"] + word["bottom"]) / 2 <= page_height
        and not any(box[0] - .5 <= (word["x0"] + word["x1"]) / 2 <= box[2] + .5
                    and box[1] - .5 <= (word["top"] + word["bottom"]) / 2 <= box[3] + .5
                    for box in boxes)]
    for line in _lines(unclaimed):
        text = " ".join(word["text"] for word in line)
        if re.fullmatch(r"\s*(?:page|페이지)\s*\d+\s*(?:/|of)\s*\d+\s*", text, re.I):
            continue
        labels = [part.casefold().strip(".:()[]") for part in text.split()]
        if re.search(r"[A-Za-z]{2,}", text) and _KOREAN.search(text) and not all(
                label in _HEADERS or label.isdigit() for label in labels):
            return True
    return False


def _ocr_words(path: Path, page_index: int, engine, work_root: Path) -> list[dict]:
    import pypdfium2 as pdfium
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="page-", dir=work_root) as task_dir:
        with pdfium.PdfDocument(path) as doc:
            page = doc[page_index]
            bitmap = page.render(scale=300 / 72)
            image = bitmap.to_pil()
            rendered = Path(task_dir) / "page.png"
            image.save(rendered)
            try:
                tokens = engine.recognize(rendered) if hasattr(engine, "recognize") else engine(rendered)
            finally:
                image.close()
                bitmap.close()
                page.close()
        scale = 72 / 300
        result = []
        for token in tokens:
            try:
                left, top = float(token["left"]) * scale, float(token["top"]) * scale
                width, height = float(token["width"]) * scale, float(token["height"]) * scale
                result.append({"text": str(token["text"]), "x0": left, "top": top,
                               "x1": left + width, "bottom": top + height,
                               "confidence": float(token.get("confidence", token.get("conf", 0)))})
            except (KeyError, TypeError, ValueError) as exc:
                raise PDFImportError("ocr_invalid_output", "OCR 위치 정보가 올바르지 않습니다.") from exc
        return result


def import_pdf(path, *, ocr_engine=None, progress: Callable[[dict], object] | None = None) -> dict:
    """Import text, scan, or mixed PDFs without silently dropping failed pages.

    ``progress(event) is False`` cancels between pages. Per-page OCR/layout
    failures are recorded in ``page_issues`` and set ``status=requires_review``.
    Invalid/encrypted documents raise PDFImportError with a machine-readable code.
    """
    path = Path(path)
    try:
        if not path.is_file():
            raise PDFImportError("file_missing", "PDF 파일을 찾을 수 없습니다.")
        if path.read_bytes()[:5] != b"%PDF-":
            raise PDFImportError("invalid_pdf", "올바른 PDF 파일이 아닙니다.")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        pdf = pdfplumber.open(path)
    except PDFImportError:
        raise
    except Exception as exc:
        cause, encrypted = exc, False
        while cause is not None:
            encrypted = encrypted or "password" in type(cause).__name__.lower() or "password" in str(cause).lower()
            cause = cause.__cause__ or cause.__context__
        code = "encrypted_pdf" if encrypted else "invalid_pdf"
        raise PDFImportError(code, "암호를 해제한 PDF가 필요합니다." if code == "encrypted_pdf" else "PDF를 읽을 수 없습니다.") from exc

    bank = {"schema_version": SCHEMA_VERSION,
            "source": {"filename": path.name, "sha256": digest, "format": "pdf", "page_count": len(pdf.pages)},
            "units": [], "entries": [], "page_issues": [], "status": "ready"}
    if not pdf.pages:
        bank["page_issues"].append({"code": "empty_pdf", "message": "PDF에 페이지가 없습니다."})
    current = None
    next_number = None
    unit_numbers: dict[str, dict[int, dict]] = {}
    work_root = Path(__file__).resolve().parents[1] / ".local" / "pdf-import"
    try:
        for page_index, page in enumerate(pdf.pages):
            page_no = page_index + 1
            if progress and progress({"phase": "extracting", "page": page_no, "page_count": len(pdf.pages), "entries": len(bank["entries"])}) is False:
                raise PDFImportCancelled()
            method = "text"
            try:
                words = page.extract_words(x_tolerance=2, y_tolerance=3)
                text = "".join(w["text"] for w in words)
                healthy_text = len(text) >= 20 and re.search(r"[A-Za-z]", text) and _KOREAN.search(text) and "\ufffd" not in text
                rows, cutoff = _table_rows(page) if healthy_text else ([], None)
                table_rows_found = bool(rows)
                if healthy_text and not rows:
                    rows, cutoff = _position_rows(words, page.width)
                if not healthy_text:
                    method = "ocr"
                    words = _ocr_words(path, page_index, ocr_engine or _default_ocr_engine(), work_root)
                    rows, cutoff = _position_rows(words, page.width, ocr=True)
                if not rows:
                    bank["page_issues"].append({"page": page_no, "code": "no_vocabulary_rows", "message": "단어와 뜻의 행 구조를 확인할 수 없습니다."})
                    continue
                if table_rows_found and _unparsed_vocabulary_text(words, rows, page.height):
                    bank["page_issues"].append({"page": page_no, "code": "unparsed_vocabulary_text",
                        "message": "표 밖에 단어와 뜻으로 보이는 내용이 있어 페이지 전체 대조가 필요합니다."})
                heading = _heading(words, cutoff or page.height * .15)
                first_number = rows[0]["source_number"]
                contiguous = current is not None and first_number == next_number
                if heading and (current is None or heading != current["label"]):
                    current = {"id": _stable_id("u_", digest, heading, len(bank["units"])), "label": heading,
                               "order": len(bank["units"]) + 1, "pages": [], "status": "ready"}
                    bank["units"].append(current)
                elif current is None or (not heading and not contiguous):
                    label = f"단원 확인 필요 (페이지 {page_no})"
                    current = {"id": _stable_id("u_", digest, label, len(bank["units"])), "label": label,
                               "order": len(bank["units"]) + 1, "pages": [], "status": "review"}
                    bank["units"].append(current)
                current["pages"].append(page_no)
                numbers = unit_numbers.setdefault(current["id"], {})
                for row in rows:
                    issues = _row_issues(row)
                    if current["status"] == "review":
                        issues.append("unit_label_review")
                    number = row["source_number"]
                    if number in numbers:
                        issues.append("duplicate_source_number")
                        previous = numbers[number]
                        previous["issues"] = list(dict.fromkeys(previous["issues"] + ["duplicate_source_number"]))
                        previous["status"] = "review"
                    entry = {"id": _stable_id("e_", digest, current["id"], page_no, number, row["bbox"]),
                             "unit_id": current["id"], "source_number": number, "word": _normalize(row["word_raw"]),
                             "word_raw": row["word_raw"], "meaning_raw": row["meaning_raw"],
                             "meaning": _normalize(row["meaning_raw"]),
                             "source": {"page": page_no, "bbox": row["bbox"], "method": method},
                             "status": "review" if issues else "ready", "issues": list(dict.fromkeys(issues))}
                    if method == "ocr":
                        entry["source"]["ocr_confidence"] = row.get("ocr_confidence")
                    bank["entries"].append(entry)
                    if number is not None:
                        numbers[number] = entry
                numeric = [r["source_number"] for r in rows if r["source_number"] is not None]
                next_number = max(numeric) + 1 if numeric else None
            except PDFImportCancelled:
                raise
            except PDFImportError as exc:
                bank["page_issues"].append({"page": page_no, "code": exc.code, "message": str(exc)})
            except Exception as exc:
                bank["page_issues"].append({"page": page_no, "code": "page_extraction_failed", "message": "이 페이지를 읽지 못했습니다. 원본을 확인하세요."})
        for unit in bank["units"]:
            numbers = sorted(unit_numbers.get(unit["id"], {}))
            expected = list(range(1, max(numbers) + 1)) if numbers else []
            if numbers != expected:
                unit["status"] = "review"
                bank["page_issues"].append({"unit_id": unit["id"], "code": "number_sequence_gap", "message": "원본 항목 번호의 누락 또는 시작 번호를 확인하세요."})
        ready = sum(e["status"] == "ready" for e in bank["entries"])
        bank["counts"] = {"units": len(bank["units"]), "total": len(bank["entries"]), "ready": ready,
                          "review": len(bank["entries"]) - ready, "page_issues": len(bank["page_issues"])}
        if bank["counts"]["review"] or bank["page_issues"] or any(u["status"] != "ready" for u in bank["units"]):
            bank["status"] = "requires_review"
        if progress:
            progress({"phase": "completed", "page": len(pdf.pages), "page_count": len(pdf.pages), "entries": len(bank["entries"]), "counts": bank["counts"]})
        return bank
    finally:
        pdf.close()
