"""v1.6.3 P3: release anchors, the Scenario 21 E2E mode and the release documents (no browser is started)."""

from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.main import app
from app.services.evaluation.registry import ENGINE_VERSION

ROOT = Path(__file__).resolve().parents[1]
BROWSER = (ROOT / "scripts" / "browser_manual_test.py").read_text(encoding="utf-8")


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_v163_release_version_anchors():
    from app.services.finops import FINOPS_CALCULATION_VERSION

    assert app.version == "1.6.3" and FINOPS_CALCULATION_VERSION == "v1.6.3"
    assert 'version = "1.6.3"' in _read("pyproject.toml")
    assert "(v1.6.3)" in _read("scripts/security_check.py")
    assert 'value="v1.6.3"' in _read("app/ui/templates/operations.html")
    assert 'release_version", "v1.6.3"' in _read("app/ui/routers/operations.py")
    assert 'assert release_ver == "v1.6.3"' in BROWSER


def test_patch_leaves_the_evaluation_engine_and_schema_alone():
    """Plan D12: no detector change and no migration in this patch."""
    assert ENGINE_VERSION == "v1.6.2"
    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(config).get_current_head() == "202611180002"


def test_scenario_21_has_a_mode_where_the_environment_key_part_must_run():
    assert "[Scenario 21]" in BROWSER and "[Scenario 21.1]" in BROWSER
    assert '"--only-scenario-21"' in BROWSER and "only_scenario_21=args.only_scenario_21" in BROWSER
    body = BROWSER.split("def scenario_21(require_env_part: bool)", 1)[1].split("\n        def finish()", 1)[0]
    refusal = body.split("elif require_env_part:", 1)[1].split("else:", 1)[0]
    assert "raise AssertionError" in refusal, "the --only-scenario-21 run must fail, not skip, without the environment key"
    only = BROWSER.split("if only_scenario_21:", 1)[1].split("\n\n", 1)[0]
    assert "scenario_21(require_env_part=True)" in only and "return finish()" in only
    assert "scenario_21(require_env_part=False)" in BROWSER  # the full run keeps Scenario 21
    manual = _read("docs/manual_test.md")
    assert "Scenario 21:" in manual and "--only-scenario-21" in manual and "E2E_ENV_API_KEY" in manual


def test_release_documents_name_this_release_as_current():
    readme = _read("README.md")
    assert "## Current Release: `v1.6.3`" in readme and "- **`v1.6.2` (Historical)**" in readme
    assert "Zookeeper" not in readme  # the compose stack runs Kafka in KRaft mode
    assert "> **Current Version**: `v1.6.3`" in _read("docs/OVERVIEW.md")
    roadmap = _read("docs/roadmap.md")
    assert "> **Roadmap Baseline**: `v1.6.3`" in roadmap
    assert re.search(r"^\| `v1\.6\.3` \| \*\*Current\*\* \|", roadmap, re.M)
    assert re.search(r"^\| `v1\.6\.4` \| \*\*Next\*\* \|", roadmap, re.M)
    assert re.search(r"^\| `v1\.6\.2` \| Completed \|", roadmap, re.M)
    for document in ("docs/manual_test.md", "docs/testing_guide.md"):
        assert "> **Testing Baseline**: `v1.6.3`" in _read(document), document


def test_known_boundaries_describe_the_fix_and_what_remains():
    security = _read("docs/security.md")
    assert "7a. **Insecure Authentication Defaults (fixed in `v1.6.3`)**" in security
    assert "not on code that refuses to start" not in security  # v1.6.1 rule overturned by plan D17
    assert "the API refuses to start with it when `APP_ENV` is anything but `dev` or `test`" in security
    assert "14. **API `admin` Keys Are Platform-Wide**" in security
    for residual in ("sets `APP_ENV=dev` explicitly", "cannot be told afterwards", "`key:legacy`", "is `unknown`"):
        assert residual in security, residual
    assert "falls back to the development key" not in _read("docs/deployment.md").split("## Production Readiness", 1)[1]


def test_agents_map_lists_the_authentication_modules():
    agents = _read("AGENTS.md")
    section = agents.split("## 0. Codebase Map", 1)[1].split("## 1. Planning", 1)[0]
    services = re.search(r"`app/services/\{([a-z_,]*startup_guard[a-z_,]*)\}\.py`", section)
    assert services, "the map must list startup_guard"
    for name in ("auth", "startup_guard", "api_key_provenance"):
        assert name in services.group(1).split(","), name
        assert (ROOT / "app" / "services" / f"{name}.py").is_file(), name
    jobs = re.search(r"`app/jobs/\{([a-z_,]+)\}\.py`", section)
    assert jobs and "api_keys" in jobs.group(1).split(",")
    for name in jobs.group(1).split(","):
        assert (ROOT / "app" / "jobs" / f"{name}.py").is_file(), name
