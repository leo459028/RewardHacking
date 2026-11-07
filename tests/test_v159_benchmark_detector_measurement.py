"""v1.5.9 Labeled Benchmark & Detector Measurement."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.models.benchmark import BenchmarkRun, BenchmarkSample, HoneypotProbe, TenantAttestationKey
from app.models.evaluation_result import EvaluationResultRecord
from app.models.evaluation_review import EvaluationReview, RunOutcome

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"


# ---------------------------------------------------------------------------
# P1: migration, models and settings
# ---------------------------------------------------------------------------


def _load_migration(filename: str):
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), MIGRATIONS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_v159_migration_is_single_head_after_v158():
    module = _load_migration("202611080001_add_v159_benchmark_honeypot_attestation.py")
    assert module.revision == "202611080001"
    assert module.down_revision == "202611070001"

    revisions: dict[str, str | None] = {}
    for path in MIGRATIONS_DIR.glob("*.py"):
        mod = _load_migration(path.name)
        revisions[mod.revision] = mod.down_revision
    referenced = {down for down in revisions.values() if down}
    heads = sorted(set(revisions) - referenced)
    assert heads == ["202611080001"]


def test_v159_tables_and_columns_created(session_factory):
    async def scenario():
        async with session_factory() as session:
            def _describe(sync_session):
                inspector = sa_inspect(sync_session.connection())
                return {
                    "tables": set(inspector.get_table_names()),
                    "run_outcomes": {c["name"] for c in inspector.get_columns("run_outcomes")},
                    "evaluation_reviews": {c["name"] for c in inspector.get_columns("evaluation_reviews")},
                    "evaluation_results": {c["name"] for c in inspector.get_columns("evaluation_results")},
                    "benchmark_uniques": [u["column_names"] for u in inspector.get_unique_constraints("benchmark_samples")],
                }

            return await session.run_sync(_describe)

    info = asyncio.run(scenario())
    assert {"benchmark_samples", "benchmark_runs", "honeypot_probes", "tenant_attestation_keys"} <= info["tables"]
    assert {
        "agent_id",
        "agent_version",
        "model_name",
        "honeypot_probe_id",
        "honeypot_outcome",
        "attestation_status",
        "attestation_digest",
        "attestation_detail",
    } <= info["run_outcomes"]
    assert {"agent_id", "agent_version", "model_name", "finding_labels", "exported_sample_id"} <= info["evaluation_reviews"]
    assert "suspicion_score" in info["evaluation_results"]
    assert ["corpus", "evidence_digest"] in info["benchmark_uniques"]


def test_v159_model_defaults_keep_v158_rows_valid(session_factory):
    async def scenario():
        async with session_factory() as session:
            session.add(RunOutcome(outcome_id="outcome_p1", run_id="run_p1", evidence={}, findings=[]))
            session.add(EvaluationReview(review_id="review_p1", run_id="run_p1"))
            session.add(
                EvaluationResultRecord(run_id="run_p1", task_type="code_fix", verdict="pass", final_score=1.0, rubric={}, judge_signal={})
            )
            session.add(
                BenchmarkSample(sample_id="bms_p1", corpus="seed-v1.5.9", label="benign", evidence_digest="d" * 64)
            )
            session.add(BenchmarkRun(benchmark_run_id="bmr_p1"))
            session.add(HoneypotProbe(probe_id="hp_p1", name="probe", prompt="p", goal="g"))
            session.add(
                TenantAttestationKey(
                    key_id="attk_p1", tenant_id="t1", algorithm="ed25519", public_key_pem="pem", key_fingerprint="f" * 64
                )
            )
            await session.commit()
            outcome = (await session.execute(select(RunOutcome))).scalar_one()
            review = (await session.execute(select(EvaluationReview))).scalar_one()
            result = (await session.execute(select(EvaluationResultRecord))).scalar_one()
            sample = (await session.execute(select(BenchmarkSample))).scalar_one()
            key = (await session.execute(select(TenantAttestationKey))).scalar_one()
            return outcome, review, result, sample, key

    outcome, review, result, sample, key = asyncio.run(scenario())
    assert outcome.agent_id == "" and outcome.model_name == "" and outcome.honeypot_outcome == ""
    assert outcome.attestation_status == "none" and outcome.attestation_detail == {}
    assert review.finding_labels == {} and review.exported_sample_id == ""
    assert result.suspicion_score == 0.0
    assert sample.scope == "tenant" and sample.status == "active" and sample.split == "dev"
    assert sample.label_history == [] and sample.finding_labels == {}
    assert key.status == "active" and key.revoked_at is None


def test_benchmark_sample_corpus_digest_is_unique(session_factory):
    from sqlalchemy.exc import IntegrityError

    async def scenario():
        async with session_factory() as session:
            session.add(BenchmarkSample(sample_id="bms_a", corpus="c1", label="benign", evidence_digest="a" * 64))
            session.add(BenchmarkSample(sample_id="bms_b", corpus="c2", label="benign", evidence_digest="a" * 64))
            await session.commit()
            session.add(BenchmarkSample(sample_id="bms_c", corpus="c1", label="hacking", evidence_digest="a" * 64))
            with pytest.raises(IntegrityError):
                await session.commit()

    asyncio.run(scenario())


def test_v159_settings_defaults_and_validation(monkeypatch):
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.evaluation_benchmark_min_tpr == pytest.approx(0.90)
    assert settings.evaluation_benchmark_gate_fpr == pytest.approx(0.05)
    assert settings.evaluation_benchmark_max_samples == 5000
    assert settings.evaluation_cheat_rate_min_samples == 20
    assert settings.evaluation_attestation_max_age_hours == 24
    assert settings.evaluation_attestation_max_bytes == 262144
    assert settings.evaluation_sigstore_enabled is False
    assert settings.evaluation_inspect_import_max_bytes == 5242880

    with pytest.raises(ValueError):
        Settings(evaluation_benchmark_min_tpr=0)
    with pytest.raises(ValueError):
        Settings(evaluation_benchmark_gate_fpr=1.0)
    with pytest.raises(ValueError):
        Settings(evaluation_cheat_rate_min_samples=0)
    get_settings.cache_clear()
