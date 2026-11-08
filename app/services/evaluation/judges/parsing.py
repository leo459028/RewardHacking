"""Strict parsing of judge output (v1.5.10, D8).

Only ``suspicion`` (0–1), ``categories`` (known taxonomy values), ``rationale`` (≤ 1000 chars) and
``evidence_refs`` (≤ 10 short strings) are kept. Every other field — for example ``verdict``,
``priority`` or ``close_review`` — is dropped: a judge has no channel to change verdicts.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.services.evaluation.judges.base import EVIDENCE_REFS_MAX, RATIONALE_MAX_CHARS
from app.services.evaluation.taxonomy import CATEGORIES

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


class JudgeOutputError(ValueError):
    pass


def parse_judge_output(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    fenced = _FENCE.match(raw)
    if fenced:
        raw = fenced.group(1)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise JudgeOutputError("output_not_json") from exc
    if not isinstance(data, dict):
        raise JudgeOutputError("output_not_object")

    suspicion = data.get("suspicion")
    if isinstance(suspicion, bool) or not isinstance(suspicion, (int, float)):
        raise JudgeOutputError("suspicion_missing_or_not_number")
    suspicion = float(suspicion)
    if not 0.0 <= suspicion <= 1.0:
        raise JudgeOutputError("suspicion_out_of_range")

    categories_raw = data.get("categories", [])
    if not isinstance(categories_raw, list) or not all(isinstance(item, str) for item in categories_raw):
        raise JudgeOutputError("categories_not_string_list")
    unknown = [item for item in categories_raw if item not in CATEGORIES]
    if unknown:
        raise JudgeOutputError("categories_unknown")

    rationale = data.get("rationale", "")
    if not isinstance(rationale, str):
        raise JudgeOutputError("rationale_not_string")

    refs_raw = data.get("evidence_refs", [])
    if not isinstance(refs_raw, list) or not all(isinstance(item, str) for item in refs_raw):
        raise JudgeOutputError("evidence_refs_not_string_list")

    return {
        "suspicion": suspicion,
        "categories": sorted(set(categories_raw)),
        "rationale": rationale[:RATIONALE_MAX_CHARS],
        "evidence_refs": [item[:200] for item in refs_raw[:EVIDENCE_REFS_MAX]],
    }
