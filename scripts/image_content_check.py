"""Scan a built container image for content that must never ship (v1.6.2 D5).

Usage:
    python scripts/image_content_check.py --image rewardhacking-api:latest
    python scripts/image_content_check.py --tar image.tar     # an existing `docker save` archive

The layers are read from a `docker save` archive; nothing is extracted to disk or executed. The result is one JSON line
on stdout. Exit codes: 0 no findings, 1 findings, 2 the image could not be read.

Rules:
    secret_file      private key material, .env files or .git, in any layer
    secret_content   text under /app that matches the rules of scripts/security_check.py, in any layer
    app_tests        /app/tests in the final filesystem
    app_config       anything in /app/config except *.example.yaml in the final filesystem
    root_user        the image config runs as root (or names no user)
    compiler         /usr/bin/gcc in the final filesystem

A file deleted by a later layer stays readable in the earlier layer, so the two secret rules look at every layer and
mark such a file with ``deleted_later``. Findings name the file and the rule, never the matched text.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import posixpath
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from security_check import line_findings  # noqa: E402

SECRET_NAME_PATTERNS = ("*.pem", "*.key", "*.p12", "id_rsa*", ".env*")
# Public CA bundles shipped by the base image and by certifi. They are checked for private keys instead.
PUBLIC_CA_PREFIXES = ("/etc/ssl/certs/", "/usr/share/ca-certificates/")
PUBLIC_CA_SUFFIXES = ("/certifi/cacert.pem",)
PUBLIC_CA_FILES = ("/usr/lib/ssl/cert.pem",)  # Debian's OpenSSL link to the system CA bundle
PRIVATE_KEY_MARKER = b"PRIVATE KEY-----"
MAX_TEXT_BYTES = 2 * 1024 * 1024
WHITEOUT_PREFIX = ".wh."
OPAQUE_MARKER = ".wh..wh..opq"


class ImageReadError(Exception):
    """The archive is missing, malformed or uses an unsupported layout."""


@dataclass(frozen=True)
class Entry:
    path: str
    layer: int
    is_dir: bool
    content: bytes | None = None


def normalize(name: str) -> str:
    return posixpath.normpath("/" + name.lstrip("./"))


def is_secret_name(path: str) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_NAME_PATTERNS)


def is_public_ca(path: str) -> bool:
    return path in PUBLIC_CA_FILES or path.startswith(PUBLIC_CA_PREFIXES) or path.endswith(PUBLIC_CA_SUFFIXES)


def _keeps_content(path: str) -> bool:
    return path.startswith("/app/") or is_secret_name(path)


def _read_layer(handle: Any, layer: int) -> tuple[list[Entry], list[str], list[str]]:
    entries: list[Entry] = []
    whiteouts: list[str] = []
    opaque: list[str] = []
    with tarfile.open(fileobj=handle, mode="r|*") as layer_tar:
        for member in layer_tar:
            path = normalize(member.name)
            parent, name = posixpath.split(path)
            if name == OPAQUE_MARKER:
                opaque.append(parent)
            elif name.startswith(WHITEOUT_PREFIX):
                whiteouts.append(posixpath.join(parent, name[len(WHITEOUT_PREFIX):]))
            else:
                content = None
                if member.isreg() and member.size <= MAX_TEXT_BYTES and _keeps_content(path):
                    extracted = layer_tar.extractfile(member)
                    content = extracted.read() if extracted else None
                entries.append(Entry(path, layer, member.isdir(), content))
    return entries, whiteouts, opaque


def _remove_tree(view: dict[str, Entry], path: str, keep_root: bool) -> None:
    prefix = path.rstrip("/") + "/"
    for existing in [p for p in view if p.startswith(prefix) or (not keep_root and p == path)]:
        del view[existing]


def read_image(archive: Path) -> tuple[dict[str, Any], list[Entry], dict[str, Entry]]:
    """Return the image config, every entry of every layer, and the final filesystem view."""
    try:
        with tarfile.open(archive, "r:*") as outer:
            manifest_file = outer.extractfile("manifest.json")
            if manifest_file is None:
                raise ImageReadError("manifest.json is not a file")
            manifest = json.loads(manifest_file.read())
            if not isinstance(manifest, list) or len(manifest) != 1:
                raise ImageReadError("expected exactly one image in the archive")
            config_file = outer.extractfile(manifest[0]["Config"])
            if config_file is None:
                raise ImageReadError("image config is not a file")
            config = json.loads(config_file.read())

            all_entries: list[Entry] = []
            view: dict[str, Entry] = {}
            for index, layer_name in enumerate(manifest[0]["Layers"]):
                layer_file = outer.extractfile(layer_name)
                if layer_file is None:
                    raise ImageReadError(f"layer {layer_name} is not a file")
                entries, whiteouts, opaque = _read_layer(layer_file, index)
                # Whiteouts hide lower layers only, so they apply before this layer's own files.
                for directory in opaque:
                    _remove_tree(view, directory, keep_root=True)
                for path in whiteouts:
                    _remove_tree(view, path, keep_root=False)
                all_entries.extend(entries)
                view.update({entry.path: entry for entry in entries})
    except (OSError, KeyError, TypeError, ValueError, tarfile.TarError) as exc:
        raise ImageReadError(f"{type(exc).__name__}: {exc}") from exc
    return config, all_entries, view


def _finding(rule: str, path: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"rule": rule, "path": path, "detail": detail, **extra}


def _secret_rule_names(content: bytes) -> list[tuple[int, str]]:
    if b"\0" in content:
        return []
    lines = content.decode("utf-8", errors="ignore").splitlines()
    return [(line_no, name) for line_no, line in enumerate(lines, start=1) for name in line_findings(line)]


def check_secrets(all_entries: list[Entry], view: dict[str, Entry]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for entry in all_entries:
        if entry.is_dir and ".git" not in PurePosixPath(entry.path).parts:
            continue
        deleted_later = view.get(entry.path) is None
        location = {"layer": entry.layer, "deleted_later": deleted_later}
        if ".git" in PurePosixPath(entry.path).parts:
            findings.append(_finding("secret_file", entry.path, "git metadata", **location))
        elif is_secret_name(entry.path):
            if not is_public_ca(entry.path):
                findings.append(_finding("secret_file", entry.path, "secret file name", **location))
            elif entry.content and PRIVATE_KEY_MARKER in entry.content:
                findings.append(_finding("secret_file", entry.path, "private key in a CA bundle path", **location))
        if entry.content and entry.path.startswith("/app/"):
            for line_no, name in _secret_rule_names(entry.content):
                findings.append(_finding("secret_content", entry.path, f"line {line_no}: {name}", **location))
    return findings


def check_final_filesystem(view: dict[str, Entry]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path, entry in sorted(view.items()):
        if path == "/app/tests" or path.startswith("/app/tests/"):
            findings.append(_finding("app_tests", path, "tests must not ship in the image"))
        elif path.startswith("/app/config/") and not entry.is_dir:
            if not fnmatch.fnmatchcase(path, "/app/config/*.example.yaml") or path.count("/") != 3:
                findings.append(_finding("app_config", path, "only *.example.yaml may ship in /app/config"))
        elif path == "/usr/bin/gcc":
            findings.append(_finding("compiler", path, "the final image must not contain a compiler"))
    return findings


def check_user(config: dict[str, Any]) -> list[dict[str, Any]]:
    user = str((config.get("config") or {}).get("User") or "").strip()
    if user.split(":", 1)[0] in {"", "root", "0"}:
        return [_finding("root_user", "image config", f"User is {user or 'not set'}")]
    return []


def scan_archive(archive: Path, image: str = "") -> dict[str, Any]:
    config, all_entries, view = read_image(archive)
    findings = check_secrets(all_entries, view) + check_final_filesystem(view) + check_user(config)
    return {
        "image": image or archive.name,
        "status": "fail" if findings else "pass",
        "layers": len({entry.layer for entry in all_entries}),
        "files": sum(1 for entry in view.values() if not entry.is_dir),
        "user": str((config.get("config") or {}).get("User") or ""),
        "findings": findings,
    }


def scan_image(image: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="rh-image-check-") as tmp:
        archive = Path(tmp) / "image.tar"
        try:
            result = subprocess.run(["docker", "save", "-o", str(archive), image], capture_output=True, text=True)
        except OSError as exc:
            raise ImageReadError(f"docker is not available: {exc}") from exc
        if result.returncode != 0:
            raise ImageReadError(f"docker save failed: {result.stderr.strip()[:300]}")
        return scan_archive(archive, image)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", help="image reference known to the local Docker daemon")
    source.add_argument("--tar", type=Path, help="path of an existing `docker save` archive")
    args = parser.parse_args(argv)
    try:
        report = scan_image(args.image) if args.image else scan_archive(args.tar)
    except ImageReadError as exc:
        print(json.dumps({"image": args.image or str(args.tar), "status": "error", "error": str(exc)}))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
