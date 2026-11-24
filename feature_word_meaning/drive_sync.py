"""Optional Drive/Sheets persistence using operating-app injected API clients.

No credentials are discovered here. The caller must inject Drive and Sheets
clients authenticated with the same approved account and explicit destination
folders. Files are scoped by parent and private appProperties, never by name.

All managed spreadsheet tabs are replaced together using atomic
spreadsheets.batchUpdate/UpdateCellsRequests. Old values are never cleared in a
separate request. Existing PDF file IDs are updated, never deleted or trashed.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable

from .exams import WORKSHEET_KIND, validate_worksheet

EXPECTED_ACCOUNT = os.environ.get("APPROVED_DRIVE_EMAIL", "").strip().lower() or None
FEATURE_TAG = "melody_word_meaning_v1"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
FOLDER_MIME = "application/vnd.google-apps.folder"
MAX_CELL_CHARACTERS = 50_000
MAX_SPREADSHEET_CELLS = 10_000_000
PROJECT_ROOT = Path(__file__).resolve().parent.parent
FILE_FIELDS = "id,name,mimeType,parents,trashed,appProperties,webViewLink,owners(emailAddress),capabilities(canEdit)"


class DriveSyncError(ValueError):
    def __init__(self, message: str, code: str = "drive_sync_failed", **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise DriveSyncError("저장할 데이터의 JSON 형식이 올바르지 않습니다.", "invalid_data") from error


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise DriveSyncError(f"{label} ID가 올바르지 않습니다.", "invalid_destination")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DriveSyncError(f"{label}이(가) 비어 있습니다.", "invalid_data")
    return value


def _path(value: str | Path, extensions: set[str]) -> Path:
    path = Path(value).resolve()
    if not path.is_relative_to(PROJECT_ROOT) or not path.is_file() or path.suffix.lower() not in extensions:
        raise DriveSyncError("현재 프로젝트 안의 올바른 원본/PDF 파일이 필요합니다.", "invalid_source")
    return path


def _cell(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else _json(value)


def _validate_tables(tables: dict[str, list[list[Any]]]) -> dict[str, list[list[str]]]:
    validated = {}
    cells = 0
    for title, rows in tables.items():
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], list) or not rows[0]:
            raise DriveSyncError("시트에 제목 행이 필요합니다.", "invalid_data")
        if len(rows[0]) != len(set(rows[0])):
            raise DriveSyncError("시트 제목 열이 중복되었습니다.", "invalid_data")
        width = len(rows[0])
        clean = []
        for row_index, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != width:
                raise DriveSyncError("시트의 행 너비가 올바르지 않습니다.", "invalid_data")
            converted = [_cell(value) for value in row]
            for column, value in enumerate(converted):
                # Sheets counts characters conservatively as UTF-16 units.
                if len(value.encode("utf-16-le")) // 2 > MAX_CELL_CHARACTERS:
                    raise DriveSyncError("Google Sheets 한 셀의 50,000자 제한을 초과했습니다. 데이터를 줄인 뒤 다시 저장하세요.", "cell_limit", tab=title, row=row_index + 1, column=column + 1)
            clean.append(converted)
        cells += len(clean) * width
        validated[title] = clean
    if cells > MAX_SPREADSHEET_CELLS:
        raise DriveSyncError("Google Sheets의 전체 셀 수 제한을 초과했습니다.", "sheet_limit")
    return validated


def _public_source(source: dict) -> dict:
    return {key: deepcopy(source[key]) for key in ("filename", "sha256", "format", "page_count", "legacy_original_ids") if key in source}


def _bank_tables(bank: dict) -> dict:
    if not isinstance(bank, dict) or not isinstance(bank.get("entries"), list) or not isinstance(bank.get("units"), list):
        raise DriveSyncError("단어은행 형식이 올바르지 않습니다.", "invalid_data")
    entries = [["entry_id", "unit_id", "source_number", "word", "meaning_raw", "meaning", "status", "source_json", "issues_json", "reviewed_at", "entry_json"]]
    for entry in bank["entries"]:
        if not isinstance(entry, dict) or entry.get("status") not in {"ready", "review", "excluded"}:
            raise DriveSyncError("단어은행 항목 상태가 올바르지 않습니다.", "invalid_data")
        entries.append([entry.get("id"), entry.get("unit_id"), entry.get("source_number"), entry.get("word"), entry.get("meaning_raw"), entry.get("meaning"), entry.get("status"), _json(entry.get("source", {})), _json(entry.get("issues", [])), entry.get("reviewed_at"), _json(entry)])
    if bank.get('source_manifest'):
        entries[0].append('word_raw')
        for row, entry in zip(entries[1:], bank['entries']):
            row.append(entry.get('word_raw', entry.get('original_word', entry.get('word', ''))))
    units = [["unit_id", "label", "order", "pages_json", "unit_json"]]
    for unit in bank["units"]:
        units.append([unit.get("id"), unit.get("label"), unit.get("order"), _json(unit.get("pages", [])), _json(unit)])
    metadata = {
        "worksheet_kind": WORKSHEET_KIND, "record_kind": "word_bank", "schema_version": bank.get("schema_version", 1),
        "bank_id": bank.get("id", ""), "title": bank.get("title", bank.get("source", {}).get("filename", "단어장")),
        "source": _public_source(bank.get("source", {})), "counts": bank.get("counts", {}),
        "page_issues": bank.get("page_issues", []), "original_id": "", "original_link": "",
    }
    if bank.get('source_manifest'):
        metadata.update(source_manifest_version=bank['source_manifest']['version'],
            source_manifest_sha256=bank['source_manifest']['sha256'],
            source_manifest_evidence=bank['source_manifest']['evidence'], bank_version=bank.get('version', 0))
    return _validate_tables({"Entries": entries, "Units": units, "Metadata": [["key", "value"], *[[key, _cell(value)] for key, value in metadata.items()]]})


def _worksheet_tables(worksheet: dict, require_reviewed: bool = True) -> dict:
    validate_worksheet(worksheet, require_reviewed=require_reviewed)
    if worksheet.get("worksheet_kind") == "meaning_writing_test":
        return _writing_worksheet_tables(worksheet)
    questions = [["number", "question_id", "word", "source_word", "meaning_raw", "original_meaning_raw", "source_entry_ids_json", "choices_json", "correct_choice_ids_json", "semantic_groups_json", "review_status", "review_evidence_json", "active_candidate_id", "question_json"]]
    candidates = [["question_id", "candidate_id", "is_active", "question_json"]]
    for number, question in enumerate(worksheet["questions"], 1):
        main = {key: value for key, value in question.items() if key not in {"candidates", "source_snapshot"}}
        questions.append([number, question["id"], question["word"], question.get("source_word", question["word"]), question["meaning_raw"], question.get("original_meaning_raw", ""), _json(question["source_entry_ids"]), _json(question["choices"]), _json(question["correct_choice_ids"]), _json(question["semantic_groups"]), question["review_status"], _json(question.get("review_evidence")), question.get("active_candidate_id", ""), _json(main)])
        for candidate in question.get("candidates", []):
            candidate_snapshot = deepcopy(candidate["question"])
            if candidate["id"] == question.get("active_candidate_id", "candidate_1"):
                candidate_snapshot = deepcopy(main)
            candidate_snapshot.pop("candidates", None)
            candidates.append([question["id"], candidate["id"], candidate["id"] == question.get("active_candidate_id", "candidate_1"), _json(candidate_snapshot)])
    sources = [["entry_id", "unit_id", "word", "meaning_raw", "meaning", "entry_json"]]
    for entry in worksheet["source_entries"]:
        sources.append([entry["id"], entry["unit_id"], entry["word"], entry.get("meaning_raw", ""), entry.get("meaning", ""), _json(entry)])
    fields = ("id", "schema_version", "worksheet_kind", "title", "instruction", "created_at", "selected_unit_ids", "selected_units", "bank_id", "selection_counts", "range_label", "partial_selection", "selection", "generation_method", "generation_version")
    metadata = {key: deepcopy(worksheet[key]) for key in fields if key in worksheet}
    metadata["source"] = _public_source(worksheet.get("source", {}))
    return _validate_tables({"Questions": questions, "Candidates": candidates, "SourceEntries": sources, "Metadata": [["key", "value"], *[[key, _cell(value)] for key, value in metadata.items()]]})


def _writing_worksheet_tables(worksheet: dict) -> dict:
    questions = [["number", "question_id", "word", "answer_text", "source_word", "original_answer_text", "word_edited", "answer_edited", "source_entry_ids_json", "review_status", "review_evidence_json", "question_json"]]
    for number, question in enumerate(worksheet["questions"], 1):
        main = {key: value for key, value in question.items() if key != "source_snapshot"}
        questions.append([number, question["id"], question["word"], question["answer_text"], question["source_word"], question["original_answer_text"], question["word_edited"], question["answer_edited"], _json(question["source_entry_ids"]), question["review_status"], _json(question.get("review_evidence")), _json(main)])
    sources = [["entry_id", "unit_id", "word", "meaning_raw", "meaning", "entry_json"]]
    for entry in worksheet["source_entries"]:
        sources.append([entry["id"], entry["unit_id"], entry["word"], entry.get("meaning_raw", ""), entry.get("meaning", ""), _json(entry)])
    fields = ("id", "schema_version", "worksheet_kind", "title", "instruction", "created_at", "selected_unit_ids", "selected_units", "bank_id", "selection_counts", "range_label", "partial_selection", "selection", "generation_method", "generation_version")
    metadata = {key: deepcopy(worksheet[key]) for key in fields if key in worksheet}
    metadata["source"] = _public_source(worksheet.get("source", {}))
    return _validate_tables({"Questions": questions, "SourceEntries": sources, "Metadata": [["key", "value"], *[[key, _cell(value)] for key, value in metadata.items()]]})


class DriveSync:
    def __init__(self, drive, sheets, vocab_folder_id: str, exam_folder_id: str, expected_account: str | None = EXPECTED_ACCOUNT, *, media_factory: Callable | None = None):
        self.drive = drive
        self.sheets = sheets
        self.vocab_folder_id = _id(vocab_folder_id, "단어은행 폴더")
        self.exam_folder_id = _id(exam_folder_id, "시험지 폴더")
        self.expected_account = _text(expected_account, "승인된 계정").strip().lower()
        self.media_factory = media_factory
        self._lock = threading.RLock()

    def _verify_identity(self):
        try:
            result = self.drive.about().get(fields="user(emailAddress)").execute()
        except Exception as error:
            raise DriveSyncError("Drive 계정을 확인할 수 없어 저장하지 않았습니다.", "account_unverified") from error
        email = result.get("user", {}).get("emailAddress") if isinstance(result, dict) else None
        account = email.strip().lower() if isinstance(email, str) else ""
        if account != self.expected_account:
            raise DriveSyncError("승인된 GCP·Drive 계정과 연결된 계정이 달라 저장하지 않았습니다.", "account_mismatch")

    def _verify_folder(self, folder_id: str):
        try:
            metadata = self.drive.files().get(fileId=folder_id, fields="id,mimeType,trashed,capabilities(canEdit)").execute()
        except Exception as error:
            raise DriveSyncError("지정된 Drive 폴더를 확인할 수 없어 저장하지 않았습니다.", "invalid_destination") from error
        if metadata.get("mimeType") != FOLDER_MIME or metadata.get("trashed") is True or metadata.get("capabilities", {}).get("canEdit") is False:
            raise DriveSyncError("지정된 Drive 폴더에 저장할 수 없습니다.", "invalid_destination")

    @staticmethod
    def _properties(kind: str, key: str) -> dict:
        return {"feature": FEATURE_TAG, "kind": kind, "record_key": key}

    def _target(self, folder_id: str, kind: str, key: str, mime: str, existing_id: str | None = None) -> dict | None:
        properties = self._properties(kind, key)
        if existing_id:
            result = self.drive.files().get(fileId=_id(existing_id, "기존 파일"), fields=FILE_FIELDS).execute()
            self._validate_target(result, folder_id, properties, mime)
            return result
        query = f"'{folder_id}' in parents and trashed = false" + "".join(f" and appProperties has {{ key='{prop}' and value='{value}' }}" for prop, value in properties.items())
        result = self.drive.files().list(q=query, spaces="drive", fields=f"files({FILE_FIELDS}),nextPageToken", pageSize=100).execute()
        files = result.get("files", [])
        if len(files) > 1 or result.get("nextPageToken"):
            raise DriveSyncError("같은 기록의 Drive 파일이 둘 이상입니다. 사용할 파일 ID를 명시해 주세요.", "ambiguous_target")
        if not files:
            return None
        self._validate_target(files[0], folder_id, properties, mime)
        return files[0]

    @staticmethod
    def _validate_target(metadata: dict, folder_id: str, properties: dict, mime: str):
        if (
            not metadata.get("id") or folder_id not in metadata.get("parents", [])
            or metadata.get("trashed") is True or metadata.get("mimeType") != mime
            or any(metadata.get("appProperties", {}).get(key) != value for key, value in properties.items())
            or metadata.get("capabilities", {}).get("canEdit") is False
        ):
            raise DriveSyncError("기존 파일은 지정 폴더의 이 기능 전용 파일이어야 합니다. 기존 시험지 파일을 덮어쓰지 않았습니다.", "invalid_existing_file")

    def _media(self, path: Path, mime: str):
        if self.media_factory:
            return self.media_factory(str(path), mimetype=mime, resumable=True)
        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as error:
            raise DriveSyncError("운영 앱에 google-api-python-client를 설치한 뒤 Drive 저장을 연결하세요.", "missing_dependency") from error
        return MediaFileUpload(str(path), mimetype=mime, resumable=True)

    def _upload(self, path: Path, folder: str, name: str, kind: str, key: str, digest: str, mime: str, existing_id: str | None, immutable: bool = False) -> dict:
        target = self._target(folder, kind, key, mime, existing_id)
        properties = self._properties(kind, key) | {"content_sha": digest}
        if target:
            old_digest = target.get("appProperties", {}).get("content_sha")
            if immutable and old_digest != digest:
                raise DriveSyncError("원본 파일의 내용이 달라 기존 원본을 바꾸지 않았습니다.", "original_changed")
            if old_digest == digest and (immutable or target.get("name") == name):
                return target
            arguments = {"fileId": target["id"], "body": {"name": name, "appProperties": properties}, "fields": FILE_FIELDS}
            if old_digest != digest:
                arguments["media_body"] = self._media(path, mime)
            return self.drive.files().update(**arguments).execute()
        return self.drive.files().create(body={"name": name, "mimeType": mime, "parents": [folder], "appProperties": properties}, media_body=self._media(path, mime), fields=FILE_FIELDS).execute()

    def _sheet(self, folder: str, title: str, kind: str, key: str, tables: dict, existing_id: str | None, progress: dict, progress_key: str) -> dict:
        target = self._target(folder, kind, key, SHEET_MIME, existing_id)
        if target is None:
            target = self.drive.files().create(body={"name": title, "mimeType": SHEET_MIME, "parents": [folder], "appProperties": self._properties(kind, key)}, fields=FILE_FIELDS).execute()
        progress[progress_key] = target["id"]
        digest = _hash(tables)
        if target.get("appProperties", {}).get("content_sha") == digest:
            if target.get("name") != title:
                target = self.drive.files().update(fileId=target["id"], body={"name": title}, fields=FILE_FIELDS).execute()
            return target
        self._write_tabs(target["id"], tables)
        return self.drive.files().update(fileId=target["id"], body={"name": title, "appProperties": self._properties(kind, key) | {"content_sha": digest}}, fields=FILE_FIELDS).execute()

    def _write_tabs(self, spreadsheet_id: str, tables: dict):
        document = self.sheets.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets(properties(sheetId,title,gridProperties))").execute()
        sheet_properties = [sheet["properties"] for sheet in document.get("sheets", [])]
        by_title = {sheet["title"]: sheet for sheet in sheet_properties}
        used_ids = {sheet["sheetId"] for sheet in sheet_properties}
        requests = []
        # A just-created Drive spreadsheet has a single Sheet1 (localized in
        # some accounts); repurpose only a solitary, still-unmanaged first tab.
        if len(sheet_properties) == 1 and sheet_properties[0]["title"] not in tables:
            first = deepcopy(sheet_properties[0])
            first["title"] = next(iter(tables))
            requests.append({"updateSheetProperties": {"properties": {"sheetId": first["sheetId"], "title": first["title"], "gridProperties": {"frozenRowCount": 1}}, "fields": "title,gridProperties.frozenRowCount"}})
            by_title[first["title"]] = first
        managed_ids = {sheet["sheetId"] for title, sheet in by_title.items() if title in tables}
        total_cells = sum(s.get("gridProperties", {}).get("rowCount", 1000) * s.get("gridProperties", {}).get("columnCount", 26) for s in sheet_properties if s["sheetId"] not in managed_ids)
        for title, rows in tables.items():
            width = len(rows[0])
            required_rows = max(len(rows), 2)
            if title in by_title:
                sheet = by_title[title]
                grid = sheet.get("gridProperties", {})
                row_count = max(required_rows, grid.get("rowCount", 1000))
                column_count = max(width, grid.get("columnCount", 26))
                sheet_id = sheet["sheetId"]
                if row_count != grid.get("rowCount") or column_count != grid.get("columnCount"):
                    requests.append({"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"rowCount": row_count, "columnCount": column_count}}, "fields": "gridProperties.rowCount,gridProperties.columnCount"}})
            else:
                sheet_id = 0
                while sheet_id in used_ids:
                    sheet_id += 1
                used_ids.add(sheet_id)
                row_count, column_count = required_rows, width
                requests.append({"addSheet": {"properties": {"sheetId": sheet_id, "title": title, "gridProperties": {"rowCount": row_count, "columnCount": column_count, "frozenRowCount": 1}}}})
            total_cells += row_count * column_count
            requests.append({"updateCells": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": row_count, "startColumnIndex": 0, "endColumnIndex": column_count},
                "rows": [{"values": [{"userEnteredValue": {"stringValue": cell}} for cell in row]} for row in rows],
                "fields": "userEnteredValue",
            }})
        if total_cells > MAX_SPREADSHEET_CELLS:
            raise DriveSyncError("기존 시트 크기를 포함한 전체 셀 수 제한을 초과했습니다.", "sheet_limit")
        self.sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests, "includeSpreadsheetInResponse": False}).execute()

    def sync_bank(self, bank: dict, source_path: str | Path, existing: dict | None = None) -> dict:
        path = _path(source_path, {".pdf", ".xlsx"})
        tables = _bank_tables(bank)
        digest = _file_hash(path)
        claimed_hash = bank.get("source", {}).get("sha256")
        if claimed_hash and claimed_hash != digest:
            raise DriveSyncError("단어은행의 원본 파일 해시가 제공 파일과 다릅니다.", "source_mismatch")
        original_name = _text(bank.get("source", {}).get("filename", path.name), "원본 파일명")
        title = _text(bank.get("title", Path(original_name).stem), "단어은행 제목") + " 단어은행"
        existing = existing or {}
        progress = {}
        with self._lock:
            self._verify_identity()
            self._verify_folder(self.vocab_folder_id)
            try:
                mime = "application/pdf" if path.suffix.lower() == ".pdf" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                # Refuse invalid supplied IDs before creating any new artifact.
                if existing.get("original_id"):
                    self._target(self.vocab_folder_id, "original_source", digest, mime, existing["original_id"])
                if existing.get("bank_sheet_id"):
                    self._target(self.vocab_folder_id, "word_bank", digest, SHEET_MIME, existing["bank_sheet_id"])
                original = self._upload(path, self.vocab_folder_id, original_name, "original_source", digest, digest, mime, existing.get("original_id"), immutable=True)
                progress["original_id"] = original["id"]
                for row in tables["Metadata"]:
                    if row[0] == "original_id":
                        row[1] = original["id"]
                    elif row[0] == "original_link":
                        row[1] = self._link(original)
                sheet = self._sheet(self.vocab_folder_id, title, "word_bank", digest, tables, existing.get("bank_sheet_id"), progress, "bank_sheet_id")
                return {**progress, "links": {"original": self._link(original), "bank": self._link(sheet, spreadsheet=True)}}
            except DriveSyncError:
                raise
            except Exception as error:
                raise DriveSyncError("Drive 단어은행 저장에 실패했습니다. 기존 파일은 보존했으며 같은 작업을 다시 시도할 수 있습니다.", retry_ids=progress) from error

    def sync_worksheet(self, worksheet: dict, student_path: str | Path, answer_path: str | Path, existing: dict | None = None, *, draft: bool = False) -> dict:
        """Publish reviewed output, or an explicitly marked renderer draft."""
        tables = _worksheet_tables(worksheet, require_reviewed=not draft)
        student, answer = _path(student_path, {".pdf"}), _path(answer_path, {".pdf"})
        key = _hash(_text(worksheet.get("id"), "시험지 ID"))
        title = _text(worksheet.get("title"), "시험지 제목")
        hashes = {"student": _file_hash(student), "answer": _file_hash(answer)}
        existing = existing or {}
        progress = {}
        with self._lock:
            self._verify_identity()
            self._verify_folder(self.exam_folder_id)
            try:
                for result_key, kind, mime in (("sheet_id", "worksheet", SHEET_MIME), ("student_id", "student_pdf", "application/pdf"), ("answer_id", "answer_pdf", "application/pdf")):
                    if existing.get(result_key):
                        self._target(self.exam_folder_id, kind, key, mime, existing[result_key])
                sheet = self._sheet(self.exam_folder_id, title, "worksheet", key, tables, existing.get("sheet_id"), progress, "sheet_id")
                student_file = self._upload(student, self.exam_folder_id, title + " 문제지.pdf", "student_pdf", key, hashes["student"], "application/pdf", existing.get("student_id"))
                progress["student_id"] = student_file["id"]
                answer_file = self._upload(answer, self.exam_folder_id, title + " 정답지.pdf", "answer_pdf", key, hashes["answer"], "application/pdf", existing.get("answer_id"))
                progress["answer_id"] = answer_file["id"]
                return {**progress, "links": {"sheet": self._link(sheet, spreadsheet=True), "student": self._link(student_file), "answer": self._link(answer_file)}}
            except DriveSyncError:
                raise
            except Exception as error:
                raise DriveSyncError("Drive 시험지 저장에 실패했습니다. 기존 파일은 보존했으며 같은 작업을 다시 시도할 수 있습니다.", retry_ids=progress) from error

    def sync_worksheet_record(self, worksheet: dict, existing: dict | None = None) -> dict:
        """Persist edited drafts to Sheets without touching the existing PDFs."""
        tables = _worksheet_tables(worksheet, require_reviewed=False)
        key = _hash(_text(worksheet.get("id"), "시험지 ID"))
        progress = {}
        with self._lock:
            self._verify_identity()
            self._verify_folder(self.exam_folder_id)
            try:
                sheet = self._sheet(self.exam_folder_id, _text(worksheet.get("title"), "시험지 제목"), "worksheet", key, tables, (existing or {}).get("sheet_id"), progress, "sheet_id")
                return {**(existing or {}), **progress, "links": {**(existing or {}).get("links", {}), "sheet": self._link(sheet, spreadsheet=True)}}
            except DriveSyncError:
                raise
            except Exception as error:
                raise DriveSyncError("Drive 시험지 저장에 실패했습니다. 수정값을 보존하고 다시 시도하세요.", retry_ids=progress) from error

    @staticmethod
    def _link(file: dict, spreadsheet: bool = False) -> str:
        if spreadsheet:
            return f"https://docs.google.com/spreadsheets/d/{file['id']}/edit"
        return file.get("webViewLink") or f"https://drive.google.com/file/d/{file['id']}/view"
