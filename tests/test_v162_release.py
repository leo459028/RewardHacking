"""v1.6.2 P5: release anchors, the browser E2E coverage of the release and the release documents (no browser is started)."""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.main import app
from app.services import checkpoint
from app.services.evaluation.registry import ENGINE_VERSION
from app.services.verification import sampling, suites

ROOT = Path(__file__).resolve().parents[1]
BROWSER = (ROOT / "scripts" / "browser_manual_test.py").read_text(encoding="utf-8")


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _alembic_head() -> str:
    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


def test_v162_release_version_anchors():
    from app.services.finops import FINOPS_CALCULATION_VERSION

    assert app.version == "1.6.2" and FINOPS_CALCULATION_VERSION == "v1.6.2"
    assert ENGINE_VERSION == "v1.6.2"
    assert 'version = "1.6.2"' in _read("pyproject.toml")
    assert "(v1.6.2)" in _read("scripts/security_check.py")
    assert 'value="v1.6.2"' in _read("app/ui/templates/operations.html")
    assert 'release_version", "v1.6.2"' in _read("app/ui/routers/operations.py")
    assert 'assert release_ver == "v1.6.2"' in BROWSER


def test_browser_suite_covers_the_v162_scenarios():
    for number in range(16, 21):
        assert f"[Scenario {number}" in BROWSER, number
        assert f"Scenario {number}:" in _read("docs/manual_test.md"), number
    for route in ("/dashboard/sandbox", "/dashboard/verification"):
        assert f'"{route}",' in BROWSER, route


def test_release_documents_name_this_release_as_current():
    assert "## Current Release: `v1.6.2`" in _read("README.md")
    assert "- **`v1.6.1` (Historical)**" in _read("README.md")
    assert "> **Current Version**: `v1.6.2`" in _read("docs/OVERVIEW.md")
    roadmap = _read("docs/roadmap.md")
    assert "> **Roadmap Baseline**: `v1.6.2`" in roadmap
    assert re.search(r"^\| `v1\.6\.2` \| \*\*Current\*\* \|", roadmap, re.M)
    assert re.search(r"^\| `v1\.6\.3` \| \*\*Next\*\* \|", roadmap, re.M)
    assert "> **Testing Baseline**: `v1.6.2`" in _read("docs/manual_test.md")


def test_documents_state_the_real_alembic_head():
    head = _alembic_head()
    assert f"Current Alembic head: `{head}`" in _read("AGENTS.md")
    assert f"**Current Head Revision**: `{head}`" in _read("docs/migrations.md")
    assert f"Alembic revision `{head}` (head)" in _read("docs/data_model.md")
    assert f"Alembic head `{head}`" in _read("README.md")


def test_agents_map_lists_the_sandbox_modules_and_id_prefixes():
    agents = _read("AGENTS.md")
    for path in ("app/workers/sandbox_runner.py", "app/services/{sandbox,verification}/", "app/{api,ui}/routers/{verification,replay}.py"):
        assert path in agents, path
    for prefix in ("sbj_", "sbc_", "vsu_", "vrf_"):
        assert f"`{prefix}`" in agents, prefix


def _record_rerun_lookups(monkeypatch) -> list[str]:
    calls: list[str] = []

    async def lock(_session, _tenant_id):
        calls.append("lock")

    async def active(_session, _outcome_id, *, lock=False):
        calls.append("active")
        return SimpleNamespace(verification_id="vrf_active", status="running", skip_reason="")

    async def latest(_session, _run_id):
        return SimpleNamespace(outcome_id="outcome_1")

    async def ready(*_args, **_kwargs):
        return SimpleNamespace(ready=True)

    monkeypatch.setattr(sampling, "lock_new_reruns", lock)
    monkeypatch.setattr(sampling, "active_for_outcome", active)
    monkeypatch.setattr(sampling, "latest_outcome", latest)
    monkeypatch.setattr(checkpoint.gate, "evaluate", ready)
    return calls


def _bound_run() -> SimpleNamespace:
    payload = {"metadata": {suites.METADATA_KEY: "vsu_1"}}
    return SimpleNamespace(run_id="run_1", tenant_id="tenant_1", status="completed", request_payload=payload, evaluation_summary={})


def test_manual_rerun_takes_the_lock_before_looking_for_an_active_one(monkeypatch):
    """Found on Postgres: 12 concurrent requests for one outcome created 12 re-runs."""
    calls = _record_rerun_lookups(monkeypatch)
    row, created = asyncio.run(sampling.request_manual(None, None, run=_bound_run(), requested_by="qa"))
    assert calls == ["lock", "active"] and row.verification_id == "vrf_active" and created is False


def test_sandbox_replay_takes_the_lock_before_looking_for_an_active_rerun(monkeypatch):
    calls = _record_rerun_lookups(monkeypatch)
    session = SimpleNamespace(add=lambda _row: None, commit=AsyncMock(), refresh=AsyncMock())
    plan = SimpleNamespace(trace_id="trace", policy_snapshot={}, workflow_id="workflow")
    attempt = asyncio.run(
        checkpoint.persist_sandbox_replay(session, None, run=_bound_run(), plan=plan, steps=[], mode="dry_run", created_by="qa", requested_by="qa")
    )
    assert calls == ["lock", "active"] and attempt.status == "queued"


def test_published_images_do_not_ship_pip():
    for dockerfile in ("Dockerfile", "docker/sandbox-python/Dockerfile"):
        final_stage = _read(dockerfile).rsplit("\nFROM ", 1)[-1]
        assert "pip uninstall -y pip" in final_stage and "rm -rf /usr/local/lib/python3.12/ensurepip" in final_stage, dockerfile


def test_opentelemetry_pins_match_and_include_the_traceparent_fix():
    pinned = set(re.findall(r"opentelemetry-[a-z-]+==([\d.]+)", _read("requirements-otel.txt")))
    assert pinned == set(re.findall(r"opentelemetry-[a-z-]+==([\d.]+)", _read("pyproject.toml"))) == {"1.45.2"}


def test_tracked_files_hold_no_local_user_paths():
    """The repository is public: a developer's home directory must not reach it through reports or logs."""
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.decode("utf-8").split("\0")
    home = re.compile(
        r"(?:\b[A-Za-z]:[\\/]+Users[\\/]+|(?<![\w.-])/c/Users/|(?<![\w.-])/Users/|(?<![\w.-])/home/)"
        r"(?!(?:me|runner|user|username|app)\b)[A-Za-z0-9._-]+"
    )
    leaks = []
    for name in filter(None, tracked):
        path = ROOT / name
        if path.is_file() and b"\0" not in (data := path.read_bytes()) and home.search(data.decode("utf-8", "replace")):
            leaks.append(name)
    assert leaks == []


def test_public_documents_keep_the_meaning_of_platform_verified():
    """User decision of 2026-11-18: the level must never read as proof against tampering."""
    statements = {
        "README.md": "It does not mean the tests passed, and it is not proof against tampering",
        "docs/roadmap.md": "It does not mean the tests passed and is not proof against tampering",
        "docs/evaluation.md": "or that the report is tamper-proof",
        "docs/security.md": "**Platform Re-Runs Are Not Tamper-Proof**",
    }
    for document, statement in statements.items():
        assert statement in _read(document), document
