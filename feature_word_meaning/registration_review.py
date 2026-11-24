"""Explicit, idempotent source re-registration preserving bank identities."""
from copy import deepcopy
from .registration_audit import VERSION, digest, norm
from .source_integrity import freeze_source_manifest, refresh_bank_state
from .storage import now


def edit_version(bank):
    return digest({'source':bank.get('source_manifest'),'title':bank.get('title'),
        'deleted_at':bank.get('deleted_at'),'deletion_pending':bank.get('deletion_pending'),
        'units':bank.get('units'),
        'entries':[{k:e.get(k) for k in ('id','unit_id','word','meaning','word_raw','meaning_raw',
            'source','related','status','manual_edit','reviewed_at','review_reason')} for e in bank['entries']]})


def start(coordinator, bank_id, version, mode, request_id):
    from fastapi import HTTPException
    service=coordinator.service
    coordinator.assert_new_import_allowed()
    job_id='j_sr_'+digest([bank_id,request_id])[:32]
    with service.lock, service.bank_write_context(bank_id):
        bank=service.get('banks',bank_id)
        try:
            existing=service.get('jobs',job_id)
        except (KeyError,HTTPException) as error:
            if isinstance(error,HTTPException) and error.status_code!=404:raise
            existing=None
        if existing:
            if existing.get('review_mode')!=mode:raise HTTPException(409,'같은 요청의 검수 방식이 변경되었습니다.')
            coordinator.enqueue_prepare(existing)
            return {'status':'processing','job_id':job_id,'bank_id':bank_id}
        current=bank.get('registration_review_job')
        if current:
            previous=coordinator.read_job(current)
            if previous['status'] in {'queued','running'}:
                return {'status':'processing','job_id':current,'bank_id':bank_id}
        if bank['version']!=version:raise HTTPException(409,'단어장이 변경되었습니다. 다시 불러온 뒤 검수해 주세요.')
        source=deepcopy(bank['source'])
        source.setdefault('original_id',bank.get('drive',{}).get('original_id'))
        job=service.store.put('jobs',{'id':job_id,'kind':'import','status':'queued',
            'automatic_recovery_version':1,
            'payload':source,'source_review_of':bank_id,'review_mode':mode,
            'review_base':edit_version(bank),'message':'원본 대조 검수 대기',
            **coordinator.new_fields()})
        bank['registration_review_job']=job_id
        service.assert_bank_write_ownership()
        service.store.put_metadata('banks',bank,expected_version=version)
    coordinator.enqueue_prepare(job)
    return {'status':'processing','job_id':job_id,'bank_id':bank_id}


def replace_source(candidate, current, job):
    """Build the replacement; caller owns the bank lock and conditional write."""
    if edit_version(current)!=job['review_base']:
        raise ValueError('검수 중 단어장이 수정되었습니다. 기존 내용을 보존했습니다. 다시 검수해 주세요.')
    audit=deepcopy(candidate['registration_audit']);audit['job_id']=job['id']
    from .registration_agent import semantic_rows
    previous_audit=current.get('registration_audit',{})
    attested=previous_audit.get('state')=='passed' and previous_audit.get('source_manifest_sha256')==current.get('source_manifest',{}).get('sha256')
    if semantic_rows(candidate)==semantic_rows(current) and (audit.get('previous_issue_count',1)==0 or attested):
        output=deepcopy(current);output['registration_audit']=audit
        audit['source_manifest_sha256']=current.get('source_manifest',{}).get('sha256')
        return output,False
    output=deepcopy(candidate)
    for key in ('id','title','source','drive','drive_state_id','created_at','title_manual_edit'):
        if key in current:output[key]=deepcopy(current[key])
    # Carry user edits only across an unambiguous original vocabulary anchor.
    def anchor(e):
        s=e.get('source',{})
        return (s.get('sheet'),s.get('row'),s.get('page'),norm(e.get('word_raw')),norm(e.get('meaning_raw')))
    new={}
    for entry in output['entries']:new.setdefault(anchor(entry),[]).append(entry)
    for old in current['entries']:
        if not(old.get('manual_edit') or old.get('reviewed_at') or old.get('status')=='excluded'):continue
        matches=new.get(anchor(old),[])
        if len(matches)!=1:raise ValueError('사용자가 수정한 어휘와 새 원문 위치를 연결할 수 없어 교체를 보류했습니다.')
        for key in ('word','meaning','status','manual_edit','reviewed_at','reviewed_by','review_reason'):
            if key in old:matches[0][key]=deepcopy(old[key])
    for old in current['units']:
        if not(old.get('manual_edit') or old.get('reviewed_at') or old.get('label')!=old.get('original_label',old.get('label'))):continue
        matches=[u for u in output['units'] if u.get('original_label',u['label'])==old.get('original_label',old['label'])]
        if len(matches)!=1:raise ValueError('사용자가 수정한 챕터를 연결할 수 없어 교체를 보류했습니다.')
        for key in ('label','status','manual_edit','reviewed_at','reviewed_by','review_reason'):
            if key in old:matches[0][key]=deepcopy(old[key])
    output['registration_audit']=audit
    output['registration_review_job']=job['id']
    output['source_history']=deepcopy(current.get('source_history',[]))+[{
        'replaced_at':now(),'job_id':job['id'],'source_manifest':deepcopy(current.get('source_manifest'))}]
    output.pop('source_manifest',None)
    output=freeze_source_manifest(refresh_bank_state(output))
    output['registration_audit']['source_manifest_sha256']=output['source_manifest']['sha256']
    return output,True
