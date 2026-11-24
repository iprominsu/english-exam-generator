"""Context contracts, with read compatibility for old inline/plain meaning items."""
import re

CONTEXT_CLAIM = re.compile(r"(?:문장|예문|문맥|지문)(?:에서|에서는|의|\s*속|\s*안)")

def plain(text):
    return re.sub(r'</?u>', '', text or '', flags=re.I)

def target_word(q):
    target = q.get('payload', {}).get('target')
    if isinstance(target, str) and target.strip():
        return target.strip()
    match = re.search(r"([A-Za-z][A-Za-z'’-]*(?: [A-Za-z][A-Za-z'’-]*){0,3})의\s*뜻", plain(q.get('instruction', '')))
    return match.group(1).strip() if match else ''

def contains_target(sentence, target):
    return bool(target and re.search(r"(?<![A-Za-z])" + re.escape(target) + r"(?![A-Za-z])", sentence, re.I))

def legacy_sentence(q):
    # Earlier releases put a full English sentence after the Korean question.
    # Never accept the isolated target word in that question as an example.
    instruction = plain(q.get('instruction', ''))
    tail = instruction.split('?', 1)[1] if '?' in instruction else ''
    return tail.strip()

def validate_word_context(q, *, generated=False):
    if q.get('type') != 'word_meaning':
        return
    payload = q['payload']
    structured = 'sentence' in payload or 'target' in payload
    promised = bool(CONTEXT_CLAIM.search(q['instruction'] + ' ' + q.get('explanation', '')))
    if not (generated or structured or promised):
        return  # Old, explicitly context-free vocabulary recall remains valid.
    if generated and not structured:
        raise ValueError('문맥형 뜻 문제에는 별도의 영어 예문과 대상 단어가 필요합니다.')
    sentence = payload.get('sentence') if structured else legacy_sentence(q)
    target = payload.get('target') if structured else target_word(q)
    if not isinstance(sentence, str) or not sentence.strip() or len(sentence)>15000:
        raise ValueError('영어 예문을 입력해 주세요.')
    if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z][A-Za-z'’-]*(?: [A-Za-z][A-Za-z'’-]*){0,5}", target.strip()):
        raise ValueError('예문에서 묻는 대상 단어를 입력해 주세요.')
    if re.search(r'<[^>]*>', sentence) or len(re.findall(r"[A-Za-z]+", sentence)) < 3 or not re.search(r'[.!?][\s\"\'”’]*$', sentence.strip()):
        raise ValueError('영어 예문은 태그 없이 완결된 문장으로 입력해 주세요.')
    if not contains_target(sentence, target.strip()):
        raise ValueError('대상 단어가 영어 예문 안에 실제로 있어야 합니다.')
    asked = re.search(r"([A-Za-z][A-Za-z'’-]*)의\s*뜻", plain(q['instruction']))
    if asked and asked.group(1).casefold() != target.strip().casefold():
        raise ValueError('지시문의 단어와 예문의 대상 단어가 다릅니다.')
