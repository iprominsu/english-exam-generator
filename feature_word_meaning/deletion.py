"""Worksheet visibility and scoped, retryable removal of owned artifacts."""
from pathlib import Path
import shutil

from fastapi import HTTPException

from .drive_sync import DriveSyncError, FEATURE_TAG, SHEET_MIME, _hash
from .storage import DriveJsonStore, StoreError, now


def related_jobs(store, worksheet_id):
    generation_id = 'j_' + worksheet_id.removeprefix('w_')
    return [job for job in store.list('jobs') if job.get('result_id') == worksheet_id
            or job.get('payload', {}).get('worksheet_id') == worksheet_id
            or (job.get('kind') == 'generate' and job['id'] == generation_id)]


def remove_local_files(store, worksheet_id):
    store.path('worksheets', worksheet_id)  # Validate the identifier first.
    base = (store.root / 'pdfs').resolve()
    directory = store.root / 'pdfs' / worksheet_id
    if directory.is_symlink() or not directory.resolve().is_relative_to(base):
        raise ValueError('시험지 파일 경로를 확인할 수 없습니다.')
    if directory.exists():
        if any(item.is_symlink() or not item.resolve().is_relative_to(directory.resolve()) for item in directory.rglob('*')):
            raise ValueError('시험지 파일 경로를 확인할 수 없습니다.')
        shutil.rmtree(directory)


def delete_worksheet(service, worksheet_id, mode, version):
    try:
        service.store.path('worksheets', worksheet_id)
    except StoreError:
        raise HTTPException(422, '올바른 시험지 ID가 필요합니다.') from None
    with service.lock:
        try:
            worksheet = service.store.get('worksheets', worksheet_id)
        except KeyError:
            if mode == 'permanent':
                return {'id': worksheet_id, 'deleted': True, 'mode': mode}
            raise HTTPException(404, '시험지를 찾을 수 없습니다.') from None
        retry_version = worksheet.get('deletion_version') if worksheet.get('deletion_pending') else None
        if mode == 'soft' and worksheet.get('deleted_at') and not worksheet.get('deletion_pending'):
            return {'id': worksheet_id, 'deleted': True, 'mode': mode}
        if version != worksheet['version'] and version != retry_version:
            raise HTTPException(409, '시험지가 변경되었습니다. 목록을 새로고침해 주세요.')
        jobs = related_jobs(service.store, worksheet_id)
        if any(job.get('status') in {'queued', 'running'} or job['id'] in service.active for job in jobs):
            raise HTTPException(409, '이 시험지의 생성·수정 작업이 끝난 뒤 삭제해 주세요.')
        if mode == 'soft':
            if worksheet.get('deletion_pending'):
                raise HTTPException(409, '완전 삭제가 진행 중입니다. 완전히 삭제를 다시 시도해 주세요.')
            if not worksheet.get('deleted_at'):
                worksheet['deleted_at'] = now()
                service.store.put_metadata('worksheets', worksheet, expected_version=worksheet['version'])
            return {'id': worksheet_id, 'deleted': True, 'mode': mode}
        if not worksheet.get('deletion_pending'):
            worksheet.update(deletion_pending=True, deletion_version=worksheet['version'])
            worksheet = service.store.put_metadata('worksheets', worksheet, expected_version=worksheet['version'])
        try:
            if isinstance(service.store, DriveJsonStore):
                # Validate the complete remote bundle before removing any file.
                plan = service.backend.plan_worksheet_deletion(worksheet, jobs)
                remove_local_files(service.store, worksheet_id)
                service.backend.execute_worksheet_deletion(plan)
            else:
                remove_local_files(service.store, worksheet_id)
            for job in jobs:
                service.store.delete('jobs', job['id'])
            service.store.delete('worksheets', worksheet_id)
        except (DriveSyncError, OSError, ValueError) as error:
            raise HTTPException(502, '완전 삭제를 마치지 못했습니다. 남은 파일과 기록을 삭제하려면 다시 시도해 주세요.') from error
        except Exception as error:
            raise HTTPException(502, '저장소 삭제에 실패했습니다. 완전히 삭제를 다시 시도해 주세요.') from error
        return {'id': worksheet_id, 'deleted': True, 'mode': mode}


def _metadata(drive, file_id):
    try:
        return drive.files().get(fileId=file_id, fields='id,mimeType,parents,trashed,appProperties,capabilities(canDelete)').execute()
    except Exception as error:
        if getattr(getattr(error, 'resp', None), 'status', None) == 404:
            return None
        raise


def _list_files(drive, query, fields='files(id,mimeType,parents,trashed,appProperties,capabilities(canDelete)),nextPageToken'):
    """Drive 페이지네이션을 따라 일치하는 파일 메타데이터를 모두 수집한다."""
    files, token = {}, None
    while True:
        args = {'q': query, 'fields': fields, 'pageSize': 1000}
        if token:
            args['pageToken'] = token
        result = drive.files().list(**args).execute()
        files.update({item['id']: item for item in result.get('files', [])})
        token = result.get('nextPageToken')
        if not token:
            return files


def _owned_file(metadata, folder, kind, key, mime, *, label='시험지'):
    props = metadata.get('appProperties', {})
    if (folder not in metadata.get('parents', []) or metadata.get('mimeType') != mime
            or props.get('feature') != FEATURE_TAG or props.get('kind') != kind
            or props.get('record_key') != key or metadata.get('capabilities', {}).get('canDelete') is not True):
        raise DriveSyncError(f'이 {label}에 속한 파일인지 확인할 수 없어 삭제하지 않았습니다.', 'invalid_delete_target')


def _collect_state_file_ids(backend, drive, states_folder, groups, *, label='시험지'):
    """상태 기록 폴더에서 삭제 대상 레코드에 속한 파일 ID를 순서대로 수집한다."""
    ordered = []
    for record_kind, records in groups:
        for record in records:
            _, _, state_files = backend._find(record_kind, record['id'])
            for metadata in state_files:
                # _find 응답에는 canDelete가 없으므로 삭제 직전에 다시 조회해 소유권을 확인한다.
                metadata = _metadata(drive, metadata['id'])
                if metadata:
                    _owned_file(metadata, states_folder, 'state_' + record_kind, _hash(record['id']), 'application/json', label=label)
                    ordered.append(metadata['id'])
    return ordered


def plan_drive_deletion(backend, worksheet, jobs):
    drive, _, folders = backend._context()  # Also verifies the approved account.
    key = _hash(worksheet['id'])
    query = f"'{folders['exam']}' in parents and appProperties has {{ key='feature' and value='{FEATURE_TAG}' }} and appProperties has {{ key='record_key' and value='{key}' }}"
    files = _list_files(drive, query)
    kinds = {'sheet_id': ('worksheet', SHEET_MIME), 'student_id': ('student_pdf', 'application/pdf'), 'answer_id': ('answer_pdf', 'application/pdf')}
    for field, (kind, mime) in kinds.items():
        file_id = worksheet.get('drive', {}).get(field)
        if file_id:
            metadata = files.get(file_id) or _metadata(drive, file_id)
            if metadata:
                _owned_file(metadata, folders['exam'], kind, key, mime)
                files[file_id] = metadata
    mime_by_kind = {kind: mime for kind, mime in kinds.values()}
    for metadata in files.values():
        kind = metadata.get('appProperties', {}).get('kind')
        if kind not in mime_by_kind:
            raise DriveSyncError('알 수 없는 시험지 파일을 삭제하지 않았습니다.', 'invalid_delete_target')
        _owned_file(metadata, folders['exam'], kind, key, mime_by_kind[kind])
    ordered = list(files)
    # Job snapshots also contain generated questions. Remove them before the
    # worksheet state, which remains as the retry journal until the last step.
    ordered.extend(_collect_state_file_ids(backend, drive, folders['states'], [('jobs', jobs), ('worksheets', [worksheet])], label='시험지'))
    return list(dict.fromkeys(ordered))


def execute_drive_deletion(backend, file_ids):
    drive, _, _ = backend._context()
    for file_id in file_ids:
        try:
            drive.files().delete(fileId=file_id).execute()
        except Exception as error:
            if getattr(getattr(error, 'resp', None), 'status', None) != 404:
                raise
