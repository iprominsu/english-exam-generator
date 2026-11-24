"""Immutable extraction evidence and explicit human edits.

The source manifest is evidence about extraction, not a claim that OCR or a
legacy bank was verified. Current words, meanings and labels are separate.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def learning_input_fingerprint(entry):
    """Content identity; display titles and chapter labels are not AI inputs."""
    return _hash({key: entry.get(key) for key in
        ("id", "word", "meaning", "related", "source", "word_raw", "meaning_raw")})


def _source_content(bank):
    return {
        "source_sha256": bank.get("source", {}).get("sha256"),
        "entries": [{"id": entry["id"], "unit_id": entry["unit_id"],
            "word_raw": entry.get("word_raw", entry.get("original_word", entry.get("word", ""))),
            "meaning_raw": entry.get("meaning_raw", entry.get("meaning", "")),
            "source": deepcopy(entry.get("source", {}))} for entry in bank.get("entries", [])],
        "units": [{"id": unit["id"], "label": unit.get("original_label", unit.get("label", "")),
            "order": unit.get("order"), "pages": deepcopy(unit.get("pages", [])),
            **({"source_identity": deepcopy(unit["source_identity"])} if "source_identity" in unit else {})}
            for unit in bank.get("units", [])],
    }


def freeze_source_manifest(bank, *, evidence="source_locations"):
    """Return a copy with one fixed manifest; never upgrade old evidence silently."""
    output = deepcopy(bank)
    if output.get("source_manifest"):
        assert_source_manifest(output)
        return output
    if evidence not in {"source_locations", "legacy_unverified"}:
        raise ValueError("원문 근거 종류가 올바르지 않습니다.")
    # Fix fallback values before any human edits. Legacy evidence remains
    # explicitly unverified even when these fields can be reconstructed.
    for entry in output.get("entries", []):
        entry.setdefault("word_raw", entry.get("original_word", entry.get("word", "")))
        entry.setdefault("meaning_raw", entry.get("meaning", ""))
    for unit in output.get("units", []):
        unit.setdefault("original_label", unit.get("label", ""))
    content = _source_content(output)
    manifest = {"version": 1, "evidence": evidence, **content}
    output["source_manifest"] = {**manifest, "sha256": _hash(manifest)}
    return output


def assert_source_manifest(bank):
    manifest = bank.get("source_manifest")
    if not manifest:
        raise ValueError("고정된 원문 기준표가 없습니다.")
    content = {key: manifest.get(key) for key in ("source_sha256", "entries", "units")}
    evidence = manifest.get("evidence")
    if (manifest.get("version") != 1 or evidence not in {"source_locations", "legacy_unverified"}
            or _hash({"version": 1, "evidence": evidence, **content}) != manifest.get("sha256")):
        raise ValueError("원문 기준표의 버전 또는 해시가 일치하지 않습니다.")
    if _source_content(bank) != content:
        raise ValueError("원문 항목·출처가 고정된 기준표와 다릅니다.")


def refresh_bank_state(bank):
    entries = bank.get("entries", [])
    bank["counts"] = {"total": len(entries), "page_issues": len(bank.get("page_issues", [])),
        **{state: sum(entry.get("status") == state for entry in entries)
           for state in ("ready", "review", "excluded")}}
    bank["status"] = "requires_review" if (bank["counts"]["review"] or bank.get("page_issues")
        or any(unit.get("status") == "review" for unit in bank.get("units", []))) else "ready"
    pending = sum(bool(entry.get("learning_pending") or entry.get("learning_stale")) for entry in entries)
    if pending or "learning_pending_count" in bank:
        bank["learning_pending_count"] = pending
        bank.setdefault("construction", {}).update(learning_complete=not pending,
            learning_pending_count=pending)
    return bank


def apply_entry_edit(entry, *, word, meaning, status=None, reviewer="사용자", at,
                     review_reason=None):
    """Mutate only current fields. Omitted status preserves the existing state."""
    word, meaning, reviewer = word.strip(), meaning.strip(), reviewer.strip()
    target_status = entry.get("status") if status is None else status
    if not word or not meaning or not reviewer or target_status not in {"ready", "review", "excluded"}:
        raise ValueError("영단어·뜻·등록 상태를 확인해 주세요.")
    changed = [field for field, value in (("word", word), ("meaning", meaning), ("status", target_status))
               if entry.get(field) != value]
    clearing_issues = status == "ready" and bool(entry.get("issues"))
    reason_changed = review_reason is not None and review_reason != entry.get("review_reason")
    if not changed and not clearing_issues and not reason_changed:
        return False
    previous = learning_input_fingerprint(entry)
    entry.setdefault("original_word", entry.get("word", ""))
    entry.update(word=word, meaning=meaning, status=target_status)
    if target_status == "ready":
        entry.update(reviewed_at=at, reviewed_by=reviewer)
        if status == "ready":
            entry["issues"] = []
    if review_reason is not None:
        entry["review_reason"] = review_reason
    entry["manual_edit"] = {"at": at, "by": reviewer, "fields": changed,
        "previous_input_fingerprint": previous, "input_fingerprint": learning_input_fingerprint(entry)}
    if any(field in changed for field in ("word", "meaning")):
        # Also covers an in-flight generation whose result has not arrived yet.
        entry["learning_stale"] = True
        entry["learning_pending"] = {"code": "manual_input_changed",
            "message": "단어·뜻을 수정했습니다. 이전 입력의 추가 학습자료는 사용하지 않습니다."}
    return True


def apply_unit_edit(unit, *, label, at, reviewer="사용자", status=None, review_reason=None):
    label = label.strip()
    if not label or status not in {None, "ready", "review"}:
        raise ValueError("챕터 이름·확인 상태를 확인해 주세요.")
    changed = ["label"] if unit.get("label") != label else []
    if status is not None and unit.get("status") != status:
        changed.append("status")
    if review_reason is not None and review_reason != unit.get("review_reason"):
        changed.append("review_reason")
    if not changed:
        return False
    unit.setdefault("original_label", unit.get("label", ""))
    unit["label"] = label
    if status is not None:
        unit["status"] = status
        if status == "ready":
            unit.update(reviewed_at=at, reviewed_by=reviewer)
    if review_reason is not None:
        unit["review_reason"] = review_reason
    # Confirmation only resolves this unit; entries and page gaps stay separate.
    unit["manual_edit"] = {"at": at, "by": reviewer, "fields": changed}
    return True


def merge_manual_edits(incoming, previous):
    """Preserve explicit edits when an import finishes after the edit request."""
    output = deepcopy(incoming)
    if not previous:
        return output
    output["title"] = previous.get("title", output.get("title"))
    previous_ids = previous.get("source", {}).get("legacy_original_ids", [])
    if previous_ids:
        source = output.setdefault("source", {})
        source["legacy_original_ids"] = list(dict.fromkeys(previous_ids + source.get("legacy_original_ids", [])))
    for key in ("deleted_at", "title_manual_edit", "drive", "drive_state_id"):
        if key in previous:
            output[key] = deepcopy(previous[key])
    edits = {entry["id"]: entry for entry in previous.get("entries", [])
             if entry.get("manual_edit") or entry.get("reviewed_at")}
    incoming_ids = {entry["id"] for entry in output.get("entries", [])}
    if not set(edits).issubset(incoming_ids):
        raise ValueError("수정한 원문 항목이 재등록 결과에서 누락됐습니다.")
    merged_entries = []
    for entry in output.get("entries", []):
        old = edits.get(entry["id"])
        if old is None:
            merged_entries.append(entry)
            continue
        merged = deepcopy(old)
        fingerprint = learning_input_fingerprint(merged)
        if (old.get("manual_edit") and entry.get("learning") and not entry.get("learning_stale")
                and learning_input_fingerprint(entry) == fingerprint
                and entry.get("learning_input_fingerprint", fingerprint) == fingerprint):
            merged["learning"] = deepcopy(entry["learning"])
            merged["learning_input_fingerprint"] = fingerprint
            merged["learning_stale"] = False
            merged.pop("learning_pending", None)
        merged_entries.append(merged)
    output["entries"] = merged_entries
    units = {unit["id"]: unit for unit in previous.get("units", [])
             if unit.get("manual_edit") or unit.get("reviewed_at") or "original_label" in unit
             and unit.get("label") != unit.get("original_label")}
    if not set(units).issubset({unit["id"] for unit in output.get("units", [])}):
        raise ValueError("수정한 챕터가 재등록 결과에서 누락됐습니다.")
    for unit in output.get("units", []):
        old = units.get(unit["id"])
        if old:
            for key in ("label", "original_label", "status", "manual_edit", "reviewed_at", "reviewed_by", "review_reason"):
                if key in old:
                    unit[key] = deepcopy(old[key])
    if previous.get("source_manifest"):
        output["source_manifest"] = deepcopy(previous["source_manifest"])
        assert_source_manifest(output)
    return refresh_bank_state(output)
