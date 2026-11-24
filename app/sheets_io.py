"""구글시트·드라이브 I/O (노트북 셀 3-3 이식 + 목록/삭제용 헬퍼 추가).

시트 스키마: no | type | instruction | answer | explanation | payload_json | set
· no  : 시트 전체 정렬 순서(1이 최신 세트의 1번 문항)
· set : 버전 번호(1 = 가장 먼저 만든 세트)
"""

import json
import math
import re

import pandas as pd
from gspread_dataframe import set_with_dataframe

from . import config, candidates, day_range
from .google_clients import drive_service, sheets_client

LEGACY_META_COLUMNS = ["source_file_id", "source_name", "exam_title", "request_id"]
RANGE_COLUMNS = ["day_start", "day_end"]
DELETION_COLUMNS = ['deleted_at', 'deletion_pending']
TIME_COLUMNS = ['created_at', 'updated_at']
META_COLUMNS = LEGACY_META_COLUMNS + RANGE_COLUMNS + DELETION_COLUMNS + TIME_COLUMNS
# Append W:X; preserve existing candidate columns L:V and historical sheet values.
SHEET_COLUMNS = ["no", "type", "instruction", "answer", "explanation", "payload_json", "set"] + LEGACY_META_COLUMNS + candidates.COLUMNS + RANGE_COLUMNS + DELETION_COLUMNS + TIME_COLUMNS

_DAY_RE = re.compile(r"DAY (\d+)(?:~(\d+))?$")


def questions_to_dataframe(questions) -> pd.DataFrame:
    rows = [
        {
            "no": q["no"],
            "type": q["type"],
            "instruction": q["instruction"],
            "answer": q["answer"],
            "explanation": q.get("explanation", ""),
            "payload_json": json.dumps(q["payload"], ensure_ascii=False),
            "set": q.get("set", ""),
            **{key: q.get(key, "") for key in META_COLUMNS},
            **candidates.row_values(q),
        }
        for q in questions
    ]
    return pd.DataFrame(rows, columns=SHEET_COLUMNS)


def _q_escape(name: str) -> str:
    return name.replace("\\", "\\\\").replace("'", "\\'")


def get_or_create_folder(name: str, parent_id: str = None) -> str:
    q = f"name='{_q_escape(name)}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    found = drive_service().files().list(q=q, fields="files(id,name)").execute(num_retries=2).get("files", [])
    if found:
        return found[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    return drive_service().files().create(body=meta, fields="id").execute()["id"]


def list_subfolders(parent_id: str) -> list:
    q = f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    return (
        drive_service()
        .files()
        .list(q=q, fields="files(id,name)", pageSize=1000)
        .execute()
        .get("files", [])
    )


def list_day_folders() -> list:
    """PROJECT_FOLDER 아래 'DAY N' 폴더들을 day 오름차순으로."""
    project_id = get_or_create_folder(config.PROJECT_FOLDER)
    out = []
    for f in list_subfolders(project_id):
        m = _DAY_RE.search(f["name"])
        if m:
            ctx = {"day": int(m.group(1)), "folder_id": f["id"], "title": f["name"]}
            if m.group(2):
                ctx["day_end"] = day_range.bounds(ctx["day"], int(m.group(2)))[1]
            out.append(ctx)
    out.sort(key=lambda x: x["day"])
    return out


def trash_existing(name, folder_id):
    q = f"name='{_q_escape(name)}' and '{folder_id}' in parents and trashed=false"
    for f in drive_service().files().list(q=q, fields="files(id)").execute(num_retries=2).get("files", []):
        drive_service().files().update(fileId=f["id"], body={"trashed": True}).execute()


def find_sheet_id(title, folder_id):
    q = (
        f"name='{_q_escape(title)}' and '{folder_id}' in parents and trashed=false "
        f"and mimeType='application/vnd.google-apps.spreadsheet'"
    )
    files = drive_service().files().list(q=q, fields="files(id,name)").execute(num_retries=2).get("files", [])
    return files[0]["id"] if files else None


def deletion_file(drive, file_id, folder_id, mime):
    meta = drive.files().get(fileId=file_id, fields='id,name,parents,mimeType,capabilities(canDelete)').execute()
    if folder_id not in meta.get('parents', []) or meta.get('mimeType') != mime or meta.get('capabilities', {}).get('canDelete') is not True:
        raise ValueError('이 시험지에 속한 파일인지 확인할 수 없어 삭제하지 않았습니다.')
    return meta


def deletion_files(drive, name, folder_id, mime):
    query = f"name='{_q_escape(name)}' and '{folder_id}' in parents and mimeType='{mime}'"
    files, token = [], None
    while True:
        args = {'q':query, 'fields':'files(id),nextPageToken', 'pageSize':1000}
        if token:
            args['pageToken'] = token
        result = drive.files().list(**args).execute()
        files.extend(deletion_file(drive, item['id'], folder_id, mime) for item in result.get('files', []))
        token = result.get('nextPageToken')
        if not token:
            return files


def execute_deletion(drive, files):
    for meta in files:
        try:
            drive.files().delete(fileId=meta['id']).execute()
        except Exception as error:
            if getattr(getattr(error, 'resp', None), 'status', None) != 404:
                raise


def find_file(name, folder_id):
    q = f"name='{_q_escape(name)}' and '{folder_id}' in parents and trashed=false"
    files = (
        drive_service()
        .files()
        .list(q=q, fields="files(id,name,webViewLink,createdTime,modifiedTime)")
        .execute(num_retries=2)
        .get("files", [])
    )
    return files[0] if files else None


def day_ctx(day: int, source_name: str = None, day_end=None) -> dict:
    """Select a book's Day folder for generation; read callers resolve by version."""
    project_id = get_or_create_folder(config.PROJECT_FOLDER)
    _, end = day_range.bounds(day, day_end)
    matches = [f for f in list_subfolders(project_id)
               if (m := _DAY_RE.search(f['name'])) and int(m.group(1)) == day and int(m.group(2) or m.group(1)) == end]
    title = config.day_container_title(day, source_title(source_name)) if source_name else config.day_title(day)
    if end != day:
        title += f"~{end}"
    if source_name:
        matches = [f for f in matches if f['name'] == title]
    if len(matches) > 1:
        raise ValueError(f'DAY {day} 폴더를 하나로 정할 수 없습니다. 교재와 버전을 확인해 주세요.')
    if matches:
        return {'day': day, 'title': matches[0]['name'], 'folder_id': matches[0]['id'], **({'day_end': end} if end != day else {})}
    folder_id = get_or_create_folder(title, parent_id=project_id)
    return {"day": day, "title": title, "folder_id": folder_id, **({'day_end': end} if end != day else {})}


def read_questions_from_ws(ws, exam_count: int):
    """시트 → 문항 리스트. 'set'이 비어 있으면 no/exam_count로 역산해 채운다."""
    return questions_from_records(ws.get_all_records(), exam_count)


def questions_from_records(records, exam_count):
    qs = []
    for r in records:
        if str(r.get("no", "")).strip() == "":
            continue
        s = str(r.get("set", "")).strip()
        qs.append(
            {
                "no": int(r["no"]),
                "type": r["type"],
                "instruction": r["instruction"],
                "answer": int(r["answer"]),
                "explanation": str(r.get("explanation", "") or ""),
                "payload": json.loads(r["payload_json"]),
                "set": int(s) if s.isdigit() else None,
                **{key: str(r.get(key, "") or "") for key in META_COLUMNS},
                **candidates.from_row(r),
            }
        )
    if qs and any(q["set"] is None for q in qs):
        total = math.ceil(max(q["no"] for q in qs) / exam_count)
        for q in qs:
            if q["set"] is None:
                q["set"] = total - (q["no"] - 1) // exam_count
    return qs


def folder_inventory(folder_id):
    """One paginated Drive read per folder, shared only within this listing."""
    files, token = [], None
    while True:
        args = {'q': f"'{_q_escape(folder_id)}' in parents and trashed=false",
                'fields': 'nextPageToken,files(id,name,mimeType,webViewLink,createdTime,modifiedTime)', 'pageSize': 1000}
        if token:
            args['pageToken'] = token
        result = drive_service().files().list(**args).execute(num_retries=2)
        files.extend(result.get('files', []))
        token = result.get('nextPageToken')
        if not token:
            return files


def folder_inventories(folder_ids):
    """Fetch only the known project folders in bounded, paginated batches."""
    folders = list(dict.fromkeys(folder_ids))
    if len(folders) <= 1:
        return {folder: folder_inventory(folder) for folder in folders}
    inventories = {folder: [] for folder in folders}
    drive = drive_service()
    for offset in range(0, len(folders), 50):
        batch = folders[offset:offset + 50]
        parents = " or ".join(f"'{_q_escape(folder)}' in parents" for folder in batch)
        token = None
        while True:
            args = {'q': f'trashed=false and ({parents})',
                    'fields': 'nextPageToken,files(id,name,mimeType,parents,webViewLink,createdTime,modifiedTime)',
                    'pageSize': 1000}
            if token:
                args['pageToken'] = token
            result = drive.files().list(**args).execute(num_retries=2)
            for item in result.get('files', []):
                for folder in item.get('parents', []):
                    if folder in batch:
                        inventories[folder].append(item)
            token = result.get('nextPageToken')
            if not token:
                break
    return inventories


def listing_questions(ctx, files, exam_count):
    titles = [ctx['title']]
    if ctx.get('day_end', ctx['day']) == ctx['day']:
        titles += [f"DAY {ctx['day']}", config.day_title(ctx['day'])]
    sheet = next((item for title in titles for item in files
                  if item['name'] == title and item.get('mimeType') == 'application/vnd.google-apps.spreadsheet'), None)
    if not sheet:
        return []
    # A range without a sheet name selects the first worksheet, just like
    # sheet1. Keep QuotaAwareHTTPClient's pacing/retries and account credentials,
    # but avoid reading spreadsheet metadata before each listing-only read.
    values = sheets_client().http_client.values_get(sheet['id'], 'A:AZ').get('values', [])
    if not values:
        return []
    header = values[0]
    if len(set(header)) != len(header):
        raise ValueError('시험지 시트의 열 이름이 중복되어 있습니다.')
    records = [dict(zip(header, row)) for row in values[1:]]
    return questions_from_records(records, exam_count)


def open_day_sheet(ctx, create: bool = False, exam_count: int = 10):
    """DAY 폴더의 시트를 연다 → (worksheet, 기존문항, spreadsheet)."""
    sid = find_sheet_id(ctx["title"], ctx["folder_id"])
    for old_title in (() if ctx.get('day_end', ctx['day']) != ctx['day'] else (f"DAY {ctx['day']}", config.day_title(ctx['day']))):
        if not sid and ctx['title'] != old_title:
            sid = find_sheet_id(old_title, ctx['folder_id'])
    if sid:
        sh = sheets_client().open_by_key(sid)
        ws = sh.sheet1
        return ws, read_questions_from_ws(ws, exam_count), sh
    if not create:
        return None, [], None
    sh = sheets_client().create(ctx["title"])
    drive_service().files().update(fileId=sh.id, addParents=ctx["folder_id"], fields="id,parents").execute()
    return sh.sheet1, [], sh


def total_sets(questions) -> int:
    return max((q["set"] for q in questions), default=0)


def write_all(ws, questions) -> None:
    # One values update: never clear the only copy before a second network write.
    values = [SHEET_COLUMNS] + questions_to_dataframe(questions).fillna("").values.tolist()
    size = max(len(values), len(ws.get_all_values()))
    if ws.row_count < size or ws.col_count < len(SHEET_COLUMNS):
        ws.resize(rows=max(size, ws.row_count), cols=max(len(SHEET_COLUMNS), ws.col_count))
    values += [[""] * len(SHEET_COLUMNS) for _ in range(size - len(values))]
    column, number = '', len(SHEET_COLUMNS)
    while number:
        number, remainder = divmod(number - 1, 26)
        column = chr(65 + remainder) + column
    ws.update(range_name=f"A1:{column}{size}", values=values, value_input_option="RAW")


def source_title(name):
    text = re.sub(r"\.xlsx$", "", name, flags=re.I)
    if "고교내신" in text:
        return "부스터보카 고교내신"
    if "부스터" in text and "수능" in text:
        return "부스터보카 수능"
    return text[:100].strip() or "어휘 시험지"


def version_ctx(ctx, questions):
    first = questions[0] if questions else {}
    # Metadata-free historical PDFs keep their original names after container renaming.
    fallback = config.day_title(ctx['day']) if re.fullmatch(r'DAY \d+', ctx['title']) else ctx['title']
    return {**ctx, "title": first.get("exam_title") or fallback,
            **day_range.stored(ctx, first),
            "source_file_id": first.get("source_file_id", ""), "source_name": first.get("source_name", "")}


def pdf_name_for(title: str, version: int) -> str:
    return f"{title}.pdf" if version == 1 else f"{title} (v{version}).pdf"
