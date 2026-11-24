import io
import re
import zipfile

from openpyxl import load_workbook
from feature_word_meaning.excel_importer import source_rows, validate_xlsx_archive


def validate_xlsx(data):
    try:
        validate_xlsx_archive(data)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(e.file_size for e in archive.infolist()) > 50 * 1024 * 1024:
                raise ValueError("압축 해제 크기가 너무 큽니다.")
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            days = [s for s in wb.sheetnames if re.fullmatch(r"Day\s+\d{1,3}", s)]
            if not days:
                raise ValueError("Day 01 형식의 시트가 필요합니다.")
            numbers=[int(day.split()[-1]) for day in days]
            if len(set(numbers)) != len(numbers) or any(not 1 <= n <= 365 for n in numbers):
                raise ValueError("Day는 중복 없이 1~365 범위로 작성해 주세요.")
            for day in days:
                rows = source_rows(wb[day])
                header = next(rows, ())
                if not {"단어", "뜻"}.issubset({str(v).strip() for v in header}):
                    raise ValueError(f"{day}: 단어·뜻 열이 필요합니다.")
                for _ in rows:
                    pass
        finally:
            wb.close()
    except (zipfile.BadZipFile, KeyError) as e:
        raise ValueError("유효한 xlsx 파일이 아닙니다.") from e
