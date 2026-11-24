"""참고 어휘 로드 (노트북 셀 3-1 이식, 변경 없음)."""

import pandas as pd
import re
from . import day_range


def load_vocab(xlsx_path: str, day: int) -> pd.DataFrame:
    with pd.ExcelFile(xlsx_path) as book:
        matches = [name for name in book.sheet_names if re.fullmatch(r"Day\s+0*" + str(day), name)]
        if len(matches) != 1:
            raise ValueError("선택한 어휘 파일에 해당 Day 시트가 없습니다.")
        df = pd.read_excel(book, sheet_name=matches[0])
    df = df.rename(columns=lambda c: str(c).strip())
    df = df.dropna(how="all").reset_index(drop=True)
    if not {"단어", "뜻"}.issubset(df.columns) or df.empty:
        raise ValueError("선택한 Day에 단어와 뜻이 필요합니다.")
    return df


def available_days(xlsx_path):
    with pd.ExcelFile(xlsx_path) as book:
        return sorted({int(m.group(1)) for name in book.sheet_names
                       if (m := re.fullmatch(r"Day\s+(\d{1,3})", name)) and 1 <= int(m.group(1)) <= 365})


def load_range(xlsx_path, day, day_end=None):
    start, end = day_range.bounds(day, day_end)
    if start == end:
        return load_vocab(xlsx_path, start)
    frames = []
    with pd.ExcelFile(xlsx_path) as book:
        for number in range(start, end + 1):
            names = [name for name in book.sheet_names if re.fullmatch(r"Day\s+0*" + str(number), name)]
            if len(names) != 1:
                raise ValueError(f"DAY {number} 시트가 없거나 중복되어 있습니다.")
            frame = pd.read_excel(book, sheet_name=names[0]).rename(columns=lambda c: str(c).strip()).dropna(how="all")
            if not {"단어", "뜻"}.issubset(frame.columns) or frame.empty:
                raise ValueError(f"DAY {number}의 단어와 뜻을 확인해 주세요.")
            if frame[["단어", "뜻"]].apply(lambda col: col.map(lambda v: not _cell(v))).any().any():
                raise ValueError(f"DAY {number}에 단어 또는 뜻이 비어 있는 행이 있습니다.")
            frame = frame.reset_index(drop=True)
            frame["_source_day"] = number
            frame["_row_order"] = frame.index
            frames.append(frame)
    # Interleave Days so a long source cannot disproportionately favour its first Day.
    result = pd.concat(frames, ignore_index=True).sort_values(["_row_order", "_source_day"]).drop(columns="_row_order").reset_index(drop=True)
    result.attrs.update(day_start=start, day_end=end)
    return result


def _cell(v):
    if v is None:
        return ""
    s = str(v).replace("\n", " ").strip()
    return "" if s.lower() == "nan" else s


def vocab_to_text(vocab_df) -> str:
    lines = []
    for _, r in vocab_df.iterrows():
        parts = [f"단어:{_cell(r.get('단어'))}", f"뜻:{_cell(r.get('뜻'))}"]
        if "_source_day" in vocab_df.columns:
            parts.insert(0, f"DAY:{int(r['_source_day'])}")
        for col in ("파생어", "유의어", "반의어"):
            val = _cell(r.get(col))
            if val:
                parts.append(f"{col}:{val}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def to_bank(vocab_df, day, *, source=None):
    """Read compatibility for old registered workbooks; no AI or migration write."""
    source = source or {}
    entries, units = [], {}
    for index, row in vocab_df.iterrows():
        number = int(row.get('_source_day', day))
        unit_id = f'legacy-day-{number}'
        units.setdefault(unit_id, {'id': unit_id, 'label': f'DAY {number}', 'order': number})
        word, meaning = _cell(row.get('단어')), _cell(row.get('뜻'))
        if word and meaning:
            entries.append({'id': f'legacy-row-{index}', 'unit_id': unit_id, 'word': word,
                            'meaning': meaning, 'meaning_raw': meaning, 'status': 'ready',
                            'source': {'row': int(index) + 2, 'sheet': f'Day {number}'}})
    return {'id': source.get('id', ''), 'title': source.get('name', ''), 'source': source,
            'units': list(units.values()), 'entries': entries}
