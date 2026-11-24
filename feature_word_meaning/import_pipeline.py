"""Connect durable learning jobs to the existing Drive-backed bank API.

This adapter is explicitly enabled by the host. It never upgrades an existing
job, starts AI from an edit request, or replaces the existing Drive identities.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import threading
import time
import uuid

from . import ai_importer
from .import_engine import (ENGINE_VERSION, PREPARATION_POLICY, EngineError, LearningEngine, LearningReviewRequired, SourceChanged,
    WorkBusy, assert_source_ready, before_review, build_manifest, digest, generation_eligible, generation_version, input_fingerprint)
from .source_integrity import (assert_source_manifest, freeze_source_manifest,
    merge_manual_edits, refresh_bank_state)
from .storage import ConflictError, now


class SourceAIUnavailable(EngineError):
    code = 'source_ai_unavailable'


class _PrepareLease:
    def __init__(self, coordinator, key, token, cancelled):
        self.coordinator, self.key, self.token, self.cancelled = coordinator, key, token, cancelled

    def check(self, record):
        claim = (record or {}).get('claim') or {}
        if (self.cancelled.is_set() or claim.get('token') != self.token
                or claim.get('expires_at', 0) <= self.coordinator.clock()):
            self.cancelled.set()
            raise WorkBusy('원문 처리 실행 소유권이 변경되었습니다.')

    def assert_owned(self):
        self.check(self.coordinator.store.get(self.key))


class DurableImportCoordinator:
    ADMISSION_KEY = 'control/admission'

    def __init__(self, service, state_store, dispatcher, *, structured_provider,
                 account_key, reuse=True, clock=time.time):
        if not callable(structured_provider) or not account_key:
            raise ValueError('An approved structured provider and shared account key are required')
        self.service, self.store, self.dispatcher = service, state_store, dispatcher
        self.provider, self.account_key, self.clock = structured_provider, account_key, clock
        self.engine = LearningEngine(state_store, provider=self._learning,
            validate=self._validate_learning,
            current_source_version=self._current_version, current_bank=self._current_bank,
            publish=self._publish, readback=self._readback, reuse=reuse,
            rebind_cache=ai_importer.rebind_cached_learning, clock=clock,
            publication_context=self._publication_context, versioned_provider=True)

    @staticmethod
    def new_fields():
        return {'engine_version': ENGINE_VERSION, 'source_status': 'pending',
                'learning_status': 'pending', 'publish_status': 'pending',
                'preparation_policy': PREPARATION_POLICY, 'registration_status': 'pending',
                'registration_policy': 'registration-audit-v1'}

    @staticmethod
    def _source_status(bank):
        return 'needs_review' if (bank.get('page_issues')
            or any(e.get('status') != 'excluded' and (e.get('status') == 'review' or not generation_eligible(e))
                for e in bank.get('entries', []))
            or any(u.get('status') == 'review' for u in bank.get('units', []))) else 'complete'

    @staticmethod
    def _invalid_learning_inputs(bank):
        return [e for e in bank.get('entries', []) if e.get('status') != 'excluded' and not generation_eligible(e)]

    def _current_bank(self, bank_id):
        return self.service.get('banks', bank_id)

    def assert_new_import_allowed(self):
        """Read this namespace's intake switch; never block existing job work."""
        from fastapi import HTTPException
        try:
            control = self.store.get(self.ADMISSION_KEY)
            allowed = control is None or (isinstance(control, dict) and control.get('accepting_new', True) is True)
        except Exception:
            raise HTTPException(503, '신규 단어장 등록 상태를 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.') from None
        if not allowed:
            raise HTTPException(503, '신규 단어장 등록을 잠시 중지했습니다. 기존 자료 조회와 수정은 계속 사용할 수 있습니다.')

    def _current_version(self, bank_id):
        return generation_version(self._current_bank(bank_id))

    def public_bank(self, bank):
        """Project verified publication only at an HTTP response boundary.

        Do not call service.get/read_job here or feed this value to storage:
        construction.complete remains the existing Sheet-write trigger there.
        """
        value = deepcopy(bank)
        construction = value.setdefault('construction', {})
        construction.update(publication_verified=False, publishing_status='pending', learning_complete=False)
        early = before_review(bank)
        if early:
            construction['registration_status'] = 'pending'
        try:
            assert_source_manifest(bank)
            if not early:
                assert_source_ready(bank)
            ready = True
        except (ValueError, SourceChanged):
            ready = False
        construction['source_status'] = self._source_status(bank) if early and ready else 'complete' if ready else 'needs_review'
        if not ready:
            construction.update(publishing_status='needs_review', learning_status='needs_review')
            return value
        try:
            parent_id = bank.get('construction', {}).get('import_job_id')
            if not parent_id:
                return value
            parent = self.service.store.get('jobs', parent_id)
            if early:
                construction['registration_status'] = parent.get('registration_status', 'pending')
            run_id = parent.get('engine_run_id')
            if not run_id or parent.get('engine_version') != ENGINE_VERSION:
                return value
            saved = self.store.get(self.engine._job(run_id))
            if not saved:
                return value
            for entry in value['entries']:
                if entry['id'] in saved.get('review_entries', {}):
                    entry['learning_review'] = {'code': saved['review_entries'][entry['id']],
                        'message': '단어·품사·뜻 연결 확인 필요' if saved['review_entries'][entry['id']] == 'learning_source_conflict' else '학습자료 문장·해석 확인 필요'}
            same_source = (saved['manifest']['bank']['id'] == bank['id']
                and saved['manifest']['source_version'] == generation_version(bank,
                    preparation_policy=saved['manifest'].get('preparation_policy', 'after_review_v1')))
            has_learning = all(entry.get('learning') and not entry.get('learning_stale')
                and not entry.get('learning_pending') for entry in bank['entries'] if not early or generation_eligible(entry))
            if early and self._invalid_learning_inputs(bank):
                has_learning = False
            receipt = saved.get('receipt') or {}
            verified = (saved['state'] == 'complete' and same_source and has_learning
                and parent.get('kind') == 'import' and parent.get('result_id') == bank['id']
                and bank['construction'].get('published_run_id') == run_id
                and receipt.get('run_id') == run_id and receipt.get('bank_id') == bank['id'])
            construction['publication_verified'] = bool(verified)
            construction['learning_complete'] = bool(verified)
            if verified:
                construction.update(publishing_status='complete', learning_status='complete')
            elif not same_source or saved['state'] in {'failed', 'superseded', 'cancelled', 'needs_review', 'complete'}:
                construction.update(publishing_status='needs_review', learning_status='needs_review')
            elif saved['state'] == 'publishing':
                construction.update(publishing_status='running', learning_status='complete')
            else:
                construction.update(publishing_status='pending', learning_status='running' if saved['state'] == 'running' else 'pending')
        except Exception:
            # Unavailable or unobserved engine state can never prove completion.
            construction.update(publishing_status='needs_review', learning_status='needs_review',
                                publication_verification_unavailable=True)
        return value

    def selection_snapshot(self, bank, unit_ids):
        """Read verified completed batches into an exam-only current-value copy.

        No AI, writes, status promotion or final Sheets publication occurs here.
        An unrelated chapter or later retry cannot discard matching completed work.
        """
        selected = list(dict.fromkeys(unit_ids or []))
        snapshot = deepcopy(bank)
        def outcome(ready, state, blocked=None, message=None):
            return snapshot, {'selection_ready': ready, 'preparation_state': state,
                'blocking_unit_ids': [] if ready else list(blocked if blocked is not None else selected),
                'message': message if message is not None else ('선택한 챕터의 학습자료 준비가 완료되었습니다.' if ready else
                    '시험 문제에 필요한 학습자료를 준비 중입니다. 완료 후 생성할 수 있습니다.')}
        if not before_review(bank):
            return outcome(True, 'ready', message='')
        try:
            assert_source_manifest(bank)
            available_units = {u['id'] for u in bank.get('units', [])}
            if not selected or not set(selected) <= available_units:
                return outcome(False, 'needs_review', message='등록된 챕터를 선택해 주세요.')
            parent_id = bank.get('construction', {}).get('import_job_id')
            parent = self._job(parent_id)
            if parent.get('registration_status') != 'complete':
                return outcome(False, 'pending', message='단어장을 저장하고 있습니다. 저장 후 사용할 수 있습니다.')
            scope = [e for e in bank['entries'] if e.get('unit_id') in selected and e.get('status') != 'excluded']
            missing_inputs = {e['id'] for e in scope if not generation_eligible(e)}
            targets = {e['id']: e for e in scope if generation_eligible(e)}
            unresolved = set(targets)
            materials, review_ids = {}, set(missing_inputs)
            current_run = parent.get('engine_run_id')
            runs = [current_run] if current_run else []
            retries = parent.get('engine_retry', 0)
            if type(retries) is not int or not 0 <= retries <= 10000:
                raise EngineError('학습자료 작업 이력을 확인할 수 없습니다.')
            runs.extend(parent_id + '_r' + str(n) for n in range(retries, 0, -1))
            runs.append(parent_id)
            source_rows = bank['source_manifest']['entries']
            positions = {entry['id']: i for i, entry in enumerate(bank['entries'])}
            current_state = 'pending'
            for run_id in dict.fromkeys(runs):
                if not unresolved:
                    break
                saved = self.store.get(self.engine._job(run_id))
                if saved is None:
                    continue
                manifest = saved['manifest']
                if (saved.get('id') != run_id or saved.get('engine_version') != ENGINE_VERSION
                        or manifest['bank']['id'] != bank['id']
                        or manifest['bank']['source_manifest']['sha256'] != bank['source_manifest']['sha256']):
                    raise EngineError('학습자료의 원문 연결을 확인할 수 없습니다.')
                if run_id == current_run:
                    current_state = saved.get('state', 'pending')
                    review_ids.update(set(saved.get('review_entries', {})).intersection(targets))
                indices = sorted({positions[entry_id] // 8 for entry_id in unresolved - review_ids})
                for index in indices:
                    batch_ids = saved.get('batch_ids', [])
                    if index >= len(batch_ids):
                        raise EngineError('학습자료 묶음 목록을 확인할 수 없습니다.')
                    batch_id = batch_ids[index]
                    batch = self.store.get(self.engine._batch(run_id, batch_id))
                    if batch is None:
                        raise EngineError('학습자료 묶음을 다시 읽을 수 없습니다.')
                    if batch.get('state') != 'complete':
                        continue
                    expected_ids = [e['id'] for e in bank['entries'][index * 8:index * 8 + 8]]
                    if (batch.get('id') != batch_id or batch.get('job_id') != run_id
                            or [e['id'] for e in batch['inputs']] != expected_ids
                            or digest(batch['inputs']) != batch['input_hash']
                            or digest(batch['learning']) != batch['result_hash']
                            or batch.get('source_rows') != [{'index': i, 'entry': source_rows[i]}
                                for i in range(index * 8, min(index * 8 + 8, len(source_rows)))]):
                        raise EngineError('저장된 학습자료를 검증할 수 없습니다.')
                    original = [e for e in batch['inputs'] if generation_eligible(e)] if before_review(manifest) else batch['inputs']
                    if set(batch['learning']) != {e['id'] for e in original}:
                        raise EngineError('학습자료의 생성 대상이 일치하지 않습니다.')
                    outputs = [{**deepcopy(e), 'learning': deepcopy(batch['learning'][e['id']])} for e in original]
                    self.engine._validated(original, outputs, manifest['spec'])
                    for entry in original:
                        entry_id = entry['id']
                        if entry_id not in unresolved or entry_id in review_ids:
                            continue
                        current = targets[entry_id]
                        if input_fingerprint(current) != input_fingerprint(entry):
                            continue
                        clean = deepcopy(current)
                        for key in ('learning', 'learning_pending', 'learning_stale', 'learning_review',
                                    'learning_available', 'learning_input_fingerprint'):
                            clean.pop(key, None)
                        candidate = {**deepcopy(clean), 'learning': deepcopy(batch['learning'][entry_id])}
                        self.engine._validated([clean], [candidate], manifest['spec'])
                        materials[entry_id] = candidate['learning']
                        unresolved.remove(entry_id)
            # Do not construct a new exam from inputs edited during the reads.
            latest = self._current_bank(bank['id'])
            assert_source_manifest(latest)
            if generation_version(latest, preparation_policy=PREPARATION_POLICY) != generation_version(bank, preparation_policy=PREPARATION_POLICY):
                return outcome(False, 'pending', message='단어장이 변경되었습니다. 준비 상태를 다시 확인해 주세요.')
            snapshot = deepcopy(latest)
            for entry in snapshot['entries']:
                if entry['id'] in materials:
                    entry['learning'] = deepcopy(materials[entry['id']])
                    entry['learning_input_fingerprint'] = input_fingerprint(entry)
                    entry['learning_stale'] = False
                    entry.pop('learning_pending', None)
                    entry.pop('learning_review', None)
            blocked_ids = unresolved | review_ids
            blocked_units = [unit_id for unit_id in selected if any(e['unit_id'] == unit_id and e['id'] in blocked_ids for e in scope)
                or not any(e['unit_id'] == unit_id for e in scope)]
            if not blocked_units:
                return outcome(True, 'ready')
            if review_ids or not scope or current_state in {'needs_review', 'superseded', 'complete'}:
                return outcome(False, 'needs_review', blocked_units, '선택한 챕터의 학습자료를 확인해야 합니다. 단어장 관리에서 확인해 주세요.')
            if current_state in {'failed', 'cancelled'}:
                return outcome(False, 'failed', blocked_units, '학습자료 준비가 중단되었습니다. 단어장 관리에서 다시 시도해 주세요.')
            return outcome(False, 'running' if current_state in {'running', 'publishing'} else 'pending', blocked_units)
        except Exception:
            return outcome(False, 'unavailable', message='학습자료 준비 상태를 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.')

    @staticmethod
    def _validate_learning(entries, outputs, *, spec):
        from .learning_quality import VERSIONS
        checked = spec['validator_version'] in VERSIONS
        if spec['validator_version'] not in {'referenced-learning-v1', *VERSIONS}:
            raise EngineError('지원되지 않는 학습자료 검증 버전입니다.')
        result = ai_importer.validate_learning_entries(entries, outputs,
            allow_inflection=checked, require_quality=checked)
        if checked and any(entry.get('learning', {}).get('quality', {}).get('version') != spec['validator_version'] for entry in result):
            raise EngineError('검수 기록이 작업 규격 버전과 일치하지 않습니다.')
        return result

    def _learning(self, entries, *, cancel_event, spec):
        def provider(request, options):
            return self.provider(request, options, cancel_event=cancel_event)
        from .learning_quality import VERSIONS, enrich_checked
        if spec['prompt_version'] == 'referenced-learning-v1' and spec['validator_version'] == 'referenced-learning-v1':
            return ai_importer.enrich_referenced(provider, entries)
        if spec['prompt_version'] not in VERSIONS or spec['validator_version'] != spec['prompt_version']:
            raise EngineError('지원되지 않는 학습자료 생성 버전입니다.')
        try:
            return enrich_checked(provider, entries, version=spec['prompt_version'])
        except ai_importer.BankImportError as error:
            if error.validation_code in {'learning_quality_review', 'learning_source_conflict'}:
                raise LearningReviewRequired(error.validation_code, error.details.get('entry_ids', [])) from None
            raise

    def _job(self, job_id):
        job = self.service.get('jobs', job_id)
        if job.get('kind') != 'import' or job.get('engine_version') != ENGINE_VERSION:
            raise EngineError('새 등록 방식의 작업이 아닙니다.')
        return job

    def enqueue_prepare(self, job):
        job_id = job['id']
        key = 'prepare/' + job_id
        def stage(values):
            previous = values[key]
            if previous:
                return {}, previous
            value = {'job_id': job_id, 'state': 'pending', 'claim': None, 'generation': 0,
                     'enqueued_at': None, 'created_at': self.clock()}
            return {key: value}, value
        pending = self.store.atomic([key], stage)
        if pending['state'] in {'complete', 'needs_review'}:
            return
        self.dispatcher.enqueue({'kind': 'prepare', 'job_id': job_id, 'generation': pending['generation']})
        def acknowledged(values):
            current = values[key]
            if current and current['generation'] == pending['generation']:
                current['enqueued_at'] = self.clock()
                return {key: current}, None
            return {}, None
        self.store.atomic([key], acknowledged)

    @contextmanager
    def _prepare_claim(self, job_id):
        key, account_key = 'prepare/' + job_id, LearningEngine._account(self.account_key)
        token, timestamp = uuid.uuid4().hex, self.clock()
        def acquire(values):
            record = values[key]
            if not record:
                raise KeyError(job_id)
            if record['state'] in {'complete', 'needs_review'}:
                return {}, None
            if (record.get('claim') or {}).get('expires_at', 0) > timestamp:
                raise WorkBusy('원문 확인 작업을 처리하고 있습니다.')
            account = values[account_key] or {'slots': []}
            account['slots'] = [s for s in account['slots'] if s['expires_at'] > timestamp]
            if len(account['slots']) >= 2:
                raise WorkBusy('다른 학습 작업을 처리하고 있습니다.')
            claim = {'token': token, 'expires_at': timestamp + 900, 'job_id': job_id, 'batch_id': 'prepare'}
            account['slots'].append(claim)
            record.update(state='running', claim=claim)
            return {key: record, account_key: account}, token
        owned = self.store.atomic([key, account_key], acquire)
        cancelled, stop = threading.Event(), threading.Event()
        owner = _PrepareLease(self, key, token, cancelled)
        def maintain():
            while not stop.wait(30):
                try:
                    def renew(values):
                        record, account = values[key], values[account_key]
                        owner.check(record)
                        timestamp = self.clock()
                        record['claim']['expires_at'] = timestamp + 900
                        matches = [s for s in account['slots'] if s['token'] == token]
                        if len(matches) != 1 or matches[0]['expires_at'] <= timestamp:
                            raise WorkBusy('원문 처리 슬롯을 확인하지 못했습니다.')
                        matches[0]['expires_at'] = timestamp + 900
                        return {key: record, account_key: account}, None
                    self.store.atomic([key, account_key], renew)
                except Exception:
                    cancelled.set()
                    return
        thread = threading.Thread(target=maintain, daemon=True, name='source-lease')
        if owned:
            thread.start()
        try:
            yield owner if owned else None
            if owned:
                owner.assert_owned()
        finally:
            stop.set()
            if owned:
                thread.join(timeout=1)
                def release(values):
                    record, account = values[key], values[account_key]
                    writes = {}
                    if account:
                        account['slots'] = [s for s in account['slots'] if s['token'] != token]
                        writes[account_key] = account
                    if record and (record.get('claim') or {}).get('token') == token:
                        record['claim'] = None
                        if record['state'] == 'running':
                            record.update(state='pending', generation=record['generation'] + 1)
                        writes[key] = record
                    return writes, None
                self.store.atomic([key, account_key], release)

    def _set_prepare(self, job_id, *, owner=None, **updates):
        key = 'prepare/' + job_id
        def change(values):
            record = values[key]
            if owner is not None:
                owner.check(record)
            record.update(updates)
            return {key: record}, record
        return self.store.atomic([key], change)

    def _save_source_part(self, job_id, index, chunk, part, owner):
        key = 'source-part/' + job_id + '/' + str(index)
        value = {'input_hash': digest(chunk), 'bank': part}
        def save(values):
            owner.check(values[owner.key])
            previous = values[key]
            if previous and previous['input_hash'] != value['input_hash']:
                raise SourceChanged('저장된 원문 위치가 바뀌었습니다.')
            return ({} if previous else {key: value}), None
        self.store.atomic([owner.key, key], save)

    def _update_source_job(self, job_id, owner, **changes):
        # Drive and Firestore are distinct stores. Fence immediately around
        # the existing conditional Drive write; never report an unowned ACK.
        owner.assert_owned()
        result = self.service.update_job(job_id, **changes)
        owner.assert_owned()
        return result

    @staticmethod
    def _merge_source_chunks(base, parts):
        bank = deepcopy(base)
        bank['entries'], bank['units'] = [], []
        for part in parts:
            mapping = {}
            for unit in part['units']:
                entries = [entry for entry in part['entries'] if entry['unit_id'] == unit['id']]
                if not entries or unit['id'] in mapping:
                    raise EngineError('원문 챕터의 항목 또는 식별자를 확인하지 못했습니다.')
                first = entries[0]
                source = first.get('source', {})
                identity = deepcopy(unit.get('source_identity') or {})
                identity.update(first_source_ref=identity.get('first_source_ref')
                    or (source.get('refs') or [first['id']])[0],
                    first_word_ref=identity.get('first_word_ref') or source.get('word_ref') or first['id'],
                    order=len(bank['units']))
                # A chunk boundary is not evidence that equally named units
                # are the same original chapter. Preserve each source segment.
                new = deepcopy(unit)
                new.update(id='u_' + digest({'source_sha256': bank['source']['sha256'],
                    'identity': identity})[:16], order=len(bank['units']), source_identity=identity)
                bank['units'].append(new)
                mapping[unit['id']] = new['id']
            for source in part['entries']:
                entry = deepcopy(source)
                entry['unit_id'] = mapping[entry['unit_id']]
                bank['entries'].append(entry)
            bank.setdefault('page_issues', []).extend(deepcopy(part.get('page_issues', [])))
        if len({entry['id'] for entry in bank['entries']}) != len(bank['entries']):
            raise EngineError('원문 항목 ID가 중복되었습니다.')
        return freeze_source_manifest(refresh_bank_state(bank))

    def prepare(self, payload):
        job_id = payload['job_id']
        with self._prepare_claim(job_id) as owner:
            if owner is None:
                return self.read_job(job_id)
            job = self._job(job_id)
            cancelled = owner.cancelled
            try:
                bank_id = 'b_' + job['payload']['sha256'][:24]
                review_previous = None
                try:
                    bank = self._current_bank(bank_id)
                    if bank.get('construction', {}).get('import_job_id') != job_id:
                        if job.get('source_review_of') == bank_id:
                            review_previous, bank = bank, None
                        else:
                            raise SourceChanged('동일 원본의 다른 단어장이 이미 있습니다.')
                except KeyError:
                    bank = None
                except Exception as error:
                    if getattr(error, 'status_code', None) == 404:
                        bank = None
                    else:
                        raise
                if bank is None:
                    self._update_source_job(job_id, owner, status='running', source_status='running',
                        message='원본에서 단어와 모든 뜻의 위치를 확인하고 있습니다.',
                        progress={'stage': 'source_read', 'completed': 0, 'total': 0})
                    path = self.service.ensure_source(job)
                    if hashlib.sha256(path.read_bytes()).hexdigest() != job['payload']['sha256']:
                        raise SourceChanged('업로드 원본이 변경되었습니다.')
                    if job.get('registration_policy') == 'registration-audit-v1':
                        from .registration_agent import step
                        from .registration_audit import audit_candidate, read_catalog
                        prefix = 'registration-agent/' + job_id + '/' + str(job.get('engine_retry', 0)) + '/'
                        def save_agent(key, value):
                            def commit(values):
                                owner.check(values[owner.key])
                                return {prefix + key: value}, None
                            self.store.atomic([owner.key, prefix + key], commit)
                        def agent_provider(request, options):
                            owner.assert_owned()
                            try:
                                result = self.provider(request, options, cancel_event=cancelled)
                            except Exception:
                                owner.assert_owned()
                                health = self.store.get(prefix + 'provider-health') or {'consecutive_failures': 0}
                                health['consecutive_failures'] += 1
                                save_agent('provider-health', health)
                                if health['consecutive_failures'] >= 3:
                                    raise SourceAIUnavailable('AI 연결 오류가 반복되어 원본 검수를 보류했습니다. 기존 자료는 보존했습니다. 잠시 후 다시 시도해 주세요.') from None
                                raise WorkBusy('AI 연결을 다시 확인하고 있습니다. 원본과 기존 자료는 보존됩니다.') from None
                            owner.assert_owned()
                            if self.store.get(prefix + 'provider-health'):
                                save_agent('provider-health', {'consecutive_failures': 0})
                            return result
                        force = job.get('review_mode') == 'ai'
                        if review_previous is not None and not force:
                            force = audit_candidate(read_catalog(path, self.service.ocr_engine), review_previous)['state'] != 'passed'
                        result = step(path, provider=agent_provider, get=lambda key: self.store.get(prefix + key),
                            save=save_agent, ocr_engine=self.service.ocr_engine, force=force, comparison=review_previous)
                        if result.get('pending'):
                            prepared = self.store.get('prepare/' + job_id)
                            self._set_prepare(job_id, owner=owner, state='pending', generation=prepared['generation'] + 1, enqueued_at=None)
                            self._update_source_job(job_id, owner, status='queued', source_status='running',
                                progress={'stage':'source_audit','completed':result['completed'],'total':result['total']},
                                message='원본을 다시 읽고 독립 검수하고 있습니다. 기존 단어장은 유지됩니다.' if review_previous else '원본의 불확실한 부분을 AI로 판독·검수하고 있습니다.')
                            self.enqueue_prepare(self._job(job_id))
                            return self.read_job(job_id)
                        base, chunks = result['bank'], []
                    else:
                        base, chunks = ai_importer.prepare_reference_source(path, self.service.ocr_engine)
                    owner.assert_owned()
                    if chunks:
                        parts = []
                        for index, chunk in enumerate(chunks):
                            key = 'source-part/' + job_id + '/' + str(index)
                            saved = self.store.get(key)
                            if saved:
                                if saved['input_hash'] != digest(chunk):
                                    raise SourceChanged('저장된 원문 위치가 바뀌었습니다.')
                                part = saved['bank']
                            else:
                                if cancelled.is_set():
                                    raise WorkBusy('원문 작업의 실행 상태를 확인하지 못했습니다.')
                                def provider(request, options):
                                    return self.provider(request, options, cancel_event=cancelled)
                                part = ai_importer.extract_references(provider, base, chunk)
                                self._save_source_part(job_id, index, chunk, part, owner)
                                if index + 1 < len(chunks):
                                    prepared = self.store.get('prepare/' + job_id)
                                    self._set_prepare(job_id, owner=owner, state='pending', generation=prepared['generation'] + 1, enqueued_at=None)
                                    self._update_source_job(job_id, owner, status='queued', source_status='running',
                                        progress={'stage': 'source_read', 'completed': index + 1, 'total': len(chunks)},
                                        message='확인한 원문 위치를 저장했습니다. 다음 부분을 이어서 확인합니다.')
                                    self.enqueue_prepare(self._job(job_id))
                                    return self.read_job(job_id)
                            parts.append(part)
                        base = self._merge_source_chunks(base, parts)
                    bank = deepcopy(base)
                    bank.update(id=bank_id, title=job['payload']['filename'].rsplit('.', 1)[0], storage='drive')
                    bank['source'].update(filename=job['payload']['filename'], stored_name=job['payload']['stored_name'])
                    if job['payload'].get('original_id'):
                        bank['source']['original_id'] = job['payload']['original_id']
                    for entry in bank['entries']:
                        if not before_review(job) or generation_eligible(entry):
                            entry['learning_pending'] = True
                        elif entry.get('status') != 'excluded':
                            entry['learning_pending'] = {'code': 'learning_input_missing',
                                'message': '단어와 뜻을 확인해야 학습자료를 만들 수 있습니다.'}
                    bank.setdefault('construction', {}).update(import_job_id=job_id, engine_version=ENGINE_VERSION,
                        source_status='pending', learning_status='pending', publishing_status='pending', learning_complete=False)
                    if before_review(job):
                        bank['construction'].update(preparation_policy=PREPARATION_POLICY, registration_status='complete')
                    bank = refresh_bank_state(bank)
                    # State JSON is durable and editable; Sheets are published
                    # after all learning succeeds. Existing IDs are not replaced.
                    with self.service.lock, self.service.bank_write_context(bank_id):
                        owner.assert_owned()
                        self.service.assert_bank_write_ownership()
                        if review_previous is not None:
                            from .registration_review import replace_source
                            current = self._current_bank(bank_id)
                            bank, changed = replace_source(bank, current, job)
                            bank = self.service.store.put_metadata('banks', bank, expected_version=current['version'])
                            old_parent = current.get('construction', {}).get('import_job_id')
                            if changed and old_parent and old_parent != job_id:
                                try:
                                    old_job = self.service.store.get('jobs', old_parent)
                                except KeyError:
                                    old_job = None
                                if old_job is not None:
                                    self.service.update_job(old_parent, status='complete', superseded_by=job_id,
                                        requires_attention=False, error_code=None, message='원본 재검수 작업으로 대체되었습니다.')
                            if not changed:
                                readback = self._current_bank(bank_id)
                                assert_source_manifest(readback)
                                if digest(readback) != digest(bank):
                                    raise WorkBusy('저장한 원본 검수 기록을 다시 확인하고 있습니다.')
                                bank = readback
                                self._update_source_job(job_id, owner, status='complete', result_id=bank_id,
                                    registration_status='complete', source_status='complete', requires_attention=False,
                                    message='원본 대조 검수를 통과했습니다. 단어장과 기존 학습자료를 보존했습니다.',
                                    progress={'stage':'source_audit','completed':len(bank['entries']),'total':len(bank['entries'])})
                                self._set_prepare(job_id, owner=owner, state='complete')
                                return self.read_job(job_id)
                        else:
                            bank = self.service.store.put_metadata('banks', bank, expected_version=0)
                        owner.assert_owned()
                if before_review(job):
                    # Confirm the authoritative durable record before announcing
                    # registration. This is independent of AI/Sheets publication.
                    with self.service.lock, self.service.bank_write_context(bank_id):
                        owner.assert_owned()
                        bank = self._current_bank(bank_id)
                        if bank.get('construction', {}).get('preparation_policy') != PREPARATION_POLICY:
                            updated = deepcopy(bank)
                            updated.setdefault('construction', {}).update(preparation_policy=PREPARATION_POLICY,
                                registration_status='complete')
                            self.service.assert_bank_write_ownership()
                            bank = self.service.store.put_metadata('banks', updated, expected_version=bank['version'])
                        readback = self._current_bank(bank_id)
                        assert_source_manifest(readback)
                        if digest(readback) != digest(bank):
                            raise WorkBusy('저장한 단어장을 다시 확인하고 있습니다.')
                        owner.assert_owned()
                        self.service.assert_bank_write_ownership()
                        bank = readback
                assert_source_manifest(bank)
                needs_review = bank.get('page_issues') or any(e.get('status') != 'ready' for e in bank['entries']) or any(u.get('status') == 'review' for u in bank['units'])
                self._update_source_job(job_id, owner, result_id=bank_id,
                    **({'registration_status': 'complete', 'source_status': self._source_status(bank)} if before_review(job) else {}))
                if needs_review and not before_review(job):
                    self._set_prepare(job_id, owner=owner, state='needs_review')
                    self._update_source_job(job_id, owner, status='interrupted', source_status='needs_review',
                        learning_status='pending', requires_attention=True, error_code='source_review_required',
                        message='원본 확인이 필요합니다. 단어장 수정에서 확인한 뒤 작업을 재시도해 주세요.')
                    return self.read_job(job_id)
                run_id = job.get('engine_run_id') or job_id
                manifest = build_manifest(bank, preparation_policy=job.get('preparation_policy', 'after_review_v1'))
                owner.assert_owned()
                self.engine.create_job(run_id, manifest, self.account_key, commit_guard=(owner.key, owner.check))
                self._update_source_job(job_id, owner, engine_run_id=run_id, status='queued',
                    source_status=self._source_status(bank) if before_review(job) else 'complete',
                    learning_status='pending', publish_status='pending', requires_attention=False, error_code=None,
                    progress={'stage': 'learning', 'completed': 0, 'total': sum(generation_eligible(e) for e in bank['entries'])
                        if before_review(job) else len(bank['entries'])},
                    message='추가 학습자료를 생성하고 있습니다.')
                self._set_prepare(job_id, owner=owner, state='complete')
            except WorkBusy:
                # Ownership/connection contention is retryable and must never
                # overwrite a newer claim or turn into human source review.
                raise
            except (EngineError, ValueError) as error:
                self._update_source_job(job_id, owner, status='interrupted', source_status='needs_review', requires_attention=True,
                    error_code=getattr(error, 'code', 'source_review_required'), message=str(error)[:500])
                self._set_prepare(job_id, owner=owner, state='needs_review')
                return self.read_job(job_id)
        self.engine.reconcile(self.dispatcher)
        return self.read_job(job_id)

    def _publish(self, manifest, entries, run_id):
        with self.service.lock, self.service.bank_write_context(manifest['bank']['id']):
            return self._publish_locked(manifest, entries, run_id)

    @contextmanager
    def _publication_context(self, bank_id):
        # Keep manual editing outside publish -> readback -> durable complete.
        with self.service.lock, self.service.bank_write_context(bank_id):
            self.service.assert_bank_write_ownership()
            yield
            self.service.assert_bank_write_ownership()

    def _publish_locked(self, manifest, entries, run_id):
        bank_id = manifest['bank']['id']
        current = self._current_bank(bank_id)
        early = before_review(manifest)
        assert_source_manifest(current)
        if not early:
            assert_source_ready(current)
        if generation_version(current, preparation_policy=manifest.get('preparation_policy', 'after_review_v1')) != manifest['source_version']:
            raise SourceChanged('저장 전에 단어장 입력이 변경되었습니다.')
        job_id = current['construction']['import_job_id']
        if before_review(current) and self._job(job_id).get('engine_run_id') != run_id:
            raise SourceChanged('더 최근의 학습자료 준비 작업이 있습니다.')
        if current.get('construction', {}).get('published_run_id') == run_id:
            return {'bank_id': bank_id, 'run_id': run_id, 'job_id': job_id}
        if early:
            bank = deepcopy(current)
            results = {entry['id']: entry for entry in entries}
            if set(results) != {entry['id'] for entry in bank['entries']}:
                raise SourceChanged('학습자료의 원문 항목이 변경되었습니다.')
            for entry in bank['entries']:
                if generation_eligible(entry):
                    source = results[entry['id']]
                    if input_fingerprint(entry) != input_fingerprint(source) or not source.get('learning'):
                        raise SourceChanged('수정된 어휘의 이전 학습자료를 적용할 수 없습니다.')
                    entry['learning'] = deepcopy(source['learning'])
                    entry['learning_input_fingerprint'] = input_fingerprint(entry)
                    entry['learning_stale'] = False
                    entry.pop('learning_pending', None)
                    entry.pop('learning_review', None)
                elif entry.get('status') == 'excluded':
                    entry.pop('learning_pending', None)
                else:
                    entry['learning_pending'] = {'code': 'learning_input_missing',
                        'message': '단어와 뜻을 확인해야 학습자료를 만들 수 있습니다.'}
            bank = refresh_bank_state(bank)
        else:
            bank = deepcopy(manifest['bank'])
            bank['entries'] = deepcopy(entries)
            for entry in bank['entries']:
                entry.pop('learning_pending', None)
                entry['learning_stale'] = False
            bank = merge_manual_edits(bank, current)
        if any(e.get('learning_stale') or not e.get('learning') for e in bank['entries'] if not early or generation_eligible(e)):
            raise SourceChanged('수정된 어휘의 이전 학습자료를 적용할 수 없습니다.')
        complete = not self._invalid_learning_inputs(bank) if early else True
        bank.setdefault('construction', {}).update(source_status=self._source_status(bank) if early else 'complete',
            learning_status='complete' if complete else 'needs_review',
            publishing_status='complete', published_run_id=run_id, learning_complete=complete)
        self.service.update_job(job_id, status='running', learning_status='complete' if complete else 'needs_review', publish_status='running',
            progress={'stage': 'saving', 'completed': len(entries), 'total': len(entries)}, message='검증한 학습자료를 시트에 저장하고 있습니다.')
        # expected_version protects the existing single-record store contract.
        # Sheet/state are separate writes: readback below is mandatory, and a
        # failed publish is retried using completed batches without model calls.
        self.service.assert_bank_write_ownership()
        self.service.store.put('banks', bank, expected_version=current['version'])
        return {'bank_id': bank_id, 'run_id': run_id, 'job_id': job_id}

    def _readback(self, receipt):
        bank = self._current_bank(receipt['bank_id'])
        if bank.get('construction', {}).get('published_run_id') != receipt['run_id']:
            raise EngineError('최종 저장 버전을 확인하지 못했습니다.')
        assert_source_manifest(bank)
        if not before_review(bank):
            assert_source_ready(bank)
        verifier = getattr(self.service.backend, 'verify_bank_publication', None) if hasattr(self.service, 'backend') else None
        if verifier is not None:
            verifier(bank)
        return bank

    def read_job(self, job_id):
        job = self._job(job_id)
        if job.get('result_id') and (not job.get('source_review_of') or job.get('engine_run_id')):
            current = self._current_bank(job['result_id'])
            parent = current.get('construction', {}).get('import_job_id')
            if parent and parent != job_id:
                job.update(status='complete', requires_attention=False, error_code=None,
                    message='원본 재검수 작업으로 대체되었습니다.', superseded_by=parent)
                return job
        run_id = job.get('engine_run_id')
        if not run_id or self.store.get(self.engine._job(run_id)) is None:
            return job
        status = self.engine.status(run_id)
        state = status['state']
        stage = 'saving' if state == 'publishing' else 'learning'
        job.update(progress={'stage': stage, 'completed': status['completed'], 'total': status['total']})
        source_status, input_missing, source_changed = 'complete', False, False
        if before_review(job):
            current = self._current_bank(job['result_id'])
            source_status = self._source_status(current)
            input_missing = bool(self._invalid_learning_inputs(current))
            source_changed = generation_version(current, preparation_policy=PREPARATION_POLICY) != status['source_version']
            job['source_status'] = source_status
        if state == 'complete' and before_review(job) and (source_changed or input_missing):
            job.update(status='interrupted', source_status=source_status, learning_status='needs_review',
                publish_status='complete' if not source_changed else 'needs_review', requires_attention=True,
                attention_at=current.get('updated_at', job.get('updated_at')),
                error_code='source_changed' if source_changed else 'learning_input_missing',
                message='단어와 뜻을 확인한 뒤 학습자료 준비를 다시 시도해 주세요. 저장한 단어장은 계속 사용할 수 있습니다.')
        elif state == 'complete':
            job.update(status='complete', source_status=source_status,
                learning_status='needs_review' if input_missing else 'complete', publish_status='complete',
                message=('학습자료를 저장했습니다. 단어와 뜻이 비어 있는 항목을 확인해 주세요.' if input_missing else
                    '학습자료 준비가 완료되었습니다. 원문 확인 상태는 유지됩니다.') if before_review(job)
                    else '원문과 전체 학습자료를 검증하고 저장했습니다.',
                requires_attention=bool(input_missing), error_code='learning_input_missing' if input_missing else None)
        elif state in {'superseded', 'needs_review', 'failed', 'cancelled'}:
            job.update(status='interrupted' if state in {'superseded', 'needs_review'} else 'failed',
                learning_status='needs_review', requires_attention=True, error_code=status.get('error_code'),
                message='추가 학습자료를 확인해야 합니다. 저장한 단어와 뜻은 유지됩니다.')
            job['learning_review_entries'] = status.get('review_entries', {})
            if status.get('error_code') == 'source_review_required':
                job['source_status'] = 'needs_review'
        else:
            job.update(status='running', learning_status='complete' if state == 'publishing' else 'running',
                publish_status='running' if state == 'publishing' else 'pending')
        return job

    def process(self, kind, run_id, batch_id=None):
        """Worker hook; make final public job state durable after engine commit."""
        try:
            if kind == 'batch':
                result = self.engine.run_batch(run_id, batch_id)
            elif kind == 'finalize':
                result = self.engine.finalize(run_id)
            else:
                raise EngineError('지원하지 않는 학습 작업입니다.')
        except Exception:
            self._sync_public_job(run_id)
            raise
        self._sync_public_job(run_id)
        return result

    def _sync_public_job(self, run_id):
        saved = self.store.get(self.engine._job(run_id))
        if saved is None:
            return
        parent_id = saved['manifest']['bank']['construction']['import_job_id']
        public = self.read_job(parent_id)
        if public['status'] in {'complete', 'failed', 'interrupted'}:
            fields = ('status', 'source_status', 'learning_status', 'publish_status',
                      'message', 'progress', 'requires_attention', 'error_code', 'learning_review_entries')
            self.service.update_job(parent_id, **{key: public[key] for key in fields if key in public})

    def retry(self, job_id):
        job = self._job(job_id)
        count = job.get('engine_retry', 0) + 1
        # An explicit retry is the only route that creates a new generation
        # after human source edits. Matching completed results remain reusable.
        self.service.update_job(job_id, engine_retry=count, engine_run_id=job_id + '_r' + str(count),
            preparation_policy=PREPARATION_POLICY,
            status='queued', error_code=None, requires_attention=False, message='저장한 수정값으로 작업을 다시 확인합니다.')
        self._set_prepare(job_id, state='pending', claim=None, generation=count, enqueued_at=None)
        self.enqueue_prepare(self._job(job_id))

    def reconcile(self):
        prepared = 0
        for key, record in self.store.scan('prepare/'):
            if record['state'] in {'complete', 'needs_review'}:
                continue
            if (record.get('claim') or {}).get('expires_at', 0) > self.clock():
                continue
            if record.get('enqueued_at') is None or self.clock() - record['enqueued_at'] >= 900:
                self._set_prepare(record['job_id'], state='pending', claim=None,
                    generation=record['generation'] + 1, enqueued_at=None)
                self.enqueue_prepare(self._job(record['job_id']))
                prepared += 1
        result = self.engine.reconcile(self.dispatcher)
        return {**result, 'source_tasks_dispatched': prepared}
