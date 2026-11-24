"""파이프라인 — 문항 생성 → 시트 누적 → PDF 렌더 → 드라이브 업로드.

노트북 셀 6의 run_generate/pick_set/render_pdf/run_render를 웹 요청 단위로
재구성했다. Drive/Sheets 호출은 동기 라이브러리(gspread, googleapiclient)라
스레드풀에서 돌리고, PDF 렌더링(Playwright)만 원래부터 비동기다.
"""

from starlette.concurrency import run_in_threadpool
from datetime import datetime, timezone
import hashlib
import json

from . import config, render, sheets_io, vocab, validation, operations, candidates, day_range, legacy_generator
from feature_word_meaning.drive_sync import EXPECTED_ACCOUNT
from .errors import AppError
from .google_clients import upload_bytes
from .worksheet_downloads import find_existing_folder


def pick_set(loaded, version, *, include_deleted=False):
    chosen = sorted([q for q in loaded if q["set"] == version], key=lambda x: x["no"])
    if not chosen:
        raise ValueError(f"v{version} 세트를 시트에서 찾지 못했습니다.")
    if not include_deleted and any(q.get('deleted_at') or q.get('deletion_pending') for q in chosen):
        raise ValueError('삭제되었거나 삭제 중인 시험지입니다.')
    for i, q in enumerate(chosen, start=1):
        q["no"] = i
    return chosen


async def _render_and_upload(questions, ctx, version) -> dict:
    meta = {"title": ctx["title"], "brand": config.BRAND}
    html_str = render.render_html(questions, meta)
    pdf_bytes = await render.html_to_pdf(html_str, render.header_template(), render.footer_template())

    pdf_name = sheets_io.pdf_name_for(ctx["title"], version)
    existing_pdf = await run_in_threadpool(sheets_io.find_file, pdf_name, ctx["folder_id"])
    f = await run_in_threadpool(
        upload_bytes, pdf_bytes, pdf_name, "application/pdf", ctx["folder_id"],
        existing_pdf["id"] if existing_pdf else None,
    )
    return {"file_id": f["id"], "web_link": f["webViewLink"], "pdf_name": pdf_name}


async def generate_and_render(day: int, vocab_path: str | None, count: int,
                              *, source=None, record=None, day_end=None,
                              bank=None, unit_ids=None, seed=None, question_types=None, allow_partial=False) -> dict:
    """Day 하나: 새 문항 생성 → 시트 누적 → 최신 세트로 PDF 렌더 → 업로드."""
    if record and record.get("questions"):
        questions = record["questions"]
    else:
        if record and record.get('bank_snapshot'):
            bank, unit_ids = record['bank_snapshot'], record['unit_ids']
        elif bank is None:
            rows = await run_in_threadpool(vocab.load_range, vocab_path, day, day_end)
            bank = vocab.to_bank(rows, day, source=source)
            unit_ids = [unit['id'] for unit in bank['units']]
        if record:
            record.update(bank_snapshot=bank, unit_ids=list(unit_ids))
            await run_in_threadpool(operations.save, record)
        questions = await run_in_threadpool(legacy_generator.generate, bank, unit_ids, count, seed=seed,
            question_types=question_types, generation_request_id=record['request_id'] if record else '', allow_partial=allow_partial)
        labels = [unit['label'] for unit in bank['units'] if unit['id'] in unit_ids]
        now = datetime.now(timezone.utc).isoformat()
        if source:
            for q in questions:
                q.update(source_file_id=source["id"], source_name=source["name"],
                         exam_title=source.get('exam_title') or f'{sheets_io.source_title(source["name"])} {day_range.label(day, day_end)}',
                         request_id=record["request_id"] if record else "")
        for q in questions:
            q.update(day_range.metadata(day, day_end))
            q.update(created_at=now, updated_at=now)
            q['payload']['_script'].update(unit_labels=labels)
        if record:
            record.update(state="generated", questions=questions)
            await run_in_threadpool(operations.save, record)

    validation.validate_questions(questions)
    source_name = questions[0].get('source_name') or (source or {}).get('name')
    scope = day_range.stored({"day": day, "day_end": day_end}, questions[0])
    ctx = await run_in_threadpool(sheets_io.day_ctx, day, source_name, **({"day_end": scope["day_end"]} if scope["day_end"] != day else {}))

    ws, existing, sh = await run_in_threadpool(
        sheets_io.open_day_sheet, ctx, True, count
    )
    prior = next((q for q in existing if record and q.get("request_id") == record["request_id"]), None)
    if prior:
        version, combined = prior["set"], existing
    else:
        version = await run_in_threadpool(_next_day_version, day, ctx, existing, count)
        shift = len(questions)
        for q in existing:
            q["no"] += shift
        for q in questions:
            q["set"] = version
        combined = sorted(questions + existing, key=lambda x: x["no"])
        await run_in_threadpool(sheets_io.write_all, ws, combined)

    chosen = pick_set(combined, version)
    ctx = sheets_io.version_ctx(ctx, chosen)
    result = await _render_and_upload(chosen, ctx, version)
    return {
        "day": day,
        **scope,
        "version": version,
        "title": ctx["title"],
        "count": len(chosen),
        "source_file_id": ctx.get("source_file_id", ""), "source_name": ctx.get("source_name", ""),
        **_source_scope(chosen), 'generation_method': 'script',
        'created': chosen[0].get('created_at') or None, 'updated': chosen[0].get('updated_at') or None,
        **result,
    }


# ------------------------------------------------------------
# 조회 / 편집 / 삭제
# ------------------------------------------------------------

def _next_day_version(day, target_ctx, existing, exam_count):
    """Keep public Day/version URLs unique across physically separated books."""
    maximum = sheets_io.total_sets(existing)
    for ctx in sheets_io.list_day_folders():
        if ctx['day'] == day and ctx['folder_id'] != target_ctx['folder_id']:
            _, loaded, _ = sheets_io.open_day_sheet(ctx, create=False, exam_count=exam_count)
            maximum = max(maximum, sheets_io.total_sets(loaded))
    return maximum + 1


def _get_ws_loaded(day: int, exam_count: int, version: int = None, *, create_project: bool = True):
    project_id = None if create_project else find_existing_folder(sheets_io.drive_service(), config.PROJECT_FOLDER)
    contexts = [c for c in sheets_io.list_day_folders(project_id=project_id) if c['day'] == day]
    matches = []
    for ctx in contexts:
        ws, loaded, _ = sheets_io.open_day_sheet(ctx, create=False, exam_count=exam_count)
        if version is None or any(q['set'] == version for q in loaded):
            matches.append((ctx, ws, loaded))
    if len(matches) > 1:
        raise ValueError(f'DAY {day}의 버전을 하나로 정할 수 없습니다. 교재별 버전을 확인해 주세요.')
    if not matches:
        raise ValueError(f'DAY {day}' + (f' v{version}' if version is not None else '') + ' 시험지를 찾을 수 없습니다.')
    return matches[0]


async def get_worksheet(day: int, version: int, exam_count: int = None) -> dict:
    ctx, ws, loaded = await run_in_threadpool(
        _get_ws_loaded, day, exam_count or config.EXAM_COUNT_DEFAULT, version
    )
    if not loaded:
        raise ValueError(f"시트를 찾을 수 없거나 비어 있습니다: {ctx['title']}")
    chosen = pick_set(loaded, version)
    # Let users open and repair an old incomplete item; save/render are strict.
    validation.validate_questions(chosen, check_context=False)
    ctx = sheets_io.version_ctx(ctx, chosen)
    return {"day": day, "version": version, "title": ctx["title"], "questions": [candidates.expose(q) for q in chosen],
            **day_range.stored(ctx, chosen[0]),
            **_source_scope(chosen),
            'created': chosen[0].get('created_at') or None, 'updated': _latest_time([q.get('updated_at') for q in chosen]),
            "source_file_id": ctx["source_file_id"], "source_name": ctx["source_name"]}


def _source_scope(questions):
    data = (questions[0].get('payload', {}).get('_script') or {}) if questions else {}
    labels = data.get('unit_labels', [])
    return {'source_bank_id': data.get('bank_id', ''), 'source_unit_ids': data.get('unit_ids', []),
            'source_unit_labels': labels, 'range_label': ', '.join(labels)}


def _save_edit_sync(day: int, version: int, edited_questions: list, exam_count: int):
    ctx, ws, loaded = _get_ws_loaded(day, exam_count, version)
    if ws is None:
        raise ValueError(f"시트를 찾을 수 없습니다: {ctx['title']}")

    original = pick_set(loaded, version)
    ctx = sheets_io.version_ctx(ctx, original)
    if len(edited_questions) != len(original):
        raise ValueError("문항 수가 변경됐습니다. 시험지를 다시 열어 주세요.")
    for index, q in enumerate(edited_questions):
        candidates.merge_saved(q, original[index])
        q.update({key: original[0].get(key, "") for key in sheets_io.META_COLUMNS})
        q['updated_at'] = datetime.now(timezone.utc).isoformat()

    others = [q for q in loaded if q["set"] != version]
    versions_desc = sorted({q["set"] for q in others} | {version}, reverse=True)

    combined = []
    for v in versions_desc:
        if v == version:
            group = edited_questions
        else:
            group = sorted([q for q in others if q["set"] == v], key=lambda x: x["no"])
        for q in group:
            q["set"] = v
        combined.extend(group)
    for i, q in enumerate(combined, start=1):
        q["no"] = i

    sheets_io.write_all(ws, combined)
    return ctx, pick_set(combined, version)


async def save_worksheet(day: int, version: int, edited_questions: list, exam_count: int = None) -> dict:
    """편집된 문항 저장 → 시트 갱신 → PDF 재렌더 → 드라이브 재업로드."""
    validation.validate_questions(edited_questions)
    ctx, chosen = await run_in_threadpool(
        _save_edit_sync, day, version, edited_questions, exam_count or config.EXAM_COUNT_DEFAULT
    )
    try:
        result = await _render_and_upload(chosen, ctx, version)
    except Exception as error:
        raise AppError("문항 시트는 저장됐지만 PDF 갱신에 실패했습니다. 입력을 유지했습니다. 다시 저장하거나 미리보기 화면의 PDF 재생성을 눌러 주세요.",
                       503, "pdf_pending", retryable=True, sheet_saved=True, pdf_saved=False) from error
    return {"day": day, "version": version, "title": ctx["title"], "count": len(chosen),
            "candidate_tokens": [candidates.token(q) for q in chosen], **result}


def append_candidate(day, version, question_index, expected_token, generated, request_id):
    ctx, ws, loaded = _get_ws_loaded(day, config.EXAM_COUNT_DEFAULT, version)
    group = sorted([q for q in loaded if q["set"] == version], key=lambda q: q["no"])
    if not 0 <= question_index < len(group):
        raise ValueError("문항 번호를 확인해 주세요.")
    q = group[question_index]
    values = candidates.items(q)
    # A lost response or checkpoint write must never create a duplicate candidate.
    if not any(v["candidate_id"] == request_id for v in values):
        candidates.check_token(q, expected_token)
        if len(values) >= candidates.MAX_CANDIDATES:
            raise AppError("더 이상 생성할 수 없습니다.", 409, "candidate_limit")
        values.append({**candidates.core(generated), "candidate_id": request_id})
        q["alternatives"] = values
        q.setdefault("candidate_selected", 0)
        q['updated_at'] = datetime.now(timezone.utc).isoformat()
        sheets_io.write_all(ws, loaded)
    result = candidates.expose(q)
    return {"day": day, "version": version, "question_index": question_index,
            "alternatives": result["alternatives"], "candidate_token": result["candidate_token"],
            "generated_index": next(i for i, v in enumerate(result["alternatives"]) if v["candidate_id"] == request_id)}


def deletion_token(questions):
    values = [{key:value for key,value in question.items() if key not in {'no','deleted_at','deletion_pending'}} for question in questions]
    return hashlib.sha256(json.dumps(values,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _delete_version_sync(day: int, version: int, exam_count: int, mode='permanent', token=None):
    from .google_clients import drive_service
    drive = drive_service()
    account = drive.about().get(fields='user(emailAddress)').execute().get('user',{}).get('emailAddress','')
    if account.strip().lower() != EXPECTED_ACCOUNT:
        raise AppError('승인된 Drive 계정과 달라 삭제하지 않았습니다.',409,'account_mismatch')
    ctx, ws, loaded = _get_ws_loaded(day, exam_count, version)
    if ws is None:
        raise ValueError(f"시트를 찾을 수 없습니다: {ctx['title']}")
    if version not in {q["set"] for q in loaded}:
        raise ValueError(f"v{version} 세트를 찾지 못했습니다.")
    chosen = sorted([question for question in loaded if question['set'] == version], key=lambda question:question['no'])
    if token is not None and token != deletion_token(chosen):
        raise AppError('시험지가 변경되었습니다. 목록을 새로고침해 주세요.',409,'delete_conflict')
    ctx = sheets_io.version_ctx(ctx, chosen)
    if mode == 'soft':
        if any(question.get('deletion_pending') for question in chosen):
            raise AppError('완전 삭제가 진행 중입니다. 완전히 삭제를 다시 시도해 주세요.',409,'delete_pending')
        for question in chosen:
            question['deleted_at'] = question.get('deleted_at') or datetime.now(timezone.utc).isoformat()
        sheets_io.write_all(ws, loaded)
        return ctx
    files = sheets_io.deletion_files(drive, sheets_io.pdf_name_for(ctx['title'],version),ctx['folder_id'],'application/pdf')
    files += operations.worksheet_deletion_files(drive, day, version, {q.get('request_id') for q in chosen if q.get('request_id')})
    remaining = sorted([q for q in loaded if q["set"] != version], key=lambda x: x["no"])
    sheet_file = None if remaining else sheets_io.deletion_file(drive, ws.spreadsheet.id, ctx['folder_id'],'application/vnd.google-apps.spreadsheet')
    for question in chosen:
        question['deletion_pending'] = '1'
    sheets_io.write_all(ws, loaded)
    sheets_io.execute_deletion(drive, files)
    for i, q in enumerate(remaining, start=1):
        q["no"] = i
    if sheet_file:
        sheets_io.execute_deletion(drive, [sheet_file])
    else:
        sheets_io.write_all(ws, remaining)
    return ctx


async def delete_worksheet(day: int, version: int, exam_count: int = None, *, mode='permanent', token=None) -> dict:
    ctx = await run_in_threadpool(
        _delete_version_sync, day, version, exam_count or config.EXAM_COUNT_DEFAULT, mode, token
    )
    return {"day": day, "version": version, "title": ctx["title"], "deleted": True, 'mode':mode}


def _list_worksheets_sync(exam_count: int):
    out = []
    folders = sheets_io.list_day_folders()
    inventories = sheets_io.folder_inventories(folder['folder_id'] for folder in folders)
    for folder in folders:
        day = folder["day"]
        ctx = dict(folder)
        files = inventories[ctx['folder_id']]
        loaded = sheets_io.listing_questions(ctx, files, exam_count)
        if not loaded:
            continue
        n_sets = sheets_io.total_sets(loaded)
        for v in range(1, n_sets + 1):
            chosen = [q for q in loaded if q['set'] == v]
            if any(q.get('deleted_at') for q in chosen):
                continue
            count = len(chosen)
            if count == 0:
                continue
            version_context = sheets_io.version_ctx(ctx, chosen)
            pdf_name = sheets_io.pdf_name_for(version_context["title"], v)
            f = next((item for item in files if item['name'] == pdf_name and item.get('mimeType') == 'application/pdf'), None)
            out.append(
                {
                    "day": day,
                    **day_range.stored(version_context),
                    "version": v,
                    "title": version_context["title"],
                    "source_file_id": version_context["source_file_id"],
                    "source_name": version_context["source_name"],
                    **_source_scope(chosen),
                    "count": count,
                    'deletion_token': deletion_token(chosen),
                    'deletion_pending': any(q.get('deletion_pending') for q in chosen),
                    "file_id": f["id"] if f else None,
                    "web_link": f["webViewLink"] if f else None,
                    "created": next((q['created_at'] for q in chosen if q.get('created_at')), None) or (f.get('createdTime') if f else None),
                    "updated": _latest_time([q.get('updated_at') for q in chosen] + [f.get('modifiedTime') if f else None]),
                }
            )
    identities = [(row['day'], row['version']) for row in out]
    if len(set(identities)) != len(identities):
        raise ValueError('같은 Day/버전이 여러 교재 폴더에 있습니다. 중복 저장 자료를 확인해 주세요.')
    return sorted(out, key=lambda row: (row['day'], row['version']))


def _latest_time(values):
    valid = []
    for value in values:
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            valid.append((parsed, value))
        except (ValueError, TypeError):
            continue
    return max(valid)[1] if valid else None


async def list_worksheets(exam_count: int = None) -> list:
    return await run_in_threadpool(
        _list_worksheets_sync, exam_count or config.EXAM_COUNT_DEFAULT
    )


