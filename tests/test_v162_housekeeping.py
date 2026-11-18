"""v1.6.2 P1: documentation that must stay in step with the code and the release process (D11, D12, D13, D46, D47)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from app.services.risk_scoring import RISK_WEIGHTS

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def test_risk_weights_match_the_security_document():
    section = _section(_read("docs/security.md"), "### 2. Multi-Factor Risk Scoring Engine", "### 3. Human Approval Gates")
    documented = {key: float(value) for key, value in re.findall(r"^\| `(\w+)` \| ([\d.]+) \|", section, re.M)}
    assert documented == RISK_WEIGHTS


def test_env_example_describes_the_current_judge_editing_gate():
    text = _read(".env.example")
    assert "only with DASHBOARD_AUTH_ENABLED=true and non-default credentials" not in text
    assert "DASHBOARD_AUTH_MODE=session and the judge.profile.write permission" in text
    assert "RH_AUTO_MIGRATE=true" in text


def test_agents_md_has_the_release_and_phase_rules():
    agents = _read("AGENTS.md")
    assert "gh release create vX.Y.Z --target <merge commit>" in agents
    assert "git merge-base --is-ancestor" in agents and "release-image.yml" in agents
    assert "one phase is one session and one topic" in agents and "1,000–1,800 lines" in agents
    assert "more than 5 means splitting the release" in agents


def test_skills_point_to_the_release_and_phase_rules():
    assert "GitHub Release" in _read(".claude/skills/rh-release-gate/SKILL.md")
    assert "phase sizing rule in `AGENTS.md` §3" in _read(".claude/skills/rh-plan-new-version/SKILL.md")
    assert "AGENTS.md` §3" in _read(".claude/skills/rh-plan-new-version/template.md")


def test_roadmap_moves_capability_gates_to_the_next_release():
    roadmap = _read("docs/roadmap.md")
    current = _section(roadmap, "### `v1.6.2` (Next)", "### `v1.6.3` (tentative number)")
    following = roadmap.split("### `v1.6.3` (tentative number)", 1)[1].split("\n### ", 1)[0]
    assert "Capability-Derived Gates" not in current and "`stdio` MCP" not in current
    assert "Capability-Derived Gates" in following and "`stdio` MCP" in following


def test_security_known_boundary_names_the_new_release_for_capability_gates():
    security = _read("docs/security.md")
    assert "instead of requester-declared constraints for `v1.6.3`" in security


def test_starlette_httpx_deprecation_is_an_error():
    pyproject = _read("pyproject.toml")
    assert "error:Using `httpx` with `starlette.testclient` is deprecated" in pyproject
    assert "httpx2" in _read("requirements-dev.txt")


def test_no_local_working_file_is_tracked():
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
    forbidden = [
        path for path in tracked
        if path.startswith("docs/plan/") or path in {"docs/handoff.md", "config/judge_profiles.yaml"}
        or path.endswith(".pem") or (re.search(r"(^|/)\.env(\.[^/]+)?$", path) and path != ".env.example")
    ]
    assert forbidden == []
