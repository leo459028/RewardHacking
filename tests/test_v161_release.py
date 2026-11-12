"""v1.6.1 P8: release anchors and the browser E2E coverage of the release (no browser is started here).

The exact version anchors live in the latest release test (earlier ones keep series checks). The E2E script must cover
the v1.6.1 scenarios, sign in only with credentials from the environment, and refuse a session-mode stack without them.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.main import app
from scripts import browser_manual_test

ROOT = Path(__file__).resolve().parents[1]
BROWSER = (ROOT / "scripts" / "browser_manual_test.py").read_text(encoding="utf-8")


def test_v161_release_version_anchors():
    from app.services.finops import FINOPS_CALCULATION_VERSION

    assert app.version == "1.6.1" and FINOPS_CALCULATION_VERSION == "v1.6.1"
    assert 'version = "1.6.1"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "(v1.6.1)" in (ROOT / "scripts" / "security_check.py").read_text(encoding="utf-8")
    assert 'value="v1.6.1"' in (ROOT / "app" / "ui" / "templates" / "operations.html").read_text(encoding="utf-8")
    assert 'release_version", "v1.6.1"' in (ROOT / "app" / "ui" / "routers" / "operations.py").read_text(encoding="utf-8")
    assert 'assert release_ver == "v1.6.1"' in BROWSER


def test_browser_suite_covers_the_v161_scenarios():
    for marker in ("Scenario 2c", "Scenario 12.1", "Scenario 12.2", "Scenario 13", "Scenario 14", "Scenario 15"):
        assert f"[{marker}]" in BROWSER, marker
    for route in ("/dashboard/admin/users", "/dashboard/change-requests", "/dashboard/audit-chain"):
        assert f'"{route}",' in BROWSER, route
    nav_block = BROWSER.split("nav_routes = [", 1)[1].split("]", 1)[0]
    assert len(re.findall(r'"/dashboard[^"]*"', nav_block)) == 21  # 18 primary views + Users, Change Requests, Audit Chain


def test_browser_suite_reads_credentials_from_the_environment_only(monkeypatch):
    assert not re.search(r"password\s*=\s*[\"'][^\"']{8,}", BROWSER, re.IGNORECASE)
    for key in ("ADMIN", "ADMIN2", "AUDITOR"):
        monkeypatch.delenv(f"E2E_{key}_USER", raising=False)
        monkeypatch.delenv(f"E2E_{key}_PASSWORD", raising=False)
    assert browser_manual_test.e2e_credentials() == {}
    monkeypatch.setenv("E2E_ADMIN_USER", "qa-admin")
    monkeypatch.setenv("E2E_ADMIN_PASSWORD", "x" * 12)
    monkeypatch.setenv("E2E_AUDITOR_USER", "qa-auditor")  # no password: not usable
    assert browser_manual_test.e2e_credentials() == {"admin": ("qa-admin", "x" * 12)}


def test_browser_suite_refuses_session_mode_without_credentials_and_lists_skips():
    assert "Dashboard sign-in is on (session mode) but E2E credentials are missing" in BROWSER
    assert "Skipped (sign-in off)" in BROWSER
    # an unexpected redirect to /dashboard/reauth fails the run instead of passing silently
    assert "unexpected_reauth" in BROWSER.split("if console_errors or page_errors or failed_requests", 1)[1].split("\n", 1)[0]
