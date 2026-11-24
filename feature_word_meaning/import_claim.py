"""Conditional Drive claims for one request-driven vocabulary import step.

Drive v3 media updates ignore If-Match. Use v2's documented file ETag and
conditional update for opted-in import jobs only; all other records retain
their established storage path. Tokens and ETags never enter public responses.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import logging
import time
import uuid

from .drive_sync import DriveSyncError, FEATURE_TAG, _hash
from .storage import ConflictError, now


class ImportClaimLost(ConflictError):
    """Another request owns the durable step; this worker must not write."""


def _alive(claim):
    try:
        return datetime.fromisoformat(claim['expires_at'].replace('Z', '+00:00')).timestamp() > time.time()
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def _v2(backend, drive):
    scope = getattr(backend._thread, 'request_scope', None)
    if scope is not None and 'import_claim_drive' in scope:
        return scope['import_claim_drive']
    from googleapiclient.discovery import build
    client = build('drive', 'v2', http=drive._http, cache_discovery=False)
    if scope is not None:
        scope['import_claim_drive'] = client
    return client


def _snapshot(backend, job_id):
    drive, _, files = backend._find('jobs', job_id)
    if len(files) != 1:
        raise DriveSyncError('등록 작업의 저장 기록을 확인할 수 없습니다.', 'ambiguous_record')
    file_id = files[0]['id']
    client = _v2(backend, drive)
    # Read the ETag before the content. A concurrent content update in between
    # makes the ensuing conditional write fail instead of accepting stale data.
    etag = client.files().get(fileId=file_id, fields='id,etag').execute().get('etag')
    if not etag:
        raise DriveSyncError('등록 작업의 동시 처리 상태를 확인할 수 없습니다.', 'missing_etag')
    value = backend._load(drive, files[0])
    if value.get('id') != job_id:
        raise DriveSyncError('등록 작업의 저장 기록이 일치하지 않습니다.', 'ambiguous_record')
    return client, etag, value


def _conditional_write(client, etag, value):
    from googleapiclient.http import MediaIoBaseUpload
    content = {key: item for key, item in value.items() if key != 'drive_state_id'}
    payload = json.dumps(content, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    properties = {'feature': FEATURE_TAG, 'kind': 'state_jobs',
                  'record_key': _hash(value['id']), 'content_sha': hashlib.sha256(payload).hexdigest()}
    request = client.files().update(fileId=value['drive_state_id'],
        body={'title': value['id'] + '.json', 'properties': [
            {'key': key, 'value': item, 'visibility': 'PRIVATE'} for key, item in properties.items()]},
        media_body=MediaIoBaseUpload(io.BytesIO(payload), mimetype='application/json', resumable=False), fields='id')
    request.headers['If-Match'] = etag
    try:
        result = request.execute()
    except Exception as error:
        if getattr(getattr(error, 'resp', None), 'status', None) == 412:
            raise ImportClaimLost('같은 등록 작업이 이미 진행 중입니다. 저장된 상태로 이어갑니다.') from None
        raise
    return {**value, 'drive_state_id': result['id']}


def _changed(value):
    result = deepcopy(value)
    result['version'] = value.get('version', 0) + 1
    result['updated_at'] = now()
    return result


def claim_import_step(backend, job_id, lease_seconds=900):
    client, etag, previous = _snapshot(backend, job_id)
    if previous.get('status') in {'complete', 'failed', 'interrupted'} or _alive(previous.get('import_step_claim')):
        return None
    token = uuid.uuid4().hex
    value = _changed(previous)
    value['import_step_claim'] = {'token': token, 'expires_at': datetime.fromtimestamp(
        time.time() + lease_seconds, timezone.utc).isoformat()}
    try:
        _conditional_write(client, etag, value)
    except ImportClaimLost:
        return None
    except Exception:
        # The server may have accepted the claim before its response was lost.
        # Reconcile this exact random token before deciding ownership is unknown.
        _, _, current = _snapshot(backend, job_id)
        claim = current.get('import_step_claim') or {}
        if claim.get('token') != token or not _alive(claim):
            raise
    backend._thread.import_claim = {'job_id': job_id, 'token': token}
    return token


def _verify_owner(backend, current):
    local = getattr(backend._thread, 'import_claim', None)
    claim = current.get('import_step_claim') or {}
    if not local or local['job_id'] != current['id'] or local['token'] != claim.get('token') or not _alive(claim):
        raise ImportClaimLost('등록 작업의 실행 상태가 바뀌었습니다. 저장된 상태로 이어갑니다.')
    return claim


def assert_import_claim(backend):
    local = getattr(backend._thread, 'import_claim', None)
    if local:
        _, _, current = _snapshot(backend, local['job_id'])
        _verify_owner(backend, current)


def guarded_import_write(backend, kind, value, previous=None):
    if kind != 'jobs' or value.get('kind') != 'import' or not value.get('automatic_recovery_version') or not (previous or {}).get('drive_state_id'):
        return None
    client, etag, current = _snapshot(backend, value['id'])
    local = getattr(backend._thread, 'import_claim', None)
    if local and local['job_id'] == value['id']:
        claim = _verify_owner(backend, current)
    else:
        claim = current.get('import_step_claim')
        if _alive(claim):
            raise ImportClaimLost('같은 등록 작업이 이미 진행 중입니다. 저장된 상태로 이어갑니다.')
    if current.get('version', 0) != (previous or {}).get('version', 0):
        raise ImportClaimLost('등록 작업이 갱신되었습니다. 저장된 상태로 이어갑니다.')
    value = deepcopy(value)
    value['import_step_claim'] = claim
    return _conditional_write(client, etag, value)


def release_import_step(backend, job_id, token):
    try:
        for _ in range(3):
            client, etag, current = _snapshot(backend, job_id)
            if (current.get('import_step_claim') or {}).get('token') != token:
                return False
            value = _changed(current)
            value['import_step_claim'] = None
            try:
                _conditional_write(client, etag, value)
                return True
            except ImportClaimLost:
                continue
        return False
    except Exception as error:
        # A committed checkpoint remains authoritative if only acknowledgement
        # or release failed. Its bounded lease permits later safe recovery.
        logging.getLogger(__name__).warning('Import claim release unavailable (%s)', type(error).__name__)
        return False
    finally:
        local = getattr(backend._thread, 'import_claim', None)
        if local and local.get('job_id') == job_id and local.get('token') == token:
            del backend._thread.import_claim
