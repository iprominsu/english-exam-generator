"""구형 Excel(.xlsx) 단어장 읽기 호환 모듈."""

from contextlib import closing
from itertools import zip_longest
import re

from feature_word_meaning.excel_importer import source_rows
from . import day_range


def _load_book(xlsx_path: str):
    # 표준 라이브러리 전용 테스트 환경에서도 모듈을 임포트할 수 있도록 지연 임포트한다.
    from openpyxl import load_workbook
    return closing(load_workbook(xlsx_path, read_only=True, data_only=True))


def _read_day_rows(book, number: int, *, strict_cells: bool = False) -> list[dict]:
    """단일 Day 시트를 찾아 검증한 뒤 행 딕셔너리 목록으로 반환한다."""
    names = [name for name in book.sheetnames if re.fullmatch(r"Day\s+0*" + str(number), name)]
    if len(names) != 1:
        raise ValueError(
            "선택한 어휘 파일에 해당 Day 시트가 없습니다."
            if not strict_cells
            else f"DAY {number} 시트가 없거나 중복되어 있습니다."
        )
    rows_iter = source_rows(book[names[0]])
    columns = {}
    for index, name in enumerate(next(rows_iter, ())):
        if name is not None:
            columns.setdefault(str(name).strip(), index)
    missing_content = f"DAY {number}의 단어와 뜻을 확인해 주세요." if strict_cells else "선택한 Day에 단어와 뜻이 필요합니다."
    if not {"단어", "뜻"}.issubset(columns):
        raise ValueError(missing_content)

    parsed = []
    for row_number, raw in enumerate(rows_iter, 2):
        if all(cell is None or (isinstance(cell, str) and not cell.strip()) for cell in raw):
            continue
        row = {name: raw[index] for name, index in columns.items() if index < len(raw)}
        if strict_cells and (not _cell(row.get("단어")) or not _cell(row.get("뜻"))):
            raise ValueError(f"DAY {number}에 단어 또는 뜻이 비어 있는 행이 있습니다.")
        row.update(_source_sheet=names[0], _source_row=row_number)
        parsed.append(row)
    if not parsed:
        raise ValueError(missing_content)
    return parsed


def load_vocab(xlsx_path: str, day: int) -> list[dict]:
    """지정한 Day 시트의 어휘 행 목록을 읽는다."""
    with _load_book(xlsx_path) as book:
        return _read_day_rows(book, day, strict_cells=False)


def available_days(xlsx_path) -> list[int]:
    """워크북에서 사용 가능한 Day 번호 목록을 오름차순으로 반환한다."""
    with _load_book(xlsx_path) as book:
        return sorted({
            int(m.group(1))
            for name in book.sheetnames
            if (m := re.fullmatch(r"Day\s+(\d{1,3})", name)) and 1 <= int(m.group(1)) <= 365
        })


def load_range(xlsx_path, day, day_end=None) -> list[dict]:
    """연속된 Day 범위의 어휘를 행 순서대로 번갈아 병합해 반환한다."""
    start, end = day_range.bounds(day, day_end)
    if start == end:
        return load_vocab(xlsx_path, start)
    chapters = []
    with _load_book(xlsx_path) as book:
        for number in range(start, end + 1):
            rows = _read_day_rows(book, number, strict_cells=True)
            for row in rows:
                row["_source_day"] = number
            chapters.append(rows)
    # Day별 단어 수 차이에 따른 편향을 막기 위해 같은 행 순서끼리 번갈아 배치한다.
    return [row for group in zip_longest(*chapters) for row in group if row is not None]


def _cell(v):
    if v is None:
        return ""
    s = str(v).replace("\n", " ").strip()
    return "" if s.lower() == "nan" else s


def to_bank(rows, day, *, source=None):
    """구형 Excel 단어장을 공통 단어은행(bank) 딕셔너리 형식으로 변환한다(AI/마이그레이션 쓰기 없음)."""
    source = source or {}
    entries, units = [], {}
    for index, row in enumerate(rows):
        number = int(row.get("_source_day", day))
        unit_id = f"legacy-day-{number}"
        units.setdefault(unit_id, {"id": unit_id, "label": f"DAY {number}", "order": number})
        word, meaning = _cell(row.get("단어")), _cell(row.get("뜻"))
        if word and meaning:
            entries.append({
                "id": f"legacy-row-{index}", "unit_id": unit_id, "word": word,
                "meaning": meaning, "meaning_raw": meaning, "status": "ready",
                "source": {"row": row.get("_source_row", index + 2), "sheet": row.get("_source_sheet", f"Day {number}")},
            })
    return {
        "id": source.get("id", ""), "title": source.get("name", ""), "source": source,
        "units": list(units.values()), "entries": entries,
    }
