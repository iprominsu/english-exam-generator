"""Read-only exports of saved worksheet PDFs; never renders or publishes files."""
from dataclasses import dataclass
import io
import re
import threading
import unicodedata
from typing import Callable, Literal
from urllib.parse import quote
import zipfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pypdf import PdfReader, PdfWriter

from .errors import public_error

MAX_ITEMS = 50
MAX_PDF_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 96 * 1024 * 1024
MAX_PAGES = 500
PdfKind = Literal["student", "answer", "combined"]
LABELS = {"student": "문제지", "answer": "정답지", "combined": "전체"}
_export_slots = threading.BoundedSemaphore(2)


class DownloadItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["standard", "word-meaning", "meaning-writing"]
    day: int | None = Field(default=None, strict=True, ge=1, le=100000)
    version: int | None = Field(default=None, strict=True, ge=1, le=100000)
    id: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")

    @model_validator(mode="after")
    def required_identity(self):
        if self.kind == "standard":
            if self.day is None or self.version is None or self.id is not None:
                raise ValueError("기존 어휘 시험지의 Day와 버전을 지정해 주세요.")
        elif self.id is None or self.day is not None or self.version is not None:
            raise ValueError("시험지 ID를 지정해 주세요.")
        return self


class DownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[DownloadItem] = Field(min_length=1, max_length=MAX_ITEMS)
    pdf_kind: PdfKind
    archive: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def valid_selection(self):
        if not self.archive and len(self.items) != 1:
            raise ValueError("개별 PDF는 시험지 한 개씩 다운로드해 주세요.")
        keys = [(item.kind, item.day, item.version, item.id) for item in self.items]
        if len(set(keys)) != len(keys):
            raise ValueError("같은 시험지를 중복 선택할 수 없습니다.")
        return self


@dataclass(frozen=True)
class DownloadResult:
    filename: str
    data: bytes
    media_type: str


PdfLoader = Callable[[DownloadItem, PdfKind], tuple[str, bytes]]


def find_existing_folder(drive, name: str, parent=None) -> str:
    """Resolve an existing folder without the normal create-on-missing path."""
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    query = f"name='{escaped}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent:
        query += f" and '{parent}' in parents"
    result = drive.files().list(q=query, fields="files(id,name),nextPageToken", pageSize=1000).execute(num_retries=2)
    folders = result.get("files", [])
    if not folders:
        raise HTTPException(404, "기존 데이터 폴더를 찾을 수 없습니다.")
    if len(folders) != 1 or result.get("nextPageToken"):
        raise HTTPException(409, "같은 이름의 데이터 폴더가 여러 개입니다. 대상 폴더를 확인해 주세요.")
    return folders[0]["id"]


def safe_filename(value: str, extension: str = ".pdf") -> str:
    """One portable leaf name, including Windows devices and invisible controls."""
    value = unicodedata.normalize("NFC", str(value))
    value = "".join(char for char in value if not unicodedata.category(char).startswith("C"))
    value = re.sub(r'[<>:"/\\|?*]', "_", value).strip(" .")
    if value.lower().endswith(extension):
        value = value[:-len(extension)].rstrip(" .")
    value = value[:100].rstrip(" .") or "시험지"
    while len(value.encode("utf-8")) > 200:
        value = value[:-1]
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", value, re.I):
        value = "_" + value
    return value + extension


def _reader(data: bytes) -> PdfReader:
    if not isinstance(data, bytes) or len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, "PDF 한 개의 다운로드 크기는 32MB 이하여야 합니다.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted or not reader.pages:
            raise ValueError("unreadable PDF")
        if len(reader.pages) > MAX_PAGES:
            raise HTTPException(413, "PDF의 페이지 수가 다운로드 한도를 초과합니다.")
        return reader
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, "저장된 PDF를 읽지 못했습니다. PDF를 재생성해 주세요.") from None


def _answer_boundary(data: bytes) -> int:
    """Recognize the legacy renderer's standalone section at a page start.

    This requires the exact heading as the first body line at the legacy print
    margin and a numbered, circled answer in the left column below it. Merely
    mentioning the heading in a question, or having a final page, proves nothing.
    All answer continuation pages belong to the detected section.
    """
    import pdfplumber

    def is_section_start(page):
        # Legacy Chromium prints the brand above 40pt, content from
        # 16mm, and the page number in the bottom margin.
        words = [word for word in page.extract_words(x_tolerance=2, y_tolerance=3)
                 if 40 <= word["top"] < page.height - 40]
        words.sort(key=lambda word: (round(word["top"], 1), word["x0"]))
        if not words:
            return False
        first_top = words[0]["top"]
        first = sorted([word for word in words if abs(word["top"] - first_top) <= 3], key=lambda word: word["x0"])
        title = re.sub(r"\s+", "", " ".join(word["text"] for word in first))
        if title != "[정답및해설]" or not (40 <= first_top <= 85):
            return False
        if not (15 <= first[0]["x0"] <= 65):
            return False
        below = [word for word in words if first_top + 5 < word["top"] <= first_top + 60
                 and word["x0"] < page.width * .45]
        if not below:
            return False
        row_top = below[0]["top"]
        row = sorted([word for word in below if abs(word["top"] - row_top) <= 3], key=lambda word: word["x0"])
        return bool(re.fullmatch(r"1\.\s*[①②③④⑤]", " ".join(word["text"] for word in row)))

    candidates = []
    total_chars = 0
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for index, page in enumerate(pdf.pages):
                try:
                    count = len(page.chars)
                    total_chars += count
                    if count > 200000 or total_chars > 2000000:
                        raise HTTPException(413, "PDF의 텍스트 크기가 다운로드 한도를 초과합니다.")
                    if is_section_start(page):
                        candidates.append(index)
                finally:
                    page.close()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, "저장된 PDF의 정답 구간을 확인하지 못했습니다. PDF를 재생성해 주세요.") from None
    if len(candidates) != 1 or candidates[0] == 0:
        raise HTTPException(422, "문제지와 정답지 경계가 없거나 불명확합니다. PDF를 재생성해 주세요.")
    return candidates[0]


def split_legacy_pdf(data: bytes, kind: PdfKind) -> bytes:
    """Keep original combined bytes, or split at a verified answer section."""
    if kind not in LABELS:
        raise HTTPException(422, "다운로드 종류를 확인해 주세요.")
    reader = _reader(data)
    if kind == "combined":
        return data
    boundary = _answer_boundary(data)
    start, stop = (0, boundary) if kind == "student" else (boundary, len(reader.pages))
    output = io.BytesIO()
    try:
        with PdfWriter() as writer:
            writer.append(reader, pages=(start, stop), import_outline=False)
            writer.write(output)
    except Exception:
        raise HTTPException(422, "저장된 PDF를 분리하지 못했습니다. PDF를 재생성해 주세요.") from None
    return output.getvalue()


def load_feature_pdf(service, item: DownloadItem, kind: PdfKind) -> tuple[str, bytes]:
    """Use the same saved state/hash checks as the existing PDF endpoint."""
    with service.lock:
        worksheet = service.get("worksheets", item.id)
        if worksheet.get("deleted_at"):
            raise HTTPException(404, "항목을 찾을 수 없습니다.")
        if worksheet.get("deletion_pending"):
            raise HTTPException(409, "완전 삭제가 진행 중인 시험지입니다.")
        expected_kind = "meaning_writing_test" if item.kind == "meaning-writing" else "word_meaning_test"
        if worksheet.get("worksheet_kind", "word_meaning_test") != expected_kind:
            raise HTTPException(409, "시험지 유형이 변경되었습니다. 목록을 새로고침해 주세요.")
        if not worksheet.get("pdfs"):
            raise HTTPException(404, "PDF를 먼저 생성해 주세요.")
        if worksheet.get("pdfs_stale"):
            raise HTTPException(409, "수정한 내용을 반영하려면 PDF를 재생성해 주세요.")
        parts = ("student", "answer") if kind == "combined" else (kind,)
        sources = []
        for part in parts:
            if not worksheet["pdfs"].get(part):
                raise HTTPException(404, "문제지와 정답지 PDF를 먼저 생성해 주세요.")
            path = service.ensure_pdf(worksheet, part)
            try:
                if path.stat().st_size > MAX_PDF_BYTES:
                    raise HTTPException(413, "PDF 한 개의 다운로드 크기는 32MB 이하여야 합니다.")
                data = path.read_bytes()
            except FileNotFoundError:
                raise HTTPException(404, "저장된 PDF를 찾을 수 없습니다.") from None
            _reader(data)
            sources.append(data)
        filename = f"{worksheet['title']} {LABELS[kind]}.pdf"
        if len(sources) == 1:
            return filename, sources[0]
        output = io.BytesIO()
        with PdfWriter() as writer:
            for source in sources:
                writer.append(_reader(source), import_outline=False)
            writer.write(output)
        data = output.getvalue()
        _reader(data)
        return filename, data


def build_download(body: DownloadRequest, loader: PdfLoader) -> DownloadResult:
    """Assemble atomically: any failed source prevents a partial-success ZIP."""
    entries = []
    used = set()
    total = 0
    for item in body.items:
        filename, data = loader(item, body.pdf_kind)
        _reader(data)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise HTTPException(413, "한 번에 다운로드할 수 있는 PDF 용량은 96MB 이하입니다.")
        filename = safe_filename(filename)
        base = filename[:-4]
        serial = 2
        while filename.casefold() in used:
            filename = f"{base} ({serial}).pdf"
            serial += 1
        used.add(filename.casefold())
        entries.append((filename, data))
    if len(entries) == 1:
        filename, data = entries[0]
        return DownloadResult(filename, data, "application/pdf")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for filename, data in entries:
            archive.writestr(filename, data)
    return DownloadResult(f"시험지 {LABELS[body.pdf_kind]}.zip", output.getvalue(), "application/zip")


def create_download_router(loader: PdfLoader) -> APIRouter:
    router = APIRouter()

    @router.post("/api/worksheet-downloads")
    def download(body: DownloadRequest):
        if not _export_slots.acquire(blocking=False):
            raise HTTPException(409, "다른 다운로드를 준비 중입니다. 잠시 후 다시 시도해 주세요.")
        try:
            result = build_download(body, loader)
            return Response(result.data, media_type=result.media_type, headers={
                "Content-Disposition": "attachment; filename=\"worksheets" + (".zip" if result.media_type == "application/zip" else ".pdf") + "\"; filename*=utf-8''" + quote(result.filename, safe=""),
                "Cache-Control": "private, no-store",
            })
        except HTTPException:
            raise
        except Exception as error:
            raise public_error(error) from None
        finally:
            _export_slots.release()

    return router
