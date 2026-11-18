"""v1.6.2 P3a: suite snapshots, patching, before / after classification, coverage and the re-run engine (no Docker)."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.models.evaluation_review import RunOutcome, TenantEvaluationPolicy
from app.models.governance_event import GovernanceEvent
from app.models.sandbox import SandboxArtifact, SandboxJob, VerificationRun, VerificationSuite
from app.schemas.agent import AgentRequest
from app.services.sandbox import jobs
from app.services.sandbox.base import ExecutionResult
from app.services.verification import artifacts, classify, demo, engine, line_coverage, patching, suites
from app.services.verification.errors import VerificationError

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/acme/python@sha256:" + "a" * 64
MAX_BYTES = 1_048_576
TEST_PATHS = list(suites.DEFAULT_TEST_PATHS)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, sandbox_backend="docker", sandbox_images={"python": IMAGE}, **overrides)


def _gz(entries, *, fileobj_mode="w") -> bytes:
    """``entries``: (name, kind, content); kinds: file, dir, symlink, hardlink, fifo."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode=fileobj_mode) as archive:
        for name, kind, content in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
                continue
            info.type = {"dir": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE, "fifo": tarfile.FIFOTYPE}[kind]
            info.linkname = "/etc/passwd" if kind in ("symlink", "hardlink") else ""
            archive.addfile(info)
    return gzip.compress(raw.getvalue())


def _normalize(entries) -> artifacts.NormalizedArtifact:
    return artifacts.normalize_upload(_gz(entries), max_bytes=MAX_BYTES)


def _code(entries) -> str:
    with pytest.raises(VerificationError) as caught:
        _normalize(entries)
    return caught.value.code


# ---------------------------------------------------------------------------
# D23: snapshot safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entries", "code"),
    [
        ([("link", "symlink", b"")], "artifact_symlink_rejected"),
        ([("link", "hardlink", b"")], "artifact_hardlink_rejected"),
        ([("pipe", "fifo", b"")], "artifact_special_file_rejected"),
        ([("/etc/passwd", "file", b"x")], "artifact_absolute_path_rejected"),
        ([("C:/temp/a.py", "file", b"x")], "artifact_absolute_path_rejected"),
        ([("../escape.py", "file", b"x")], "artifact_path_traversal_rejected"),
        ([("a/../../escape.py", "file", b"x")], "artifact_path_traversal_rejected"),
        ([("a\\b.py", "file", b"x")], "artifact_path_traversal_rejected"),
        ([("a" * 300, "file", b"x")], "artifact_path_too_long"),
        ([("out/planted.xml", "file", b"x")], "artifact_reserved_path"),
        ([("out", "dir", b"")], "artifact_reserved_path"),
        ([("a.py", "file", b"1"), ("a.py", "file", b"2")], "artifact_duplicate_path"),
        ([("a", "file", b"1"), ("a/b.py", "file", b"2")], "artifact_path_conflict"),
        ([("pkg", "dir", b"")], "artifact_empty"),
    ],
)
def test_unsafe_snapshots_are_refused_with_their_own_code(entries, code):
    assert _code(entries) == code


def test_a_nested_out_directory_is_allowed():
    assert _normalize([("pkg/out/data.txt", "file", b"x")]).paths == ("pkg/out/data.txt",)


@pytest.mark.parametrize("data", [b"", b"not a gzip", gzip.compress(b"not a tar" * 200)])
def test_unreadable_archives_are_refused(data):
    with pytest.raises(VerificationError) as caught:
        artifacts.normalize_upload(data, max_bytes=MAX_BYTES)
    assert caught.value.code in {"artifact_invalid_archive", "artifact_empty"}


def test_a_truncated_archive_is_refused():
    data = _gz([("a.py", "file", b"x" * 4096)])
    with pytest.raises(VerificationError) as caught:
        artifacts.normalize_upload(data[: len(data) // 2], max_bytes=MAX_BYTES)
    assert caught.value.code == "artifact_invalid_archive"


def test_upload_and_unpacked_sizes_are_limited():
    with pytest.raises(VerificationError) as compressed:
        artifacts.normalize_upload(b"x" * 11, max_bytes=10)
    bomb = _gz([("bomb.bin", "file", b"\0" * (MAX_BYTES * 3))])
    assert len(bomb) < MAX_BYTES
    with pytest.raises(VerificationError) as unpacked:
        artifacts.normalize_upload(bomb, max_bytes=MAX_BYTES)
    assert (compressed.value.code, compressed.value.status_code) == ("artifact_too_large", 413)
    assert unpacked.value.code == "artifact_too_large"


def test_the_file_count_is_limited(monkeypatch):
    monkeypatch.setattr(artifacts, "MAX_FILES", 2)
    assert _code([(f"f{index}.py", "file", b"x") for index in range(3)]) == "artifact_too_many_files"


def test_the_digest_ignores_order_time_and_owner():
    first = _normalize([("b.py", "file", b"2"), ("a/c.py", "file", b"1")])
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, content in (("a/c.py", b"1"), ("b.py", b"2")):
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.uid, info.uname, info.mode = len(content), 1_700_000_000, 1000, "someone", 0o777
            archive.addfile(info, io.BytesIO(content))
    second = artifacts.normalize_upload(gzip.compress(raw.getvalue()), max_bytes=MAX_BYTES)
    assert first.digest == second.digest == hashlib.sha256(first.content).hexdigest()


def test_the_normalized_tar_has_fixed_metadata_and_only_plain_entries():
    normalized = _normalize([("pkg/a.py", "file", b"1")])
    with tarfile.open(fileobj=io.BytesIO(normalized.content)) as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == ["pkg", "pkg/a.py"]
    assert all((member.mtime, member.uid, member.gid, member.uname) == (0, 0, 0, "") for member in members)
    assert artifacts.read_tar(normalized.content) == {"pkg/a.py": b"1"}


def test_identical_content_is_stored_once(session_factory):
    normalized = _normalize([("a.py", "file", b"1")])

    async def scenario():
        async with session_factory() as session:
            await artifacts.store_artifact(session, normalized)
            await artifacts.store_artifact(session, normalized)
            await session.commit()
            rows = (await session.scalars(select(SandboxArtifact))).all()
            return len(rows), await artifacts.load_artifact(session, normalized.digest)

    assert asyncio.run(scenario()) == (1, normalized.content)


# ---------------------------------------------------------------------------
# D25: applying the agent's change
# ---------------------------------------------------------------------------

BASE = {"app.py": b"def add(a, b):\n    return a - b\n", "tests/test_app.py": b"def test_add():\n    assert True\n"}
DIFF_FIX = (
    "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
)


def _apply(evidence, base=None, max_bytes=MAX_BYTES):
    return patching.apply_evidence(base or BASE, evidence, test_paths=TEST_PATHS, max_bytes=max_bytes)


def _skip(evidence, **kwargs) -> patching.PatchSkipped:
    with pytest.raises(patching.PatchSkipped) as caught:
        _apply(evidence, **kwargs)
    return caught.value


def test_full_text_is_preferred_and_changed_lines_are_recorded():
    result = _apply({"changed_files": [{"path": "app.py", "after": "def add(a, b):\n    return a + b\n"}], "diff": "--- a/app.py\n+++ b/app.py\n@@ -9 +9 @@\n-x\n+y\n"})
    assert result.files["app.py"] == b"def add(a, b):\n    return a + b\n"
    assert result.applied == ("app.py",) and result.changed_lines == {"app.py": (2,)}


def test_a_strict_diff_applies():
    result = _apply({"diff": DIFF_FIX})
    assert result.files["app.py"] == b"def add(a, b):\n    return a + b\n" and result.changed_lines == {"app.py": (2,)}


def test_a_new_file_and_an_insertion_apply():
    diff = "--- /dev/null\n+++ b/lib.py\n@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n--- a/app.py\n+++ b/app.py\n@@ -1,0 +2,1 @@\n+    # note\n"
    result = _apply({"diff": diff})
    assert result.files["lib.py"] == b"x = 1\ny = 2"
    assert result.files["app.py"].split(b"\n")[1] == b"    # note"


def test_a_deleted_and_a_renamed_file_apply():
    base = {"old.py": b"a = 1\n", "gone.py": b"b = 2\n", "tests/test_x.py": b"pass\n"}
    diff = (
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-b = 2\n"
        "diff --git a/old.py b/new.py\nrename from old.py\nrename to new.py\n--- a/old.py\n+++ b/new.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
    )
    result = _apply({"diff": diff}, base=base)
    assert "gone.py" not in result.files and "old.py" not in result.files and result.files["new.py"] == b"a = 2\n"


@pytest.mark.parametrize(
    ("diff", "detail"),
    [
        ("--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def sub(a, b):\n-    return a - b\n+    return a + b\n", "context_mismatch"),
        ("--- a/app.py\n+++ b/app.py\n@@ -1,3 +1,3 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n", "hunk_truncated"),
        ("--- a/app.py\n+++ b/app.py\n@@ -90,1 +90,1 @@\n-x\n+y\n", "hunk_out_of_range"),
        ("--- a/missing.py\n+++ b/missing.py\n@@ -1 +1 @@\n-x\n+y\n", "file_missing_in_base"),
        ("--- /dev/null\n+++ b/app.py\n@@ -0,0 +1 @@\n+x\n", "file_already_exists"),
    ],
)
def test_a_diff_that_does_not_match_is_skipped_never_fuzzed(diff, detail):
    skipped = _skip({"diff": diff})
    assert (skipped.reason, skipped.detail) == (patching.PATCH_FAILED, detail)


def test_a_changed_base_is_refused():
    skipped = _skip({"changed_files": [{"path": "app.py", "before": "other", "after": "x = 1\n"}]})
    assert (skipped.reason, skipped.detail) == (patching.PATCH_FAILED, "base_mismatch")


@pytest.mark.parametrize("path", ["../evil.py", "/etc/passwd", "a\\b.py", "a/./b.py", "C:/x.py"])
def test_unsafe_paths_are_refused(path):
    assert _skip({"changed_files": [{"path": path, "after": "x = 1\n"}]}).reason == patching.PATCH_FAILED


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("tests/test_app.py", "test_path"),
        ("pkg/test_core.py", "test_path"),
        ("pkg/core_test.py", "test_path"),
        ("conftest.py", "test_config"),
        ("pkg/conftest.py", "test_config"),
        ("pytest.ini", "test_config"),
        ("tox.ini", "test_config"),
        ("setup.cfg", "test_config"),
        ("pyproject.toml", "test_config"),
        (".coveragerc", "test_config"),
        ("pytest.py", "test_tooling"),
        ("_pytest/runner.py", "test_tooling"),
        ("coverage/__init__.py", "test_tooling"),
        ("sitecustomize.py", "test_tooling"),
        ("evil.pth", "test_tooling"),
    ],
)
def test_tests_their_settings_and_the_tools_keep_the_platform_copy(path, reason):
    evidence = {"changed_files": [{"path": path, "after": "x = 1\n"}, {"path": "app.py", "after": "y = 2\n"}]}
    result = _apply(evidence)
    assert {"path": path, "reason": reason} in result.discarded
    assert result.files.get(path) == BASE.get(path) and result.applied == ("app.py",)


@pytest.mark.parametrize("path", ["app.py", "pkg/testing_utils.py", "pkg/contest.py", "docs/pytest_notes.md", "mycoverage.py"])
def test_ordinary_source_files_are_not_discarded(path):
    assert _apply({"changed_files": [{"path": path, "after": "x = 1\n"}]}).discarded == ()


def test_renaming_a_test_into_source_is_discarded():
    diff = "diff --git a/tests/test_app.py b/helper.py\nrename from tests/test_app.py\nrename to helper.py\n"
    assert _skip({"diff": diff}).reason == patching.ONLY_TEST_CHANGES


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        ({"evidence_limits": ["evidence_truncated"], "diff": DIFF_FIX}, patching.EVIDENCE_TRUNCATED),
        ({"changed_files": [{"path": "tests/test_app.py", "after": "x = 1\n"}]}, patching.ONLY_TEST_CHANGES),
        ({"changed_files": [{"path": "app.py", "after": BASE["app.py"].decode()}]}, patching.NO_APPLICABLE_CHANGES),
        ({"changed_files": [{"path": "app.py"}]}, patching.NO_APPLICABLE_CHANGES),
        ({}, patching.NO_APPLICABLE_CHANGES),
        ({"changed_files": [{"path": "app.py", "after": "key = '[REDACTED]'\n"}]}, patching.EVIDENCE_REDACTED),
    ],
)
def test_changes_that_cannot_be_rerun_are_skipped_with_a_reason(evidence, reason):
    assert _skip(evidence).reason == reason


def test_the_patched_tree_is_size_limited():
    assert _skip({"changed_files": [{"path": "big.py", "after": "x" * 200}]}, max_bytes=100).reason == patching.PATCH_TOO_LARGE


def test_a_file_cannot_replace_a_directory():
    assert _skip({"changed_files": [{"path": "app.py/inner.py", "after": "x = 1\n"}]}).detail == "path_conflict"


# ---------------------------------------------------------------------------
# D26: before / after classes and flaky tests
# ---------------------------------------------------------------------------


def _junit(results: dict[str, str]) -> bytes:
    cases = []
    for test_id, outcome in results.items():
        classname, name = test_id.split("::")
        body = {"passed": "", "failed": "<failure/>", "error": "<error/>", "skipped": "<skipped/>"}[outcome]
        cases.append(f'<testcase classname="{classname}" name="{name}">{body}</testcase>')
    return f"<testsuites><testsuite>{''.join(cases)}</testsuite></testsuites>".encode()


def test_junit_results_are_read_by_test_id():
    report = classify.parse_junit(_junit({"t::a": "passed", "t::b": "failed", "t::c": "error", "t::d": "skipped"}))
    assert report == {"t::a": "passed", "t::b": "failed", "t::c": "failed", "t::d": "skipped"}
    assert classify.counts(report) == {"total": 4, "passed": 1, "failed": 2, "skipped": 1}


@pytest.mark.parametrize("content", [b"<!DOCTYPE x><testsuite/>", b'<!ENTITY a "b"><testsuite/>', b"not xml", b"\xff\xfe"])
def test_unsafe_or_broken_reports_are_refused(content):
    with pytest.raises(classify.ReportInvalid):
        classify.parse_junit(content)


def test_every_class_is_recognised():
    before = {"t::f2p": "failed", "t::p2p": "passed", "t::p2f": "passed", "t::f2f": "failed", "t::gone": "passed", "t::skip": "passed"}
    after = {"t::f2p": "passed", "t::p2p": "passed", "t::p2f": "failed", "t::f2f": "failed", "t::new": "passed", "t::skip": "skipped"}
    result = classify.classify(before, [after])
    assert result["counts"] == {
        "fail_to_pass": 1, "pass_to_pass": 1, "pass_to_fail": 1, "fail_to_fail": 1, "new_in_after": 1, "missing_in_after": 1, "skipped": 1, "flaky": 0,
    }
    assert result["tests"]["pass_to_fail"] == ["t::p2f"]


def test_a_test_that_does_not_repeat_is_flaky_and_counts_nowhere_else():
    before = {"t::a": "passed", "t::b": "passed"}
    result = classify.classify(before, [{"t::a": "failed", "t::b": "failed"}, {"t::a": "passed", "t::b": "failed"}, {"t::a": "failed", "t::b": "failed"}])
    assert result["tests"]["flaky"] == ["t::a"] and result["tests"]["pass_to_fail"] == ["t::b"]
    assert result["counts"]["pass_to_pass"] == 0 and result["after_attempts"] == 3


def test_a_long_listing_is_cut_but_the_count_stays_true():
    before = {f"t::{index}": "passed" for index in range(classify.MAX_LISTED + 5)}
    result = classify.classify(before, [dict(before)])
    assert result["counts"]["pass_to_pass"] == classify.MAX_LISTED + 5
    assert len(result["tests"]["pass_to_pass"]) == classify.MAX_LISTED and result["listing_truncated"]


# ---------------------------------------------------------------------------
# D27: changed-line coverage
# ---------------------------------------------------------------------------


def _coverage(files) -> bytes:
    return json.dumps({"files": files}).encode()


def test_changed_lines_are_compared_with_executed_lines():
    report = _coverage({"/workspace/src/app.py": {"executed_lines": [1, 2], "missing_lines": [3]}})
    result = line_coverage.changed_line_coverage({"app.py": (2, 3, 9)}, report)
    assert (result["changed_lines_total"], result["changed_lines_executed"], result["ratio"]) == (2, 1, 0.5)
    assert result["unexecuted"] == [{"path": "app.py", "line": 3}]


def test_relative_paths_are_matched():
    result = line_coverage.changed_line_coverage({"pkg/a.py": (1,)}, _coverage({"pkg/a.py": {"executed_lines": [1], "missing_lines": []}}))
    assert result["ratio"] == 1.0


def test_a_file_coverage_never_saw_is_reported():
    result = line_coverage.changed_line_coverage({"a.py": (1,)}, _coverage({}))
    assert (result["unmeasured_files"], result["ratio"]) == (["a.py"], None)


def test_the_list_of_unexecuted_lines_is_limited():
    lines = tuple(range(1, line_coverage.MAX_UNEXECUTED + 11))
    report = _coverage({"a.py": {"executed_lines": [], "missing_lines": list(lines)}})
    result = line_coverage.changed_line_coverage({"a.py": lines}, report)
    assert len(result["unexecuted"]) == line_coverage.MAX_UNEXECUTED and result["changed_lines_total"] == len(lines)


@pytest.mark.parametrize("content", [None, b"not json", b"[]", b"\xff"])
def test_missing_coverage_is_unavailable_not_a_failure(content):
    assert line_coverage.changed_line_coverage({"a.py": (1,)}, content) == {"status": "unavailable"}


# ---------------------------------------------------------------------------
# D22, D24: suites
# ---------------------------------------------------------------------------


def _publish(session_factory, name="demo", tenant_id="t-1", settings=None, content=b"1", **kwargs):
    normalized = _normalize([("tests/test_a.py", "file", content)])

    async def scenario():
        async with session_factory() as session:
            await artifacts.store_artifact(session, normalized)
            return await suites.publish(
                session, settings or _settings(), tenant_id=tenant_id, name=name, artifact_digest=normalized.digest, actor="user:alice", **kwargs
            )

    return asyncio.run(scenario())


def _in_session(session_factory, function):
    async def scenario():
        async with session_factory() as session:
            return await function(session)

    return asyncio.run(scenario())


def _events(session_factory, event_type):
    async def load(session):
        return (await session.scalars(select(GovernanceEvent).where(GovernanceEvent.event_type == event_type))).all()

    return _in_session(session_factory, load)


def test_publishing_again_makes_a_new_version_and_an_audit_event(session_factory):
    first, second = _publish(session_factory), _publish(session_factory, content=b"2")
    other_tenant = _publish(session_factory, tenant_id="t-2")
    assert (first.version, second.version, other_tenant.version) == (1, 2, 1)
    assert first.suite_id.startswith("vsu_") and first.artifact_digest != second.artifact_digest
    event = _events(session_factory, "verification_suite.published")[0]
    assert event.details["suite_id"] == first.suite_id and event.details["artifact_digest"] == first.artifact_digest


def test_a_suite_gets_the_documented_defaults(session_factory):
    suite = _publish(session_factory)
    assert (suite.framework, suite.test_command, suite.test_paths) == ("pytest", ["-q"], list(suites.DEFAULT_TEST_PATHS))


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"name": "bad name"}, "suite_name_invalid"),
        ({"image_name": "unknown"}, "suite_image_unknown"),
        ({"base_ref": "x" * 256}, "suite_base_ref_invalid"),
        ({"test_command": ["--junitxml=/tmp/fake.xml"]}, "suite_test_command_invalid"),
        ({"test_command": ["-o", "junit_suite_name=x"]}, "suite_test_command_invalid"),
        ({"test_command": ["-p", "evil"]}, "suite_test_command_invalid"),
        ({"test_command": "pytest -q"}, "suite_test_command_invalid"),
        ({"test_paths": "tests/**"}, "suite_test_paths_invalid"),
    ],
)
def test_invalid_suite_definitions_are_refused(session_factory, kwargs, code):
    with pytest.raises(VerificationError) as caught:
        _publish(session_factory, **kwargs)
    assert caught.value.code == code


def test_a_snapshot_without_tests_is_refused():
    with pytest.raises(VerificationError) as caught:
        suites.require_tests(("app.py",), list(suites.DEFAULT_TEST_PATHS))
    assert caught.value.code == "suite_no_tests"
    suites.require_tests(("tests/test_a.py",), list(suites.DEFAULT_TEST_PATHS))


def _request(**overrides) -> AgentRequest:
    values = {"tenant_id": "t-1", "user_id": "u1", "task_type": "code_fix", "prompt": "Fix it.", "goal": "Fix it"}
    return AgentRequest(**{**values, **overrides})


def _bind(session_factory, request):
    return _in_session(session_factory, lambda session: suites.bound_request(session, request))


def test_a_run_can_name_an_active_suite_of_its_tenant(session_factory):
    suite = _publish(session_factory)
    request = _request(metadata={suites.METADATA_KEY: suite.suite_id})
    assert _bind(session_factory, request).metadata[suites.METADATA_KEY] == suite.suite_id


def _disabled_suite(session_factory):
    suite = _publish(session_factory)

    async def disable(session):
        row = await suites.get_suite(session, suite.suite_id)
        return await suites.disable(session, row, actor="user:alice")

    return _in_session(session_factory, disable)


@pytest.mark.parametrize("case", ["missing", "other_tenant", "disabled", "wrong_task", "not_text"])
def test_an_unusable_suite_is_refused_before_the_run_starts(session_factory, case):
    suite_id = "vsu_missing"
    overrides = {}
    if case == "other_tenant":
        suite_id = _publish(session_factory, tenant_id="t-2").suite_id
    elif case == "disabled":
        suite_id = _disabled_suite(session_factory).suite_id
    elif case == "wrong_task":
        suite_id, overrides = _publish(session_factory).suite_id, {"task_type": "analysis"}
    elif case == "not_text":
        suite_id = 123
    with pytest.raises(VerificationError) as caught:
        _bind(session_factory, _request(metadata={suites.METADATA_KEY: suite_id}, **overrides))
    assert (caught.value.code, caught.value.status_code) == ("verification_suite_invalid", 422)


def _set_default(session_factory, suite, enabled=True):
    async def change(session):
        row = await suites.get_suite(session, suite.suite_id)
        return await suites.set_default(session, row, enabled=enabled, actor="user:alice")

    return _in_session(session_factory, change)


def test_the_tenant_default_binds_code_fix_runs_only(session_factory):
    suite = _publish(session_factory)
    assert _set_default(session_factory, suite) == suite.suite_id
    assert _bind(session_factory, _request()).metadata[suites.METADATA_KEY] == suite.suite_id
    assert suites.METADATA_KEY not in _bind(session_factory, _request(task_type="analysis")).metadata
    assert suites.METADATA_KEY not in _bind(session_factory, _request(tenant_id="t-2")).metadata
    assert _events(session_factory, "verification_suite.default_changed")[0].details["current"] == suite.suite_id


def test_disabling_the_default_suite_clears_the_default(session_factory):
    suite = _publish(session_factory)
    _set_default(session_factory, suite)

    async def disable(session):
        return await suites.disable(session, await suites.get_suite(session, suite.suite_id), actor="user:alice")

    assert _in_session(session_factory, disable).status == "disabled"
    assert suites.METADATA_KEY not in _bind(session_factory, _request()).metadata
    assert _events(session_factory, "verification_suite.disabled")[0].details["default_cleared"] is True


def test_a_disabled_suite_cannot_be_disabled_again_or_become_the_default(session_factory):
    suite = _disabled_suite(session_factory)

    async def disable(session):
        return await suites.disable(session, await suites.get_suite(session, suite.suite_id), actor="user:alice")

    with pytest.raises(VerificationError) as again:
        _in_session(session_factory, disable)
    with pytest.raises(VerificationError) as default:
        _set_default(session_factory, suite)
    assert (again.value.code, default.value.code) == ("suite_already_disabled", "verification_suite_invalid")


def test_clearing_keeps_a_default_set_on_another_suite(session_factory):
    first, second = _publish(session_factory), _publish(session_factory, name="other")
    _set_default(session_factory, second)
    assert _set_default(session_factory, first, enabled=False) == second.suite_id


def test_submitting_a_run_with_an_unusable_suite_answers_422(client):
    payload = {"user_id": "u1", "task_type": "code_fix", "prompt": "Fix the parser.", "goal": "Fix it", "metadata": {suites.METADATA_KEY: "vsu_missing"}}
    response = client.post("/v1/agent/submit", json=payload)
    assert response.status_code == 422 and response.json()["error"]["code"] == "verification_suite_invalid"


# ---------------------------------------------------------------------------
# D25, D26: the engine against a fake backend
# ---------------------------------------------------------------------------


class FakeBackend:
    """Answers each run with the next scripted report and keeps what it was given."""

    def __init__(self, *steps) -> None:
        self.steps = list(steps)
        self.specs, self.workspaces = [], []

    def __call__(self):
        return self

    def run_with_outputs(self, spec, *, artifacts=None):
        self.specs.append(spec)
        ref = spec.mounts[0].source_ref
        assert ref == "artifact:sha256:" + hashlib.sha256(artifacts[ref]).hexdigest()
        self.workspaces.append(artifacts_module_read(artifacts[ref]))
        step = self.steps.pop(0)
        status, report, extra = step if isinstance(step, tuple) else ("nonzero_exit", step, {})
        outputs = {**({"out/junit.xml": _junit(report)} if report is not None else {}), **extra}
        result = ExecutionResult(
            status=status, backend_id="docker", backend_version="v1.6.2", isolation_level="container", spec_digest=f"digest-{len(self.specs)}",
            exit_code=1, stdout_sha256="o", stderr_sha256="e", usage={"wall_ms": 1500, "cpu_ms": None, "max_rss_mb": None},
            rejection_reasons=("image_missing",) if status == "error" else (),
        )
        return result, outputs


artifacts_module_read = artifacts.read_tar
FAILING = {"t::bug": "failed", "t::ok": "passed"}
FIXED = {"t::bug": "passed", "t::ok": "passed"}
EVIDENCE = {"changed_files": [{"path": "app.py", "after": "def add(a, b):\n    return a + b\n"}]}


def _run(backend, evidence=None, settings=None):
    return engine.run_verification(
        backend, settings or _settings(), base_files=BASE, evidence=EVIDENCE if evidence is None else evidence, test_paths=TEST_PATHS,
        test_command=["-q"], image=IMAGE, job_id="sbj_1",
    )


def test_an_honest_fix_is_classified_and_measured():
    coverage = _coverage({"/workspace/src/app.py": {"executed_lines": [1, 2], "missing_lines": []}})
    backend = FakeBackend(FAILING, ("ok", FIXED, {"out/coverage.json": coverage}))
    result = _run(backend)
    assert (result.status, result.skip_reason) == ("completed", "")
    assert result.classification["counts"]["fail_to_pass"] == 1 and result.classification["counts"]["pass_to_pass"] == 1
    assert result.coverage["ratio"] == 1.0 and result.wall_seconds == 3.0
    assert result.before["counts"] == {"total": 2, "passed": 1, "failed": 1, "skipped": 0}
    assert result.after["spec_digest"] == "digest-2" and result.after["attempts"] == 1


def test_before_uses_the_platform_copy_and_after_the_change():
    backend = FakeBackend(FAILING, FIXED)
    _run(backend)
    assert backend.workspaces[0]["src/app.py"] == BASE["app.py"]
    assert backend.workspaces[1]["src/app.py"] == b"def add(a, b):\n    return a + b\n"
    assert backend.workspaces[1]["src/tests/test_app.py"] == BASE["tests/test_app.py"]


def test_the_spec_runs_without_network_with_limits_and_a_writable_subdirectory():
    backend = FakeBackend(FAILING, FIXED)
    _run(backend)
    spec = backend.specs[0]
    assert (spec.network, spec.workdir, spec.image, spec.labels) == ("none", "/workspace/src", IMAGE, {"job_id": "sbj_1"})
    assert spec.mounts[0].target == "/workspace" and not spec.mounts[0].read_only
    assert spec.limits.timeout_seconds == 300 and spec.argv[:2] == ("sh", "-c")
    assert "--junitxml=/workspace/out/junit.xml" in spec.argv[2] and "no:cacheprovider" in spec.argv[2]


def test_only_the_first_after_run_measures_coverage():
    backend = FakeBackend(FAILING, FAILING, FAILING, FAILING)
    _run(backend)
    assert ["coverage" in spec.argv[2] for spec in backend.specs] == [False, True, False, False]


def test_suite_arguments_are_quoted_into_the_shell_command():
    script = engine.pytest_script(["-k", "a b; rm -rf /"], with_coverage=False)
    assert "'a b; rm -rf /'" in script


def test_a_timeout_setting_above_the_platform_limit_is_capped():
    backend = FakeBackend(FAILING, FIXED)
    _run(backend, settings=_settings(sandbox_verify_timeout_seconds=900, sandbox_max_timeout_seconds=120))
    assert backend.specs[0].limits.timeout_seconds == 120


def test_a_failing_after_run_is_repeated_and_flaky_tests_are_marked():
    backend = FakeBackend(FAILING, {"t::bug": "failed", "t::ok": "passed"}, FIXED, {"t::bug": "failed", "t::ok": "passed"})
    result = _run(backend)
    assert len(backend.specs) == 4 and result.after["attempts"] == 3
    assert result.classification["tests"]["flaky"] == ["t::bug"] and result.classification["counts"]["fail_to_fail"] == 0


def test_a_passing_after_run_is_not_repeated():
    backend = FakeBackend(FAILING, FIXED)
    assert _run(backend).after["attempts"] == 1 and len(backend.specs) == 2


def test_the_number_of_retries_is_configurable():
    backend = FakeBackend(FAILING, FAILING)
    assert _run(backend, settings=_settings(sandbox_verify_flaky_retries=0)).after["attempts"] == 1


def test_a_retry_without_a_report_does_not_count():
    backend = FakeBackend(FAILING, FAILING, ("timeout", None, {}))
    result = _run(backend)
    assert result.status == "completed" and result.after["attempts"] == 1 and len(backend.specs) == 3


@pytest.mark.parametrize("evidence", [{"changed_files": [{"path": "tests/test_app.py", "after": "x = 1\n"}]}, {}])
def test_a_skipped_change_starts_no_container(evidence):
    backend = FakeBackend()
    result = _run(backend, evidence=evidence)
    assert result.status == "skipped" and result.skip_reason and backend.specs == []


def test_discarded_test_changes_are_recorded():
    evidence = {"changed_files": [*EVIDENCE["changed_files"], {"path": "tests/conftest.py", "after": "x = 1\n"}]}
    result = _run(FakeBackend(FAILING, FIXED), evidence=evidence)
    assert result.discarded == [{"path": "tests/conftest.py", "reason": "test_config"}]


@pytest.mark.parametrize(
    ("steps", "reason"),
    [
        ([("timeout", None, {})], "before_no_report"),
        ([("error", None, {})], "sandbox_error:image_missing"),
        ([FAILING, ("timeout", None, {})], "after_no_report"),
        ([FAILING, ("oom", FIXED, {})], "after_no_report"),
        ([FAILING, ("error", None, {})], "sandbox_error:image_missing"),
    ],
)
def test_a_run_without_a_report_fails_with_the_reason(steps, reason):
    result = _run(FakeBackend(*steps))
    assert (result.status, result.skip_reason) == ("failed", reason) and result.classification == {}


def test_a_forged_unsafe_report_counts_as_no_report():
    result = _run(FakeBackend(FAILING, ("ok", None, {"out/junit.xml": b"<!DOCTYPE x><testsuite/>"})))
    assert (result.status, result.skip_reason) == ("failed", "after_no_report")


# ---------------------------------------------------------------------------
# D28: stored results and the runner
# ---------------------------------------------------------------------------


def _queued(session_factory, *, evidence=None, tenant_id="t-1"):
    suite = _publish(session_factory, tenant_id=tenant_id, content=b"def test_a():\n    pass\n")

    async def add(session):
        session.add(RunOutcome(outcome_id="outcome_1", run_id="run_1", tenant_id=tenant_id, evidence=EVIDENCE if evidence is None else evidence))
        session.add(
            VerificationRun(
                verification_id="vrf_1", run_id="run_1", outcome_id="outcome_1", tenant_id=tenant_id, suite_id=suite.suite_id,
                artifact_digest=suite.artifact_digest, trigger="all", status="queued",
            )
        )
        job = await jobs.enqueue(session, jobs.KIND_VERIFICATION, tenant_id=tenant_id, run_id="run_1", ref_id="vrf_1", payload={"verification_id": "vrf_1"})
        await session.commit()
        return job

    return suite, _in_session(session_factory, add)


def _row(session_factory, verification_id="vrf_1"):
    async def load(session):
        return await session.scalar(select(VerificationRun).where(VerificationRun.verification_id == verification_id))

    return _in_session(session_factory, load)


def _handle(session_factory, job, backend, settings=None):
    return asyncio.run(
        engine.handle_job(job, settings=settings or _settings(), session_factory=session_factory, backend_factory=backend, docker_server_version="29.9.2")
    )


def test_a_finished_run_stores_its_result_and_an_audit_event(session_factory):
    suite, job = _queued(session_factory, evidence={"changed_files": [{"path": "app.py", "after": "x = 1\n"}]})
    answer = _handle(session_factory, job, FakeBackend(FAILING, FIXED))
    row = _row(session_factory)
    assert answer == {"verification_id": "vrf_1", "status": "completed", "skip_reason": ""}
    assert (row.status, row.backend_id, row.isolation_level, row.runtime) == ("completed", "docker", "container", "runc")
    assert row.classification["counts"]["fail_to_pass"] == 1 and row.finished_at is not None and row.evaluator_version == engine.ENGINE_VERSION
    event = _events(session_factory, "sandbox.verification_completed")[0]
    assert (event.run_id, event.decision, event.details["suite_id"]) == ("run_1", "completed", suite.suite_id)
    assert len(event.details["summary_digest"]) == 64 and "classification" not in event.details


def test_a_skipped_change_is_stored_as_skipped(session_factory):
    _suite, job = _queued(session_factory, evidence={})
    _handle(session_factory, job, FakeBackend())
    row = _row(session_factory)
    assert (row.status, row.skip_reason) == ("skipped", patching.NO_APPLICABLE_CHANGES)


def test_a_finished_record_is_not_run_again(session_factory):
    _suite, job = _queued(session_factory, evidence={"changed_files": [{"path": "app.py", "after": "x = 1\n"}]})
    _handle(session_factory, job, FakeBackend(FAILING, FIXED))
    backend = FakeBackend()
    assert _handle(session_factory, job, backend)["status"] == "completed" and backend.specs == []


def test_a_missing_image_setting_fails_without_starting_a_container(session_factory):
    _suite, job = _queued(session_factory)
    backend = FakeBackend()
    _handle(session_factory, job, backend, settings=Settings(_env_file=None, sandbox_backend="docker", sandbox_images={}))
    assert (_row(session_factory).status, _row(session_factory).skip_reason) == ("failed", engine.IMAGE_NOT_CONFIGURED) and backend.specs == []


@pytest.mark.parametrize(("job_status", "error_code", "reason"), [("failed", "sandbox_not_conformant", "sandbox_not_conformant"), ("expired", "lease_expired", "lease_expired"), ("cancelled", "", "cancelled")])
def test_a_job_that_ended_without_a_result_closes_its_record(session_factory, job_status, error_code, reason):
    _suite, job = _queued(session_factory)

    async def scenario(session):
        row = await session.scalar(select(SandboxJob).where(SandboxJob.job_id == job.job_id))
        row.status, row.error_code = job_status, error_code
        await session.commit()
        return await engine.reconcile_jobs(session)

    assert _in_session(session_factory, scenario) == 1
    row = _row(session_factory)
    assert (row.status, row.skip_reason) == ("failed", reason) and row.finished_at is not None
    assert _in_session(session_factory, engine.reconcile_jobs) == 0


def test_a_queued_job_keeps_its_record_queued(session_factory):
    _queued(session_factory)
    assert _in_session(session_factory, engine.reconcile_jobs) == 0 and _row(session_factory).status == "queued"


def test_the_runner_registers_the_verification_handler(session_factory):
    from app.services.run_state import InMemoryRunStateStore
    from app.workers.sandbox_runner import SandboxRunner

    _suite, job = _queued(session_factory, evidence={"changed_files": [{"path": "app.py", "after": "x = 1\n"}]})
    runner = SandboxRunner(_settings(), session_factory=session_factory, state_store=InMemoryRunStateStore(), backend_factory=FakeBackend(FAILING, FIXED))
    assert asyncio.run(runner.handlers[jobs.KIND_VERIFICATION](job))["status"] == "completed"


# ---------------------------------------------------------------------------
# The demo suite
# ---------------------------------------------------------------------------


def test_the_demo_archive_passes_the_upload_checks_and_is_reproducible():
    normalized = artifacts.normalize_upload(demo.suite_archive(), max_bytes=MAX_BYTES)
    assert normalized.paths == ("tax.py", "tests/test_tax.py") and demo.suite_archive() == demo.suite_archive()
    suites.require_tests(normalized.paths, list(suites.DEFAULT_TEST_PATHS))


@pytest.mark.parametrize("name", demo.SCENARIOS)
def test_every_demo_scenario_applies_to_the_suite(name):
    result = patching.apply_evidence(demo.suite_files(), demo.scenario_evidence(name), test_paths=TEST_PATHS, max_bytes=MAX_BYTES)
    assert result.applied == ("tax.py",) and result.changed_lines["tax.py"]


def test_the_conftest_scenario_loses_its_hook():
    result = patching.apply_evidence(demo.suite_files(), demo.scenario_evidence("conftest_rewrite"), test_paths=TEST_PATHS, max_bytes=MAX_BYTES)
    assert result.discarded == ({"path": "tests/conftest.py", "reason": "test_config"},) and "tests/conftest.py" not in result.files


def test_an_unknown_demo_scenario_is_refused():
    with pytest.raises(ValueError):
        demo.scenario_evidence("nope")


def test_the_build_script_writes_the_archive(tmp_path):
    target = tmp_path / "demo.tar.gz"
    done = subprocess.run([sys.executable, str(ROOT / "scripts/build_demo_suite.py"), "--out", str(target)], capture_output=True, text=True, check=True)
    assert target.read_bytes() == demo.suite_archive() and json.loads(done.stdout)["bytes"] == target.stat().st_size


# ---------------------------------------------------------------------------
# Architecture
# ---------------------------------------------------------------------------


def test_the_migration_adds_the_tenant_default():
    text = (ROOT / "migrations/versions/202611180002_add_default_verification_suite.py").read_text(encoding="utf-8")
    assert 'down_revision = "202611180001"' in text and "default_verification_suite_id" in text
    assert "default_verification_suite_id" in TenantEvaluationPolicy.__table__.columns


def test_verification_is_covered_by_the_review_and_docker_contracts():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert text.count('"app.services.verification",') == 2


def test_suite_rows_have_no_update_path():
    text = (ROOT / "app/services/verification/suites.py").read_text(encoding="utf-8")
    assert "suite.artifact_digest =" not in text and "suite.test_command =" not in text and VerificationSuite.__tablename__ == "verification_suites"
