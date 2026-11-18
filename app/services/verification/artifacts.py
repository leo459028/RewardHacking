"""Suite snapshots (v1.6.2 D22, D23): validate an uploaded ``tar.gz`` and rewrite it as the platform's own tar.

Nothing is unpacked on disk. Only regular files and directories pass, and the sandbox only ever receives the normalized
tar, whose sha256 is the artifact digest: sorted entries, fixed time, owner and modes.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import re
import tarfile
import zlib
from dataclasses import dataclass
from typing import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sandbox import SandboxArtifact
from app.services.verification.errors import VerificationError

MAX_FILES = 5000
MAX_PATH_CHARS = 255
RESERVED_TOP_LEVEL = "out"  # the sandbox writes its reports to /workspace/out
_HEADER_ALLOWANCE = 2048  # tar headers per member, on top of the file content
_DRIVE_PATH = re.compile(r"^[A-Za-z]:")
_FILTER_ROOT = "/rh-artifact-check"
_REGULAR_TYPES = (tarfile.REGTYPE, tarfile.AREGTYPE)


@dataclass(frozen=True)
class NormalizedArtifact:
    content: bytes
    digest: str
    file_count: int
    paths: tuple[str, ...]


class _BoundedReader:
    """Stops a decompression bomb before ``tarfile`` buffers it."""

    def __init__(self, raw: io.BufferedIOBase, limit: int) -> None:
        self._raw = raw
        self._left = limit

    def read(self, size: int = -1) -> bytes:
        chunk = self._raw.read(size)
        self._left -= len(chunk)
        if self._left < 0:
            raise _too_large()
        return chunk


def _too_large() -> VerificationError:
    return VerificationError("artifact_too_large", "The snapshot is larger than SANDBOX_ARTIFACT_MAX_BYTES.", 413)


def _reject(code: str, name: str) -> VerificationError:
    return VerificationError(code, f"Rejected archive entry: {name[:200]!r}.")


def _member_path(member: tarfile.TarInfo) -> str:
    """The clean relative path of an accepted entry; every refusal has its own code (D23)."""
    name = member.name
    if member.issym():
        raise _reject("artifact_symlink_rejected", name)
    if member.islnk():
        raise _reject("artifact_hardlink_rejected", name)
    if not member.isdir() and member.type not in _REGULAR_TYPES:
        raise _reject("artifact_special_file_rejected", name)
    if name.startswith("/") or _DRIVE_PATH.match(name):
        raise _reject("artifact_absolute_path_rejected", name)
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if "\\" in name or "\x00" in name or ".." in parts:
        raise _reject("artifact_path_traversal_rejected", name)
    path = "/".join(parts)
    if len(path) > MAX_PATH_CHARS:
        raise _reject("artifact_path_too_long", name)
    if parts and parts[0] == RESERVED_TOP_LEVEL:
        raise VerificationError("artifact_reserved_path", "The top-level out/ directory is reserved for sandbox reports.")
    try:
        tarfile.data_filter(member, _FILTER_ROOT)  # the standard library's rules, as a second layer
    except tarfile.FilterError as exc:
        raise _reject("artifact_invalid_archive", name) from exc
    return path


def _read_members(archive: tarfile.TarFile, max_bytes: int) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    directories: set[str] = set()
    total = 0
    for member in archive:
        path = _member_path(member)
        if not path:
            continue
        if member.isdir():
            directories.add(path)
            continue
        if path in files:
            raise _reject("artifact_duplicate_path", path)
        if len(files) >= MAX_FILES:
            raise VerificationError("artifact_too_many_files", f"A snapshot may hold at most {MAX_FILES} files.")
        total += member.size
        if total > max_bytes:
            raise _too_large()
        files[path] = archive.extractfile(member).read()
    for path in files:
        if path in directories or any("/".join(path.split("/")[:depth]) in files for depth in range(1, path.count("/") + 1)):
            raise _reject("artifact_path_conflict", path)
    return files


def build_tar(files: Mapping[str, bytes], *, prefix: str = "") -> bytes:
    """A reproducible tar of ``files``, optionally below the directory ``prefix``."""
    names = {f"{prefix}/{path}" if prefix else path: content for path, content in files.items()}
    directories = {prefix} if prefix else set()
    for name in names:
        parts = name.split("/")
        directories.update("/".join(parts[:depth]) for depth in range(1, len(parts)))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as output:
        for name in sorted(directories | set(names)):
            info = tarfile.TarInfo(name)
            info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
            if name in names:
                info.mode, info.size = 0o644, len(names[name])
                output.addfile(info, io.BytesIO(names[name]))
            else:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                output.addfile(info)
    return buffer.getvalue()


def read_tar(content: bytes) -> dict[str, bytes]:
    """The files of a normalized artifact."""
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
        return {member.name: archive.extractfile(member).read() for member in archive.getmembers() if member.isfile()}


def normalize_upload(data: bytes, *, max_bytes: int) -> NormalizedArtifact:
    if len(data) > max_bytes:
        raise _too_large()
    limit = max_bytes + (MAX_FILES + 16) * _HEADER_ALLOWANCE
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as raw:
            with tarfile.open(fileobj=_BoundedReader(raw, limit), mode="r|") as archive:
                files = _read_members(archive, max_bytes)
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, OSError, zlib.error) as exc:
        raise VerificationError("artifact_invalid_archive", "The snapshot is not a readable tar.gz archive.") from exc
    if not files:
        raise VerificationError("artifact_empty", "The snapshot holds no files.")
    content = build_tar(files)
    if len(content) > max_bytes:
        raise _too_large()
    return NormalizedArtifact(content, hashlib.sha256(content).hexdigest(), len(files), tuple(sorted(files)))


async def store_artifact(session: AsyncSession, artifact: NormalizedArtifact) -> None:
    """Identical content is stored once. Joins the caller's transaction."""
    if await session.get(SandboxArtifact, artifact.digest) is None:
        session.add(SandboxArtifact(sha256=artifact.digest, content=artifact.content, bytes=len(artifact.content)))
        await session.flush()


async def load_artifact(session: AsyncSession, digest: str) -> bytes | None:
    row = await session.get(SandboxArtifact, digest)
    return bytes(row.content) if row is not None else None
