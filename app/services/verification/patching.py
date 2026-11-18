"""Apply an agent's change to the platform's snapshot (v1.6.2 D25). Plain text handling, inside the runner.

Tests, test settings and anything that could replace the test tools always stay the platform's copy. Hunks apply
strictly: the context must match exactly and there is no fuzz. A change that cannot be applied faithfully is skipped with
a reason, because running something other than what the agent submitted would prove nothing.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.services.evaluation.evidence import DiffFile, Hunk, matches_any, normalize_path, parse_unified_diff

EVIDENCE_TRUNCATED = "evidence_truncated"
EVIDENCE_REDACTED = "evidence_redacted"
PATCH_FAILED = "patch_failed"
PATCH_TOO_LARGE = "patch_too_large"
NO_APPLICABLE_CHANGES = "no_applicable_changes"
ONLY_TEST_CHANGES = "only_test_changes"

TEST_CONFIG_FILES = frozenset({"conftest.py", "pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", ".coveragerc"})
# The agent's files sit first on the import path, so these names would replace the tools that produce the report.
TOOL_MODULES = frozenset({"pytest", "_pytest", "coverage", "sitecustomize", "usercustomize"})
_REDACTION_MARK = "[REDACTED"
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchSkipped(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class PatchResult:
    files: dict[str, bytes]
    applied: tuple[str, ...]
    discarded: tuple[dict[str, str], ...]
    changed_lines: dict[str, tuple[int, ...]] = field(default_factory=dict)


def protection_reason(path: str, test_paths: list[str] | tuple[str, ...]) -> str:
    """Why this path must keep the platform's copy; empty when the agent may change it."""
    parts = path.split("/")
    name = parts[-1]
    if name in TEST_CONFIG_FILES:
        return "test_config"
    if matches_any(path, list(test_paths)):
        return "test_path"
    if name.endswith(".pth") or name.removesuffix(".py") in TOOL_MODULES or TOOL_MODULES.intersection(parts[:-1]):
        return "test_tooling"
    return ""


def _clean_path(raw: Any) -> str:
    text = str(raw or "")
    path = normalize_path(text)
    parts = path.split("/")
    unsafe = not path or text.startswith("/") or "\\" in text or "\x00" in text or any(part in ("", ".", "..") for part in parts)
    if unsafe or re.match(r"^[A-Za-z]:", path):
        raise PatchSkipped(PATCH_FAILED, "unsafe_path")
    return path


def _hunk_range(hunk: Hunk) -> tuple[int, int, int]:
    match = _HUNK_HEADER.match(hunk.header)
    if match is None:
        raise PatchSkipped(PATCH_FAILED, "hunk_header_invalid")
    old_start, new_count = int(match.group(1)), int(match.group(4)) if match.group(4) is not None else 1
    old_count = int(match.group(2)) if match.group(2) is not None else 1
    if sum(line.kind in " -" for line in hunk.lines) != old_count or sum(line.kind in " +" for line in hunk.lines) != new_count:
        raise PatchSkipped(PATCH_FAILED, "hunk_truncated")
    return old_start, old_count, new_count


def _apply_hunks(base: str, hunks: list[Hunk]) -> str:
    source = base.split("\n") if base else []
    output: list[str] = []
    cursor = 0
    for hunk in hunks:
        old_start, old_count, _new = _hunk_range(hunk)
        index = old_start if old_count == 0 else old_start - 1  # "-5,0" inserts after line 5
        if index < cursor or index > len(source):
            raise PatchSkipped(PATCH_FAILED, "hunk_out_of_range")
        output.extend(source[cursor:index])
        cursor = index
        for line in hunk.lines:
            if line.kind == "+":
                output.append(line.text)
                continue
            if cursor >= len(source) or source[cursor] != line.text:
                raise PatchSkipped(PATCH_FAILED, "context_mismatch")
            if line.kind == " ":
                output.append(line.text)
            cursor += 1
    output.extend(source[cursor:])
    return "\n".join(output)


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchSkipped(PATCH_FAILED, "base_not_text") from exc


def _changed_lines(before: str, after: str) -> tuple[int, ...]:
    matcher = difflib.SequenceMatcher(None, before.split("\n"), after.split("\n"), autojunk=False)
    lines: list[int] = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            lines.extend(range(j1 + 1, j2 + 1))
    return tuple(lines)


def _check_redaction(path: str, text: str, base: str) -> None:
    if _REDACTION_MARK in text and _REDACTION_MARK not in base:
        raise PatchSkipped(EVIDENCE_REDACTED, path)


def _apply_diff(tree: dict[str, bytes], item: DiffFile, path: str, old_path: str) -> tuple[str, str | None]:
    """Returns the old and new text of ``path``; ``None`` means the file is deleted."""
    source = old_path or path
    exists = source in tree
    if item.status == "added":
        if path in tree:
            raise PatchSkipped(PATCH_FAILED, "file_already_exists")
        return "", _apply_hunks("", item.hunks)
    if not exists:
        raise PatchSkipped(PATCH_FAILED, "file_missing_in_base")
    base = _decode(tree[source])
    if not item.hunks and item.status != "renamed":
        raise PatchSkipped(PATCH_FAILED, "no_hunks")
    text = _apply_hunks(base, item.hunks)
    if item.status == "deleted":
        if text.strip("\n"):
            raise PatchSkipped(PATCH_FAILED, "delete_incomplete")
        return base, None
    return base, text


def _check_tree(tree: Mapping[str, bytes]) -> None:
    for path in tree:
        parts = path.split("/")
        if any("/".join(parts[:depth]) in tree for depth in range(1, len(parts))):
            raise PatchSkipped(PATCH_FAILED, "path_conflict")


def apply_evidence(
    base_files: Mapping[str, bytes], evidence: Mapping[str, Any], *, test_paths: list[str] | tuple[str, ...], max_bytes: int
) -> PatchResult:
    if evidence.get("evidence_limits"):
        raise PatchSkipped(EVIDENCE_TRUNCATED, ",".join(str(item) for item in evidence["evidence_limits"]))

    full_text: dict[str, dict[str, Any]] = {}
    for entry in evidence.get("changed_files") or []:
        if entry.get("after") is not None:
            full_text[_clean_path(entry.get("path"))] = dict(entry)
    diff_items = [item for item in parse_unified_diff(str(evidence.get("diff") or "")) if item.path]

    tree = dict(base_files)
    applied: list[str] = []
    discarded: dict[str, str] = {}
    changed: dict[str, tuple[int, ...]] = {}

    def keep(*paths: str) -> bool:
        reasons = {path: protection_reason(path, test_paths) for path in paths}
        if not any(reasons.values()):
            return True
        for path in paths:
            discarded[path] = reasons[path] or "renamed_with_test_path"
        return False

    def record(path: str, before: str, after: str) -> None:
        applied.append(path)
        if path.endswith(".py"):
            changed[path] = _changed_lines(before, after)

    for path, entry in sorted(full_text.items()):
        if not keep(path):
            continue
        base = _decode(tree[path]) if path in tree else ""
        claimed_base = entry.get("before")
        if claimed_base is not None and path in tree and claimed_base != base:
            raise PatchSkipped(PATCH_FAILED, "base_mismatch")
        text = str(entry["after"])
        _check_redaction(path, text, base)
        if text != base:
            tree[path] = text.encode("utf-8")
            record(path, base, text)

    for item in diff_items:
        path = _clean_path(item.path)
        old_path = _clean_path(item.old_path) if item.old_path else ""
        if path in full_text or not keep(*[value for value in (path, old_path) if value]):
            continue
        before, after = _apply_diff(tree, item, path, old_path)
        if after is None:
            del tree[old_path or path]
            applied.append(path)
            continue
        _check_redaction(path, after, before)
        if old_path:
            del tree[old_path]
        tree[path] = after.encode("utf-8")
        record(path, before, after)

    if not applied:
        raise PatchSkipped(ONLY_TEST_CHANGES if discarded else NO_APPLICABLE_CHANGES)
    if sum(len(content) for content in tree.values()) > max_bytes:
        raise PatchSkipped(PATCH_TOO_LARGE)
    _check_tree(tree)
    return PatchResult(
        files=tree,
        applied=tuple(sorted(set(applied))),
        discarded=tuple({"path": path, "reason": reason} for path, reason in sorted(discarded.items())),
        changed_lines=changed,
    )
