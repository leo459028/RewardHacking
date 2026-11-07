"""v1.5.10 LLM-as-Judge Triage (Shadow) & Quota-Capped Review Sampling."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.models.benchmark import BenchmarkRun
from app.models.evaluation_review import EvaluationReview
from app.models.judge import JudgeAssessment, ReviewSamplingDecision

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "migrations" / "versions"
FIXTURES = ROOT / "tests" / "fixtures" / "v1510"


# ---------------------------------------------------------------------------
# P1: migration, models and settings
# ---------------------------------------------------------------------------


def _load_migration(filename: str):
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), MIGRATIONS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_v1510_migration_is_single_head_after_v159():
    module = _load_migration("202611090001_add_v1510_judge_triage_sampling.py")
    assert module.revision == "202611090001"
    assert module.down_revision == "202611080001"
    revisions: dict[str, str | None] = {}
    for path in MIGRATIONS_DIR.glob("*.py"):
        mod = _load_migration(path.name)
        revisions[mod.revision] = mod.down_revision
    referenced = {down for down in revisions.values() if down}
    assert sorted(set(revisions) - referenced) == ["202611090001"]


def test_v1510_tables_and_columns_created(session_factory):
    async def scenario():
        async with session_factory() as session:
            def _describe(sync_session):
                inspector = sa_inspect(sync_session.connection())
                return {
                    "tables": set(inspector.get_table_names()),
                    "reviews": {c["name"] for c in inspector.get_columns("evaluation_reviews")},
                    "benchmark_runs": {c["name"] for c in inspector.get_columns("benchmark_runs")},
                }

            return await session.run_sync(_describe)

    info = asyncio.run(scenario())
    assert {"judge_assessments", "review_sampling_decisions"} <= info["tables"]
    assert {"triage_priority", "triage_score", "triage_source", "judge_assessment_id"} <= info["reviews"]
    assert "judge_config" in info["benchmark_runs"]


def test_v1510_model_defaults_keep_v159_rows_valid(session_factory):
    async def scenario():
        async with session_factory() as session:
            session.add(EvaluationReview(review_id="review_p1", run_id="run_p1"))
            session.add(BenchmarkRun(benchmark_run_id="bmr_p1"))
            session.add(JudgeAssessment(assessment_id="jdg_p1", run_id="run_p1"))
            session.add(ReviewSamplingDecision(decision_id="smp_p1", run_id="run_p1", day="2026-11-09"))
            await session.commit()
            review = (await session.execute(select(EvaluationReview))).scalar_one()
            run = (await session.execute(select(BenchmarkRun))).scalar_one()
            assessment = (await session.execute(select(JudgeAssessment))).scalar_one()
            decision = (await session.execute(select(ReviewSamplingDecision))).scalar_one()
            return review, run, assessment, decision

    review, run, assessment, decision = asyncio.run(scenario())
    assert review.triage_priority == "" and review.triage_score is None and review.judge_assessment_id == ""
    assert run.judge_config == {}
    assert assessment.status == "ok" and assessment.mode == "shadow" and assessment.injection_suspected is False
    assert decision.decision == "not_selected" and decision.slice == "none" and decision.skip_reason == ""


def test_sampling_decision_is_unique_per_run(session_factory):
    from sqlalchemy.exc import IntegrityError

    async def scenario():
        async with session_factory() as session:
            session.add(ReviewSamplingDecision(decision_id="smp_a", run_id="run_x", day="2026-11-09"))
            await session.commit()
            session.add(ReviewSamplingDecision(decision_id="smp_b", run_id="run_x", day="2026-11-09"))
            with pytest.raises(IntegrityError):
                await session.commit()

    asyncio.run(scenario())


def test_v1510_settings_defaults_and_validation(monkeypatch):
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.evaluation_judge_mode == "shadow"
    assert settings.effective_judge_provider == "deterministic"
    assert settings.evaluation_judge_allow_external is False
    assert settings.evaluation_sampling_enabled is True
    assert settings.evaluation_sample_min_score == pytest.approx(0.5)
    assert settings.evaluation_sample_daily_cap_per_tenant == 20
    assert settings.evaluation_sample_max_share == pytest.approx(0.05)
    assert settings.evaluation_sample_random_rate == pytest.approx(0.02)
    assert settings.evaluation_sample_random_daily_cap_per_tenant == 5
    # The judge API key is a secret and never rendered in repr.
    secret_settings = Settings(evaluation_judge_api_key="sk-test-value-not-real")
    assert "sk-test-value-not-real" not in repr(secret_settings)
    with pytest.raises(ValidationError):
        Settings(evaluation_judge_mode="enforce")
    with pytest.raises(ValidationError):
        Settings(evaluation_judge_provider="magic")
    with pytest.raises(ValidationError):
        Settings(evaluation_sample_random_rate=1.5)
    with pytest.raises(ValidationError):
        Settings(evaluation_judge_timeout_seconds=0)
    get_settings.cache_clear()


def test_legacy_llm_judge_enabled_alias():
    legacy = Settings(llm_judge_enabled=True)
    assert legacy.effective_judge_provider == "openai_compatible"
    assert legacy.judge_uses_legacy_alias is True
    explicit = Settings(llm_judge_enabled=True, evaluation_judge_provider="deterministic")
    assert explicit.effective_judge_provider == "deterministic"
    assert explicit.judge_uses_legacy_alias is False
