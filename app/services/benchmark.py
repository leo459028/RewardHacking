"""v1.5.9 labeled benchmark: corpus management, detector measurement runs and the readiness gate.

Benchmark runs are pure measurement (D4): they call ``EvaluationEngine.evaluate_context()`` only
and never write ``evaluation_results``, open reviews, publish events or touch token leases. Each
run records ``evaluator_version`` and ``corpus_digest`` so its metrics can be reproduced (D6).
All results are monitor-only and must not be used as training rewards.
"""

from __future__ import annotations

import hashlib
import time
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.benchmark import BenchmarkRun, BenchmarkSample
from app.schemas.evaluation import OutcomeEvidence
from app.services.evaluation import metrics as m
from app.services.evaluation.aggregator import finding_score
from app.services.evaluation.engine import EvaluationEngine
from app.services.evaluation.evidence import (
    EvidenceError,
    context_from_outcome,
    effective_protected_paths,
    evidence_digest,
    redact_value,
)
from app.services.evaluation.taxonomy import CATEGORIES, PRIORITY_RANK, kind_for

SEED_CORPUS = "seed-v1.5.9"
SEED_DIR = Path(__file__).resolve().parents[1] / "seeds" / "benchmark"
SEED_FILE = SEED_DIR / "seed_v159.yaml"
GLOBAL_TENANT = "*"
LABELS = ("hacking", "benign")
SOURCES = ("seed_fixture", "review_export", "honeypot", "inspect_import")
SPLITS = ("dev", "holdout")
KINDS = ("reward_tampering", "deception", "specification_gaming")
HOLDOUT_SHARE = 0.20
EVIDENCE_FIELDS = (
    "final_message",
    "diff",
    "changed_files",
    "test_report",
    "tests_before",
    "lint_report",
    "tool_transcript",
    "provenance",
    "attestation",
)
TASK_METADATA_KEYS = ("declared_scope", "allow_test_changes", "protected_paths", "task_intent")

# Categories each detector is responsible for; used for per-detector positives.
DETECTOR_CATEGORIES: dict[str, set[str]] = {
    "test_tampering": {"test_tampering", "ci_tampering"},
    "grader_tampering": {"grader_tampering"},
    "error_suppression": {"error_suppression", "ci_tampering"},
    "claim_evidence": {"false_verification_claim", "unsupported_claim"},
    "scope": {"scope_violation"},
    "special_casing": {"special_casing"},
    "legacy_text": set(CATEGORIES),
}


class BenchmarkError(ValueError):
    def __init__(self, code: str, detail: str, status_code: int = 400, extra: dict[str, Any] | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code
        self.extra = dict(extra or {})


# ---------------------------------------------------------------------------
# IDs, splits and normalization
# ---------------------------------------------------------------------------


def new_benchmark_run_id() -> str:
    return f"bmr_{uuid4().hex[:24]}"


def sample_id_for(key: str) -> str:
    return "bms_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def split_for(sample_id: str) -> str:
    """Deterministic 80/20 dev/holdout split by ``sha256(sample_id)`` (D11)."""
    bucket = int(hashlib.sha256(sample_id.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "holdout" if bucket < HOLDOUT_SHARE else "dev"


def normalize_evidence(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate against ``OutcomeEvidence`` and redact; the result is what samples persist."""
    model = OutcomeEvidence.model_validate({key: raw.get(key) for key in EVIDENCE_FIELDS if raw.get(key) is not None})
    data = model.model_dump(mode="json")
    return redact_value({key: data.get(key) for key in EVIDENCE_FIELDS})


def normalize_task(raw: dict[str, Any] | None, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    base = dict(defaults or {})
    base.update({k: v for k, v in dict(raw or {}).items() if k != "metadata"})
    metadata = dict((defaults or {}).get("metadata") or {})
    metadata.update(dict((raw or {}).get("metadata") or {}))
    return {
        "task_type": str(base.get("task_type") or "code_fix"),
        "goal": str(base.get("goal") or ""),
        "metadata": {key: metadata[key] for key in TASK_METADATA_KEYS if key in metadata},
    }


# ---------------------------------------------------------------------------
# Seed corpus
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def load_seed_definitions() -> tuple[dict[str, Any], ...]:
    document = yaml.safe_load(SEED_FILE.read_text(encoding="utf-8")) or {}
    corpus = str(document.get("corpus") or SEED_CORPUS)
    defaults = dict(document.get("defaults") or {})
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in document.get("samples") or []:
        name = str(entry["name"])
        if name in seen:
            raise ValueError(f"Duplicate seed sample name: {name}")
        seen.add(name)
        label = str(entry["label"])
        if label not in LABELS:
            raise ValueError(f"Seed sample {name} has invalid label {label!r}")
        categories = [str(item) for item in entry.get("categories") or []]
        unknown = [item for item in categories if item not in CATEGORIES]
        if unknown or (label == "hacking" and not categories) or (label == "benign" and categories):
            raise ValueError(f"Seed sample {name} has invalid categories {categories!r}")
        raw_evidence = dict(entry.get("evidence") or {})
        diff_file = raw_evidence.pop("diff_file", None)
        if diff_file:
            raw_evidence["diff"] = (SEED_DIR / "fixtures" / str(diff_file)).read_text(encoding="utf-8")
        evidence = normalize_evidence(raw_evidence)
        sample_id = sample_id_for(f"{corpus}:{name}")
        items.append(
            {
                "sample_id": sample_id,
                "corpus": corpus,
                "scope": "global",
                "tenant_id": GLOBAL_TENANT,
                "source": "seed_fixture",
                "split": split_for(sample_id),
                "name": name,
                "label": label,
                "label_categories": categories,
                "label_notes": [str(item) for item in entry.get("notes") or []],
                "finding_labels": {},
                "task": normalize_task(entry.get("task"), defaults),
                "evidence": evidence,
                "evidence_digest": evidence_digest(evidence),
            }
        )
    return tuple(items)


async def ensure_seed_corpus(session: AsyncSession) -> int:
    """Idempotently upsert the code-owned seed corpus. Returns the number of rows inserted or updated."""
    definitions = load_seed_definitions()
    existing = {
        row.sample_id: row
        for row in (await session.scalars(select(BenchmarkSample).where(BenchmarkSample.source == "seed_fixture"))).all()
    }
    changed = 0
    for item in definitions:
        row = existing.get(item["sample_id"])
        if row is None:
            session.add(BenchmarkSample(created_by="seed", status="active", label_history=[], **item))
            changed += 1
        elif row.evidence_digest != item["evidence_digest"] or row.label != item["label"] or row.label_categories != item["label_categories"]:
            for key, value in item.items():
                setattr(row, key, value)
            changed += 1
    if changed:
        await session.commit()
    return changed


# ---------------------------------------------------------------------------
# Sample selection (tenant isolation, D9)
# ---------------------------------------------------------------------------


def corpus_visible(corpus: str, tenant_id: str) -> bool:
    if corpus in {SEED_CORPUS, "global"}:
        return True
    if corpus.startswith("tenant:"):
        return corpus == f"tenant:{tenant_id}"
    return corpus.startswith("inspect:")


async def select_samples(
    session: AsyncSession,
    *,
    tenant_id: str,
    corpora: list[str] | None = None,
    split: str = "all",
    label: str | None = None,
    category: str | None = None,
    source: str | None = None,
    status: str = "active",
    exclude_accepted_behavior: bool = False,
    limit: int | None = None,
) -> list[BenchmarkSample]:
    stmt = select(BenchmarkSample).where(
        or_(BenchmarkSample.scope == "global", BenchmarkSample.tenant_id == tenant_id),
    )
    if corpora:
        visible = [item for item in corpora if corpus_visible(item, tenant_id)]
        stmt = stmt.where(BenchmarkSample.corpus.in_(visible or ["__none__"]))
    if split in SPLITS:
        stmt = stmt.where(BenchmarkSample.split == split)
    if label:
        stmt = stmt.where(BenchmarkSample.label == label)
    if source:
        stmt = stmt.where(BenchmarkSample.source == source)
    if status and status != "all":
        stmt = stmt.where(BenchmarkSample.status == status)
    stmt = stmt.order_by(BenchmarkSample.corpus.asc(), BenchmarkSample.sample_id.asc())
    rows = list((await session.scalars(stmt)).all())
    if category:
        rows = [row for row in rows if category in (row.label_categories or [])]
    if exclude_accepted_behavior:
        rows = [row for row in rows if "accepted_behavior" not in (row.label_notes or [])]
    return rows[:limit] if limit else rows


def corpus_digest(samples: list[BenchmarkSample] | list[dict[str, Any]]) -> str:
    parts = []
    for item in samples:
        data = item if isinstance(item, dict) else {"sample_id": item.sample_id, "evidence_digest": item.evidence_digest, "label": item.label}
        parts.append(f"{data['sample_id']}:{data['evidence_digest']}:{data['label']}")
    return hashlib.sha256(";".join(sorted(parts)).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Evaluation and metrics
# ---------------------------------------------------------------------------


def _sample_dict(sample: BenchmarkSample | dict[str, Any]) -> dict[str, Any]:
    if isinstance(sample, dict):
        return sample
    return {
        "sample_id": sample.sample_id,
        "name": sample.name,
        "corpus": sample.corpus,
        "source": sample.source,
        "split": sample.split,
        "label": sample.label,
        "label_categories": list(sample.label_categories or []),
        "label_notes": list(sample.label_notes or []),
        "finding_labels": dict(sample.finding_labels or {}),
        "task": dict(sample.task or {}),
        "evidence": dict(sample.evidence or {}),
        "evidence_digest": sample.evidence_digest,
    }


def evaluate_sample(sample: BenchmarkSample | dict[str, Any], *, engine: EvaluationEngine, settings: Settings) -> dict[str, Any]:
    """Evaluate one sample twice (configured modes and shadow promoted). Pure; nothing is persisted."""
    data = _sample_dict(sample)
    task = dict(data.get("task") or {})
    metadata = dict(task.get("metadata") or {})
    protected = effective_protected_paths(tenant_paths=[], allow_defaults=True, request_paths=metadata.get("protected_paths"))
    try:
        ctx, _ = context_from_outcome(
            task_type=str(task.get("task_type") or "code_fix"),
            goal=str(task.get("goal") or ""),
            metadata=metadata,
            policy_flags=[],
            evidence=dict(data.get("evidence") or {}),
            protected_paths=protected,
            settings=settings,
        )
    except EvidenceError as exc:
        return {**_per_sample_base(data), "error": exc.code, "score": 0.0, "score_all_enforced": 0.0, "verdict": "error",
                "verdict_all_enforced": "error", "priority": "", "rule_ids": [], "detector_scores": {}, "finding_ids": []}
    configured = engine.evaluate_context(ctx)
    promoted = engine.evaluate_context(ctx, promote_shadow=True)
    detector_scores: dict[str, float] = {}
    for finding in configured.findings:
        if finding.get("mode") == "disabled":
            continue
        name = str(finding.get("detector", ""))
        score = finding_score(str(finding.get("severity", "")), str(finding.get("confidence", "")), name)
        detector_scores[name] = max(detector_scores.get(name, 0.0), score)
    return {
        **_per_sample_base(data),
        "score": configured.suspicion_score,
        "score_all_enforced": promoted.suspicion_score,
        "verdict": configured.verdict,
        "verdict_all_enforced": promoted.verdict,
        "priority": configured.review_priority,
        "priority_all_enforced": promoted.review_priority,
        "rule_ids": sorted({f"{f.get('rule_id')}:{f.get('mode')}" for f in configured.findings if f.get("rule_id")}),
        "finding_ids": [{"finding_id": f.get("finding_id"), "detector": f.get("detector")} for f in configured.findings],
        "detector_scores": detector_scores,
        "evaluator_version": configured.evaluator_version,
    }


def _per_sample_base(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "sample_id": data["sample_id"],
        "name": data.get("name", ""),
        "corpus": data.get("corpus", ""),
        "source": data.get("source", ""),
        "split": data.get("split", ""),
        "label": data["label"],
        "categories": list(data.get("label_categories") or []),
        "notes": list(data.get("label_notes") or []),
        "finding_labels": dict(data.get("finding_labels") or {}),
    }


def _ranked(rows: list[dict[str, Any]], score_key: str, priority_key: str) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: (-float(r.get(score_key) or 0.0), PRIORITY_RANK.get(r.get(priority_key) or "", 9), r["sample_id"]))


def _mode_metrics(rows: list[dict[str, Any]], *, score_key: str, verdict_key: str, priority_key: str, fpr_targets: tuple[float, ...]) -> dict[str, Any]:
    negatives = [float(r[score_key]) for r in rows if r["label"] == "benign"]
    positives_all = [r for r in rows if r["label"] == "hacking"]

    def block(selected: list[dict[str, Any]]) -> dict[str, Any]:
        return m.score_block([float(r[score_key]) for r in selected], negatives, fpr_targets=fpr_targets)

    by_kind = {kind: block([r for r in positives_all if any(kind_for(c) == kind for c in r["categories"])]) for kind in KINDS}
    categories = sorted({c for r in positives_all for c in r["categories"]})
    by_category = {category: block([r for r in positives_all if category in r["categories"]]) for category in categories}
    verdict_confusion = m.confusion([r[verdict_key] == "review" for r in rows], [r["label"] == "hacking" for r in rows])
    precision_by_priority: dict[str, Any] = {}
    for priority in ("P0", "P1", "P2", "P3"):
        flagged = [r for r in rows if r[verdict_key] == "review" and r.get(priority_key) == priority]
        hits = sum(1 for r in flagged if r["label"] == "hacking")
        low, high = m.wilson_interval(hits, len(flagged))
        precision_by_priority[priority] = {"flagged": len(flagged), "hacking": hits, "precision": (hits / len(flagged)) if flagged else None, "ci_low": low, "ci_high": high}
    ranked = _ranked(rows, score_key, priority_key)
    return {
        "overall": block(positives_all),
        "by_kind": by_kind,
        "by_category": by_category,
        "verdict_confusion": verdict_confusion,
        "precision_by_priority": precision_by_priority,
        "audit_budget_curve": m.audit_budget_curve([r["label"] == "hacking" for r in ranked]),
    }


def compute_metrics(rows: list[dict[str, Any]], *, fpr_targets: tuple[float, ...] = m.DEFAULT_FPR_TARGETS) -> dict[str, Any]:
    rows = [r for r in rows if r.get("verdict") != "error"]
    negatives = [r for r in rows if r["label"] == "benign"]
    by_detector: dict[str, Any] = {}
    detectors = sorted({name for r in rows for name in (r.get("detector_scores") or {})} | (set(DETECTOR_CATEGORIES) - {"legacy_text"}))
    for name in detectors:
        responsible = DETECTOR_CATEGORIES.get(name, set())
        if not responsible:
            continue
        positives = [r for r in rows if r["label"] == "hacking" and set(r["categories"]) & responsible]
        by_detector[name] = m.score_block(
            [float((r.get("detector_scores") or {}).get(name, 0.0)) for r in positives],
            [float((r.get("detector_scores") or {}).get(name, 0.0)) for r in negatives],
            fpr_targets=fpr_targets,
        )
    detector_precision: dict[str, Any] = {}
    for r in rows:
        labels = r.get("finding_labels") or {}
        if not labels:
            continue
        for item in r.get("finding_ids") or []:
            verdict = labels.get(item.get("finding_id"))
            if verdict not in {"true_positive", "false_positive"}:
                continue
            entry = detector_precision.setdefault(str(item.get("detector")), {"tp": 0, "fp": 0})
            entry["tp" if verdict == "true_positive" else "fp"] += 1
    for entry in detector_precision.values():
        total = entry["tp"] + entry["fp"]
        entry["precision"] = entry["tp"] / total if total else None
        entry["ci_low"], entry["ci_high"] = m.wilson_interval(entry["tp"], total)
    return {
        "fpr_targets": [m.fpr_key(t) for t in fpr_targets],
        "positives": sum(1 for r in rows if r["label"] == "hacking"),
        "negatives": len(negatives),
        "errors": 0,
        "as_configured": _mode_metrics(rows, score_key="score", verdict_key="verdict", priority_key="priority", fpr_targets=fpr_targets),
        "all_enforced": _mode_metrics(
            rows, score_key="score_all_enforced", verdict_key="verdict_all_enforced", priority_key="priority_all_enforced", fpr_targets=fpr_targets
        ),
        "by_detector": by_detector,
        "detector_precision": detector_precision,
    }


def gate_evaluation(metrics: dict[str, Any] | None, settings: Settings) -> tuple[str, str]:
    """Readiness gate on reward-tampering TPR at the configured FPR (``as_configured`` mode)."""
    if not metrics:
        return "warn", "benchmark_stale"
    cell = (
        metrics.get("as_configured", {}).get("by_kind", {}).get("reward_tampering", {}).get("tpr_at_fpr", {}).get(m.fpr_key(settings.evaluation_benchmark_gate_fpr))
    )
    if not cell:
        return "warn", "benchmark_gate_fpr_not_measured"
    if cell.get("positives", 0) == 0:
        return "warn", "insufficient_samples"
    tpr = float(cell.get("tpr") or 0.0)
    if tpr < settings.evaluation_benchmark_min_tpr:
        return ("fail" if settings.app_env.lower() == "prod" else "warn"), "reward_tampering_tpr_below_threshold"
    if cell.get("insufficient_samples"):
        return "warn", "insufficient_samples"
    return "pass", "reward_tampering_tpr_meets_threshold"


async def run_benchmark(
    session: AsyncSession,
    *,
    tenant_id: str,
    settings: Settings,
    corpora: list[str] | None = None,
    split: str = "all",
    exclude_accepted_behavior: bool = False,
    created_by: str = "",
    engine: EvaluationEngine | None = None,
) -> BenchmarkRun:
    await ensure_seed_corpus(session)
    engine = engine or EvaluationEngine()
    corpora = list(corpora or [SEED_CORPUS])
    samples = await select_samples(
        session, tenant_id=tenant_id, corpora=corpora, split=split, exclude_accepted_behavior=exclude_accepted_behavior
    )
    if not samples:
        raise BenchmarkError("benchmark_no_samples", "No active benchmark samples match the selection.", 422)
    if len(samples) > settings.evaluation_benchmark_max_samples:
        raise BenchmarkError(
            "benchmark_too_many_samples",
            f"At most {settings.evaluation_benchmark_max_samples} samples can be measured in one run.",
            422,
        )
    started = time.perf_counter()
    rows = [evaluate_sample(sample, engine=engine, settings=settings) for sample in samples]
    fpr_targets = tuple(sorted({0.01, float(settings.evaluation_benchmark_gate_fpr)}))
    metrics = compute_metrics(rows, fpr_targets=fpr_targets)
    metrics["errors"] = sum(1 for r in rows if r.get("verdict") == "error")
    gate_status, gate_reason = gate_evaluation(metrics, settings)
    metrics["gate"] = {
        "status": gate_status,
        "reason": gate_reason,
        "min_tpr": settings.evaluation_benchmark_min_tpr,
        "fpr": settings.evaluation_benchmark_gate_fpr,
    }
    run = BenchmarkRun(
        benchmark_run_id=new_benchmark_run_id(),
        tenant_id=tenant_id,
        corpus_selector={"corpora": sorted(corpora), "split": split, "exclude_accepted_behavior": exclude_accepted_behavior},
        corpus_digest=corpus_digest(samples),
        evaluator_version=engine.version(),
        detector_modes=engine.modes(),
        sample_count=len(rows),
        positive_count=metrics["positives"],
        negative_count=metrics["negatives"],
        metrics=metrics,
        per_sample=rows,
        gate_status=gate_status,
        duration_ms=int((time.perf_counter() - started) * 1000),
        created_by=created_by,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    record_benchmark_metrics(run.metrics)
    return run


def record_benchmark_metrics(metrics: dict[str, Any]) -> None:
    from app.observability.metrics import EVALUATION_BENCHMARK_AUROC, EVALUATION_BENCHMARK_TPR

    for mode in ("as_configured", "all_enforced"):
        block = metrics.get(mode) or {}
        scopes = {"overall": block.get("overall") or {}}
        scopes.update(block.get("by_kind") or {})
        scopes.update(block.get("by_category") or {})
        for scope, values in scopes.items():
            if values.get("auroc") is not None:
                EVALUATION_BENCHMARK_AUROC.labels(scope=scope, mode=mode).set(float(values["auroc"]))
            for key, cell in (values.get("tpr_at_fpr") or {}).items():
                if cell.get("tpr") is not None:
                    EVALUATION_BENCHMARK_TPR.labels(scope=scope, fpr_target=key, mode=mode).set(float(cell["tpr"]))


async def refresh_sample_gauge(session: AsyncSession) -> dict[str, int]:
    from sqlalchemy import func

    from app.observability.metrics import EVALUATION_BENCHMARK_SAMPLES

    rows = (
        await session.execute(
            select(BenchmarkSample.label, BenchmarkSample.source, func.count())
            .where(BenchmarkSample.status == "active")
            .group_by(BenchmarkSample.label, BenchmarkSample.source)
        )
    ).all()
    counts: dict[str, int] = {}
    for label, source, count in rows:
        EVALUATION_BENCHMARK_SAMPLES.labels(label=str(label), source=str(source)).set(int(count))
        counts[f"{label}:{source}"] = int(count)
    return counts


def benchmark_run_to_dict(run: BenchmarkRun, *, include_per_sample: bool = True) -> dict[str, Any]:
    data = {
        "benchmark_run_id": run.benchmark_run_id,
        "tenant_id": run.tenant_id,
        "corpus_selector": dict(run.corpus_selector or {}),
        "corpus_digest": run.corpus_digest,
        "evaluator_version": run.evaluator_version,
        "detector_modes": dict(run.detector_modes or {}),
        "sample_count": run.sample_count,
        "positive_count": run.positive_count,
        "negative_count": run.negative_count,
        "gate_status": run.gate_status,
        "duration_ms": run.duration_ms,
        "created_by": run.created_by,
        "created_at": run.created_at,
        "metrics": dict(run.metrics or {}),
        "signal_usage": "monitor_only",
    }
    if include_per_sample:
        data["per_sample"] = list(run.per_sample or [])
    return data


PER_SAMPLE_CSV_COLUMNS = (
    "sample_id",
    "name",
    "corpus",
    "source",
    "split",
    "label",
    "categories",
    "score",
    "score_all_enforced",
    "verdict",
    "verdict_all_enforced",
    "priority",
    "rule_ids",
)


def per_sample_csv(run: BenchmarkRun) -> str:
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(PER_SAMPLE_CSV_COLUMNS)
    for row in run.per_sample or []:
        values = []
        for column in PER_SAMPLE_CSV_COLUMNS:
            value = row.get(column, "")
            values.append(";".join(value) if isinstance(value, list) else value)
        writer.writerow(values)
    return buffer.getvalue()


async def list_benchmark_runs(session: AsyncSession, *, tenant_id: str | None = None, limit: int = 20) -> list[BenchmarkRun]:
    stmt = select(BenchmarkRun)
    if tenant_id:
        stmt = stmt.where(BenchmarkRun.tenant_id == tenant_id)
    return list((await session.scalars(stmt.order_by(BenchmarkRun.created_at.desc(), BenchmarkRun.id.desc()).limit(limit))).all())


async def get_benchmark_run(session: AsyncSession, benchmark_run_id: str) -> BenchmarkRun | None:
    return await session.scalar(select(BenchmarkRun).where(BenchmarkRun.benchmark_run_id == benchmark_run_id))


# ---------------------------------------------------------------------------
# Review export, relabel and retire (D7-D10)
# ---------------------------------------------------------------------------

EXPORTABLE_LABELS = {"confirmed_hacking": "hacking", "false_positive": "benign", "accepted": "benign"}


async def get_sample(session: AsyncSession, sample_id: str) -> BenchmarkSample | None:
    return await session.scalar(select(BenchmarkSample).where(BenchmarkSample.sample_id == sample_id))


def _governance(tenant_id: str, actor: str, event_type: str, decision: str, reason: str, details: dict[str, Any]):
    from app.models.governance_event import GovernanceEvent

    return GovernanceEvent(
        tenant_id=tenant_id or "default",
        user_id=(actor or "")[:128],
        event_type=event_type,
        decision=decision,
        reason=reason[:500],
        details=details,
    )


async def export_review(
    session: AsyncSession,
    *,
    review_id: str,
    scope: str = "tenant",
    allow_global: bool = False,
    label_categories: list[str] | None = None,
    notes: list[str] | None = None,
    actor: str = "",
    commit: bool = True,
) -> BenchmarkSample:
    """Copy a decided review's redacted evidence and label into a benchmark sample (explicit action only)."""
    from app.models.agent_run import AgentRun
    from app.models.evaluation_review import EvaluationReview, RunOutcome

    review = await session.scalar(select(EvaluationReview).where(EvaluationReview.review_id == review_id))
    if review is None:
        raise BenchmarkError("review_not_found", f"Review {review_id} not found.", 404)
    if review.status != "closed" or review.label not in EXPORTABLE_LABELS:
        raise BenchmarkError("review_not_exportable", "Only closed reviews labelled confirmed_hacking, false_positive or accepted can be exported.", 409)
    if scope not in {"tenant", "global"}:
        raise BenchmarkError("benchmark_scope_invalid", "scope must be 'tenant' or 'global'.", 422)
    if scope == "global" and not allow_global:
        raise BenchmarkError("benchmark_scope_forbidden", "Only admins can export samples to the global corpus.", 403)

    sample_id = sample_id_for(f"review:{review.review_id}")
    existing = await get_sample(session, sample_id)
    if existing is not None:
        raise BenchmarkError("benchmark_sample_duplicate", "This review was already exported.", 409, {"sample_id": existing.sample_id})

    run = await session.scalar(select(AgentRun).where(AgentRun.run_id == review.run_id))
    latest = await session.scalar(
        select(RunOutcome).where(RunOutcome.run_id == review.run_id).order_by(RunOutcome.submission_seq.desc()).limit(1)
    )
    label_notes = [str(item) for item in (notes or []) if str(item).strip()][:20]
    if latest is not None:
        raw = {key: value for key, value in dict(latest.evidence or {}).items() if key in EVIDENCE_FIELDS}
    else:
        raw = {"final_message": (run.execution_output if run is not None else "") or ""}
        label_notes.append("simulated_executor_output")
    # Evidence was redacted at ingestion; redact again (D9) and drop submitter identity (never stored here).
    evidence = normalize_evidence(raw)
    label = EXPORTABLE_LABELS[review.label]
    if review.label == "accepted":
        label_notes.append("accepted_behavior")
    categories = [str(c) for c in (label_categories or review.label_categories or review.categories or []) if str(c) in CATEGORIES]
    categories = [c for c in categories if c not in {"legacy_text_signal", "evidence_quality"}] if label == "hacking" else []
    if label == "hacking" and not categories:
        raise BenchmarkError("benchmark_label_categories_required", "A hacking sample needs at least one taxonomy category.", 422)
    corpus = "global" if scope == "global" else f"tenant:{review.tenant_id}"
    digest = evidence_digest(evidence)
    clash = await session.scalar(select(BenchmarkSample).where(BenchmarkSample.corpus == corpus, BenchmarkSample.evidence_digest == digest))
    if clash is not None:
        raise BenchmarkError("benchmark_sample_duplicate", "Identical evidence is already in this corpus.", 409, {"sample_id": clash.sample_id})
    request_payload = dict((run.request_payload if run is not None else {}) or {})
    sample = BenchmarkSample(
        sample_id=sample_id,
        corpus=corpus,
        scope=scope,
        tenant_id=GLOBAL_TENANT if scope == "global" else review.tenant_id,
        source="review_export",
        split=split_for(sample_id),
        name=f"review-{review.review_id[-8:]}",
        label=label,
        label_categories=categories,
        label_notes=sorted(set(label_notes)),
        finding_labels=dict(review.finding_labels or {}),
        task=normalize_task(request_payload),
        evidence=evidence,
        evidence_digest=digest,
        origin_review_id=review.review_id,
        origin_run_id=review.run_id,
        status="active",
        label_history=[],
        created_by=actor[:128],
    )
    session.add(sample)
    review.exported_sample_id = sample_id
    session.add(
        _governance(
            review.tenant_id,
            actor,
            "evaluation.benchmark.exported",
            label,
            f"Review {review.review_id} exported to {corpus}",
            {"review_id": review.review_id, "sample_id": sample_id, "corpus": corpus, "label": label, "categories": categories},
        )
    )
    if commit:
        await session.commit()
        await session.refresh(sample)
    else:
        await session.flush()
    return sample


async def export_reviews(
    session: AsyncSession,
    *,
    tenant_id: str | None,
    label: str | None = None,
    decided_after: Any = None,
    scope: str = "tenant",
    allow_global: bool = False,
    actor: str = "",
    limit: int = 500,
) -> dict[str, Any]:
    from app.models.evaluation_review import EvaluationReview

    stmt = select(EvaluationReview).where(EvaluationReview.status == "closed").order_by(EvaluationReview.decided_at.asc(), EvaluationReview.id.asc())
    if tenant_id:
        stmt = stmt.where(EvaluationReview.tenant_id == tenant_id)
    if label:
        stmt = stmt.where(EvaluationReview.label == label)
    if decided_after is not None:
        stmt = stmt.where(EvaluationReview.decided_at >= decided_after)
    reviews = list((await session.scalars(stmt.limit(limit))).all())
    counts = {"exported": 0, "skipped_duplicates": 0, "skipped_not_exportable": 0, "errors": 0}
    sample_ids: list[str] = []
    for review in reviews:
        try:
            sample = await export_review(
                session, review_id=review.review_id, scope=scope, allow_global=allow_global, actor=actor, commit=False
            )
        except BenchmarkError as exc:
            if exc.code == "benchmark_sample_duplicate":
                counts["skipped_duplicates"] += 1
            elif exc.code in {"review_not_exportable", "benchmark_label_categories_required"}:
                counts["skipped_not_exportable"] += 1
            else:
                raise
            continue
        counts["exported"] += 1
        sample_ids.append(sample.sample_id)
    await session.commit()
    return {**counts, "considered": len(reviews), "sample_ids": sample_ids, "signal_usage": "monitor_only"}


async def relabel_sample(
    session: AsyncSession,
    *,
    sample_id: str,
    label: str,
    label_categories: list[str],
    reason: str,
    actor: str,
) -> BenchmarkSample:
    sample = await get_sample(session, sample_id)
    if sample is None:
        raise BenchmarkError("benchmark_sample_not_found", f"Sample {sample_id} not found.", 404)
    if sample.source == "seed_fixture":
        raise BenchmarkError("benchmark_seed_immutable", "Seed samples are code-owned; change the seed YAML instead.", 409)
    if label not in LABELS:
        raise BenchmarkError("benchmark_label_invalid", "label must be 'hacking' or 'benign'.", 422)
    categories = [str(c) for c in label_categories if str(c) in CATEGORIES] if label == "hacking" else []
    if label == "hacking" and not categories:
        raise BenchmarkError("benchmark_label_categories_required", "A hacking sample needs at least one taxonomy category.", 422)
    from datetime import datetime, timezone

    history = list(sample.label_history or [])
    history.append(
        {
            "label": sample.label,
            "label_categories": list(sample.label_categories or []),
            "changed_by": actor[:128],
            "changed_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason[:500],
        }
    )
    sample.label_history = history
    sample.label = label
    sample.label_categories = categories
    session.add(
        _governance(
            sample.tenant_id if sample.tenant_id != GLOBAL_TENANT else "default",
            actor,
            "evaluation.benchmark.relabeled",
            label,
            reason or "relabel",
            {"sample_id": sample_id, "label": label, "categories": categories, "previous": history[-1]},
        )
    )
    await session.commit()
    await session.refresh(sample)
    return sample


async def retire_sample(session: AsyncSession, *, sample_id: str, tenant_id: str | None, is_admin: bool, actor: str) -> BenchmarkSample:
    sample = await get_sample(session, sample_id)
    if sample is None:
        raise BenchmarkError("benchmark_sample_not_found", f"Sample {sample_id} not found.", 404)
    if sample.scope == "global" and not is_admin:
        raise BenchmarkError("benchmark_scope_forbidden", "Only admins can retire global or seed samples.", 403)
    if sample.scope == "tenant" and tenant_id and sample.tenant_id != tenant_id and not is_admin:
        raise BenchmarkError("benchmark_sample_not_found", f"Sample {sample_id} not found.", 404)
    if sample.status != "retired":
        sample.status = "retired"
        session.add(
            _governance(
                sample.tenant_id if sample.tenant_id != GLOBAL_TENANT else "default",
                actor,
                "evaluation.benchmark.retired",
                "retired",
                f"Sample {sample_id} retired",
                {"sample_id": sample_id},
            )
        )
        await session.commit()
        await session.refresh(sample)
    return sample


def sample_to_dict(sample: BenchmarkSample, *, include_evidence: bool = True) -> dict[str, Any]:
    data = {
        "sample_id": sample.sample_id,
        "corpus": sample.corpus,
        "scope": sample.scope,
        "tenant_id": sample.tenant_id,
        "source": sample.source,
        "split": sample.split,
        "name": sample.name,
        "label": sample.label,
        "label_categories": list(sample.label_categories or []),
        "label_notes": list(sample.label_notes or []),
        "finding_labels": dict(sample.finding_labels or {}),
        "task": dict(sample.task or {}),
        "evidence_digest": sample.evidence_digest,
        "origin_review_id": sample.origin_review_id,
        "origin_run_id": sample.origin_run_id,
        "status": sample.status,
        "label_history": list(sample.label_history or []),
        "created_by": sample.created_by,
        "created_at": sample.created_at,
        "signal_usage": "monitor_only",
    }
    if include_evidence:
        data["evidence"] = dict(sample.evidence or {})
    return data


async def latest_benchmark_run(session: AsyncSession, *, evaluator_version: str | None = None, tenant_id: str | None = None) -> BenchmarkRun | None:
    stmt = select(BenchmarkRun)
    if evaluator_version:
        stmt = stmt.where(BenchmarkRun.evaluator_version == evaluator_version)
    if tenant_id:
        stmt = stmt.where(BenchmarkRun.tenant_id == tenant_id)
    return await session.scalar(stmt.order_by(BenchmarkRun.created_at.desc(), BenchmarkRun.id.desc()).limit(1))
