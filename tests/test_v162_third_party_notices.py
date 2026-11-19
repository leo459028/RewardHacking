"""v1.6.2: third-party notices inside both images and the copyleft gate of the build (no Docker needed)."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docker" / "sandbox-python" / "third_party_notices.py"
_spec = importlib.util.spec_from_file_location("third_party_notices", SCRIPT)
notices = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = notices
_spec.loader.exec_module(notices)

STATUS = """Package: bash
Status: install ok installed
Version: 5.2.37-2+b5

Package: libc6
Status: install ok installed
Source: glibc (2.41-12)
Version: 2.41-12

Package: removed-tool
Status: deinstall ok config-files
Version: 1.0
"""


def test_dpkg_status_lists_installed_packages_with_their_source():
    assert notices.debian_packages(STATUS) == [
        {"name": "bash", "version": "5.2.37-2+b5", "source": "bash"},
        {"name": "libc6", "version": "2.41-12", "source": "glibc"},
    ]


def test_machine_readable_copyright_names_its_licenses():
    text = "Files: *\nLicense: GPL-3+\n\nFiles: doc/*\nLicense: GFDL-NIV-1.3\n\nLicense: GPL-3+\n Full text...\n"
    assert notices.copyright_licenses(text) == ["GFDL-NIV-1.3", "GPL-3+"]
    assert notices.copyright_licenses("Free-form copyright text") == []


@pytest.mark.parametrize("license_text", ["MIT", "BSD-3-Clause", "Apache-2.0 OR BSD-3-Clause", "PSF-2.0", "MIT-0"])
def test_permissive_licenses_ship(license_text):
    assert notices.license_problem("example", license_text) == ""


@pytest.mark.parametrize(
    "license_text",
    ["GPL-3.0-or-later", "AGPL-3.0", "SSPL-1.0", "GNU General Public License v2 (GPLv2)", "MIT OR GPL-2.0"],
)
def test_strong_copyleft_fails_the_build(license_text):
    assert "strong copyleft" in notices.license_problem("example", license_text)


@pytest.mark.parametrize("license_text", ["MPL-2.0", "LGPL-2.1-or-later", "Mozilla Public License 2.0 (MPL 2.0)"])
def test_weak_copyleft_needs_a_review(license_text):
    assert "not reviewed" in notices.license_problem("example", license_text)
    assert notices.license_problem("certifi", license_text) == ""


def test_lgpl_is_not_mistaken_for_gpl():
    assert "strong" not in notices.license_problem("example", "GNU Lesser General Public License v3 (LGPLv3)")


def test_a_package_without_a_license_fails():
    assert "no license" in notices.license_problem("example", "")


def test_the_notices_link_the_source_of_weak_copyleft_packages():
    python = [{"name": "orjson", "version": "3.13.0", "license": "MPL-2.0 AND (Apache-2.0 OR MIT)", "texts": [("LICENSE", "text")]},
              {"name": "pytest", "version": "9.0.3", "license": "MIT", "texts": [("LICENSE", "MIT text")]}]
    debian = [{"name": "bash", "version": "5.2", "source": "bash"}]
    text = notices.render("rewardhacking", debian, {"bash": ["GPL-3+"]}, python)
    assert "https://pypi.org/project/orjson/3.13.0/#files" in text and "pypi.org/project/pytest" not in text
    assert "snapshot.debian.org" in text and "bash 5.2 (source: bash) - GPL-3+" in text
    assert "===== pytest 9.0.3: LICENSE =====\nMIT text" in text


def test_the_generator_uses_only_the_standard_library():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}


@pytest.mark.parametrize(("dockerfile", "python"), [("Dockerfile", "/opt/venv/bin/python"), ("docker/sandbox-python/Dockerfile", "python")])
def test_both_images_write_the_notices_before_dropping_root(dockerfile, python):
    final_stage = (ROOT / dockerfile).read_text(encoding="utf-8").rsplit("\nFROM ", 1)[-1]
    run = final_stage.index(f"{python} /tmp/third_party_notices.py")
    assert run < final_stage.index("USER ") and final_stage.index("pip uninstall -y pip") < run
    assert "--out /licenses/THIRD_PARTY_NOTICES.txt" in final_stage and "rm /tmp/third_party_notices.py" in final_stage
    for label in ("org.opencontainers.image.licenses=\"Apache-2.0\"", "org.opencontainers.image.source=\"https://github.com/leo459028/RewardHacking\"",
                  "/licenses/THIRD_PARTY_NOTICES.txt."):
        assert label in final_stage, label


def test_ci_checks_that_both_images_carry_the_notices():
    steps = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))["jobs"]["image"]["steps"]
    step = next(step for step in steps if step.get("name") == "Third-party notices are in both images")
    assert "rewardhacking:ci" in step["run"] and "rewardhacking-sandbox-python:ci" in step["run"] and "/licenses/THIRD_PARTY_NOTICES.txt" in step["run"]
