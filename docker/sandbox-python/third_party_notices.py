"""Write the third-party notices of an image and refuse a strong copyleft Python package (v1.6.2).

Usage, as root during the image build, with the interpreter whose packages ship in the image:
    python third_party_notices.py --image <name> --out /licenses/THIRD_PARTY_NOTICES.txt

The file lists every Debian package (version, source package, licenses named in its copyright file) and every Python
package (version, license), holds the full license and NOTICE texts of the Python packages, and says where to obtain
the source code that GPL, LGPL and MPL ask for. Standard library only: both images use it, and neither has pip.

The build fails when a Python package declares a strong copyleft license (GPL, AGPL, SSPL), a weak copyleft license
that is not on WEAK_COPYLEFT_ALLOWED, or no license at all, so a new dependency is reviewed before it ships.
"""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import os
import re
import sys

DPKG_STATUS = "/var/lib/dpkg/status"
DOC_ROOT = "/usr/share/doc"
PYTHON_LICENSE = "/usr/local/lib/python3.12/LICENSE.txt"
STRONG_COPYLEFT = re.compile(r"\b(?:AGPL|SSPL)|(?<![LA])GPL\b|GNU (?:Affero )?General Public License", re.I)
WEAK_COPYLEFT = re.compile(r"\b(?:LGPL|MPL|EPL|CDDL|EUPL)\b|Mozilla Public License|Lesser General Public License", re.I)
# Weak copyleft packages that were reviewed: their source is linked below, and their license allows this use.
WEAK_COPYLEFT_ALLOWED = frozenset({"certifi", "orjson"})
LICENSE_FILE = re.compile(r"(?:^|/)(?:LICEN[CS]E|COPYING|NOTICE)[^/]*$", re.I)


def debian_packages(status_text: str) -> list[dict[str, str]]:
    """Installed packages from the dpkg status file: name, version and source package."""
    packages = []
    for block in status_text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line and not line[0].isspace() and ":" in line:
                key, _, value = line.partition(":")
                fields[key] = value.strip()
        if fields.get("Package") and "installed" in fields.get("Status", "").split():
            source = fields.get("Source", fields["Package"]).split(" ")[0]
            packages.append({"name": fields["Package"], "version": fields.get("Version", ""), "source": source})
    return sorted(packages, key=lambda item: item["name"])


def copyright_licenses(text: str) -> list[str]:
    """License names of a machine-readable (DEP-5) copyright file; empty for a free-form file."""
    return sorted({value.strip() for value in re.findall(r"^License:\s*(\S.*)$", text, re.M)})


def python_license(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return expression
    classifiers = [item.split(" :: ")[-1] for item in (meta.get_all("Classifier") or []) if item.startswith("License ::")]
    if classifiers:
        return " / ".join(classifiers)
    field = (meta.get("License") or "").strip().splitlines()
    return field[0][:120] if field and field[0].upper() not in {"", "UNKNOWN"} else ""


def license_problem(name: str, license_text: str) -> str:
    """Why a Python package may not ship, or an empty string when it may."""
    if not license_text:
        return f"{name}: declares no license"
    weak = bool(WEAK_COPYLEFT.search(license_text))
    if STRONG_COPYLEFT.search(WEAK_COPYLEFT.sub("", license_text)):
        return f"{name}: strong copyleft license ({license_text})"
    if weak and name.lower() not in WEAK_COPYLEFT_ALLOWED:
        return f"{name}: weak copyleft license not reviewed yet ({license_text}); add it to WEAK_COPYLEFT_ALLOWED after review"
    return ""


def license_texts(dist: metadata.Distribution) -> list[tuple[str, str]]:
    texts = []
    for item in sorted(dist.files or [], key=str):
        if LICENSE_FILE.search(str(item)):
            path = dist.locate_file(item)
            try:
                texts.append((str(item), open(path, encoding="utf-8", errors="replace").read().strip()))
            except OSError:
                continue
    return texts


def render(image: str, debian: list[dict[str, str]], debian_licenses: dict[str, list[str]], python: list[dict]) -> str:
    weak = [item for item in python if WEAK_COPYLEFT.search(item["license"])]
    lines = [
        f"THIRD-PARTY SOFTWARE NOTICES: {image}",
        "",
        "This image contains open-source software from other projects. Each component keeps its own license:",
        f"- {len(debian)} Debian packages. Each package's copyright and license text is at {DOC_ROOT}/<package>/copyright.",
        f"- The Python interpreter. License: {PYTHON_LICENSE} (Python Software Foundation License).",
        f"- {len(python)} Python packages. Their license and NOTICE texts are reproduced at the end of this file.",
        "This project itself is licensed under the Apache License 2.0 (https://github.com/leo459028/RewardHacking).",
        "",
        "SOURCE CODE",
        "- Debian packages under the GPL, LGPL or other licenses that ask for source code: the source of the exact version",
        "  listed below is kept by Debian at https://snapshot.debian.org/ (look up the source package and version).",
    ]
    for item in weak:
        lines.append(f"- {item['name']} {item['version']} ({item['license']}): https://pypi.org/project/{item['name']}/{item['version']}/#files")
    lines += ["- If you cannot obtain a source this way, open an issue at https://github.com/leo459028/RewardHacking/issues.", ""]
    lines += [f"DEBIAN PACKAGES ({len(debian)})", ""]
    for item in debian:
        named = ", ".join(debian_licenses.get(item["name"]) or ["see the copyright file"])
        lines.append(f"{item['name']} {item['version']} (source: {item['source']}) - {named}")
    lines += ["", f"PYTHON PACKAGES ({len(python)})", ""]
    lines += [f"{item['name']} {item['version']} - {item['license']}" for item in python]
    lines += ["", "LICENSE AND NOTICE TEXTS OF THE PYTHON PACKAGES", ""]
    for item in python:
        for path, text in item["texts"]:
            lines += [f"===== {item['name']} {item['version']}: {path} =====", text, ""]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    debian = debian_packages(open(DPKG_STATUS, encoding="utf-8", errors="replace").read())
    debian_licenses = {}
    for item in debian:
        path = os.path.join(DOC_ROOT, item["name"].split(":")[0], "copyright")
        if not os.path.exists(path):
            print(f"third-party notices: {item['name']} has no copyright file", file=sys.stderr)
            return 1
        debian_licenses[item["name"]] = copyright_licenses(open(path, encoding="utf-8", errors="replace").read())

    python, problems = [], []
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name, declared = dist.metadata["Name"], python_license(dist)
        texts = license_texts(dist)
        problem = license_problem(name, declared) or ("" if texts else f"{name}: ships no license file")
        if problem:
            problems.append(problem)
        python.append({"name": name, "version": dist.version, "license": declared, "texts": texts})
    if problems:
        print("third-party notices: these Python packages may not ship:", *problems, sep="\n  ", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render(args.image, debian, debian_licenses, python))
    os.chmod(args.out, 0o644)
    print(f"third-party notices: {len(debian)} Debian and {len(python)} Python packages written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
