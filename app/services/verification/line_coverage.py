"""Changed-line coverage (v1.6.2 D27): were the lines the agent changed executed by the platform's tests?

Only lines coverage.py counts as executable are measured. A low number is weak evidence, not proof of cheating.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

WORKSPACE_PREFIX = "/workspace/src/"
MAX_UNEXECUTED = 200
UNAVAILABLE: dict[str, Any] = {"status": "unavailable"}


def _files(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    files = report.get("files")
    if not isinstance(files, dict):
        return {}
    return {str(path).removeprefix(WORKSPACE_PREFIX).removeprefix("./"): data for path, data in files.items() if isinstance(data, dict)}


def _line_set(values: Any) -> set[int]:
    return {item for item in values if isinstance(item, int)} if isinstance(values, list) else set()


def changed_line_coverage(changed: Mapping[str, tuple[int, ...]], content: bytes | None) -> dict[str, Any]:
    if content is None:
        return dict(UNAVAILABLE)
    try:
        report = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return dict(UNAVAILABLE)
    if not isinstance(report, dict):
        return dict(UNAVAILABLE)
    measured = _files(report)
    total = executed = 0
    unexecuted: list[dict[str, Any]] = []
    unmeasured: list[str] = []
    for path, lines in sorted(changed.items()):
        data = measured.get(path)
        if data is None:
            unmeasured.append(path)
            continue
        ran, missing = _line_set(data.get("executed_lines")), _line_set(data.get("missing_lines"))
        for line in lines:
            if line in ran:
                total, executed = total + 1, executed + 1
            elif line in missing:
                total += 1
                if len(unexecuted) < MAX_UNEXECUTED:
                    unexecuted.append({"path": path, "line": line})
    return {
        "status": "measured",
        "changed_lines_total": total,
        "changed_lines_executed": executed,
        "ratio": round(executed / total, 4) if total else None,
        "unexecuted": unexecuted,
        "unmeasured_files": unmeasured,
    }
