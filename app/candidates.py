"""Per-question candidates, stored alongside the active question in its Drive sheet."""
import copy
import hashlib
import json
from . import validation
from .errors import AppError

MAX_CANDIDATES = 10
COLUMNS = [f"candidate_{i}_json" for i in range(1, 11)] + ["candidate_selected"]
FIELDS = ("type", "instruction", "answer", "explanation", "payload")


def core(question):
    return {"no": 1, **{k: copy.deepcopy(question.get(k, "") if k == "explanation" else question[k]) for k in FIELDS}}


def items(question):
    values = copy.deepcopy(question.get("alternatives") or [{**core(question), "candidate_id": "original"}])
    selected = question.get("candidate_selected", 0)
    if not 1 <= len(values) <= MAX_CANDIDATES or type(selected) is not int or not 0 <= selected < len(values):
        raise ValueError("문항 후보 정보를 확인해 주세요.")
    values = [{**core(value), "candidate_id": value["candidate_id"]} for value in values]
    # Direct edits in Google Sheets remain the source of truth for the active slot.
    values[selected] = {**core(question), "candidate_id": values[selected]["candidate_id"]}
    return values


def token(question):
    value = {"items": items(question), "selected": question.get("candidate_selected", 0)}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def expose(question):
    result = copy.deepcopy(question)
    result.update(alternatives=items(question), candidate_selected=question.get("candidate_selected", 0), candidate_token=token(question))
    return result


def check_token(question, expected):
    if expected != token(question):
        raise AppError("문항 또는 후보가 다른 화면에서 변경됐습니다. 편집 내용을 복사해 두고 시험지를 다시 열어 주세요.", 409, "candidate_conflict")


def merge_saved(edited, original):
    if edited.get("candidate_token") is not None:
        # Retrying a committed save after a lost response/PDF failure is safe.
        if token(edited) != token(original):
            check_token(original, edited["candidate_token"])
    elif original.get("alternatives"):
        raise AppError("새로고침 후 문항 후보를 다시 불러와 주세요.", 409, "candidate_conflict")
    old = items(original)
    values = edited.get("alternatives")
    if values is None:
        edited["alternatives"] = old
        edited["candidate_selected"] = original.get("candidate_selected", 0)
    else:
        if [v.get("candidate_id") for v in values] != [v["candidate_id"] for v in old]:
            raise AppError("문항 후보 목록이 변경됐습니다. 시험지를 다시 열어 주세요.", 409, "candidate_conflict")
    # Provenance and frozen generation scope are server-owned. Editing visible
    # question text must not silently replace its bank/checkpoint references.
    for index, value in enumerate(edited['alternatives']):
        proof = old[index].get('payload', {}).get('_script')
        if proof is not None:
            value['payload']['_script'] = copy.deepcopy(proof)
    selected = edited.get('candidate_selected', 0)
    if type(selected) is int and 0 <= selected < len(old):
        proof = old[selected].get('payload', {}).get('_script')
        if proof is not None:
            edited['payload']['_script'] = copy.deepcopy(proof)
    edited["alternatives"] = items(edited)
    validation.validate_questions(copy.deepcopy(edited["alternatives"]), check_context=False)
    return edited


def row_values(question):
    values = items(question) if question.get("alternatives") else []
    row = {name: "" for name in COLUMNS}
    for index, value in enumerate(values):
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded) > 45000:
            raise ValueError("문항 후보의 내용이 너무 깁니다. 내용을 줄여 주세요.")
        row[COLUMNS[index]] = encoded
    if values:
        row["candidate_selected"] = question.get("candidate_selected", 0)
    return row


def from_row(row):
    values = [json.loads(row[name]) for name in COLUMNS[:-1] if row.get(name)]
    return {"alternatives": values, "candidate_selected": int(row.get("candidate_selected") or 0)} if values else {}
