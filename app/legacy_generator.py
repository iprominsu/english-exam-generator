"""Deterministic legacy question types from a frozen common vocabulary bank.

AI may prepare labelled learning material during import. Exam generation only
selects that material and source meanings; it never calls an AI or fabricates
sentences, definitions, dictionary senses or spelling mistakes.
"""
from copy import deepcopy
import hashlib
import json
import random
import re
import unicodedata

from feature_word_meaning.script_generator import _comparison_parts, _conflicts, _confusion_score
from feature_word_meaning.exams import select_words, ExamError
from feature_word_meaning.learning_targets import target_surface
from .errors import AppError
from . import validation
from .question_context import contains_target

TYPES = ('def_match', 'sentence_match', 'common_blank', 'count_correct', 'blank_ko', 'word_meaning', 'passage_blank')
VERSION = 2


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _key(value):
    return ' '.join(_text(value).split()).casefold()


def validate_selection(bank, unit_ids):
    """단어은행 내 선택한 챕터 ID 목록의 유효성을 검증한다."""
    allowed = {unit['id'] for unit in bank.get('units', [])}
    if not isinstance(unit_ids, list) or not unit_ids or len(set(unit_ids)) != len(unit_ids) or not set(unit_ids) <= allowed:
        raise AppError('등록된 챕터를 선택해 주세요.', 422, 'invalid_selection')


def _source(bank, unit_ids):
    validate_selection(bank, unit_ids)
    selected, known, contexts = {}, {}, []
    for entry in bank.get('entries', []):
        word = _text(entry.get('word'))
        meaning = _text(entry.get('meaning', entry.get('meaning_raw')))
        if not word or not meaning:
            continue
        key = _key(word)
        known.setdefault(key, []).extend(_comparison_parts(meaning))
        known[key].extend(_comparison_parts(_text(entry.get('meaning_raw'))))
        if entry.get('status') == 'ready':
            contexts.append(_comparison_parts(meaning))
        if entry.get('status') != 'ready' or entry.get('unit_id') not in unit_ids:
            continue
        selection_key = ' '.join(unicodedata.normalize('NFC', word).split())
        item = selected.setdefault(selection_key, {'word': selection_key, 'meanings': [], 'entries': [], 'learning': {}})
        if meaning not in item['meanings']:
            item['meanings'].append(meaning)
        item['entries'].append(deepcopy(entry))
        learning = {} if (entry.get('learning_stale') or entry.get('learning_pending') or entry.get('learning_review')) else entry.get('learning') or {}
        checked = False
        if learning.get('quality'):
            from feature_word_meaning.learning_quality import validate_receipt
            try:
                validate_receipt(entry, learning['quality'])
                checked = True
            except (ValueError, KeyError, TypeError):
                learning = {}
        for field in ('definitions', 'examples', 'passages'):
            for material in learning.get(field, []) if isinstance(learning.get(field, []), list) else []:
                if not isinstance(material, dict) or material.get('origin') not in ('source', 'ai_enrichment'):
                    continue
                sense = _text(material.get('meaning'))
                # Imported enrichment must point back to an actual source sense.
                parts = _comparison_parts(sense)
                if not parts or parts[0] not in _comparison_parts(meaning):
                    continue
                value = deepcopy(material)
                value.pop('_target_word', None)
                if checked and field != 'definitions':
                    value['_target_word'] = target_surface(value.get('sentence' if field == 'examples' else 'text', ''), word)
                    if not value['_target_word']:
                        continue
                value['entry_id'] = entry['id']
                item['learning'].setdefault(field, []).append(value)
    records = list(selected.values())
    for item in records:
        item['meaning'] = ' / '.join(item['meanings'])
        base = tuple(dict.fromkeys(known[_key(item['word'])]))
        base_set = set(base)
        item['parts'] = tuple(dict.fromkeys((*base, *(part for context in contexts if base_set.intersection(context) for part in context))))
    return records


def _materials(item, kind):
    field = {'definition': 'definitions', 'example': 'examples', 'passage': 'passages'}[kind]
    output, seen = [], set()
    for material in item['learning'].get(field, []):
        text = _text(material.get('sentence' if kind == 'example' else 'text'))
        if not text or text in seen or len(text) > 12000 or re.search(r'<[^>]*>', text):
            continue
        if kind != 'definition':
            if not re.fullmatch(r"[A-Za-z][A-Za-z'’-]*(?: [A-Za-z][A-Za-z'’-]*){0,5}", _material_target(material, item['word'])):
                continue
            if not contains_target(text, _material_target(material, item['word'])) or len(re.findall(r'[A-Za-z]+', text)) < 3 or not re.search(r'[.!?][\s"\'”’]*$', text):
                continue
            if kind == 'passage' and len(re.findall(r'[.!?](?:\s|$)', text)) < 2:
                continue
        seen.add(text)
        output.append(material)
    return output


def capabilities(bank, unit_ids):
    return _capabilities(bank, unit_ids, _source(bank, unit_ids))


def _capabilities(bank, unit_ids, records):
    available = []
    if len(records) >= 5:
        available.append('def_match')
        if sum(bool(_materials(item, 'example')) for item in records) >= 5:
            available.append('sentence_match')
        if any(len(_materials(item, 'example')) >= 2 for item in records):
            available.append('common_blank')
        if len(records) >= 10:
            available.append('count_correct')
        if any(_text(value.get('translation')) for item in records for value in _materials(item, 'example')):
            available.append('blank_ko')
        if any(_materials(item, 'example') for item in records):
            available.append('word_meaning')
        if any(_materials(item, 'passage') for item in records):
            available.append('passage_blank')
    missing = [kind for kind in TYPES if kind not in available]
    scope = [entry for entry in bank.get('entries', []) if entry.get('unit_id') in unit_ids]
    review_count = sum(entry.get('status') == 'review' for entry in scope)
    page_count = len(bank.get('page_issues', []))
    return {'available_types': available, 'unavailable_types': missing, 'word_count': len(records),
            'review_count': review_count, 'ready_count': sum(entry.get('status') == 'ready' for entry in scope),
            'page_issue_count': page_count, 'requires_review': bool(review_count or page_count),
            'notice': '등록된 학습자료가 있는 유형으로 구성합니다. 예문·해석·지문이 없는 유형은 출제하지 않습니다.' if missing else '',
            'generation_method': 'script'}


def _wrong(item, records, rng, count=1):
    eligible = [other for other in records if _key(other['word']) != _key(item['word'])
                and not _conflicts(item['parts'], other['parts']) and not _conflicts(other['parts'], item['parts'])]
    rng.shuffle(eligible)
    eligible.sort(key=lambda other: -_confusion_score(item['word'], {'source_words': [other['word']]}))
    chosen = []
    for other in eligible:
        if any(_conflicts(other['parts'], previous['parts']) for previous in chosen):
            continue
        chosen.append(other)
        if len(chosen) == count:
            return chosen
    raise AppError('서로 뜻이 겹치지 않는 보기 자료가 부족합니다. 더 많은 챕터를 선택해 주세요.', 422, 'insufficient_distractors')


def _blank(text, word):
    # Keep the original matching of possessives/hyphenated/repeated targets for
    # saved materials. New checked materials supply their verified surface form.
    return re.sub(r'(?<![A-Za-z])' + re.escape(word) + r'(?![A-Za-z])', '______', text, flags=re.I)


def _material_target(material, headword):
    return material.get('_target_word') or headword


def _build(kind, records, rng, offset):
    referenced, materials = [], []
    def take(items, number=1):
        if len(items) < number:
            raise AppError('이 유형에 필요한 학습자료가 부족합니다.', 422, 'learning_material_missing')
        start = offset % len(items)
        return (items[start:] + items[:start])[:number]
    def use(items):
        referenced.extend(items)
        return items
    def choice(target, values):
        rng.shuffle(values)
        return values, values.index(target) + 1
    explanation = ''
    if kind in ('def_match', 'pair_ko', 'sentence_match', 'count_correct'):
        pool = [item for item in records if _materials(item, 'example')] if kind == 'sentence_match' else records
        rows = use(take(pool, 10 if kind == 'count_correct' else 5))
        field = 'ko' if kind == 'pair_ko' else 'def'
        english = kind == 'def_match' and all(_materials(item, 'definition') for item in rows)
        correct_count = rng.randint(3, 7) if kind == 'count_correct' else len(rows) - 1
        wrong_indices = set(rng.sample(range(len(rows)), len(rows) - correct_count))
        values, corrections = [], []
        for index, item in enumerate(rows):
            wrong = index in wrong_indices
            other = use(_wrong(item, records, rng))[0] if wrong and not english else item
            if kind == 'sentence_match':
                material = _materials(item, 'example')[0]
                materials.append(material)
                meaning = other['meaning'] if wrong else material['meaning']
                values.append({'en': material['sentence'], 'u': _material_target(material, item['word']), 'ko': f'({meaning})'})
            elif english:
                # Keep one language within the entire definition question.
                own_definitions = {_key(value['text']) for value in _materials(item, 'definition')}
                definition_pool = [candidate for candidate in records if _materials(candidate, 'definition')
                                   and _key(_materials(candidate, 'definition')[0]['text']) not in own_definitions]
                other = use(_wrong(item, definition_pool, rng))[0] if wrong else item
                material = _materials(other, 'definition')[0]
                materials.append(material)
                values.append({'word': item['word'], field: material['text']})
            else:
                values.append({'word': item['word'], 'ko' if kind == 'count_correct' else field: other['meaning']})
            if wrong:
                corrections.append(f"{index + 1}번 {item['word']}: {item['meaning']}")
        if kind == 'count_correct':
            instruction = '등록된 어휘 목록의 단어·뜻 연결과 일치하는 것은 모두 몇 개인가?'
            payload = {'table': values, 'choices': [f'{n}개' for n in range(3, 8)]}
            answer = correct_count - 2
            explanation = f'목록과 일치하는 연결은 {correct_count}개입니다. 바르게 연결하면 ' + ', '.join(corrections) + '입니다.'
        else:
            instruction = ('등록된 학습자료에서 밑줄 친 단어와 뜻의 연결이 일치하지 않는 것은?' if kind == 'sentence_match'
                           else '등록된 ' + ('영영풀이' if english else '어휘 목록') + '의 단어·뜻 연결과 일치하지 않는 것은?')
            payload = {'items' if kind == 'sentence_match' else 'pairs': values}
            answer = next(iter(wrong_indices)) + 1
            explanation = '등록된 뜻에 맞게 연결하면 ' + ', '.join(corrections) + '입니다.'
    else:
        material_kind = 'passage' if kind == 'passage_blank' else 'example'
        eligible = [item for item in records if len(_materials(item, material_kind)) >= (2 if kind == 'common_blank' else 1)]
        if kind == 'blank_ko':
            eligible = [item for item in eligible if any(_text(m.get('translation')) for m in _materials(item, 'example'))]
        item = use(take(eligible))[0]
        material_list = _materials(item, material_kind)
        if kind == 'blank_ko':
            material_list = [m for m in material_list if _text(m.get('translation'))]
        selected = material_list[:2 if kind == 'common_blank' else 1]
        materials.extend(selected)
        material = selected[0]
        distractor_pool = records
        if kind in ('common_blank', 'passage_blank', 'blank_ko'):
            text_key = 'text' if kind == 'passage_blank' else 'sentence'
            templates = {_key(_blank(value[text_key], _material_target(value, item['word']))) for value in selected}
            def same_registered_context(other):
                alternatives = _materials(other, material_kind)
                if kind == 'blank_ko':
                    alternatives = [value for value in alternatives if _key(value.get('translation')) == _key(material.get('translation'))]
                patterns = {_key(_blank(value[text_key], _material_target(value, other['word']))) for value in alternatives}
                return templates <= patterns
            distractor_pool = [other for other in records if not same_registered_context(other)]
        wrong = use(_wrong(item, distractor_pool, rng, 4))
        target = material['meaning'] if kind == 'word_meaning' else item['word']
        values, answer = choice(target, [target, *(r['meaning'] if kind == 'word_meaning' else r['word'] for r in wrong)])
        payload = {'choices': values}
        if kind == 'common_blank':
            instruction = '등록된 예문 두 개의 빈칸에 공통으로 들어갈 단어는?'
            payload['sentences'] = [_blank(value['sentence'], _material_target(value, item['word'])) for value in selected]
        elif kind == 'blank_ko':
            instruction = '우리말 해석에 맞게 등록된 예문의 빈칸에 들어갈 단어는?'
            payload.update(sentence=_blank(material['sentence'], _material_target(material, item['word'])), ko=material['translation'])
        elif kind == 'word_meaning':
            instruction = '다음 등록 예문에서 밑줄 친 단어의 뜻으로 알맞은 것은?'
            payload.update(sentence=material['sentence'], target=_material_target(material, item['word']))
        else:
            instruction = '등록된 지문의 빈칸에 들어갈 단어는?'
            payload['passage'] = _blank(material['text'], _material_target(material, item['word']))
        if kind != 'word_meaning' and any(_material_target(value, item['word']).casefold() != item['word'].casefold() for value in selected):
            instruction += (' (~는 목적어가 들어갈 자리를 뜻합니다.)' if '~' in item['word'].split() else
                ' (필요한 경우 문장에 맞게 형태를 바꾸어 쓰는 단어의 원형을 고르세요.)')
        explanation = f"등록된 학습자료의 단어는 {item['word']}, 뜻은 {material['meaning']}입니다."
        if _text(material.get('translation')):
            explanation += ' ' + material['translation']
    evidence = []
    seen = set()
    for item in referenced:
        for entry in item['entries']:
            if entry['id'] not in seen:
                evidence.append({key: deepcopy(entry[key]) for key in ('id', 'unit_id', 'word', 'meaning', 'meaning_raw', 'source') if key in entry})
                seen.add(entry['id'])
    payload['_script'] = {'version': VERSION, 'source_entries': evidence, 'learning_materials': deepcopy(materials),
                          'semantic_review_performed': False}
    if len(json.dumps(payload, ensure_ascii=False)) > 43000:
        raise AppError('문항에 필요한 원문 자료가 저장 한도를 초과합니다. 더 짧은 학습자료 또는 다른 챕터를 선택해 주세요.',
                       422, 'source_material_too_large')
    return {'no': 1, 'type': kind, 'instruction': instruction, 'payload': payload, 'answer': answer, 'explanation': explanation}


def generate(bank, unit_ids, count, *, seed=None, question_types=None, generation_request_id='', allow_partial=False):
    if type(count) is not int or not 1 <= count <= 20:
        raise AppError('문항 수는 1~20개여야 합니다.', 422, 'invalid_count')
    if bank.get('page_issues') and not allow_partial:
        raise AppError('확인이 필요한 페이지가 있습니다. 원본 확인 또는 등록된 항목만 출제를 선택해 주세요.', 422, 'page_review_required')
    try:
        select_words(bank, unit_ids, allow_partial=allow_partial)
    except ExamError as error:
        raise AppError(str(error), 422, error.code, **error.details) from None
    records = _source(bank, unit_ids)
    info = _capabilities(bank, unit_ids, records)
    selected_types = question_types or info['available_types']
    allowed = set(info['available_types']) | ({'pair_ko'} if 'def_match' in info['available_types'] else set())
    if not selected_types or not isinstance(selected_types, list) or any(kind not in allowed for kind in selected_types):
        raise AppError('선택한 유형에 필요한 단어·예문·해석·지문이 부족합니다.', 422, 'learning_material_missing', **info)
    if seed is None:
        seed = generation_request_id or hashlib.sha256(json.dumps([bank, unit_ids], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if type(seed) not in (str, int):
        raise AppError('생성 기준값을 확인해 주세요.', 422, 'invalid_seed')
    rng = random.Random(seed)
    rng.shuffle(records)
    questions = []
    for index in range(count):
        kind = selected_types[index % len(selected_types)]
        question = _build(kind, records, rng, index)
        question['no'] = index + 1
        question['payload']['_script'].update(bank_id=bank.get('id', ''), unit_ids=list(unit_ids),
            generation_request_id=generation_request_id, seed=seed, allow_partial=allow_partial)
        questions.append(question)
    validation.validate_questions(questions, generated=True)
    return questions


def regenerate(bank, unit_ids, current, previous, *, request_id, generation_request_id=''):
    def identity(value):
        result = {key: deepcopy(value[key]) for key in ('type', 'instruction', 'answer', 'explanation', 'payload') if key in value}
        result.get('payload', {}).pop('_script', None)
        return result
    for attempt in range(10):
        result = generate(bank, unit_ids, 1, seed=f'{request_id}:{attempt}', question_types=[current['type']],
                          generation_request_id=generation_request_id,
                          allow_partial=current.get('payload', {}).get('_script', {}).get('allow_partial', False))[0]
        if all(identity(result) != identity(value) for value in previous):
            return result
    raise AppError('새로운 문항 조합이 부족합니다. 다른 챕터를 선택해 주세요.', 422, 'candidate_exhausted')


def reuse_saved_material(bank, question):
    """Retain usable historical AI context; never manufacture replacement text."""
    payload, kind = question['payload'], question['type']
    def add(word, field, material):
        for entry in bank['entries']:
            if _key(entry['word']) != _key(word):
                continue
            value = {**material, 'origin': 'ai_enrichment', 'meaning': entry['meaning'], 'reused_saved_question': True}
            entry.setdefault('learning', {}).setdefault(field, []).append(value)
    if kind == 'word_meaning':
        add(payload.get('target', ''), 'examples', {'sentence': payload.get('sentence', ''), 'translation': ''})
    elif kind in ('common_blank', 'blank_ko', 'passage_blank'):
        word = payload['choices'][question['answer'] - 1]
        texts = payload['sentences'] if kind == 'common_blank' else [payload.get('sentence' if kind == 'blank_ko' else 'passage', '')]
        for text in texts:
            field = 'passages' if kind == 'passage_blank' else 'examples'
            key = 'text' if kind == 'passage_blank' else 'sentence'
            add(word, field, {key: re.sub(r'_{3,}', lambda _: word, text), 'translation': payload.get('ko', '')})
    elif kind == 'sentence_match':
        for index, row in enumerate(payload['items'], 1):
            add(row['u'], 'examples', {'sentence': row['en'], 'translation': ''})
    elif kind == 'def_match':
        for index, row in enumerate(payload['pairs'], 1):
            if index != question['answer'] and re.search('[A-Za-z]', row['def']) and not re.search('[가-힣]', row['def']):
                add(row['word'], 'definitions', {'text': row['def']})
