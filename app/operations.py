"""Drive-backed generation checkpoints. Retrying never repeats a saved AI result."""
import json
from datetime import datetime, timezone
from . import config, sheets_io
from .google_clients import drive_service, upload_bytes
from .errors import AppError


def folder():
    return sheets_io.get_or_create_folder("생성 작업 기록", sheets_io.get_or_create_folder(config.PROJECT_FOLDER))


def load(request_id):
    parent = folder()
    meta = sheets_io.find_file(request_id + ".json", parent)
    if not meta:
        return None
    data = json.loads(drive_service().files().get_media(fileId=meta["id"]).execute())
    data["_file_id"], data["_folder_id"] = meta["id"], parent
    return data


def save(record):
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    parent = record.get("_folder_id") or folder()
    content = {k: v for k, v in record.items() if not k.startswith("_")}
    meta = upload_bytes(json.dumps(content, ensure_ascii=False).encode(), record["request_id"] + ".json",
                        "application/json", parent, record.get("_file_id"))
    record["_file_id"], record["_folder_id"] = meta["id"], parent


def public(record):
    if not record:
        return {"state": "not_found"}
    return {k: record[k] for k in ("request_id", "state", "result", "error", "updated_at") if k in record}


def worksheet_deletion_files(drive, day, version, request_ids):
    project = sheets_io.get_or_create_folder(config.PROJECT_FOLDER)
    directories = drive.files().list(q=f"'{project}' in parents and name='생성 작업 기록' and mimeType='application/vnd.google-apps.folder' and trashed=false", fields='files(id)').execute().get('files', [])
    if len(directories) > 1:
        raise AppError('작업 기록 폴더가 중복되어 삭제하지 않았습니다.', 409, 'ambiguous_record')
    if not directories:
        return []
    parent, output, token = directories[0]['id'], [], None
    while True:
        args = {'q':f"'{parent}' in parents and mimeType='application/json'", 'fields':'files(id),nextPageToken', 'pageSize':1000}
        if token:
            args['pageToken'] = token
        result = drive.files().list(**args).execute()
        for file in result.get('files', []):
            record = json.loads(drive.files().get_media(fileId=file['id']).execute())
            target = record.get('result') or {}
            params = record.get('params') or {}
            if (record.get('request_id') in request_ids or target.get('day') == day and target.get('version') == version
                    or params.get('day') == day and params.get('version') == version):
                if record.get('state') in {'generating', 'rendering'}:
                    raise AppError('이 시험지의 작업을 완료하거나 정리한 뒤 삭제해 주세요.',409,'active_job')
                output.append(sheets_io.deletion_file(drive, file['id'], parent, 'application/json'))
        token = result.get('nextPageToken')
        if not token:
            return output


def check_request(record, params):
    if record and record["params"] != params:
        raise AppError("같은 작업 번호에 다른 입력을 사용할 수 없습니다.", 409, "request_conflict")
    if record and record["state"] == "generating":
        raise AppError("이 작업은 처리 중이거나 완료 여부 확인이 필요합니다. 새 작업을 만들기 전에 목록을 확인해 주세요.",
                       409, "uncertain", retryable=False)
