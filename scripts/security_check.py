#!/usr/bin/env python3
"""Security and secret leakage scanning script.

Scans git commit history and working tree files for exposed API keys,
passwords, private keys, and cloud credentials.
"""

from __future__ import annotations

import hashlib
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

PASSWORD_RULE = "Hardcoded Password/Secret"

# Substrings of benign mock data. They only excuse the password rule, which is the one that guesses: a line with a
# real token or private key is reported even when it also says "mock".
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
    # v1.5.7 MCP redaction test fixture (fake, non-functional value asserted to be redacted)
    r"sk-secret1234567890",
    # v1.5.8 outcome-evidence redaction test fixture (fake, non-functional value asserted to be redacted)
    r"sk-abcdefghijklmnop1234",
    # v1.5.12 Dashboard editing-gate test fixture (fake, non-default Basic Auth credentials)
    r"s3cret-pass",
]


# Fake credentials reviewed line by line: sha256 of the whole stripped line. Git history cannot be rewritten, so lines
# that only exist in old commits stay listed. Changing such a line needs a new review and a new digest.
KNOWN_FIXTURE_LINES = {
    "1ca668e96f1edf7fd3ff5b085559d88c472e9bd313959ad1132ea80211e803e4": "docs/test_report.md: names the fake redaction fixture",
    "85924b7c656125faf01563b8b1c365be0d6d141b985e9408419d79c1362bf264": "scripts/security_check.py (history): earlier allowlist entry for the header-only key fixture",
    "671670754679724e9b7cd8b1163b54d73b11f757ec57fe979c4e62506a82f956": "scripts/security_check.py: allowlist entry of the fake redaction fixture",
    "95d66fb862655c4f4774ba0688491b9e3c281ce7898ce7e1f7dc8236d14b6df4": "tests/test_v158_outcome_evidence_review_queue.py: asserts the fake key was redacted",
    "28691f4da4780b320174b9ccdbfc7b5b67d63fb0446ad80bf5880b94b944b5a3": "tests/test_v158_outcome_evidence_review_queue.py: fake key in a redaction diff",
    "366a9f569d2506d31652cda2a5ad3e102e233bd85d11e83841804e3c8068a756": "tests/test_v162_image_hardening.py (history): fake token and access key fixture",
    "b6eb6ee6d3acd9ba6dcf7397d15ccae4bab7f7c260c87d1159a1825460e44b1b": "tests/test_v162_image_hardening.py: header-only private key fixture",
}


def line_findings(line: str) -> list[str]:
    """Names of the secret rules this line breaks."""
    text = line.strip()
    if hashlib.sha256(text.encode("utf-8")).hexdigest() in KNOWN_FIXTURE_LINES:
        return []
    excused = any(re.search(allow, text) for allow in ALLOWLIST_PATTERNS)
    return [name for pattern, name in SECRET_PATTERNS if re.search(pattern, text) and not (excused and name == PASSWORD_RULE)]


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
            for name in line_findings(content):
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
                    for name in line_findings(line):
                        findings.append(f"[{file_path}:{line_no}] Possible {name}: {line.strip()[:60]}...")
            except Exception:
                continue
    except Exception as exc:
        findings.append(f"Working tree scan error: {exc}")
    return findings


def main() -> int:
    print("Running RewardHacking Security & Secret Scan (v1.6.3)...")
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
