"""v1.6.0 P1.5: agent tooling scripts (D30-D32) and project skills (D29, D33)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bump_version  # noqa: E402
import phase_check  # noqa: E402
import release_gate  # noqa: E402


# ---------------------------------------------------------------------------
# D30 bump_version.py
# ---------------------------------------------------------------------------


def _copy_anchor_files(tmp_path: Path) -> Path:
    for anchor in bump_version.ANCHORS:
        target = tmp_path / anchor.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / anchor.path, target)
    return tmp_path


def test_repository_anchors_agree():
    assert bump_version.check(ROOT) == 0


def test_bump_rewrites_every_anchor(tmp_path):
    root = _copy_anchor_files(tmp_path)
    assert bump_version.bump(root, "9.8.7") == 0
    found, errors = bump_version.scan(root)
    assert errors == []
    assert {value for _, value in found} == {"9.8.7"}
    assert len(found) == len(bump_version.ANCHORS)
    assert {anchor.number for anchor, _ in found} == {1, 2, 3, 4, 5, 6}


def test_bump_dry_run_changes_nothing(tmp_path):
    root = _copy_anchor_files(tmp_path)
    before = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert bump_version.bump(root, "9.8.7", dry_run=True) == 0
    assert (root / "pyproject.toml").read_text(encoding="utf-8") == before


def test_bump_refuses_missing_or_duplicated_anchor(tmp_path):
    root = _copy_anchor_files(tmp_path)
    core = root / "app/services/finops/core.py"
    text = core.read_text(encoding="utf-8")
    core.write_text(text + "\n" + text.split("\n")[[i for i, line in enumerate(text.split("\n")) if line.startswith("FINOPS_CALCULATION_VERSION")][0]] + "\n", encoding="utf-8")
    assert bump_version.bump(root, "9.8.7") == 1  # duplicated
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    _, errors = bump_version.scan(root)
    assert any("pyproject.toml" in error for error in errors)


def test_bump_detects_disagreeing_anchors(tmp_path):
    root = _copy_anchor_files(tmp_path)
    path = root / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    current = next(value for anchor, value in bump_version.scan(root)[0] if anchor.path == "pyproject.toml")
    path.write_text(text.replace(f'version = "{current}"', 'version = "0.0.1"', 1), encoding="utf-8")
    found, errors = bump_version.scan(root)
    assert errors == [] and len({value for _, value in found}) == 2
    assert bump_version.check(root) == 1


def test_bump_rejects_bad_version(tmp_path):
    assert bump_version.bump(_copy_anchor_files(tmp_path), "v1.6.0") == 2


# ---------------------------------------------------------------------------
# D31 phase_check.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message,ok",
    [
        ("wip(v1.6.0): P3 retention service", True),
        ("wip(v1.6.0): P1.5a agent tooling scripts", True),
        ("wip(v1.6.0): P2b OTel export", True),
        ("wip(v1.6.0): P2b-1 OTel export core", True),
        ("wip(v1.6.0): P2b- missing number", False),
        ("feat(v1.6.0): runtime foundations", True),
        ("fix: something", False),
        ("wip(1.6.0): P3 missing v", False),
        ("wip(v1.6.0): retention without phase", False),
        ("", False),
    ],
)
def test_commit_message_format(message, ok):
    assert (phase_check.validate_message(message) is None) is ok


@pytest.mark.parametrize(
    "path",
    ["docs/plan/v1_6_0plan.md", "./docs/plan/x.md", "docs\\handoff.md", ".env", "config/.env.local"],
)
def test_forbidden_paths(path):
    assert phase_check.forbidden_paths([path]) == [path]


def test_allowed_paths():
    assert phase_check.forbidden_paths(["app/main.py", ".env.example", "docs/roadmap.md", "envoy.yaml"]) == []


def test_check_steps_include_contracts_and_deprecation_errors():
    steps = phase_check.check_steps(["tests/test_x.py"], full=False)
    joined = [" ".join(step) for step in steps]
    assert any(item.startswith("ruff check app tests scripts") for item in joined)
    assert "lint-imports" in joined and "git diff --check" in joined
    assert joined[-1].endswith("-W error::DeprecationWarning tests/test_x.py")
    assert not phase_check.check_steps([], full=True)[-1][-1].startswith("tests/")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def temp_repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "tester@example.invalid")
    _git(tmp_path, "config", "user.name", "tester")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "docs" / "plan").mkdir(parents=True)
    (tmp_path / "docs" / "plan" / "p.md").write_text("plan\n", encoding="utf-8")
    return tmp_path


def test_guarded_commit_commits_only_listed_paths(temp_repo):
    (temp_repo / "b.py").write_text("y = 2\n", encoding="utf-8")
    code = phase_check.guarded_commit("wip(v9.9.9): P1 test", ["a.py"], "Co-Authored-By: Tester <t@example.invalid>", cwd=temp_repo)
    assert code == 0
    log = _git(temp_repo, "log", "-1", "--format=%B").stdout
    assert log.startswith("wip(v9.9.9): P1 test") and "Co-Authored-By: Tester" in log
    assert _git(temp_repo, "show", "--name-only", "--format=").stdout.split() == ["a.py"]


def test_guarded_commit_refuses_plan_file(temp_repo):
    assert phase_check.guarded_commit("wip(v9.9.9): P1 test", ["docs/plan/p.md"], cwd=temp_repo) == 2
    assert _git(temp_repo, "rev-parse", "--verify", "HEAD").returncode != 0  # no commit


def test_guarded_commit_refuses_pre_staged_forbidden_file(temp_repo):
    _git(temp_repo, "add", "docs/plan/p.md")
    assert phase_check.guarded_commit("wip(v9.9.9): P1 test", ["a.py"], cwd=temp_repo) == 2
    assert _git(temp_repo, "rev-parse", "--verify", "HEAD").returncode != 0


def test_guarded_commit_stops_when_staging_fails(temp_repo):
    assert phase_check.guarded_commit("wip(v9.9.9): P1 test", ["a.py", "does_not_exist.py"], cwd=temp_repo) == 1
    assert _git(temp_repo, "rev-parse", "--verify", "HEAD").returncode != 0


def test_guarded_commit_rejects_bad_message(temp_repo):
    assert phase_check.guarded_commit("update stuff", ["a.py"], cwd=temp_repo) == 2


# ---------------------------------------------------------------------------
# D32 release_gate.py
# ---------------------------------------------------------------------------


def test_release_gate_lists_every_stage2_step(capsys):
    assert release_gate.main(["--list"]) == 0
    out = capsys.readouterr().out
    for key in ("version_anchors", "secret_scan", "ruff", "import_contracts", "diff_check", "pytest_full",
                "docker_build", "docker_ps", "alembic_upgrade", "alembic_current", "browser_e2e"):
        assert key in out


def test_release_gate_skip_docker_also_skips_e2e():
    groups = {step.group for step in release_gate.select_steps(skip_docker=True, skip_e2e=False)}
    assert groups == {"core"}
    groups = {step.group for step in release_gate.select_steps(skip_docker=False, skip_e2e=True)}
    assert groups == {"core", "docker"}


def test_release_gate_extracts_facts():
    step = {s.key: s for s in release_gate.STEPS}
    assert release_gate.extract_facts(step["pytest_full"], "....\n470 passed in 247.77s (0:04:07)\n") == {
        "summary": "470 passed in 247.77s (0:04:07)", "passed": "470", "failed": "0"}
    facts = release_gate.extract_facts(step["pytest_full"], "== 2 failed, 468 passed in 250.00s ==")
    assert facts["failed"] == "2" and facts["passed"] == "468"
    assert release_gate.extract_facts(step["alembic_current"], "202611130001 (head)\n") == {"head": "202611130001"}
    assert release_gate.extract_facts(step["import_contracts"], "Contracts: 3 kept, 0 broken.") == {"kept": "3", "broken": "0"}


def test_release_gate_summary_marks_failures():
    step = release_gate.STEPS[2]
    text = release_gate.render([release_gate.Result(step, 1, 0.5, ["E501 boom"], {})], [release_gate.STEPS[-1]])
    assert "FAILED" in text and "E501 boom" in text and "browser_e2e | skipped" in text


# ---------------------------------------------------------------------------
# D29 project skills and D33 AGENTS.md slimming
# ---------------------------------------------------------------------------

SKILLS = ("rh-plan-new-version", "rh-phase-commit", "rh-version-bump", "rh-release-gate", "rh-testing-conventions")


def _frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    block = text.split("---\n", 2)[1]
    fields = {}
    for line in block.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


@pytest.mark.parametrize("name", SKILLS)
def test_skill_frontmatter(name):
    path = ROOT / ".claude" / "skills" / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    fields = _frontmatter(text)
    assert fields.get("name") == name
    assert len(fields.get("description", "")) >= 80, "description must say when to use the skill"
    assert len(text.splitlines()) <= 60
    for script in ("phase_check.py", "bump_version.py", "release_gate.py"):
        if script in text:
            assert (ROOT / "scripts" / script).exists()


def test_plan_template_has_every_mandatory_section():
    template = (ROOT / ".claude" / "skills" / "rh-plan-new-version" / "template.md").read_text(encoding="utf-8")
    for heading in ("## 2. 功能目標與驗收標準", "## 3. UI / Dashboard 驗證指引", "## 4. 實作設計與受影響模組",
                    "## 5. 驗證測試套件與指令", "## 6. 實作後需同步更新的文件", "## 7. 需記錄至 `docs/test_report.md` 的測試數據",
                    "## 9. 實作進度紀錄（Implementation Progress Log）", "### 1.4 設計決議"):
        assert heading in template


def test_agents_md_keeps_every_rule():
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for keyword in ("**Monotonic Safety**", "**Approval ≠ Review**", "**Evidence over Claims**", "**Tampering Outranks Gaming**",
                    "**Deterministic, Versioned Detectors**", "**Every Detector Rule Has Negative Fixtures**",
                    "**Monitor-Only Signals**", "**No Unauthorized Git Mutations**", "**Verify Before Committing**",
                    "**Never Upload `docs/plan/*.md` or `docs/handoff.md`**", "**Router Split Rule", "-W error::DeprecationWarning",
                    "**Entity ID Prefixes**", "docs/handoff.md"):
        assert keyword in agents, keyword
    for script in ("scripts/phase_check.py", "scripts/release_gate.py", "scripts/bump_version.py"):
        assert script in agents
    assert "app/services/finops/core.py" in agents


def test_test_conventions_moved_not_lost():
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "testing_guide.md").read_text(encoding="utf-8")
    assert "## Agent Test Conventions" in guide and "§ Agent Test Conventions" in agents
    for marker in ("**Triggering `review_required` in Tests**", "**Triggering `awaiting_approval` in Tests**",
                   "**Triggering honeypot / attestation paths in tests**", "**Triggering judge / sampling paths in tests**",
                   "**Judge activation in tests (v1.5.12)**"):
        assert marker in guide and marker not in agents
