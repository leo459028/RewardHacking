"""Evidence model, parsers and redaction for the v1.5.8 evaluation engine.

Agent-reported evidence is treated as a *claim* (provenance ``self_reported``) until a trusted
component verifies it. All evidence is redacted *before* detection so that findings, snippets and
persisted rows never contain credentials or emails.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from app.services.mcp_gateway import SENSITIVE_KEY_NAMES, SENSITIVE_KEY_SUFFIXES, SENSITIVE_VALUE_PATTERNS

DEFAULT_PROTECTED_PATHS: tuple[str, ...] = (
    "tests/**",
    "test/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/*_test.go",
    "**/*.spec.ts",
    "**/*.test.ts",
    "**/*.spec.js",
    "**/*.test.js",
    "**/conftest.py",
    "pytest.ini",
    "setup.cfg",
    "tox.ini",
    "noxfile.py",
    ".github/workflows/**",
    ".gitlab-ci.yml",
    "jest.config.*",
    "vitest.config.*",
    ".eslintrc*",
    "ruff.toml",
)

TEST_FILE_PATTERNS: tuple[str, ...] = (
    "tests/**",
    "test/**",
    "**/__tests__/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/*_test.go",
    "**/*.spec.*",
    "**/*.test.*",
    "**/conftest.py",
)

CI_FILE_PATTERNS: tuple[str, ...] = (
    ".github/workflows/**",
    ".gitlab-ci.yml",
    "azure-pipelines.yml",
    "Jenkinsfile",
    "Makefile",
    "tox.ini",
    "noxfile.py",
    ".circleci/**",
    "bitbucket-pipelines.yml",
)

_KV_SECRET_PATTERN = re.compile(
    r"(?i)\b([\w-]*?(?:api[_-]?key|secret|password|passwd|access[_-]?token|refresh[_-]?token|client[_-]?secret|token))"
    r"(\s*[:=]\s*)([\"']?)([^\s\"',;]{6,})"
)
_UNSAFE_XML_PATTERN = re.compile(r"<!\s*(doctype|entity)", re.IGNORECASE)
# Stricter than the MCP gateway pattern: the local part must end with an alphanumeric character so
# diff lines such as "+@pytest.mark.skip" or decorators are never mistaken for email addresses.
EVIDENCE_EMAIL_PATTERN = re.compile(r"(?<![\w.%+-])[A-Za-z0-9][A-Za-z0-9._%+-]*(?<=[A-Za-z0-9])@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


class EvidenceError(ValueError):
    """Evidence rejected before evaluation (maps to an HTTP 4xx code)."""

    def __init__(self, code: str, detail: str, status_code: int = 422):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Path matching
# ---------------------------------------------------------------------------


def normalize_path(path: str) -> str:
    value = str(path or "").strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value.lstrip("/")


def path_matches(path: str, pattern: str) -> bool:
    candidate = normalize_path(path)
    pat = normalize_path(pattern)
    if not candidate or not pat:
        return False
    basename = candidate.rsplit("/", 1)[-1]
    if fnmatch.fnmatchcase(candidate, pat):
        return True
    if pat.endswith("/**"):
        prefix = pat[:-3]
        if candidate == prefix or candidate.startswith(prefix + "/"):
            return True
    if pat.startswith("**/"):
        rest = pat[3:]
        if fnmatch.fnmatchcase(candidate, rest) or fnmatch.fnmatchcase(basename, rest):
            return True
        if "/" in rest and ("/" + rest.split("/**")[0] + "/") in ("/" + candidate):
            return True
    if "/" not in pat and fnmatch.fnmatchcase(basename, pat):
        return True
    return False


def matches_any(path: str, patterns: list[str] | tuple[str, ...]) -> str:
    for pattern in patterns:
        if path_matches(path, pattern):
            return pattern
    return ""


def is_test_file(path: str) -> bool:
    return bool(matches_any(path, TEST_FILE_PATTERNS))


def is_ci_file(path: str) -> bool:
    return bool(matches_any(path, CI_FILE_PATTERNS))


# ---------------------------------------------------------------------------
# Unified diff parsing (dependency-free)
# ---------------------------------------------------------------------------


@dataclass
class DiffLine:
    kind: str  # "+", "-", " "
    text: str
    old_no: int | None
    new_no: int | None


@dataclass
class Hunk:
    header: str
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class DiffFile:
    path: str
    old_path: str = ""
    status: str = "modified"  # added | deleted | modified | renamed
    hunks: list[Hunk] = field(default_factory=list)

    def added(self) -> list[tuple[int | None, str, str]]:
        return [(line.new_no, line.text, hunk.header) for hunk in self.hunks for line in hunk.lines if line.kind == "+"]

    def removed(self) -> list[tuple[int | None, str, str]]:
        return [(line.old_no, line.text, hunk.header) for hunk in self.hunks for line in hunk.lines if line.kind == "-"]

    @property
    def paths(self) -> list[str]:
        return [item for item in dict.fromkeys([self.path, self.old_path]) if item]


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_HEADER_PREFIXES = ("diff --git ", "--- ", "+++ ", "@@", "index ", "new file mode", "deleted file mode", "rename ", "similarity ")


def _strip_prefix(value: str) -> str:
    value = value.strip().split("\t", 1)[0]
    if value == "/dev/null":
        return ""
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return normalize_path(value)


def parse_unified_diff(text: str) -> list[DiffFile]:
    """Parse git-style or plain unified diffs.

    Hunk line counts from ``@@ -a,b +c,d @@`` decide where a hunk ends, so removed lines whose
    content starts with ``--`` (SQL comments, CLI flags) are never mistaken for file headers.
    """
    files: list[DiffFile] = []
    current: DiffFile | None = None
    hunk: Hunk | None = None
    old_no = new_no = 0
    old_left = new_left = 0
    pending_old: str | None = None

    for raw in (text or "").splitlines():
        # Inside the declared hunk length every line is body; after it, stay lenient for hand-edited
        # diffs with wrong counts unless the line clearly starts a new header.
        in_hunk = hunk is not None and (old_left > 0 or new_left > 0 or not raw.startswith(_HEADER_PREFIXES))
        if in_hunk and hunk is not None:
            if raw.startswith("+"):
                hunk.lines.append(DiffLine("+", raw[1:], None, new_no))
                new_no += 1
                new_left -= 1
                continue
            if raw.startswith("-"):
                hunk.lines.append(DiffLine("-", raw[1:], old_no, None))
                old_no += 1
                old_left -= 1
                continue
            if raw.startswith(" ") or raw == "":
                hunk.lines.append(DiffLine(" ", raw[1:] if raw else "", old_no, new_no))
                old_no += 1
                new_no += 1
                old_left -= 1
                new_left -= 1
                continue
            if raw.startswith("\\"):
                continue
            # Malformed hunk: fall through and treat the line as a header.
        if raw.startswith("diff --git "):
            parts = raw[len("diff --git "):].split(" b/", 1)
            old_path = _strip_prefix(parts[0])
            new_path = normalize_path(parts[1]) if len(parts) > 1 else old_path
            current = DiffFile(path=new_path, old_path=old_path if old_path != new_path else "")
            files.append(current)
            hunk = None
            pending_old = None
            continue
        if raw.startswith("--- "):
            pending_old = raw[4:]
            continue
        if raw.startswith("+++ "):
            old_path = _strip_prefix(pending_old) if pending_old is not None else ""
            new_path = _strip_prefix(raw[4:])
            if current is None or current.hunks:
                current = DiffFile(path=new_path or old_path)
                files.append(current)
            else:
                current.path = new_path or old_path or current.path
            if old_path and new_path and old_path != new_path:
                current.old_path = old_path
            if pending_old is not None and not old_path and new_path:
                current.status = "added"
            elif old_path and not new_path:
                current.status = "deleted"
                current.path = old_path
            pending_old = None
            hunk = None
            continue
        if current is None:
            continue
        if raw.startswith("new file mode"):
            current.status = "added"
            continue
        if raw.startswith("deleted file mode"):
            current.status = "deleted"
            continue
        if raw.startswith("rename from "):
            current.old_path = normalize_path(raw[len("rename from "):])
            current.status = "renamed"
            continue
        if raw.startswith("rename to "):
            current.path = normalize_path(raw[len("rename to "):])
            current.status = "renamed"
            continue
        match = _HUNK_RE.match(raw)
        if match:
            hunk = Hunk(header=raw)
            current.hunks.append(hunk)
            old_no = int(match.group(1))
            new_no = int(match.group(3))
            old_left = int(match.group(2)) if match.group(2) is not None else 1
            new_left = int(match.group(4)) if match.group(4) is not None else 1
            continue
    return files


# ---------------------------------------------------------------------------
# Test report parsing
# ---------------------------------------------------------------------------


@dataclass
class TestSummaryData:
    __test__ = False  # not a pytest test class

    present: bool = False
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    total: int = 0
    source: str = ""
    command: str = ""


def parse_junit_xml(content: str, max_bytes: int) -> TestSummaryData:
    raw = content or ""
    if len(raw.encode("utf-8")) > max_bytes:
        raise EvidenceError("test_report_too_large", f"Test report exceeds {max_bytes} bytes.", 413)
    if _UNSAFE_XML_PATTERN.search(raw):
        raise EvidenceError("test_report_rejected_unsafe_xml", "JUnit XML with DOCTYPE or ENTITY declarations is rejected.")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise EvidenceError("test_report_invalid_xml", f"Invalid JUnit XML: {exc}") from exc
    cases = list(root.iter("testcase"))
    summary = TestSummaryData(present=True, source="junit_xml")
    if cases:
        for case in cases:
            if case.find("failure") is not None:
                summary.failed += 1
            elif case.find("error") is not None:
                summary.errors += 1
            elif case.find("skipped") is not None:
                summary.skipped += 1
            else:
                summary.passed += 1
        summary.total = len(cases)
        return summary
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    for suite in suites:
        tests = int(suite.get("tests", "0") or 0)
        failures = int(suite.get("failures", "0") or 0)
        errors = int(suite.get("errors", "0") or 0)
        skipped = int(suite.get("skipped", suite.get("disabled", "0")) or 0)
        summary.total += tests
        summary.failed += failures
        summary.errors += errors
        summary.skipped += skipped
        summary.passed += max(0, tests - failures - errors - skipped)
    return summary


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def redact_text(text: str) -> str:
    value = text or ""
    for pattern in SENSITIVE_VALUE_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    value = _KV_SECRET_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED]", value)
    value = EVIDENCE_EMAIL_PATTERN.sub("[REDACTED_EMAIL]", value)
    return value


def _sensitive_key(key: str) -> bool:
    lowered = str(key).strip().lower()
    return lowered in SENSITIVE_KEY_NAMES or lowered.endswith(SENSITIVE_KEY_SUFFIXES)


def redact_value(value: Any, depth: int = 0) -> Any:
    if depth > 12:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if _sensitive_key(key) else redact_value(child, depth + 1)) for key, child in value.items()}
    if isinstance(value, list):
        return [redact_value(child, depth + 1) for child in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def evidence_digest(evidence: dict[str, Any]) -> str:
    payload = json.dumps(evidence, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _truncate_bytes(text: str, limit: int) -> tuple[str, bool]:
    encoded = (text or "").encode("utf-8")
    if len(encoded) <= limit:
        return text or "", False
    cut = encoded[:limit].decode("utf-8", errors="ignore")
    if "\n" in cut:
        cut = cut[: cut.rfind("\n") + 1]
    return cut, True


# ---------------------------------------------------------------------------
# Evaluation context
# ---------------------------------------------------------------------------


@dataclass
class EvaluationContext:
    task_type: str
    goal: str
    metadata: dict[str, Any]
    result_text: str
    policy_flags: list[str]
    provenance: str = "simulated"  # simulated | self_reported | ci_attested
    has_outcome: bool = False
    final_message: str = ""
    diff_files: list[DiffFile] = field(default_factory=list)
    changed_files: dict[str, dict[str, str | None]] = field(default_factory=dict)
    tests: TestSummaryData = field(default_factory=TestSummaryData)
    tests_before: dict[str, int] | None = None
    lint: dict[str, Any] | None = None
    tool_transcript: list[dict[str, Any]] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=list)
    declared_scope: list[str] | None = None
    allow_test_changes: bool = False
    lint_suppression_threshold: int = 3
    notes: list[dict[str, str]] = field(default_factory=list)
    # v1.5.9 CI attestation verification result ("" = not attempted)
    attestation_status: str = ""
    attestation_detail: dict[str, Any] = field(default_factory=dict)


def apply_attestation_result(ctx: EvaluationContext, status: str, detail: dict[str, Any] | None = None) -> EvaluationContext:
    """Attach a verification result. Only ``verified`` removes the unverified-attestation note."""
    ctx.attestation_status = status
    ctx.attestation_detail = dict(detail or {})
    if ctx.provenance != "ci_attested":
        return ctx
    if status == "verified":
        ctx.notes = [note for note in ctx.notes if note.get("code") != "attestation_unverified"]
    else:
        reason = ctx.attestation_detail.get("reason") or status
        for note in ctx.notes:
            if note.get("code") == "attestation_unverified":
                note["detail"] = f"CI attestation was not verified ({reason})."
    return ctx


def context_from_executor(
    *,
    task_type: str,
    goal: str,
    metadata: dict[str, Any],
    result_text: str,
    policy_flags: list[str],
    lint_suppression_threshold: int = 3,
) -> EvaluationContext:
    return EvaluationContext(
        task_type=task_type,
        goal=goal,
        metadata=dict(metadata or {}),
        result_text=result_text or "",
        policy_flags=list(policy_flags or []),
        provenance="simulated",
        lint_suppression_threshold=lint_suppression_threshold,
    )


def effective_protected_paths(
    *,
    tenant_paths: list[str] | None,
    allow_defaults: bool,
    request_paths: list[str] | None,
) -> list[str]:
    """Tenant policy (admin) + defaults; request metadata may only *add* paths (D11)."""
    combined: list[str] = []
    if allow_defaults:
        combined.extend(DEFAULT_PROTECTED_PATHS)
    combined.extend(str(item) for item in (tenant_paths or []) if str(item).strip())
    combined.extend(str(item) for item in (request_paths or []) if str(item).strip())
    return [normalize_path(item) for item in dict.fromkeys(combined) if normalize_path(item)]


def context_from_outcome(
    *,
    task_type: str,
    goal: str,
    metadata: dict[str, Any],
    policy_flags: list[str],
    evidence: dict[str, Any],
    protected_paths: list[str],
    settings: Any,
) -> tuple[EvaluationContext, dict[str, Any]]:
    """Validate, bound and redact outcome evidence, then build the evaluation context.

    Returns ``(context, redacted_evidence)``; the redacted evidence is what gets persisted.
    """
    notes: list[dict[str, str]] = []
    data = dict(evidence or {})

    final_message, cut = _truncate_bytes(str(data.get("final_message") or ""), settings.evaluation_max_final_message_bytes)
    if cut:
        notes.append({"code": "final_message_truncated", "detail": "Final message exceeded the size limit and was truncated."})
    diff_text, cut = _truncate_bytes(str(data.get("diff") or ""), settings.evaluation_max_diff_bytes)
    if cut:
        notes.append({"code": "evidence_truncated", "detail": "Diff exceeded the size limit and was truncated."})

    changed: list[dict[str, Any]] = []
    for item in list(data.get("changed_files") or []):
        entry = dict(item or {})
        for side in ("before", "after"):
            content = entry.get(side)
            if content is not None and len(str(content).encode("utf-8")) > settings.evaluation_max_file_content_bytes:
                entry[side] = None
                notes.append({"code": "file_content_dropped", "detail": f"{entry.get('path')}: {side} content exceeded the size limit."})
        changed.append(entry)

    test_report = data.get("test_report") or None
    tests = TestSummaryData()
    if test_report:
        report = dict(test_report)
        if report.get("format") == "junit_xml":
            tests = parse_junit_xml(str(report.get("content") or ""), settings.evaluation_max_test_report_bytes)
            if report.get("summary"):
                tests.command = str((report.get("summary") or {}).get("command") or "")
        elif report.get("summary"):
            summary = dict(report.get("summary") or {})
            passed = int(summary.get("passed") or 0)
            failed = int(summary.get("failed") or 0)
            skipped = int(summary.get("skipped") or 0)
            errors = int(summary.get("errors") or 0)
            total = summary.get("total")
            tests = TestSummaryData(
                present=True,
                passed=passed,
                failed=failed,
                skipped=skipped,
                errors=errors,
                total=int(total) if total is not None else passed + failed + skipped + errors,
                source="summary",
                command=str(summary.get("command") or ""),
            )

    provenance = str(data.get("provenance") or "self_reported")
    if provenance == "ci_attested":
        notes.append({"code": "attestation_unverified", "detail": "CI attestation was not verified."})

    redacted = redact_value(
        {
            "final_message": final_message,
            "diff": diff_text,
            "changed_files": changed,
            "test_report": test_report,
            "tests_before": data.get("tests_before"),
            "lint_report": data.get("lint_report"),
            "tool_transcript": list(data.get("tool_transcript") or []),
            "provenance": provenance,
            "attestation": dict(data.get("attestation") or {}),
        }
    )

    meta = dict(metadata or {})
    scope = meta.get("declared_scope")
    ctx = EvaluationContext(
        task_type=task_type,
        goal=goal,
        metadata=meta,
        result_text=redacted["final_message"],
        policy_flags=list(policy_flags or []),
        provenance=provenance,
        has_outcome=True,
        final_message=redacted["final_message"],
        diff_files=parse_unified_diff(redacted["diff"]),
        changed_files={
            normalize_path(item.get("path", "")): {"before": item.get("before"), "after": item.get("after")}
            for item in redacted["changed_files"]
            if normalize_path(item.get("path", ""))
        },
        tests=tests,
        tests_before=dict(redacted["tests_before"]) if redacted.get("tests_before") else None,
        lint=dict(redacted["lint_report"]) if redacted.get("lint_report") else None,
        tool_transcript=redacted["tool_transcript"],
        protected_paths=list(protected_paths),
        declared_scope=[str(item) for item in scope] if isinstance(scope, list) and scope else None,
        allow_test_changes=bool(meta.get("allow_test_changes")),
        lint_suppression_threshold=int(settings.evaluation_lint_suppression_threshold),
        notes=notes,
    )
    return ctx, redacted
