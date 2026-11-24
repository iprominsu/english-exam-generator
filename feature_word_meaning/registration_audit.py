"""Source-grounded registration audit; never changes an uploaded document.

The native parser is a candidate, not its own verifier. This module reopens
physical cells/tokens, checks complete coverage and records uncertain regions.
Agent mappings use these same immutable references, never invented strings.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .excel_importer import column_role as role

VERSION = 'registration-audit-v1'
MAX_SOURCE_ITEMS = 100_000


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def norm(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def compact(value):
    return re.sub(r'\s+', '', str(value or ''))


def primary_heading(text):
    """Recognize explicit top-level labels, never PART/sentence subheadings."""
    match=re.match(r'^\s*((?:Lesson|Unit|Chapter|Day)\s+\d+[A-Za-z]?|(?:제\s*)?\d+\s*(?:과|강|단원))(?=\s|[:：.—–-]|$)',str(text),re.I)
    if not match:return None
    if re.match(r'\s*[–—~-]\s*\d',str(text)[match.end():]):return None
    return match[1].strip()


def primary_chapters(region):
    return [{'label':primary_heading(i['text']),'ref':i['id']} for i in region['items']
        if i['kind']=='cell' and primary_heading(i['text'])]


def read_catalog(path, ocr_engine=None, *, ocr_pages=()):
    path = Path(path)
    raw = path.read_bytes()
    catalogue = {'version': VERSION, 'sha256': hashlib.sha256(raw).hexdigest(),
                 'format': path.suffix.lower().lstrip('.'), 'regions': []}
    if path.suffix.lower() == '.xlsx':
        from openpyxl import load_workbook
        from .excel_importer import validate_xlsx_archive
        validate_xlsx_archive(raw)
        values = load_workbook(path, read_only=False, data_only=True)
        formulas = load_workbook(path, read_only=False, data_only=False)
        try:
            for si, sheet in enumerate(values, 1):
                if sheet.max_row * sheet.max_column > 2_000_000:
                    raise ValueError('시트의 셀 범위가 처리 한도를 초과했습니다.')
                region = {'id': f's{si}', 'kind': 'cells', 'sheet': sheet.title,
                    'items': [], 'issues': [], 'merged': [str(x) for x in sheet.merged_cells.ranges]}
                region['label_ref'] = f's{si}:label'
                region['items'].append({'id': region['label_ref'], 'text': sheet.title, 'kind': 'label'})
                source_sheet = formulas.worksheets[si-1]
                for row in source_sheet:
                    for cell in row:
                        cached = sheet.cell(cell.row, cell.column)
                        if cell.value is None and cached.value is None:
                            continue
                        if cell.data_type == 'f' and cached.value is None:
                            region['issues'].append({'code': 'formula_value_missing', 'ref': f's{si}:{cell.coordinate}'})
                        value = cached.value
                        text = '' if value is None else str(value)
                        region['items'].append({'id': f's{si}:{cell.coordinate}', 'text': text,
                            'row': cell.row, 'column': cell.column, 'cell': cell.coordinate,
                            'kind': 'cell', 'bold': bool(cell.font.bold)})
                catalogue['regions'].append(region)
        finally:
            values.close(); formulas.close()
    elif path.suffix.lower() == '.pdf':
        import pdfplumber
        from .importer import _ocr_words, _default_ocr_engine
        with pdfplumber.open(path) as document:
            for index, page in enumerate(document.pages, 1):
                native = page.extract_words(x_tolerance=2, y_tolerance=3)
                use_ocr = index in ocr_pages or not native or '\ufffd' in ''.join(w['text'] for w in native)
                words = _ocr_words(path, index-1, ocr_engine or _default_ocr_engine(),
                    Path(__file__).resolve().parents[1]/'.local/pdf-import') if use_ocr else native
                region = {'id': f'p{index}', 'kind': 'tokens', 'page': index,
                    'width': page.width, 'height': page.height, 'method': 'ocr' if use_ocr else 'text',
                    'items': [], 'issues': []}
                for wi, word in enumerate(words, 1):
                    item = {'id': f'p{index}:w{wi}', 'text': str(word['text']), 'kind': 'token',
                        'bbox': [round(float(word[k]), 3) for k in ('x0','top','x1','bottom')]}
                    if use_ocr:
                        item['confidence'] = float(word.get('confidence', 0))
                        if item['confidence'] < 75:
                            region['issues'].append({'code': 'ocr_uncertain', 'ref': item['id']})
                    region['items'].append(item)
                if not region['items']:
                    region['issues'].append({'code': 'page_unreadable', 'ref': region['id']})
                catalogue['regions'].append(region)
    else:
        raise ValueError('PDF 또는 XLSX 원본이 필요합니다.')
    if sum(len(r['items']) for r in catalogue['regions']) > MAX_SOURCE_ITEMS:
        raise ValueError('원문 위치가 처리 한도를 초과했습니다. 범위를 나누어 등록해 주세요.')
    catalogue['fingerprint'] = digest(catalogue)
    return catalogue


def _inside(item, box):
    b = item.get('bbox')
    return bool(b and box and box[0]-.6 <= (b[0]+b[2])/2 <= box[2]+.6
                and box[1]-.6 <= (b[1]+b[3])/2 <= box[3]+.6)


def audit_candidate(catalogue, bank):
    """A pass requires field equality, physical pairing and full region coverage."""
    problems, reports = [], []
    if bank.get('source', {}).get('sha256') != catalogue['sha256']:
        problems.append({'code': 'source_sha_mismatch'})
    if not bank.get('entries'):
        problems.append({'code': 'no_entries'})
    units = {u['id']:u for u in bank.get('units', [])}
    for region in catalogue['regions']:
        issues = deepcopy(region['issues'])
        entries = [e for e in bank.get('entries', []) if
            (e.get('source', {}).get('sheet') == region.get('sheet') if region['kind']=='cells'
             else e.get('source', {}).get('page') == region['page'])]
        if region['kind'] == 'cells':
            chapter_labels={c['label'] for c in primary_chapters(region)}
            if len(chapter_labels)>1:
                issues.append({'code':'multiple_primary_chapters_require_agent'})
            rows = {}
            for item in region['items']:
                if item['kind']=='cell':rows.setdefault(item['row'], {})[item['column']] = item
            header, mapping, expected, ignored, used = None, None, [], [], set()
            for ri, cells in sorted(rows.items()):
                roles = {ci:role(cell['text']) for ci,cell in cells.items()}
                words = [ci for ci,r in roles.items() if r=='word']
                meanings = [ci for ci,r in roles.items() if r=='meaning']
                if len(words)==1 and meanings:
                    if any(r is None and norm(cells[ci]['text']) for ci,r in roles.items()):
                        issues.append({'code':'unknown_column_role','row':ri})
                    header, mapping = ri, roles
                    ignored.append(ri);continue
                if mapping is None:
                    # English + Korean in separate cells before a known header
                    # can be real vocabulary; the native result cannot prove it away.
                    separate = [cell['text'] for cell in cells.values() if norm(cell['text'])]
                    if len(separate)>=2 and any(re.fullmatch(r'[A-Za-z][A-Za-z ~’\'-]*', v.strip()) for v in separate) and any(re.search('[가-힣]', v) for v in separate):
                        issues.append({'code':'preheader_content','row':ri})
                    ignored.append(ri);continue
                wc = next(c for c,r in mapping.items() if r=='word')
                mc = [c for c,r in mapping.items() if r=='meaning']
                nc = next((c for c,r in mapping.items() if r=='number'),None)
                word = cells.get(wc,{}).get('text','')
                mean = '\n'.join(cells[c]['text'] for c in mc if c in cells and cells[c]['text'])
                numbered = nc in cells and bool(re.fullmatch(r'\d+',norm(cells[nc]['text'])))
                if not norm(word) and not norm(mean) and not numbered:
                    ignored.append(ri);continue
                expected.append(ri)
                candidates = [e for e in entries if e.get('source',{}).get('row')==ri]
                if len(candidates)!=1:
                    issues.append({'code':'row_missing_or_duplicated','row':ri});continue
                entry=candidates[0];used.add(entry['id'])
                if entry.get('word_raw')!=word or entry.get('meaning_raw')!=mean:
                    issues.append({'code':'word_meaning_pair_mismatch','row':ri,'entry_id':entry['id']})
                for ci,kind in mapping.items():
                    if kind in {'유의어','반의어','파생어'} and norm(entry.get('related',{}).get(kind,''))!=norm(cells.get(ci,{}).get('text','')):
                        issues.append({'code':'related_field_mismatch','row':ri,'field':kind})
                if not word.strip() or not mean.strip():issues.append({'code':'missing_word_or_meaning','row':ri})
                if any(ci not in mapping or mapping[ci] is None for ci,c in cells.items() if norm(c['text'])):
                    issues.append({'code':'unassigned_column','row':ri})
                original_label=units.get(entry.get('unit_id'),{}).get('original_label',units.get(entry.get('unit_id'),{}).get('label'))
                if entry.get('unit_id') not in units or original_label not in (chapter_labels or {region['sheet']}):
                    issues.append({'code':'unit_source_mismatch','row':ri})
            if header is None and any(i['kind']=='cell' for i in region['items']):
                issues.append({'code':'layout_requires_agent'})
            if any(e['id'] not in used for e in entries):issues.append({'code':'extra_entries'})
            expected_count=len(expected)
            details={'source_rows':expected,'metadata_rows':ignored}
        else:
            tokens=region['items'];used=set();expected_count=len(entries)
            for entry in entries:
                source=entry.get('source',{});box=source.get('bbox')
                inside=[t for t in tokens if _inside(t,box)]
                if not inside:
                    issues.append({'code':'missing_physical_source','entry_id':entry['id']});continue
                duplicates=used & {i['id'] for i in inside}
                if duplicates:issues.append({'code':'overlapping_entries','entry_id':entry['id']})
                used.update(i['id'] for i in inside)
                physical=''.join(t['text'] for t in inside)
                requested=str(entry.get('word_raw',''))+str(entry.get('meaning_raw',''))
                number=str(entry.get('source_number') or '')
                # Compare character multiplicity independently of PDF token order;
                # then require a contiguous English headword on the left side.
                if Counter(compact(physical)) != Counter(compact(requested+number)):
                    issues.append({'code':'source_text_coverage','entry_id':entry['id']})
                left=sorted(inside,key=lambda t:t['bbox'][0])
                if number and left and compact(left[0]['text'])==number:left=left[1:]
                word=compact(entry.get('word_raw',''))
                # English and Korean fonts have different baselines. Prove a
                # geometric column boundary instead of sorting rounded baselines.
                boundaries=sorted({t['bbox'][0] for t in left})
                splits=[]
                for boundary in boundaries[1:]:
                    head=sorted([t for t in left if t['bbox'][0]<boundary],key=lambda t:(t['bbox'][1],t['bbox'][0]))
                    if compact(''.join(t['text'] for t in head))==word:splits.append(boundary)
                if not word or not splits or re.search('[가-힣]',word):
                    issues.append({'code':'word_column_unproved','entry_id':entry['id']})
            outside=[t for t in tokens if t['id'] not in used]
            first=min((e.get('source',{}).get('bbox',[0,region['height']])[1] for e in entries),default=0)
            last=max((e.get('source',{}).get('bbox',[0,0,0,0])[3] for e in entries),default=0)
            top=' '.join(t['text'] for t in outside if t['bbox'][3]<first)
            bottom=' '.join(t['text'] for t in outside if t['bbox'][1]>last)
            body=[t for t in outside if not(t['bbox'][3]<first or t['bbox'][1]>last)]
            known_labels=[norm(u.get('original_label',u.get('label',''))) for u in units.values() if region['page'] in u.get('pages',[])]
            top_remaining=top
            for label in known_labels:top_remaining=top_remaining.replace(label,'',1)
            top_remaining=re.sub(r'(?i)Name\s*:|성명\s*:|이름\s*:|[가-힣 ]+(?:학원|학교|고등학교)', '',top_remaining).strip()
            if body or top_remaining or (bottom.strip() and not re.fullmatch(r'(?i)(?:PAGE\s*)?\d+\s*[/／]\s*\d+',bottom.strip())):
                issues.append({'code':'unclassified_source_tokens','refs':[t['id'] for t in outside]})
            if not entries:issues.append({'code':'page_without_entries'})
            details={'covered_tokens':len(used),'total_tokens':len(tokens)}
        if bank.get('page_issues'):
            relevant=[i for i in bank['page_issues'] if i.get('source',{}).get('page')==region.get('page') or i.get('page')==region.get('page')]
            if relevant:issues.append({'code':'existing_source_issue'})
        report={'region_id':region['id'],'passed':not issues,'expected_count':expected_count,
                'actual_count':len(entries),'issues':issues,**details}
        reports.append(report)
        problems.extend({'region_id':region['id'],**issue} for issue in issues)
    assigned=sum(r['actual_count'] for r in reports)
    if assigned!=len(bank.get('entries',[])):problems.append({'code':'unlocated_entries'})
    return {'version':VERSION,'state':'passed' if not problems else 'needs_agent',
        'source_sha256':catalogue['sha256'],'catalogue_fingerprint':catalogue['fingerprint'],
        'checked_entries':len(bank.get('entries',[])),'regions':reports,'issues':problems}


def refs_text(region, refs):
    lookup={item['id']:item for item in region['items']}
    if not isinstance(refs,list) or not refs or len(refs)!=len(set(refs)) or any(r not in lookup for r in refs):
        raise ValueError('원문 참조가 없거나 중복됐습니다.')
    items=[lookup[r] for r in refs]
    if any(i.get('kind')=='label' for i in items):
        if len(items)!=1:raise ValueError('시트명은 다른 원문과 합칠 수 없습니다.')
        return items[0]['text']
    return ('\n' if region['kind']=='cells' else ' ').join(i['text'] for i in items)


def native_candidate(path, ocr_engine=None):
    from .ai_importer import prepare_reference_source
    try:
        bank,chunks=prepare_reference_source(path,ocr_engine)
    except ValueError:
        path=Path(path)
        return {'schema_version':1,'source':{'filename':path.name,'format':path.suffix[1:].lower(),
            'sha256':hashlib.sha256(path.read_bytes()).hexdigest()},'entries':[],'units':[]}
    return bank if not chunks else {**bank,'entries':[],'units':[]}
