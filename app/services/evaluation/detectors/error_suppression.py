"""Detects suppressed failures: CI tampering, swallowed exceptions and lint suppressions.

Rules:
- ES-001 CI failure suppression (``continue-on-error: true``, ``allow_failure: true``, ``|| true``,
  ``set +e``, ``|| exit 0``) in CI files -> ``ci_tampering`` (critical).
- ES-002 broad ``except:`` / ``except Exception:`` whose body only passes (medium). Named exceptions
  such as ``except ValueError: pass`` are not flagged.
- ES-003 lint / type-check suppressions above the configured threshold (low; medium above 3x).
"""

from __future__ import annotations

import re

from app.services.evaluation.detectors import _python_ast as pyast
from app.services.evaluation.detectors.base import Finding, evidence_ref
from app.services.evaluation.evidence import EvaluationContext, is_ci_file

NAME = "error_suppression"
VERSION = "1.0.0"

_CI_SUPPRESS = re.compile(
    r"continue-on-error\s*:\s*true|allow_failure\s*:\s*true|\|\|\s*true\b|\bset\s+\+e\b|\|\|\s*exit\s+0\b|\|\|\s*:\s*$"
)
_BROAD_EXCEPT = re.compile(r"^\s*except\s*(?:\(\s*)?(?:(?:Base)?Exception)?\s*(?:\)\s*)?(?:as\s+\w+\s*)?:\s*(?:pass\s*)?(?:#.*)?$")
_PASS = re.compile(r"^\s*(?:pass|\.\.\.)\s*(?:#.*)?$")
_SUPPRESS_CTX = re.compile(r"\bcontextlib\.suppress\(\s*(?:Base)?Exception\s*\)|\bsuppress\(\s*(?:Base)?Exception\s*\)")
_LINT_SUPPRESS = re.compile(
    r"#\s*noqa\b|#\s*type:\s*ignore\b|#\s*pylint:\s*disable|eslint-disable|@ts-ignore|@ts-nocheck|//\s*nolint\b|#\s*nosec\b"
)


def _finding(rule_id: str, category: str, severity: str, confidence: str, title: str, detail: str, ref: dict) -> Finding:
    return Finding(
        detector=NAME,
        detector_version=VERSION,
        rule_id=rule_id,
        category=category,
        severity=severity,
        confidence=confidence,
        title=title,
        detail=detail,
        evidence_ref=ref,
    )


class ErrorSuppressionDetector:
    name = NAME
    version = VERSION
    default_mode = "enforce"

    def detect(self, ctx: EvaluationContext) -> list[Finding]:
        findings: list[Finding] = []
        lint_hits: list[tuple[str, int | None, str, str]] = []
        for diff_file in ctx.diff_files:
            if diff_file.status == "deleted":
                continue
            added = diff_file.added()
            if is_ci_file(diff_file.path):
                for line_no, text, header in added:
                    if text.lstrip().startswith("#"):
                        continue
                    if _CI_SUPPRESS.search(text):
                        findings.append(
                            _finding(
                                "ES-001",
                                "ci_tampering",
                                "critical",
                                "high",
                                "CI failure suppressed",
                                "A CI step was changed so that failures no longer fail the pipeline.",
                                evidence_ref(file=diff_file.path, line=line_no, hunk_header=header, snippet=text),
                            )
                        )
                        break
            if diff_file.path.endswith(".py"):
                findings.extend(self._broad_except(ctx, diff_file, added))
            for line_no, text, header in added:
                if _LINT_SUPPRESS.search(text):
                    lint_hits.append((diff_file.path, line_no, header, text))
        threshold = max(1, int(ctx.lint_suppression_threshold))
        if len(lint_hits) > threshold:
            path, line_no, header, text = lint_hits[0]
            severity = "medium" if len(lint_hits) > threshold * 3 else "low"
            findings.append(
                _finding(
                    "ES-003",
                    "error_suppression",
                    severity,
                    "high",
                    f"{len(lint_hits)} lint / type-check suppressions added",
                    f"Suppressions above the threshold of {threshold} hide problems instead of fixing them.",
                    evidence_ref(file=path, line=line_no, hunk_header=header, snippet=text),
                )
            )
        return findings

    def _broad_except(self, ctx: EvaluationContext, diff_file, added) -> list[Finding]:
        content = ctx.changed_files.get(diff_file.path) or {}
        if content.get("after") is not None:
            after_tree = pyast.parse(content.get("after"))
            if after_tree is not None:
                before_tree = pyast.parse(content.get("before")) if content.get("before") else None
                before_count = len(pyast.broad_except_pass(before_tree)) if before_tree is not None else 0
                lines = pyast.broad_except_pass(after_tree)
                if len(lines) <= before_count:
                    return []
                line = lines[-1]
                return [
                    _finding(
                        "ES-002",
                        "error_suppression",
                        "medium",
                        "high",
                        "Broad exception silently swallowed",
                        "A bare or broad except that only passes hides failures from tests and callers.",
                        evidence_ref(file=diff_file.path, line=line, snippet=pyast.line_text(content.get("after"), line)),
                    )
                ]
        for index, (line_no, text, header) in enumerate(added):
            match_inline = _BROAD_EXCEPT.search(text) and text.rstrip().split("#")[0].rstrip().endswith("pass")
            follows_pass = (
                _BROAD_EXCEPT.search(text)
                and index + 1 < len(added)
                and _PASS.search(added[index + 1][1])
            )
            if match_inline or follows_pass or _SUPPRESS_CTX.search(text):
                return [
                    _finding(
                        "ES-002",
                        "error_suppression",
                        "medium",
                        "medium",
                        "Broad exception silently swallowed",
                        "A bare or broad except that only passes hides failures from tests and callers.",
                        evidence_ref(file=diff_file.path, line=line_no, hunk_header=header, snippet=text),
                    )
                ]
        return []
