"""Adapt existing Excel vocabulary sheets to the common bank contract."""

from __future__ import annotations

import hashlib
import io
import re
import unicodedata
import zipfile
from pathlib import Path

from openpyxl import load_workbook


XLSX_MAX_UNCOMPRESSED = 256 * 1024 * 1024
XLSX_MAX_MEMBER = 128 * 1024 * 1024
XLSX_MAX_MEMBERS = 10000
XLSX_MAX_ROWS = 100000
XLSX_MAX_COLUMNS = 1024
XLSX_MAX_CELLS = 2000000


def validate_xlsx_archive(content):
    """Reject malformed/oversized containers before persistence or XML parsing.

    This checks package structure, not worksheet names, visible sheets, columns,
    or cell content. Normal and unfamiliar layouts still share the AI importer.
    """
    try:
        source = io.BytesIO(content) if isinstance(content, (bytes, bytearray)) else content
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if not {'[Content_Types].xml', 'xl/workbook.xml', '_rels/.rels'}.issubset(names):
                raise ValueError('올바른 Excel 파일이 아닙니다.')
            if len(names) != len(set(names)) or any(member.flag_bits & 1 for member in members):
                raise ValueError('중복되거나 암호화된 Excel 파일은 등록할 수 없습니다.')
            if (len(members) > XLSX_MAX_MEMBERS
                    or any(member.file_size > XLSX_MAX_MEMBER for member in members)
                    or sum(member.file_size for member in members) > XLSX_MAX_UNCOMPRESSED):
                raise ValueError('Excel 압축 해제 크기가 너무 큽니다. 파일을 나누어 등록해 주세요.')
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as error:
        raise ValueError('올바른 Excel 파일이 아닙니다.') from error


def source_rows(sheet):
    """Read actual cells instead of trusting a potentially inflated dimension.

    Formatting far outside the data must not create millions of empty rows or
    thousands of padding columns. Reject genuinely excessive data explicitly;
    never truncate vocabulary or silently omit a source row.
    """
    reset = getattr(sheet, 'reset_dimensions', None)
    if callable(reset):
        reset()
    cells = 0
    for row_number, row in enumerate(sheet.iter_rows(values_only=True), 1):
        if row_number > XLSX_MAX_ROWS:
            raise ValueError('Excel 행 범위가 너무 큽니다. 데이터가 있는 행만 새 파일에 복사해 등록해 주세요.')
        end = len(row)
        while end and row[end - 1] is None:
            end -= 1
        if end > XLSX_MAX_COLUMNS:
            raise ValueError('Excel 열 범위가 너무 큽니다. 데이터가 있는 열만 새 파일에 복사해 등록해 주세요.')
        cells += end
        if cells > XLSX_MAX_CELLS:
            raise ValueError('Excel 데이터가 너무 많습니다. 파일을 나누어 등록해 주세요.')
        yield row[:end]


def _text(value):
    return "" if value is None else unicodedata.normalize("NFC", str(value)).strip()


def _column_role(value):
    label = re.sub(r"\s+", " ", _text(value)).casefold().strip(" .:=↔")
    aliases = {
        'word': {'단어', '영단어', '어휘', '표제어', 'word', 'english', 'english word', 'headword', 'lexeme', 'vocabulary', 'term'},
        'number': {'번호', '연번', 'no', 'number'},
        '유의어': {'유의어', '동의어', 'synonym', 'synonyms'},
        '반의어': {'반의어', 'antonym', 'antonyms'},
        '파생어': {'파생어', 'derivative', 'derivatives'},
        'metadata': {'확인', '체크', 'check', 'checked', '품사', 'pos', 'part of speech', '단원', '챕터', '차시', 'chapter', 'unit', 'day'},
    }
    if re.fullmatch(r'(?:뜻|의미|뜻풀이|meaning|gloss|korean gloss)(?:\s*\d+)?', label):
        return 'meaning'
    return next((role for role, labels in aliases.items() if label in labels), None)


def structured_sheet_rows(rows):
    """Use explicit headers anywhere in a sheet, never language adjacency.

    Unknown columns/layouts still require the conservative fallback. Every
    nonempty row is retained as either a source entry or explicit metadata.
    """
    rows = [list(row) for row in rows]
    headers = []
    for index, row in enumerate(rows):
        roles = [_column_role(value) for value in row]
        if roles.count('word') == 1 and 'meaning' in roles:
            if any(_text(value) and role is None for value, role in zip(row, roles)):
                return None
            headers.append((index, roles))
    if not headers:
        return None
    roles = headers[0][1]
    if any(other != roles for _, other in headers):
        return None  # A different table layout must not inherit stale columns.
    word_column = roles.index('word')
    meaning_columns = [i for i, role in enumerate(roles) if role == 'meaning']
    header_rows = {index for index, _ in headers}
    number_note = next((i for i, row in enumerate(rows[:headers[0][0]])
        if re.search(r'(?:어휘|단어).*숫자.*문장\s*번호', ' '.join(map(_text, row)))), None)
    result = []
    for index, row in enumerate(rows):
        cells = ['' if value is None else str(value) for value in row]
        if not any(_text(value) for value in row):
            continue
        def raw(column):
            return cells[column] if column < len(cells) else ''
        word_raw = raw(word_column)
        meanings = [i for i in meaning_columns if raw(i)]
        # Titles/notes/section labels outside the declared data columns are
        # retained as metadata, not manufactured vocabulary or unit boundaries.
        number_column = roles.index('number') if 'number' in roles else None
        number = raw(number_column) if number_column is not None else ''
        numbered = bool(re.fullmatch(r'\d+', number))
        metadata = index in header_rows or (not numbered and not _text(word_raw) and not any(_text(raw(i)) for i in meaning_columns))
        if index < headers[0][0] and not metadata:
            return None  # Do not silently discard possible pre-header words.
        unknown = [i for i, value in enumerate(cells) if _text(value) and
                   (i >= len(roles) or roles[i] is None)]
        if unknown and not metadata:
            return None
        item = {'row': index + 1, 'cells': cells, 'header_row': headers[0][0] + 1,
                'metadata': metadata, 'word_column': word_column + 1,
                'meaning_columns': [i + 1 for i in meanings]}
        if not metadata:
            word = _text(word_raw)
            suffix = re.fullmatch(r'(.+?)\s+(\d{1,3}(?:\s*[,·–-]\s*\d{1,3})*)', word) if number_note is not None else None
            if suffix:
                item['sentence_number'] = suffix[2]
                item['sentence_number_note_row'] = number_note + 1
                word = suffix[1]
            item.update(word=word, word_raw=word_raw,
                meaning_raw='\n'.join(raw(i) for i in meanings),
                source_number=int(number) if re.fullmatch(r'\d+', number) else None,
                related={role: _text(raw(i)) for i, role in enumerate(roles)
                         if role in {'유의어', '반의어', '파생어'} and _text(raw(i))})
        result.append(item)
    return result


def import_excel(path, *, progress=None):
    path = Path(path)
    validate_xlsx_archive(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    workbook = load_workbook(path, read_only=True, data_only=True)
    units, entries, warnings = [], [], []
    try:
        for sheet_index, sheet in enumerate(workbook.worksheets):
            catalogue = structured_sheet_rows(source_rows(sheet))
            if catalogue is None:
                warnings.append({"sheet": sheet.title, "message": "단어·뜻 열을 찾지 못했습니다. 이 시트는 확인이 필요합니다."})
                continue
            unit_id = f"u_{sheet_index + 1:03d}"
            units.append({"id": unit_id, "label": sheet.title, "order": sheet_index, "pages": []})
            for item in catalogue:
                if item['metadata']:
                    continue
                row_number = item['row']
                word_raw, meaning_raw = item['word_raw'], item['meaning_raw']
                word, meaning = item['word'], _text(meaning_raw)
                issues = [] if word and meaning else ["영단어 또는 뜻이 비어 있습니다."]
                entries.append({
                    "id": f"e_{sha[:12]}_{sheet_index + 1:03d}_{row_number:05d}",
                    "unit_id": unit_id, "source_number": item['source_number'] or row_number - 1,
                    "word": word, "word_raw": word_raw, "meaning_raw": meaning_raw, "meaning": meaning,
                    "source": {"sheet": sheet.title, "row": row_number,
                        "header_row": item['header_row'], "word_column": item['word_column'],
                        "meaning_columns": item['meaning_columns'],
                        **{key: item[key] for key in ('sentence_number', 'sentence_number_note_row') if key in item},
                        "cells": {str(index + 1): value for index, value in enumerate(item['cells'])}},
                    "status": "review" if issues else "ready", "issues": issues,
                    "related": item['related'],
                })
            if progress:
                progress({"page": sheet_index + 1, "total": len(workbook.worksheets), "message": f"{sheet.title} 읽기 완료"})
    finally:
        workbook.close()
    if not entries:
        raise ValueError("단어·뜻 열이 있는 어휘 시트를 찾지 못했습니다.")
    return {
        "schema_version": 1,
        "source": {"filename": path.name, "sha256": sha, "format": "xlsx", "page_count": None},
        "units": units, "entries": entries, "warnings": warnings,
        "counts": {"total": len(entries), "ready": sum(e["status"] == "ready" for e in entries), "review": sum(e["status"] == "review" for e in entries), "excluded": 0},
    }

