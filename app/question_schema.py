"""Structured exam output contract shared by every GPT reasoning level."""

def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def exam_schema(count, kind=None):
    text = {"type": "string"}
    def array(item, length):
        return {"type": "array", "items": item, "minItems": length, "maxItems": length}
    choices = array(text, 5)
    payloads = {
        "def_match": {"pairs": array(object_schema({"word": text, "def": text}), 5)},
        "sentence_match": {"items": array(object_schema({"en": text, "u": text, "ko": text}), 5)},
        "common_blank": {"sentences": array(text, 2), "choices": choices},
        "count_correct": {"table": array(object_schema({"word": text, "ko": text}), 10), "choices": choices},
        "blank_ko": {"sentence": text, "ko": text, "choices": choices},
        "word_meaning": {"sentence": text, "target": text, "choices": choices},
        "passage_blank": {"passage": text, "choices": choices},
    }
    variants = []
    if kind == 'pair_ko':
        payloads['pair_ko'] = {'pairs': array(object_schema({'word': text, 'ko': text}), 5)}
    for question_type, payload in payloads.items():
        if kind and question_type != kind:
            continue
        variants.append(object_schema({
            "no": {"type": "integer", "minimum": 1},
            "type": {"type": "string", "enum": [question_type]},
            "instruction": text, "answer": {"type": "integer", "minimum": 1, "maximum": 5},
            "explanation": text, "payload": object_schema(payload),
        }))
    return object_schema({"questions": array({"anyOf": variants}, count)})
