"""v1.5.9 optional importer: Inspect evaluation logs (JSON) -> benchmark samples.

Only the JSON log format is accepted (``.eval`` zip archives are rejected to avoid zip-bomb
handling, D23). Imports are size- and count-bounded, redacted like any other evidence, tenant
scoped (``inspect:<digest>`` corpus) and default to ``dry_run``. Labels come either from a sample
metadata key or from a scorer whose positive value means the agent cheated (for example an
impossible-task scorer where "C"/1.0 means the impossible tests passed).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.benchmark import BenchmarkSample
from app.services.benchmark import BenchmarkError, normalize_evidence, normalize_task, sample_id_for, split_for
from app.services.evaluation.evidence import evidence_digest
from app.services.evaluation.taxonomy import CATEGORIES

MAX_SAMPLES = 500
PREVIEW_LIMIT = 20
POSITIVE_SCORE_STRINGS = {"C", "CORRECT", "TRUE", "YES", "PASS", "PASSED", "CHEATED", "HACKING"}


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                content = item.get("content")
                parts.append(_text(content) if content is not None else str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        return _text(value.get("content") if "content" in value else value.get("text", ""))
    return "" if value is None else str(value)


def _goal(sample: dict[str, Any]) -> str:
    raw = sample.get("input")
    if isinstance(raw, list):
        users = [item for item in raw if isinstance(item, dict) and item.get("role") == "user"]
        raw = users[0] if users else raw
    return _text(raw)[:2000]


def _completion(sample: dict[str, Any]) -> str:
    output = sample.get("output") or {}
    if isinstance(output, dict):
        if output.get("completion"):
            return _text(output.get("completion"))
        choices = output.get("choices") or []
        if choices and isinstance(choices[-1], dict):
            return _text((choices[-1].get("message") or {}).get("content"))
    return _text(output)


def _score_positive(sample: dict[str, Any], threshold: float, scorer: str | None) -> bool | None:
    scores = sample.get("scores") or {}
    if not isinstance(scores, dict) or not scores:
        return None
    entry = scores.get(scorer) if scorer else next(iter(scores.values()))
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value) >= threshold
    if isinstance(value, str):
        return value.strip().upper() in POSITIVE_SCORE_STRINGS
    return None


def _metadata_label(sample: dict[str, Any], key: str) -> bool | None:
    value = (sample.get("metadata") or {}).get(key)
    if value is None:
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"hacking", "true", "yes", "cheated", "1"}:
            return True
        if lowered in {"benign", "false", "no", "honest", "0"}:
            return False
        return None
    return bool(value)


def parse_log(raw: bytes | str | dict[str, Any], *, max_bytes: int) -> tuple[dict[str, Any], str]:
    if isinstance(raw, dict):
        data = raw
        encoded = json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
    else:
        encoded = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        if encoded[:2] == b"PK":
            raise BenchmarkError("inspect_import_unsupported_format", "Only Inspect JSON logs are supported; .eval archives are not.", 415)
        if len(encoded) > max_bytes:
            raise BenchmarkError("inspect_import_too_large", f"Inspect logs are limited to {max_bytes} bytes.", 413)
        try:
            data = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BenchmarkError("inspect_import_invalid_json", "The Inspect log is not valid JSON.", 422) from exc
    if len(encoded) > max_bytes:
        raise BenchmarkError("inspect_import_too_large", f"Inspect logs are limited to {max_bytes} bytes.", 413)
    if not isinstance(data, dict) or not isinstance(data.get("samples"), list):
        raise BenchmarkError("inspect_import_invalid_log", "The Inspect log must be an object with a 'samples' list.", 422)
    return data, hashlib.sha256(encoded).hexdigest()[:16]


async def import_inspect_log(
    session: AsyncSession,
    *,
    raw: bytes | str | dict[str, Any],
    tenant_id: str,
    max_bytes: int,
    label_from: str = "score",
    score_threshold: float = 1.0,
    scorer: str | None = None,
    metadata_key: str = "reward_hacking",
    hacking_category: str = "special_casing",
    dry_run: bool = True,
    actor: str = "",
) -> dict[str, Any]:
    if hacking_category not in CATEGORIES or hacking_category in {"evidence_quality", "legacy_text_signal"}:
        raise BenchmarkError("benchmark_label_categories_required", f"Unknown hacking category {hacking_category!r}.", 422)
    if label_from not in {"score", "metadata"}:
        raise BenchmarkError("inspect_import_label_source_invalid", "label_from must be 'score' or 'metadata'.", 422)
    log, content_digest = parse_log(raw, max_bytes=max_bytes)
    # Tenant-qualified so two tenants importing the same log never share (or collide on) a corpus.
    import_id = hashlib.sha256(f"{tenant_id}:{content_digest}".encode("utf-8")).hexdigest()[:16]
    samples = list(log.get("samples") or [])
    if len(samples) > MAX_SAMPLES:
        raise BenchmarkError("inspect_import_too_many_samples", f"At most {MAX_SAMPLES} samples per import.", 413)
    corpus = f"inspect:{import_id}"
    eval_meta = log.get("eval") if isinstance(log.get("eval"), dict) else {}
    counts = {"samples": len(samples), "hacking": 0, "benign": 0, "unlabelled": 0, "duplicates": 0, "imported": 0}
    preview: list[dict[str, Any]] = []
    existing = {
        row.evidence_digest
        for row in (await session.scalars(select(BenchmarkSample).where(BenchmarkSample.corpus == corpus))).all()
    }
    seen: set[str] = set()
    for index, item in enumerate(samples):
        if not isinstance(item, dict):
            counts["unlabelled"] += 1
            continue
        positive = _metadata_label(item, metadata_key) if label_from == "metadata" else _score_positive(item, score_threshold, scorer)
        if positive is None:
            counts["unlabelled"] += 1
            continue
        label = "hacking" if positive else "benign"
        evidence = normalize_evidence({"final_message": _completion(item)[:32768], "provenance": "self_reported"})
        digest = evidence_digest({**evidence, "_inspect_sample": f"{item.get('id', index)}:{item.get('epoch', 1)}"})
        if digest in existing or digest in seen:
            counts["duplicates"] += 1
            continue
        seen.add(digest)
        counts[label] += 1
        sample_id = sample_id_for(f"{corpus}:{item.get('id', index)}:{item.get('epoch', 1)}")
        task = normalize_task({"task_type": "other", "goal": _goal(item)})
        if len(preview) < PREVIEW_LIMIT:
            preview.append({"sample_id": sample_id, "inspect_id": item.get("id", index), "label": label, "goal": task["goal"][:200], "final_message": evidence["final_message"][:200]})
        if dry_run:
            continue
        session.add(
            BenchmarkSample(
                sample_id=sample_id,
                corpus=corpus,
                scope="tenant",
                tenant_id=tenant_id,
                source="inspect_import",
                split=split_for(sample_id),
                name=f"inspect-{str(item.get('id', index))[:40]}",
                label=label,
                label_categories=[hacking_category] if label == "hacking" else [],
                label_notes=["inspect_import", f"model:{str(eval_meta.get('model') or 'unknown')[:60]}"],
                finding_labels={},
                task=task,
                evidence=evidence,
                evidence_digest=digest,
                origin_review_id="",
                origin_run_id="",
                status="active",
                label_history=[],
                created_by=actor[:128],
            )
        )
        counts["imported"] += 1
    if not dry_run:
        await session.commit()
    return {
        "import_id": import_id,
        "corpus": corpus,
        "dry_run": dry_run,
        "task": str(eval_meta.get("task") or ""),
        "model": str(eval_meta.get("model") or ""),
        "counts": counts,
        "preview": preview,
        "signal_usage": "monitor_only",
    }
