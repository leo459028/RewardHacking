"""Hardened Docker backend (v1.6.2 D16-D18). The only module that imports the Docker SDK.

Used only inside ``sandbox-runner``. Each run gets its own named volumes: a seed container that never starts receives
the workspace (``put_archive``), then the real container runs read-only, without network, capabilities or root, and
everything is removed afterwards. Output is read as a stream with hard limits, because the program inside is untrusted.
Files come back through ``get_archive`` and are never unpacked on disk; only regular files are accepted.
"""

from __future__ import annotations

import hashlib
import io
import logging
import tarfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Mapping

from app.services.sandbox import docker_config
from app.services.sandbox.base import BackendCapabilities, ExecutionResult, OutputFile
from app.services.sandbox.spec import WORKSPACE_REF, ExecutionSpec, Mount, spec_digest, validate_spec

logger = logging.getLogger(__name__)

LABEL_SANDBOX = "rh.sandbox"
LABEL_JOB = "rh.job_id"
HOSTNAME = "sandbox"
OUTPUT_DIR = "out"
TMPFS_OPTIONS = "noexec,nosuid,size=64m"
TAR_OVERHEAD_BYTES = 1_048_576  # headers of the output archive, on top of max_output_files_bytes
STREAM_DRAIN_SECONDS = 5
ORPHAN_GRACE_SECONDS = 300


class _BoundedOutput:
    """Hashes everything, keeps at most ``limit`` bytes."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.digest = hashlib.sha256()
        self.kept = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.digest.update(chunk)
        self.total += len(chunk)
        room = self.limit - len(self.kept)
        if room > 0:
            self.kept.extend(chunk[:room])

    @property
    def truncated(self) -> bool:
        return self.total > self.limit

    def text(self) -> str:
        return bytes(self.kept).decode("utf-8", errors="ignore")


class _LimitedStream(io.RawIOBase):
    """File-like view of a chunk iterator that stops at ``limit`` bytes (for ``tarfile`` stream mode)."""

    def __init__(self, chunks: Iterable[bytes], limit: int) -> None:
        self._chunks: Iterator[bytes] = iter(chunks)
        self._buffer = b""
        self._remaining = limit
        self.exceeded = False

    def readable(self) -> bool:
        return True

    def readinto(self, target: Any) -> int:
        while not self._buffer:
            try:
                self._buffer = next(self._chunks)
            except StopIteration:
                return 0
        if self._remaining <= 0:
            self.exceeded = True
            return 0
        size = min(len(target), len(self._buffer), self._remaining)
        target[:size] = self._buffer[:size]
        self._buffer = self._buffer[size:]
        self._remaining -= size
        return size


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_name(exc: BaseException) -> str:
    return type(exc).__name__


def _safe_member_path(name: str) -> str | None:
    path = name.replace("\\", "/").removeprefix("./")
    parts = [part for part in path.split("/") if part]
    if not parts or path.startswith("/") or ".." in parts:
        return None
    return "/".join(parts)


class DockerBackend:
    def __init__(self, settings: Any, *, client: Any = None) -> None:
        self._settings = settings
        self._client = client
        self._caps = docker_config.capabilities(settings)
        self._runtime = docker_config.runtime_of(settings)
        self._user = docker_config.user_of(settings)
        self._allow_image_ids = docker_config.local_image_ids_allowed(settings)
        self._pull = docker_config.pull_policy(settings)
        self._last_host_config: dict[str, Any] = {}

    def capabilities(self) -> BackendCapabilities:
        return self._caps

    def hardening_expectations(self) -> dict[str, str]:
        ids = docker_config.user_ids(self._settings)
        return {
            "uid": str(ids[0]) if ids else "",
            "runtime": self._runtime,
            "isolation_level": self._caps.isolation_level.label,
        }

    def last_host_config(self) -> dict[str, Any]:
        """``HostConfig`` of the latest container, as Docker reports it (conformance evidence)."""
        return dict(self._last_host_config)

    def _docker(self) -> Any:
        if self._client is None:
            import docker  # deferred: only sandbox-runner has the SDK and the socket

            self._client = docker.from_env()
        return self._client

    def server_version(self) -> str:
        return str(self._docker().version().get("Version", ""))

    def run(self, spec: ExecutionSpec, *, artifacts: Mapping[str, bytes] | None = None) -> ExecutionResult:
        return self.run_with_outputs(spec, artifacts=artifacts)[0]

    def run_with_outputs(
        self, spec: ExecutionSpec, *, artifacts: Mapping[str, bytes] | None = None
    ) -> tuple[ExecutionResult, dict[str, bytes]]:
        """Run ``spec`` and return its result plus the content of the files it left in ``/workspace/out``."""
        digest = spec_digest(spec)
        reasons = validate_spec(spec, self._caps, allow_image_ids=self._allow_image_ids)
        if docker_config.user_ids(self._settings) is None:
            reasons.append("docker_user_invalid")
        if not reasons:
            reasons.extend(self._artifact_problems(spec, artifacts or {}))
        if reasons:
            return self._result("rejected", digest, rejection_reasons=tuple(dict.fromkeys(reasons))), {}
        try:
            return self._execute(spec, digest, artifacts or {})
        except Exception as exc:  # noqa: BLE001 - any Docker failure is a result, never a crash of the runner
            logger.warning("sandbox run failed: %s", _error_name(exc))
            return self._result("error", digest, rejection_reasons=(f"docker_error:{_error_name(exc)}",)), {}

    def _result(self, status: str, digest: str, **fields: Any) -> ExecutionResult:
        return ExecutionResult(
            status=status,
            backend_id=self._caps.backend_id,
            backend_version=self._caps.backend_version,
            isolation_level=self._caps.isolation_level.label,
            spec_digest=digest,
            **fields,
        )

    @staticmethod
    def _artifact_problems(spec: ExecutionSpec, artifacts: Mapping[str, bytes]) -> list[str]:
        problems: list[str] = []
        for mount in spec.mounts or ():
            if mount.source_ref == WORKSPACE_REF:
                continue
            content = artifacts.get(mount.source_ref)
            if content is None:
                problems.append("artifact_missing")
            elif hashlib.sha256(content).hexdigest() != mount.source_ref.rsplit(":", 1)[-1]:
                problems.append("artifact_digest_mismatch")
        return problems

    def _image_available(self, image: str) -> bool:
        client = self._docker()
        try:
            client.images.get(image)
            return True
        except Exception as exc:  # noqa: BLE001
            if _error_name(exc) != "ImageNotFound":
                raise
        if self._pull != "missing":
            return False
        client.images.pull(image)
        return True

    def _labels(self, spec: ExecutionSpec) -> dict[str, str]:
        return {LABEL_SANDBOX: "1", LABEL_JOB: str((spec.labels or {}).get("job_id", ""))}

    def container_options(self, spec: ExecutionSpec, volumes: dict[str, dict[str, str]]) -> dict[str, Any]:
        """Every hardening setting of D16 in one place (asserted item by item in the tests)."""
        limits = spec.limits
        return {
            "command": list(spec.argv),
            "working_dir": spec.workdir,
            "environment": dict(spec.env),
            "user": self._user,
            "hostname": HOSTNAME,
            "labels": self._labels(spec),
            "read_only": True,
            "network_mode": "none",
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "pids_limit": limits.pids,
            "mem_limit": f"{limits.memory_mb}m",
            "memswap_limit": f"{limits.memory_mb}m",
            "nano_cpus": limits.cpu_millis * 1_000_000,
            "tmpfs": {"/tmp": TMPFS_OPTIONS},
            "ipc_mode": "private",
            "init": True,
            "runtime": self._runtime,
            "volumes": volumes,
            # Output is read live from the attach stream, so nothing is written to the host's log files.
            "log_config": {"type": "none", "config": {}},
            "stdin_open": False,
            "tty": False,
        }

    def _workspace_tar(self, mount: Mount, artifacts: Mapping[str, bytes]) -> bytes:
        """The tree for one mount, owned by the sandbox user. Only regular files and directories pass (D23)."""
        uid, gid = docker_config.user_ids(self._settings)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as output:
            def add(info: tarfile.TarInfo, content: bytes | None = None) -> None:
                info.uid, info.gid, info.uname, info.gname, info.mtime = uid, gid, "", "", 0
                output.addfile(info, io.BytesIO(content) if content is not None else None)

            directory = tarfile.TarInfo(OUTPUT_DIR)
            directory.type, directory.mode = tarfile.DIRTYPE, 0o755
            add(directory)
            if mount.source_ref != WORKSPACE_REF:
                with tarfile.open(fileobj=io.BytesIO(artifacts[mount.source_ref]), mode="r:*") as source:
                    for member in source.getmembers():
                        path = _safe_member_path(member.name)
                        # ``out/`` belongs to the run: a planted file there would read as output the sandbox produced.
                        reserved = path is not None and (path == OUTPUT_DIR or path.startswith(f"{OUTPUT_DIR}/"))
                        if path is None or reserved or not (member.isfile() or member.isdir()):
                            raise ValueError("artifact_invalid")
                        info = tarfile.TarInfo(path)
                        info.type = tarfile.DIRTYPE if member.isdir() else tarfile.REGTYPE
                        info.mode = 0o755 if member.isdir() else 0o644
                        if member.isdir():
                            add(info)
                        else:
                            content = source.extractfile(member).read()
                            info.size = len(content)
                            add(info, content)
        return buffer.getvalue()

    def _seed_volumes(
        self, spec: ExecutionSpec, names: list[str], artifacts: Mapping[str, bytes]
    ) -> None:
        """Copy each workspace into its volume through a container that never starts."""
        client = self._docker()
        bind = {name: {"bind": mount.target, "mode": "rw"} for name, mount in zip(names, spec.mounts)}
        seed = client.containers.create(
            spec.image, command=["true"], volumes=bind, network_mode="none", read_only=True, user=self._user,
            labels=self._labels(spec), runtime=self._runtime,
        )
        try:
            for mount in spec.mounts:
                if not seed.put_archive(mount.target, self._workspace_tar(mount, artifacts)):
                    raise RuntimeError("workspace_copy_failed")
        finally:
            seed.remove(force=True)

    def _collect_outputs(self, container: Any, spec: ExecutionSpec) -> tuple[list[OutputFile], dict[str, bytes], bool]:
        target = next((m.target for m in spec.mounts or () if not m.read_only and m.source_ref == WORKSPACE_REF), None)
        target = target or next((m.target for m in spec.mounts or () if not m.read_only), None)
        if target is None:
            return [], {}, False
        budget = spec.limits.max_output_files_bytes
        try:
            chunks, _stat = container.get_archive(f"{target.rstrip('/')}/{OUTPUT_DIR}")
        except Exception as exc:  # noqa: BLE001
            if _error_name(exc) == "NotFound":
                return [], {}, False
            raise
        stream = _LimitedStream(chunks, budget + TAR_OVERHEAD_BYTES)
        files: list[OutputFile] = []
        contents: dict[str, bytes] = {}
        dropped = False
        try:
            with tarfile.open(fileobj=io.BufferedReader(stream), mode="r|") as archive:
                for member in archive:
                    if member.isdir():
                        continue
                    path = _safe_member_path(member.name)
                    if path is None or not member.isfile() or member.size > budget:
                        dropped = True
                        continue
                    content = archive.extractfile(member).read()
                    budget -= len(content)
                    contents[path] = content
                    files.append(OutputFile(path=path, sha256=hashlib.sha256(content).hexdigest(), bytes=len(content)))
        except tarfile.TarError:
            dropped = True
        return sorted(files, key=lambda item: item.path), contents, dropped or stream.exceeded

    def _execute(
        self, spec: ExecutionSpec, digest: str, artifacts: Mapping[str, bytes]
    ) -> tuple[ExecutionResult, dict[str, bytes]]:
        client = self._docker()
        if not self._image_available(spec.image):
            return self._result("error", digest, rejection_reasons=("image_missing",)), {}
        token = uuid.uuid4().hex[:16]
        names = [f"rh-sbx-{token}-{index}" for index, _ in enumerate(spec.mounts or ())]
        volumes = []
        container = None
        attach = None
        try:
            for name in names:
                volumes.append(client.volumes.create(name=name, labels={LABEL_SANDBOX: "1"}))
            if names:
                try:
                    self._seed_volumes(spec, names, artifacts)
                except ValueError as exc:
                    return self._result("rejected", digest, rejection_reasons=(str(exc),)), {}
            bind = {
                name: {"bind": mount.target, "mode": "ro" if mount.read_only else "rw"}
                for name, mount in zip(names, spec.mounts or ())
            }
            container = client.containers.create(spec.image, name=f"rh-sbx-{token}", **self.container_options(spec, bind))
            stdout, stderr = _BoundedOutput(spec.limits.max_output_bytes), _BoundedOutput(spec.limits.max_output_bytes)
            attach = container.attach(stdout=True, stderr=True, stream=True, demux=True, logs=False)
            reader = threading.Thread(target=self._drain, args=(attach, stdout, stderr), daemon=True)
            reader.start()

            started_at, started = _now(), time.monotonic()
            container.start()
            timed_out = self._wait(container, spec.limits.timeout_seconds, started)
            wall_ms = int((time.monotonic() - started) * 1000)
            finished_at = _now()
            reader.join(STREAM_DRAIN_SECONDS)

            container.reload()
            attrs = container.attrs or {}
            state = attrs.get("State") or {}
            self._last_host_config = dict(attrs.get("HostConfig") or {})
            exit_code = state.get("ExitCode")
            if timed_out:
                status, exit_code = "timeout", None
            elif state.get("OOMKilled"):
                status = "oom"
            else:
                status = "ok" if exit_code == 0 else "nonzero_exit"
            files, contents, files_dropped = ([], {}, False) if timed_out else self._collect_outputs(container, spec)
            return (
                self._result(
                    status,
                    digest,
                    exit_code=exit_code,
                    stdout=stdout.text(),
                    stderr=stderr.text(),
                    truncated=stdout.truncated or stderr.truncated or files_dropped,
                    stdout_sha256=stdout.digest.hexdigest(),
                    stderr_sha256=stderr.digest.hexdigest(),
                    output_files=tuple(files),
                    usage={"wall_ms": wall_ms, "cpu_ms": None, "max_rss_mb": None},
                    started_at=started_at,
                    finished_at=finished_at,
                ),
                contents,
            )
        finally:
            self._cleanup(attach, container, volumes)

    @staticmethod
    def _drain(frames: Iterable[Any], stdout: _BoundedOutput, stderr: _BoundedOutput) -> None:
        try:
            for out, err in frames:
                if out:
                    stdout.add(out)
                if err:
                    stderr.add(err)
        except Exception as exc:  # noqa: BLE001 - the stream closes when the container is removed
            logger.debug("sandbox output stream ended: %s", _error_name(exc))

    @staticmethod
    def _wait(container: Any, timeout_seconds: int, started: float) -> bool:
        """True when the time limit was hit; the container is killed then. Other Docker failures propagate."""
        try:
            container.wait(timeout=timeout_seconds)
            return False
        except Exception:
            # The SDK reports the timeout differently per transport (ReadTimeout, or ConnectionError on npipe).
            if time.monotonic() - started < timeout_seconds:
                raise
        try:
            container.kill()
        except Exception:
            # The program may have exited at the deadline; then its real exit status stands.
            container.reload()
            if ((container.attrs or {}).get("State") or {}).get("Running"):
                raise
            return False
        container.wait(timeout=30)
        return True

    @staticmethod
    def _cleanup(attach: Any, container: Any, volumes: list[Any]) -> None:
        close = getattr(attach, "close", None)
        for action in ([close] if callable(close) else []) + ([lambda: container.remove(force=True)] if container is not None else []):
            try:
                action()
            except Exception as exc:  # noqa: BLE001
                logger.warning("sandbox cleanup failed: %s", _error_name(exc))
        for volume in volumes:
            try:
                volume.remove(force=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("sandbox volume cleanup failed: %s", _error_name(exc))

    def sweep_orphans(self, *, now: datetime | None = None) -> int:
        """Remove what a crashed runner left behind. Only objects older than the longest possible run are touched."""
        client = self._docker()
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=int(self._caps.max_limits["timeout_seconds"]) + ORPHAN_GRACE_SECONDS)
        containers = client.containers.list(all=True, filters={"label": f"{LABEL_SANDBOX}=1"})
        volumes = client.volumes.list(filters={"label": f"{LABEL_SANDBOX}=1"})
        stale = [item for item in containers if self._older_than((item.attrs or {}).get("Created"), cutoff)]
        stale += [item for item in volumes if self._older_than((item.attrs or {}).get("CreatedAt"), cutoff)]
        removed = 0
        for item in stale:
            try:
                item.remove(force=True)
                removed += 1
            except Exception as exc:  # noqa: BLE001 - one stuck object must not stop the runner; the next sweep retries
                logger.warning("sandbox orphan cleanup failed: %s", _error_name(exc))
        return removed

    @staticmethod
    def _older_than(value: Any, cutoff: datetime) -> bool:
        try:
            # Docker reports nanoseconds; datetime keeps microseconds.
            text = str(value).replace("Z", "+00:00")
            head, dot, tail = text.partition(".")
            if dot:
                zone = tail[tail.find("+"):] if "+" in tail else ""
                text = f"{head}.{tail[:6]}{zone}"
            created = datetime.fromisoformat(text)
        except ValueError:
            return False
        return (created if created.tzinfo else created.replace(tzinfo=timezone.utc)) < cutoff
