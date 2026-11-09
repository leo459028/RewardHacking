"""Stage 2 release gate runner (v1.6.0 D32; AGENTS.md section 3).

Usage:
    python scripts/release_gate.py --out <summary.md>                 # full gate
    python scripts/release_gate.py --skip-docker --skip-e2e --out ... # automated suite only
    python scripts/release_gate.py --list                             # print the steps, run nothing

Runs every step in order, records command, exit code, duration and the tail of the output, and extracts the pytest
pass / fail counts and the Alembic head. ``--out`` writes a Markdown summary to paste (after review) at the top of
``docs/test_report.md``; the script never edits the test report itself, so only actual results get recorded.
Docker must already be running for the Docker / E2E steps. Exit code is non-zero when any step failed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAIL_LINES = 15


@dataclass(frozen=True)
class Step:
    key: str
    group: str  # core | docker | e2e
    cmd: tuple[str, ...]
    timeout: int = 900


@dataclass
class Result:
    step: Step
    code: int
    seconds: float
    tail: list[str] = field(default_factory=list)
    facts: dict[str, str] = field(default_factory=dict)


PY = sys.executable
STEPS: tuple[Step, ...] = (
    Step("version_anchors", "core", (PY, "scripts/bump_version.py", "--check"), 60),
    Step("secret_scan", "core", (PY, "scripts/security_check.py"), 600),
    Step("ruff", "core", ("ruff", "check", "app", "tests", "scripts"), 300),
    Step("import_contracts", "core", ("lint-imports",), 300),
    Step("diff_check", "core", ("git", "diff", "--check"), 60),
    Step("pytest_full", "core", (PY, "-m", "pytest", "-q", "-W", "error::DeprecationWarning"), 1800),
    Step("docker_build", "docker", ("docker", "compose", "up", "-d", "--build"), 1800),
    Step("docker_ps", "docker", ("docker", "compose", "ps"), 120),
    Step("alembic_upgrade", "docker", ("docker", "compose", "exec", "-T", "api", "alembic", "upgrade", "head"), 300),
    Step("alembic_current", "docker", ("docker", "compose", "exec", "-T", "api", "alembic", "current"), 120),
    Step("browser_e2e", "e2e", (PY, "scripts/browser_manual_test.py"), 1800),
)

_HEAD_RE = re.compile(r"^(\d{12})\b.*\(head\)", re.M)
_CONTRACTS_RE = re.compile(r"Contracts: (\d+) kept, (\d+) broken")


def extract_facts(step: Step, output: str) -> dict[str, str]:
    facts: dict[str, str] = {}
    if step.key == "pytest_full":
        summary = [line for line in output.splitlines() if " in " in line and ("passed" in line or "failed" in line)]
        if summary:
            facts["summary"] = summary[-1].strip("= ").strip()
            passed = re.search(r"(\d+) passed", summary[-1])
            failed = re.search(r"(\d+) failed", summary[-1])
            facts["passed"] = passed.group(1) if passed else "0"
            facts["failed"] = failed.group(1) if failed else "0"
    elif step.key == "alembic_current":
        head = _HEAD_RE.search(output)
        if head:
            facts["head"] = head.group(1)
    elif step.key == "import_contracts":
        match = _CONTRACTS_RE.search(output)
        if match:
            facts["kept"], facts["broken"] = match.group(1), match.group(2)
    elif step.key == "version_anchors":
        match = re.search(r"OK: all (\d+) anchors are (\S+)", output)
        if match:
            facts["anchors"], facts["version"] = match.group(1), match.group(2)
    return facts


def select_steps(skip_docker: bool, skip_e2e: bool) -> list[Step]:
    skipped = {"docker"} if skip_docker else set()
    if skip_e2e or skip_docker:
        skipped.add("e2e")  # E2E needs the Docker stack
    return [step for step in STEPS if step.group not in skipped]


def run_step(step: Step, cwd: Path = ROOT) -> Result:
    print(f"==> {step.key}: {' '.join(step.cmd)}", flush=True)
    started = time.perf_counter()
    try:
        proc = subprocess.run(step.cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=step.timeout)
        output, code = (proc.stdout or "") + (proc.stderr or ""), proc.returncode
    except subprocess.TimeoutExpired:
        output, code = f"timed out after {step.timeout}s", 124
    except FileNotFoundError as exc:
        output, code = f"command not found: {exc}", 127
    seconds = time.perf_counter() - started
    result = Result(step, code, seconds, output.splitlines()[-TAIL_LINES:], extract_facts(step, output))
    print(f"    {'ok' if code == 0 else 'FAILED'} (exit {code}, {seconds:.1f}s) {result.facts or ''}", flush=True)
    return result


def render(results: list[Result], skipped: list[Step]) -> str:
    ok = all(r.code == 0 for r in results)
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    lines = [
        f"## Release gate run: {'PASSED' if ok else 'FAILED'} ({when})",
        "",
        f"- Branch / commit: `{branch}` @ `{commit}`",
        "",
        "| Step | Command | Exit | Seconds | Facts |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for r in results:
        facts = ", ".join(f"{k}={v}" for k, v in r.facts.items())
        lines.append(f"| {r.step.key} | `{' '.join(r.step.cmd[1:] if r.step.cmd[0] == PY else r.step.cmd)}` | {r.code} | {r.seconds:.1f} | {facts} |")
    for step in skipped:
        lines.append(f"| {step.key} | skipped | - | - | - |")
    failed = [r for r in results if r.code != 0]
    for r in failed:
        lines += ["", f"### {r.step.key} output (last {TAIL_LINES} lines)", "", "```text", *r.tail, "```"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-docker", action="store_true", help="skip Docker and E2E steps")
    parser.add_argument("--skip-e2e", action="store_true", help="skip the browser E2E step")
    parser.add_argument("--keep-going", action="store_true", help="run later steps even after a failure")
    parser.add_argument("--list", action="store_true", help="print the steps and exit")
    parser.add_argument("--out", help="write the Markdown summary to this path")
    args = parser.parse_args(argv)
    steps = select_steps(args.skip_docker, args.skip_e2e)
    skipped = [step for step in STEPS if step not in steps]
    if args.list:
        for step in STEPS:
            mark = "skip" if step in skipped else "run "
            print(f"{mark} {step.group:<6} {step.key:<17} {' '.join(step.cmd)}")
        return 0
    results: list[Result] = []
    for step in steps:
        result = run_step(step)
        results.append(result)
        if result.code != 0 and not args.keep_going:
            skipped += [s for s in steps if s not in [r.step for r in results]]
            break
    summary = render(results, skipped)
    if args.out:
        Path(args.out).write_text(summary, encoding="utf-8", newline="\n")
        print(f"Summary written to {args.out}")
    else:
        print(summary)
    return 0 if all(r.code == 0 for r in results) and len(results) == len(steps) else 1


if __name__ == "__main__":
    raise SystemExit(main())
