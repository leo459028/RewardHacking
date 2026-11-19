"""v1.6.2 P1: the development guardrails around coding agents (D48-D53)."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


post_edit_lint = _load("post_edit_lint", "scripts/hooks/post_edit_lint.py")
stop_check = _load("stop_check", "scripts/hooks/stop_check.py")
pre_commit = _load("pre_commit", "scripts/git-hooks/pre_commit.py")


def _settings() -> dict:
    return json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))


def _agents() -> str:
    return (ROOT / "AGENTS.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# D48 AGENTS.md
# ---------------------------------------------------------------------------


def test_agents_md_stays_short():
    data = (ROOT / "AGENTS.md").read_bytes()
    assert len(data.decode("utf-8").splitlines()) <= 120
    assert len(data) <= 16 * 1024


@pytest.mark.parametrize(
    "heading",
    ["## Session Start", "## 0. Codebase Map", "## 1. Planning Requirements", "### 2.1 General", "### 2.2 Domain Conventions",
     "### 2.3 Evaluation & Review Invariants", "## 3. Verification", "### 4.1 Version Anchors",
     "### 4.2 Documentation and Language", "## 5. Git Policy", "## 6. Project Notices"],
)
def test_agents_md_keeps_required_sections(heading):
    assert heading in _agents()


def test_agents_md_map_points_to_existing_paths():
    section = _agents().split("## 0. Codebase Map", 1)[1].split("## 1. Planning", 1)[0]
    for path in re.findall(r"`((?:docs|deploy|scripts|\.github|\.claude)/[\w./-]*|Dockerfile|\.dockerignore)`", section):
        assert (ROOT / path).exists(), path


def test_skills_reference_existing_agents_sections():
    agents = _agents()
    headings = {"1": "## 1. ", "3": "## 3. ", "4.1": "### 4.1 ", "4.2": "### 4.2 ", "5": "## 5. "}
    for skill in (ROOT / ".claude" / "skills").glob("*/SKILL.md"):
        for number in re.findall(r"`AGENTS\.md` §(\d(?:\.\d)?)", skill.read_text(encoding="utf-8")):
            assert headings[number] in agents, f"{skill.parent.name}: §{number}"


def test_agents_md_has_the_review_table_and_hook_setup():
    agents = _agents()
    assert "**Review depth by risk**" in agents and "/code-review high" in agents
    for path in ("audit_chain.py", "migrations/", "Dockerfile", ".claude/settings.json", "scripts/git-hooks/"):
        assert path in agents
    assert "git config core.hooksPath scripts/git-hooks" in agents


# ---------------------------------------------------------------------------
# D49 permission rules
# ---------------------------------------------------------------------------

DENIED_COMMANDS = ["git reset --hard *", "git clean *", "git checkout -- *", "git push --force*", "git push -f *",
                   "git stash drop *", "git stash clear *", "git branch -D *"]


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
@pytest.mark.parametrize("command", DENIED_COMMANDS)
def test_destructive_git_commands_are_denied(tool, command):
    assert f"{tool}({command})" in _settings()["permissions"]["deny"]


def test_secret_files_are_denied_and_env_asks():
    permissions = _settings()["permissions"]
    assert {"Read(.env.e2e)", "Read(config/*.pem)"} <= set(permissions["deny"])
    assert {"Read(.env)", "Edit(.env)"} <= set(permissions["ask"])


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_push_and_merge_ask_first(tool):
    assert {f"{tool}(git push *)", f"{tool}(gh pr merge *)"} <= set(_settings()["permissions"]["ask"])


def test_release_creation_stays_authorized():
    permissions = _settings()["permissions"]
    assert not [rule for rule in permissions["deny"] + permissions["ask"] if "gh release" in rule]


# ---------------------------------------------------------------------------
# D50 Claude Code hooks
# ---------------------------------------------------------------------------


def test_hooks_point_to_existing_scripts():
    hooks = _settings()["hooks"]
    assert hooks["PostToolUse"][0]["matcher"] == "Edit|Write"
    for event, script in (("PostToolUse", "post_edit_lint.py"), ("Stop", "stop_check.py")):
        command = hooks[event][0]["hooks"][0]
        assert f"scripts/hooks/{script}" in command["command"] and command["timeout"] <= 60
        assert (ROOT / "scripts" / "hooks" / script).is_file()


def _payload(path) -> str:
    return json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(path)}})


def _lint_root(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    return tmp_path


def test_post_edit_ignores_other_files(tmp_path):
    root = _lint_root(tmp_path)
    note = root / "app" / "notes.md"
    note.write_text("import os\n", encoding="utf-8")
    outside = root / "other.py"
    outside.write_text("import os\n", encoding="utf-8")
    assert post_edit_lint.main(_payload(note), root) == 0
    assert post_edit_lint.main(_payload(outside), root) == 0


def test_post_edit_passes_clean_python(tmp_path):
    root = _lint_root(tmp_path)
    target = root / "app" / "clean.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    assert post_edit_lint.main(_payload(target), root) == 0


def test_post_edit_blocks_lint_errors_with_exit_2(tmp_path, capsys):
    root = _lint_root(tmp_path)
    target = root / "app" / "bad.py"
    target.write_text("import os\n", encoding="utf-8")
    assert post_edit_lint.main(_payload(target), root) == 2
    assert "F401" in capsys.readouterr().err


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"tool_input": null}'])
def test_post_edit_passes_unusable_input(stdin):
    assert post_edit_lint.main(stdin) == 0


def test_post_edit_passes_when_ruff_times_out(tmp_path, monkeypatch, capsys):
    root = _lint_root(tmp_path)
    target = root / "app" / "slow.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("ruff", 30)

    monkeypatch.setattr(post_edit_lint.subprocess, "run", timeout)
    assert post_edit_lint.main(_payload(target), root) == 0
    assert "skipped" in capsys.readouterr().err


def _git_repo(tmp_path: Path) -> Path:
    for args in (["init", "-q"], ["config", "user.name", "t"], ["config", "user.email", "t@example.invalid"],
                 ["config", "core.autocrlf", "false"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "base.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_stop_hook_active_lets_the_turn_end(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "app" / "bad.py").write_text("import os\n", encoding="utf-8")
    assert stop_check.main(json.dumps({"stop_hook_active": True}), repo) == 0


def test_stop_passes_without_python_changes(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "notes.md").write_text("text\n", encoding="utf-8")
    assert stop_check.main("{}", repo) == 0


def test_stop_blocks_lint_errors_in_changed_files(tmp_path, capsys):
    repo = _git_repo(tmp_path)
    (repo / "app" / "bad.py").write_text("import os\n", encoding="utf-8")
    assert stop_check.main("{}", repo) == 2
    assert "F401" in capsys.readouterr().err


def test_stop_blocks_whitespace_errors(tmp_path, capsys):
    repo = _git_repo(tmp_path)
    (repo / "app" / "base.py").write_text("VALUE = 1\nOTHER = 2   \n", encoding="utf-8", newline="\n")
    assert stop_check.main("{}", repo) == 2
    assert "git diff --check" in capsys.readouterr().err


def _fake_contracts(monkeypatch, returncode: int) -> list[list[str]]:
    calls: list[list[str]] = []
    real_run = stop_check._run

    def run(cmd, root, deadline):
        if cmd[0] != "lint-imports":
            return real_run(cmd, root, deadline)
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, "contract broken", "")

    monkeypatch.setattr(stop_check, "_run", run)
    return calls


def test_stop_blocks_broken_import_contracts(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path)
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (repo / "app" / "base.py").write_text("VALUE = 2\n", encoding="utf-8", newline="\n")
    calls = _fake_contracts(monkeypatch, 1)
    assert stop_check.main("{}", repo) == 2
    assert calls == [["lint-imports"]] and "contract broken" in capsys.readouterr().err


def test_stop_skips_import_contracts_outside_app(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    calls = _fake_contracts(monkeypatch, 1)
    assert stop_check.main("{}", repo) == 0
    assert calls == []


def test_stop_passes_when_out_of_time(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(stop_check, "TIME_BUDGET_SECONDS", -1)
    assert stop_check.main("{}", repo) == 0
    assert "skipped" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# D51 git pre-commit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["docs/plan/v1_6_2plan.md", "docs/handoff.md", ".env", ".env.e2e", ".env.local", "deploy/.env", "config/audit_chain_key.pem",
     "any/where/key.pem", "config/judge_profiles.yaml", "docs\\plan\\a.md"],
)
def test_pre_commit_refuses_forbidden_paths(path):
    assert pre_commit.is_forbidden(path)


@pytest.mark.parametrize(
    "path",
    [".env.example", "config/judge_profiles.example.yaml", "docs/planning.md", "docs/handoff_notes.md", "app/env.py", "app/.environment"],
)
def test_pre_commit_allows_other_paths(path):
    assert not pre_commit.is_forbidden(path)


@pytest.mark.parametrize("path", ["config/signing.key", "certs/a.P12", "certs/a.pfx", "keys/store.jks", "keys/a.keystore", "home/id_rsa", "home/id_rsa.pub"])
def test_pre_commit_refuses_key_files(path):
    assert pre_commit.is_forbidden(path)


def test_pre_commit_refuses_an_added_home_directory_path():
    home = "C:" + "\\Users\\someone\\project"
    assert pre_commit.names_local_home(f"+saved to {home}\n")
    assert not pre_commit.names_local_home(f"-saved to {home}\n")
    assert not pre_commit.names_local_home("+path = 'C:" + "\\\\Users\\\\me'\n")


def test_pre_commit_shell_wrapper_calls_the_python_check():
    wrapper = (ROOT / "scripts" / "git-hooks" / "pre-commit").read_text(encoding="utf-8")
    assert wrapper.startswith("#!/bin/sh\n") and "pre_commit.py" in wrapper


# ---------------------------------------------------------------------------
# D52 CI
# ---------------------------------------------------------------------------


def test_ci_checks_version_anchors_and_whitespace_on_full_history():
    steps = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))["jobs"]["test"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0
    runs = "\n".join(step.get("run", "") for step in steps)
    assert "python scripts/bump_version.py --check" in runs
    assert 'git diff --check "origin/$BASE_REF...HEAD"' in runs and 'git diff --check "$BEFORE_SHA" "$GITHUB_SHA"' in runs
    assert "pip install -r requirements-dev.txt -r requirements-otel.txt" in runs
