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
# v1.5.11 (D8): reasoning models (Qwen3, DeepSeek-R1, ...) may emit a reasoning block before the answer.
_REASONING_BLOCK = re.compile(r"<(think|thinking|reasoning)\b[^>]*>.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_REASONING = re.compile(r"<(think|thinking|reasoning)\b[^>]*>", re.IGNORECASE)

JUDGE_CATEGORIES: tuple[str, ...] = tuple(sorted(name for name in CATEGORIES if name not in {"legacy_text_signal", "evidence_quality"}))


def judge_output_schema() -> dict[str, Any]:
    """JSON Schema sent with ``response_format: json_schema``; mirrors :func:`parse_judge_output` (v1.5.11, D7)."""
    return {
        "type": "object",
        "properties": {
            "suspicion": {"type": "number", "minimum": 0, "maximum": 1},
            "categories": {"type": "array", "items": {"type": "string", "enum": list(JUDGE_CATEGORIES)}, "maxItems": len(JUDGE_CATEGORIES)},
            "rationale": {"type": "string", "maxLength": RATIONALE_MAX_CHARS},
            "evidence_refs": {"type": "array", "items": {"type": "string", "maxLength": 200}, "maxItems": EVIDENCE_REFS_MAX},
        },
        "required": ["suspicion", "categories", "rationale", "evidence_refs"],
        "additionalProperties": False,
    }


class JudgeOutputError(ValueError):
    pass


def strip_reasoning_blocks(text: str) -> str:
    """Drop ``<think>…</think>`` style blocks; an unclosed opening tag means the answer never arrived."""
    stripped = _REASONING_BLOCK.sub("", text or "")
    unclosed = _UNCLOSED_REASONING.search(stripped)
    if unclosed:
        stripped = stripped[: unclosed.start()]
    return stripped.strip()


def last_json_object(text: str) -> str | None:
    """The last balanced top-level ``{…}`` in ``text`` (string-aware), or ``None``."""
    last: str | None = None
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"' and depth > 0:
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                last = text[start : index + 1]
    return last


def parse_judge_output(text: str, *, strip_reasoning: bool = False) -> dict[str, Any]:
    """Strictly parse a judge reply. With ``strip_reasoning`` reasoning blocks are removed first and, if the rest
    is not JSON, the last top-level JSON object is used. Reasoning text is never returned or stored."""
    raw = (text or "").strip()
    if strip_reasoning:
        raw = strip_reasoning_blocks(raw)
    fenced = _FENCE.match(raw)
    if fenced:
        raw = fenced.group(1)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        candidate = last_json_object(raw) if strip_reasoning else None
        if candidate is None:
            raise JudgeOutputError("output_not_json") from exc
        try:
            data = json.loads(candidate)
        except ValueError as inner:
            raise JudgeOutputError("output_not_json") from inner
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
