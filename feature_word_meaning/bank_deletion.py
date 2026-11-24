"""Scoped permanent bank removal with a durable retry journal."""
from copy import deepcopy
from pathlib import Path

from fastapi import HTTPException

from .deletion import _metadata, _owned_file, execute_drive_deletion
from .drive_sync import DriveSyncError, FEATURE_TAG, SHEET_MIME, _hash
from .storage import DriveJsonStore


def plan_bank_files(backend, bank, imports, shared):
    drive, _, folders = backend._context()
    source = bank.get('source', {})
    digest = source.get('sha256')
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise DriveSyncError('단어장 원본 해시를 확인할 수 없습니다.')
    ordered = []
    if not shared:
        mime = 'application/pdf' if source.get('format') == 'pdf' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        kinds = {'original_source': mime, 'word_bank': SHEET_MIME}
        query = f"'{folders['vocab']}' in parents and appProperties has {{ key='feature' and value='{FEATURE_TAG}' }} and appProperties has {{ key='record_key' and value='{digest}' }}"
        files, token = {}, None
        while True:
            args = {'q': query, 'fields': 'files(id,mimeType,parents,appProperties,capabilities(canDelete)),nextPageToken', 'pageSize': 1000}
            if token:
                args['pageToken'] = token
            result = drive.files().list(**args).execute()
            files.update({item['id']: item for item in result.get('files', [])})
            token = result.get('nextPageToken')
            if not token:
                break
        explicit = [(bank.get('drive', {}).get('bank_sheet_id'), 'word_bank'),
                    (bank.get('drive', {}).get('original_id'), 'original_source'),
                    (source.get('original_id'), 'original_source')]
        explicit += [(job.get('payload', {}).get('original_id'), 'original_source') for job in imports]
        for file_id, kind in explicit:
            if file_id:
                item = files.get(file_id) or _metadata(drive, file_id)
                if item:
                    _owned_file(item, folders['vocab'], kind, digest, kinds[kind])
                    files[file_id] = item
        for item in files.values():
            kind = item.get('appProperties', {}).get('kind')
            if kind not in kinds:
                raise DriveSyncError('단어장 소유 파일을 확인할 수 없습니다.')
            _owned_file(item, folders['vocab'], kind, digest, kinds[kind])
            ordered.append(item['id'])
    # The bank state is last so failures remain retryable with the original version.
    for kind, records in [('jobs', imports), ('banks', [bank])]:
        for record in records:
            _, _, files = backend._find(kind, record['id'])
            for item in files:
                metadata = _metadata(drive, item['id'])
                if metadata:
                    _owned_file(metadata, folders['states'], 'state_' + kind, _hash(record['id']), 'application/json')
                    ordered.append(metadata['id'])
    return list(dict.fromkeys(ordered))


def permanently_delete_bank(service, bank):
    store, bank_id = service.store, bank['id']
    source = bank.get('source', {})
    digest = source.get('sha256')
    imports = [job for job in store.list('jobs') if job.get('kind') == 'import'
               and (job.get('result_id') == bank_id or (digest and job.get('payload', {}).get('sha256') == digest))]
    shared = any(other['id'] != bank_id and digest and other.get('source', {}).get('sha256') == digest for other in store.list('banks'))
    try:
        plan = plan_bank_files(service.backend, bank, imports, shared) if isinstance(store, DriveJsonStore) else []
        path = None
        if not shared and source.get('stored_name'):
            name = source['stored_name']
            base = (store.root / 'uploads').resolve()
            path = store.root / 'uploads' / name
            if Path(name).name != name or path.is_symlink() or not path.resolve().is_relative_to(base):
                raise ValueError('원본 파일 경로를 확인할 수 없습니다.')
        if not bank.get('deletion_pending'):
            bank.update(deletion_pending=True, deletion_version=bank['version'])
            bank = store.put_metadata('banks', bank, expected_version=bank['version'])
        # Old worksheets used a live bank reference; retain their own frozen copy.
        for worksheet in store.list('worksheets'):
            if worksheet.get('bank_id') == bank_id and not worksheet.get('generation_bank_snapshot'):
                snapshot = deepcopy(bank)
                for key in ('deletion_pending', 'deletion_version', 'deleted_at'):
                    snapshot.pop(key, None)
                worksheet['generation_bank_snapshot'] = snapshot
                store.put_metadata('worksheets', worksheet, expected_version=worksheet['version'])
        if path:
            path.unlink(missing_ok=True)
        if plan:
            execute_drive_deletion(service.backend, plan)
        for job in imports:
            store.delete('jobs', job['id'])
        store.delete('banks', bank_id)
    except Exception as error:
        raise HTTPException(502, '완전 삭제를 마치지 못했습니다. 완전히 삭제를 다시 시도해 주세요.') from error
    return {'id': bank_id, 'deleted': True, 'mode': 'permanent'}
