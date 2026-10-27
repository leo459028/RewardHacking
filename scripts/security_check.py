#!/usr/bin/env python3
"""Security and secret leakage scanning script.

Scans git commit history and working tree files for exposed API keys,
passwords, private keys, and cloud credentials.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# High-confidence patterns for sensitive credentials
SECRET_PATTERNS = [
    (r"sk-[a-zA-Z0-9]{20,}", "OpenAI API Key"),
    (r"ghp_[a-zA-Z0-9]{36,}", "GitHub Personal Access Token"),
    (r"github_pat_[a-zA-Z0-9]{22}_[a-zA-Z0-9]{59}", "GitHub Fine-Grained Token"),
    (r"AIza[0-9A-Za-z\-_]{35}", "Google API Key"),
    (r"AKIA[0-9A-Z]{16}", "AWS Access Key ID"),
    (r"-----BEGIN (RSA|EC|OPENSSH|DSA|PGP) PRIVATE KEY-----", "Private Key"),
    (r"(?i)(password|secret|passwd)\s*[:=]\s*['\"][^'\"]{8,}['\"]", "Hardcoded Password/Secret"),
]

# Allowlisted substrings known to be benign mock data or tests
ALLOWLIST_PATTERNS = [
    r"demo-key",
    r"admin-key",
    r"key-a",
    r"operator-key",
    r"viewer-key",
    r"super-key",
    r"secret_rotation",
    r"DASHBOARD_PASSWORD",
    r"operator:secret",
    r"admin:admin",
    r"app:app@localhost",
    r"dummy",
    r"mock",
]


def check_git_history() -> list[str]:
    """Scan git commit history patches for leaked secrets."""
    findings = []
    try:
        result = subprocess.run(
            ["git", "log", "-p", "--all"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
        if result.returncode != 0:
            return [f"Failed to execute git log: {result.stderr}"]

        lines = result.stdout.splitlines()
        current_commit = "unknown"
        for line in lines:
            if line.startswith("commit "):
                current_commit = line.split()[1][:8]
            if not line.startswith("+") or line.startswith("+++"):
                continue

            content = line[1:].strip()
            # Skip if matched allowlist
            if any(re.search(allow, content) for allow in ALLOWLIST_PATTERNS):
                continue

            for pattern, name in SECRET_PATTERNS:
                if re.search(pattern, content):
                    findings.append(f"[Commit {current_commit}] Possible {name}: {content[:60]}...")
    except Exception as exc:
        findings.append(f"Git history scan error: {exc}")
    return findings


def check_working_tree() -> list[str]:
    """Scan all tracked files in working tree."""
    findings = []
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode != 0:
            return [f"Failed to list git files: {result.stderr}"]

        tracked_files = [Path(p.strip()) for p in result.stdout.splitlines() if p.strip()]
        for file_path in tracked_files:
            if not file_path.is_file() or file_path.suffix in [".png", ".jpg", ".pyc"]:
                continue
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                for line_no, line in enumerate(content.splitlines(), start=1):
                    # Skip allowlisted
                    if any(re.search(allow, line) for allow in ALLOWLIST_PATTERNS):
                        continue
                    for pattern, name in SECRET_PATTERNS:
                        if re.search(pattern, line):
                            findings.append(f"[{file_path}:{line_no}] Possible {name}: {line.strip()[:60]}...")
            except Exception:
                continue
    except Exception as exc:
        findings.append(f"Working tree scan error: {exc}")
    return findings


def main() -> int:
    print("Running RewardHacking Security & Secret Scan (v1.5.3)...")
    git_findings = check_git_history()
    tree_findings = check_working_tree()

    all_findings = git_findings + tree_findings
    if all_findings:
        print(f"\n[!] Found {len(all_findings)} potential secret leakage(s):")
        for finding in all_findings:
            print(f"  - {finding}")
        return 1

    print("[OK] No sensitive API keys, secrets, or credentials found in git history or working tree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
