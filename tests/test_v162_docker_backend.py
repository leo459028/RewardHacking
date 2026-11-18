"""v1.6.2 P2: hardened Docker backend (fake client), hardening cases, execution gate and architecture contracts."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import io
import tarfile
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models.sandbox import SandboxConformanceRun
from app.services.sandbox import ExecutionSpec, Limits, Mount, conformance, docker_backend, docker_config, gate
from app.services.sandbox.base import ExecutionResult
from app.services.sandbox.docker_backend import DockerBackend

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/acme/runner@sha256:" + "a" * 64
LIMITS = Limits(timeout_seconds=5, cpu_millis=500, memory_mb=128, pids=32, max_output_bytes=64, max_output_files_bytes=1024)
WORKSPACE = Mount(source_ref="workspace", target="/workspace", read_only=False)


class ImageNotFound(Exception):
    pass


class NotFound(Exception):
    pass


def _tar(entries) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, kind, content in entries:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class FakeVolume:
    def __init__(self, client, name) -> None:
        self.client, self.name = client, name

    def remove(self, force=False) -> None:
        self.client.removed_volumes.append(self.name)


class FakeContainer:
    def __init__(self, client, image, kwargs) -> None:
        self.client, self.image, self.kwargs = client, image, kwargs
        self.attrs = {"State": {"ExitCode": client.exit_code, "OOMKilled": client.oom}, "HostConfig": {"Runtime": kwargs.get("runtime")}}
        self.killed = self.removed = self.started = False

    def put_archive(self, path, data) -> bool:
        self.client.archives.append((path, data))
        return self.client.put_ok

    def attach(self, **kwargs):
        self.client.attach_kwargs = kwargs
        return iter(self.client.frames)

    def start(self) -> None:
        self.started = True
        self.client.events.append("start")

    def wait(self, timeout=None):
        if self.client.hang and not self.killed:
            time.sleep(timeout + 0.01)
            raise ConnectionError("timed out")
        return {"StatusCode": self.client.exit_code}

    def kill(self) -> None:
        self.killed = True

    def reload(self) -> None:
        pass

    def get_archive(self, path):
        self.client.archive_paths.append(path)
        if self.client.outputs is None:
            raise NotFound(path)
        return iter([self.client.outputs]), {"name": "out"}

    def remove(self, force=False) -> None:
        self.removed = True
        self.client.events.append("remove")


class _Containers:
    def __init__(self, client) -> None:
        self.client = client

    def create(self, image, **kwargs):
        container = FakeContainer(self.client, image, kwargs)
        self.client.containers_created.append(container)
        return container

    def list(self, all=False, filters=None):
        return self.client.listed_containers


class _Volumes:
    def __init__(self, client) -> None:
        self.client = client

    def create(self, name, labels=None):
        self.client.volume_labels[name] = labels
        return FakeVolume(self.client, name)

    def list(self, filters=None):
        return self.client.listed_volumes


class _Images:
    def __init__(self, client) -> None:
        self.client = client

    def get(self, ref):
        if not self.client.image_present:
            raise ImageNotFound(ref)
        return object()

    def pull(self, ref):
        self.client.pulled.append(ref)
        self.client.image_present = True


class FakeDocker:
    def __init__(self, **overrides) -> None:
        self.exit_code, self.oom, self.hang, self.put_ok, self.image_present = 0, False, False, True, True
        self.frames = [(b"ok\n", None)]
        self.outputs = None
        self.containers_created, self.archives, self.events, self.archive_paths, self.pulled = [], [], [], [], []
        self.removed_volumes, self.listed_containers, self.listed_volumes = [], [], []
        self.volume_labels, self.attach_kwargs = {}, {}
        self.containers, self.volumes, self.images = _Containers(self), _Volumes(self), _Images(self)
        self.__dict__.update(overrides)

    @property
    def main(self) -> FakeContainer:
        return self.containers_created[-1]

    def version(self):
        return {"Version": "29.9.2"}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, sandbox_backend="docker", **overrides)


def _spec(**overrides) -> ExecutionSpec:
    return replace(ExecutionSpec(tool_name="t", argv=("echo", "ok"), limits=LIMITS, image=IMAGE, mounts=(WORKSPACE,)), **overrides)


def _backend(client=None, **settings) -> tuple[DockerBackend, FakeDocker]:
    client = client or FakeDocker()
    return DockerBackend(_settings(**settings), client=client), client


# ---------------------------------------------------------------------------
# D16: hardening settings, one by one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("read_only", True),
        ("network_mode", "none"),
        ("cap_drop", ["ALL"]),
        ("security_opt", ["no-new-privileges:true"]),
        ("user", "65534:65534"),
        ("pids_limit", 32),
        ("mem_limit", "128m"),
        ("memswap_limit", "128m"),
        ("nano_cpus", 500_000_000),
        ("tmpfs", {"/tmp": "noexec,nosuid,size=64m"}),
        ("ipc_mode", "private"),
        ("init", True),
        ("runtime", "runc"),
        ("hostname", "sandbox"),
        ("command", ["echo", "ok"]),
        ("working_dir", "/workspace"),
        ("environment", {}),
        ("log_config", {"type": "none", "config": {}}),
        ("tty", False),
        ("stdin_open", False),
    ],
)
def test_container_is_created_with_the_hardening_setting(key, expected):
    backend, client = _backend()
    assert backend.run(_spec()).status == "ok"
    assert client.main.kwargs[key] == expected


def test_container_has_no_privilege_escalating_options():
    backend, client = _backend()
    backend.run(_spec())
    for forbidden in ("privileged", "cap_add", "pid_mode", "userns_mode", "devices", "extra_hosts", "ports"):
        assert forbidden not in client.main.kwargs


def test_seccomp_profile_is_not_disabled():
    backend, client = _backend()
    backend.run(_spec())
    assert not any("seccomp" in option for option in client.main.kwargs["security_opt"])


def test_labels_identify_sandbox_containers():
    backend, client = _backend()
    backend.run(_spec(labels={"job_id": "sbj_1"}))
    assert client.main.kwargs["labels"] == {"rh.sandbox": "1", "rh.job_id": "sbj_1"}
    assert all(labels == {"rh.sandbox": "1"} for labels in client.volume_labels.values())


def test_only_the_spec_environment_reaches_the_container():
    backend, client = _backend()
    backend.run(_spec(env={"PYTHONUNBUFFERED": "1"}))
    assert client.main.kwargs["environment"] == {"PYTHONUNBUFFERED": "1"}


def test_runsc_is_passed_to_docker():
    backend, client = _backend(sandbox_docker_runtime="runsc")
    assert backend.run(_spec()).isolation_level == "gvisor" and client.main.kwargs["runtime"] == "runsc"


def test_runc_limits_processes_with_the_cgroup_only():
    backend, client = _backend()
    backend.run(_spec())
    assert client.main.kwargs["pids_limit"] == LIMITS.pids and "ulimits" not in client.main.kwargs


def test_runsc_limits_the_program_with_a_hard_nproc_limit():
    backend, client = _backend(sandbox_docker_runtime="runsc")
    backend.run(_spec())
    assert client.main.kwargs["ulimits"] == [{"name": "nproc", "soft": LIMITS.pids, "hard": LIMITS.pids}]


@pytest.mark.parametrize("pids", [1, 16, 32, 512])
def test_runsc_host_limit_leaves_room_for_gvisor_threads(pids):
    measured_need = 36 + 2 * pids
    assert docker_config.host_pids_limit("runsc", pids) > measured_need
    assert docker_config.host_pids_limit("runc", pids) == pids


def test_configured_user_is_used():
    backend, client = _backend(sandbox_docker_user="1234:1234")
    backend.run(_spec())
    assert client.main.kwargs["user"] == "1234:1234"
    assert backend.hardening_expectations()["uid"] == "1234"


def test_root_user_is_rejected_before_docker_is_called():
    backend, client = _backend(sandbox_docker_user="0:0")
    result = backend.run(_spec())
    assert result.status == "rejected" and "docker_user_invalid" in result.rejection_reasons
    assert client.containers_created == []


def test_workspace_is_seeded_by_a_container_that_never_starts():
    backend, client = _backend()
    backend.run(_spec())
    seed, main = client.containers_created
    assert seed.started is False and seed.removed is True and main.started is True
    assert seed.kwargs["network_mode"] == "none" and seed.kwargs["read_only"] is True
    path, data = client.archives[0]
    assert path == "/workspace"
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        member = archive.getmember("out")
    assert member.isdir() and (member.uid, member.gid) == (65534, 65534)


def test_read_only_mount_is_read_only_in_the_real_container():
    backend, client = _backend()
    backend.run(_spec(mounts=(Mount(source_ref="workspace", target="/workspace"),)))
    volume = next(iter(client.main.kwargs["volumes"].values()))
    assert volume == {"bind": "/workspace", "mode": "ro"}


def test_invalid_spec_never_reaches_docker():
    backend, client = _backend()
    result = backend.run(_spec(argv="echo hi; rm -rf /"))
    assert result.status == "rejected" and "argv_shell_string" in result.rejection_reasons
    assert client.containers_created == [] and client.volume_labels == {}


def test_image_id_needs_the_setting_and_never_works_in_production():
    image_id = "sha256:" + "c" * 64
    assert _backend()[0].run(_spec(image=image_id)).status == "rejected"
    assert _backend(sandbox_docker_allow_local_image_ids=True)[0].run(_spec(image=image_id)).status == "ok"
    prod = _backend(sandbox_docker_allow_local_image_ids=True, app_env="prod")[0].run(_spec(image=image_id))
    assert prod.status == "rejected" and "image_not_digest_pinned" in prod.rejection_reasons


# ---------------------------------------------------------------------------
# D16: statuses and cleanup
# ---------------------------------------------------------------------------


def test_successful_run_reports_identity_and_output():
    backend, _ = _backend()
    result = backend.run(_spec())
    assert (result.status, result.exit_code, result.stdout, result.backend_id, result.isolation_level) == ("ok", 0, "ok\n", "docker", "container")
    assert result.stdout_sha256 == hashlib.sha256(b"ok\n").hexdigest() and result.started_at and result.finished_at
    assert result.usage["wall_ms"] >= 0


def test_nonzero_exit_is_reported():
    backend, _ = _backend(FakeDocker(exit_code=3))
    result = backend.run(_spec())
    assert (result.status, result.exit_code) == ("nonzero_exit", 3)


def test_oom_flag_wins_over_a_zero_exit_code():
    backend, _ = _backend(FakeDocker(oom=True, exit_code=0))
    assert backend.run(_spec()).status == "oom"


def test_a_sigkilled_container_waits_for_dockers_late_oom_report(monkeypatch):
    reloads = []

    def reload(container) -> None:
        reloads.append(1)
        container.attrs["State"]["OOMKilled"] = len(reloads) >= 3

    monkeypatch.setattr(FakeContainer, "reload", reload)
    monkeypatch.setattr(docker_backend, "OOM_REPORT_POLL_SECONDS", 0)
    backend, _ = _backend(FakeDocker(exit_code=137))
    assert backend.run(_spec()).status == "oom" and len(reloads) == 3


def test_a_sigkill_without_an_oom_report_is_not_called_an_oom(monkeypatch):
    monkeypatch.setattr(docker_backend, "OOM_REPORT_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(docker_backend, "OOM_REPORT_POLL_SECONDS", 0.001)
    backend, _ = _backend(FakeDocker(exit_code=137))
    result = backend.run(_spec())
    assert (result.status, result.exit_code) == ("nonzero_exit", 137)


@pytest.mark.parametrize("exit_code", [0, 1, 143])
def test_other_exit_codes_do_not_wait_for_an_oom_report(monkeypatch, exit_code):
    reloads = []
    monkeypatch.setattr(FakeContainer, "reload", lambda container: reloads.append(1))
    backend, _ = _backend(FakeDocker(exit_code=exit_code))
    backend.run(_spec())
    assert len(reloads) == 1


def test_timeout_kills_the_container():
    backend, client = _backend(FakeDocker(hang=True))
    result = backend.run(_spec(limits=replace(LIMITS, timeout_seconds=1)))
    assert (result.status, result.exit_code) == ("timeout", None)
    assert client.main.killed is True and client.main.removed is True


def _container_class(client, cls):
    client.containers.create = lambda image, **kwargs: client.containers_created.append(cls(client, image, kwargs)) or client.containers_created[-1]


def test_a_program_that_exits_at_the_deadline_keeps_its_exit_status():
    client = FakeDocker(hang=True, exit_code=0)

    class ExitedAtDeadline(FakeContainer):
        def kill(self):
            raise RuntimeError("409 container is not running")

    _container_class(client, ExitedAtDeadline)
    result = _backend(client)[0].run(_spec(limits=replace(LIMITS, timeout_seconds=1)))
    assert (result.status, result.exit_code) == ("ok", 0)


def test_a_failed_kill_of_a_running_container_is_an_error():
    client = FakeDocker(hang=True)

    class Unkillable(FakeContainer):
        def kill(self):
            self.attrs["State"]["Running"] = True
            raise RuntimeError("daemon refused")

    _container_class(client, Unkillable)
    result = _backend(client)[0].run(_spec(limits=replace(LIMITS, timeout_seconds=1)))
    assert result.status == "error" and client.main.removed is True


def test_early_wait_failure_is_an_error_not_a_timeout():
    client = FakeDocker()

    class Broken(FakeContainer):
        def wait(self, timeout=None):
            raise ConnectionError("daemon went away")

    client.containers.create = lambda image, **kwargs: client.containers_created.append(Broken(client, image, kwargs)) or client.containers_created[-1]
    backend, _ = _backend(client)
    result = backend.run(_spec())
    assert result.status == "error" and result.rejection_reasons == ("docker_error:ConnectionError",)
    assert client.main.removed is True and client.removed_volumes


def test_missing_image_is_not_pulled_by_default():
    backend, client = _backend(FakeDocker(image_present=False))
    result = backend.run(_spec())
    assert (result.status, result.rejection_reasons) == ("error", ("image_missing",))
    assert client.pulled == [] and client.containers_created == []


def test_pull_policy_missing_pulls_the_pinned_image():
    backend, client = _backend(FakeDocker(image_present=False), sandbox_docker_pull="missing")
    assert backend.run(_spec()).status == "ok" and client.pulled == [IMAGE]


def test_container_and_volumes_are_removed_after_every_run():
    backend, client = _backend()
    backend.run(_spec())
    assert all(container.removed for container in client.containers_created)
    assert client.removed_volumes == list(client.volume_labels)


def test_failed_workspace_copy_cleans_up_and_reports_an_error():
    backend, client = _backend(FakeDocker(put_ok=False))
    result = backend.run(_spec())
    assert result.status == "error" and client.removed_volumes and client.containers_created[0].removed


def test_cleanup_failure_does_not_hide_the_result():
    client = FakeDocker()
    client.removed_volumes = None  # FakeVolume.remove raises AttributeError
    backend, _ = _backend(client)
    assert backend.run(_spec()).status == "ok"


# ---------------------------------------------------------------------------
# D16 / D17: untrusted output is bounded
# ---------------------------------------------------------------------------


def test_output_is_truncated_but_hashed_in_full():
    payload = b"x" * 1000
    backend, _ = _backend(FakeDocker(frames=[(payload[:400], None), (payload[400:], b"err")]))
    result = backend.run(_spec())
    assert result.truncated and len(result.stdout) == 64 and result.stderr == "err"
    assert result.stdout_sha256 == hashlib.sha256(payload).hexdigest()


def test_output_files_are_digested_and_returned():
    outputs = _tar([("out", "dir", None), ("out/report.txt", "file", b"report-1\n")])
    backend, client = _backend(FakeDocker(outputs=outputs))
    result, contents = backend.run_with_outputs(_spec())
    assert contents == {"out/report.txt": b"report-1\n"} and client.archive_paths == ["/workspace/out"]
    assert result.output_files[0].sha256 == hashlib.sha256(b"report-1\n").hexdigest()


def test_symlinks_in_the_output_are_dropped():
    outputs = _tar([("out", "dir", None), ("out/link", "symlink", None), ("out/ok.txt", "file", b"ok")])
    backend, _ = _backend(FakeDocker(outputs=outputs))
    result, contents = backend.run_with_outputs(_spec())
    assert list(contents) == ["out/ok.txt"] and result.truncated is True


def test_path_traversal_in_the_output_is_dropped():
    outputs = _tar([("../escape.txt", "file", b"x"), ("out/ok.txt", "file", b"ok")])
    backend, _ = _backend(FakeDocker(outputs=outputs))
    result, contents = backend.run_with_outputs(_spec())
    assert list(contents) == ["out/ok.txt"] and result.truncated is True


def test_output_file_over_the_budget_is_dropped():
    outputs = _tar([("out/big.bin", "file", b"x" * 2000), ("out/small.txt", "file", b"ok")])
    backend, _ = _backend(FakeDocker(outputs=outputs))
    result, contents = backend.run_with_outputs(_spec())
    assert list(contents) == ["out/small.txt"] and result.truncated is True


def test_output_archive_stream_stops_at_the_hard_limit():
    consumed = []

    def endless():
        while True:
            consumed.append(1)
            yield b"\0" * 65536

    stream = docker_backend._LimitedStream(endless(), 200_000)
    data = io.BufferedReader(stream).read()
    assert len(data) == 200_000 and stream.exceeded and len(consumed) < 10


def test_missing_output_directory_is_not_an_error():
    backend, _ = _backend(FakeDocker(outputs=None))
    assert backend.run(_spec()).output_files == ()


def test_read_only_workspace_has_no_output_collection():
    backend, client = _backend(FakeDocker(outputs=_tar([("out/a", "file", b"x")])))
    backend.run(_spec(mounts=(Mount(source_ref="workspace", target="/workspace"),)))
    assert client.archive_paths == []


# ---------------------------------------------------------------------------
# D23: only the platform's own artifact enters a workspace
# ---------------------------------------------------------------------------


def _artifact(entries):
    tar = _tar(entries)
    return tar, "artifact:sha256:" + hashlib.sha256(tar).hexdigest()


def test_artifact_is_copied_with_the_sandbox_owner():
    tar, source = _artifact([("src", "dir", None), ("src/app.py", "file", b"print(1)\n")])
    backend, client = _backend()
    result = backend.run(_spec(mounts=(Mount(source_ref=source, target="/workspace", read_only=False),)), artifacts={source: tar})
    assert result.status == "ok"
    with tarfile.open(fileobj=io.BytesIO(client.archives[0][1])) as archive:
        member = archive.getmember("src/app.py")
        assert (member.uid, member.gid, member.mode) == (65534, 65534, 0o644)
        assert archive.extractfile(member).read() == b"print(1)\n"


def test_missing_artifact_is_rejected():
    source = "artifact:sha256:" + "d" * 64
    result = _backend()[0].run(_spec(mounts=(Mount(source_ref=source, target="/workspace"),)))
    assert (result.status, result.rejection_reasons) == ("rejected", ("artifact_missing",))


def test_artifact_with_the_wrong_digest_is_rejected():
    source = "artifact:sha256:" + "d" * 64
    result = _backend()[0].run(_spec(mounts=(Mount(source_ref=source, target="/workspace"),)), artifacts={source: b"other"})
    assert result.rejection_reasons == ("artifact_digest_mismatch",)


@pytest.mark.parametrize(
    "entries",
    [
        [("link", "symlink", None)],
        [("../up.txt", "file", b"x")],
        [("out", "dir", None)],
        [("out/junit.xml", "file", b"<testsuite tests='50'/>")],
    ],
)
def test_artifact_with_unsafe_members_is_rejected(entries):
    tar, source = _artifact(entries)
    backend, client = _backend()
    result = backend.run(_spec(mounts=(Mount(source_ref=source, target="/workspace"),)), artifacts={source: tar})
    assert (result.status, result.rejection_reasons) == ("rejected", ("artifact_invalid",))
    assert client.removed_volumes


# ---------------------------------------------------------------------------
# Orphans of a crashed runner
# ---------------------------------------------------------------------------


class _Orphan:
    def __init__(self, attrs) -> None:
        self.attrs, self.removed = attrs, False

    def remove(self, force=False) -> None:
        self.removed = True


def test_sweep_removes_only_objects_older_than_the_longest_run():
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    old, recent = _Orphan({"Created": "2026-11-18T10:00:00.123456789Z"}), _Orphan({"Created": "2026-11-18T11:59:00Z"})
    old_volume, bad_volume = _Orphan({"CreatedAt": "2026-11-18T10:00:00Z"}), _Orphan({"CreatedAt": "not a date"})
    backend, _ = _backend(FakeDocker(listed_containers=[old, recent], listed_volumes=[old_volume, bad_volume]))
    assert backend.sweep_orphans(now=now) == 2
    assert (old.removed, recent.removed, old_volume.removed, bad_volume.removed) == (True, False, True, False)


def test_one_stuck_orphan_does_not_stop_the_sweep():
    class Stuck(_Orphan):
        def remove(self, force=False):
            raise RuntimeError("409 removal in progress")

    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    stuck, old = Stuck({"Created": "2026-11-18T10:00:00Z"}), _Orphan({"Created": "2026-11-18T10:00:00Z"})
    backend, _ = _backend(FakeDocker(listed_containers=[stuck, old]))
    assert backend.sweep_orphans(now=now) == 1 and old.removed is True


# ---------------------------------------------------------------------------
# D15: hardening cases must detect a weak container
# ---------------------------------------------------------------------------


class ScriptedBackend:
    """Answers every case with a fixed stdout, as a misconfigured container would."""

    def __init__(self, stdout: str = "", status: str = "ok", host: dict | None = None, uid: str = "65534", **fields) -> None:
        self.stdout, self.status, self.host, self.uid, self.fields = stdout, status, host or {}, uid, fields
        self.spec = None

    def capabilities(self):
        return docker_config.capabilities(_settings())

    def hardening_expectations(self):
        return {"uid": self.uid, "runtime": "runc", "isolation_level": "container"}

    def last_host_config(self):
        return self.host

    def run(self, spec):
        self.spec = spec
        return ExecutionResult(self.status, "docker", "v1.6.2", self.fields.pop("level", "container"), "d", stdout=f"{conformance.STARTED}\n{self.stdout}", **self.fields)


def _case(case, **kwargs):
    backend = ScriptedBackend(**kwargs)
    return case(lambda: backend, IMAGE)


NET_OK = "tcp-blocked\ndns-blocked\ninterfaces-begin\nInter-|\n face |\n    lo: 0 0\ninterfaces-end\n"


@pytest.mark.parametrize(
    ("case", "good", "bad"),
    [
        (conformance.root_fs_read_only, "probe-readonly", "probe-writable"),
        (conformance.network_unreachable, NET_OK, NET_OK.replace("tcp-blocked", "tcp-open")),
        (conformance.network_unreachable, NET_OK, NET_OK.replace("dns-blocked", "dns-open")),
        (conformance.network_unreachable, NET_OK, NET_OK.replace("lo: 0 0", "lo: 0 0\n  eth0: 1 1")),
        (conformance.network_unreachable, NET_OK, "tool-missing\n" + NET_OK),
        (conformance.capabilities_dropped, "CapEff:\t0000000000000000\n", "CapEff:\t00000000a80425fb\n"),
        (conformance.no_new_privileges, "NoNewPrivs:\t1\n", "NoNewPrivs:\t0\n"),
        (conformance.host_env_not_visible, "PATH=/bin\n", "POSTGRES_DSN=postgres://x\n"),
        (conformance.workspace_isolated, "checked-paths\n", "visible:/var/run/docker.sock\nchecked-paths\n"),
    ],
)
def test_hardening_case_tells_good_from_bad(case, good, bad):
    assert _case(case, stdout=good).passed
    assert not _case(case, stdout=bad).passed


def test_a_container_that_never_started_passes_no_hardening_case():
    for case in conformance.HARDENING_CASES:
        backend = ScriptedBackend(status="error")
        assert not case(lambda: backend, IMAGE).passed, case.__name__


def test_runs_as_non_root_compares_with_the_configured_uid():
    assert _case(conformance.runs_as_non_root, stdout="65534\n").passed
    assert not _case(conformance.runs_as_non_root, stdout="0\n", uid="0").passed
    assert not _case(conformance.runs_as_non_root, stdout="1000\n").passed


def test_memory_case_needs_an_oom_result():
    assert _case(conformance.memory_limit_enforced, status="oom").passed
    assert not _case(conformance.memory_limit_enforced, status="ok").passed


CONTROL_OK = {"stdout": "spawned=8\nall-spawned\nprocs=10\n"}
REFUSED = {"stdout": "spawned=13\n", "stderr": "sh: can't fork: Resource temporarily unavailable\n", "status": "nonzero_exit"}


class PidsBackend(ScriptedBackend):
    """Answers the control run and the limited run of ``pids_limit_enforced`` separately."""

    def __init__(self, control: dict, limited: dict) -> None:
        super().__init__()
        self.answers = {conformance.PIDS_CONTROL_LIMIT: control, conformance.PIDS_LIMIT: limited}

    def run(self, spec):
        answer = self.answers[spec.limits.pids]
        return ExecutionResult(
            answer.get("status", "ok"), "docker", "v1.6.2", "container", "d",
            stdout=f"{conformance.STARTED}\n{answer.get('stdout', '')}", stderr=answer.get("stderr", ""),
        )


def _pids_case(control: dict, limited: dict):
    backend = PidsBackend(control, limited)
    return conformance.pids_limit_enforced(lambda: backend, IMAGE)


def test_pids_case_passes_when_the_limit_refuses_the_fork():
    assert _pids_case(CONTROL_OK, REFUSED).passed


def test_pids_case_accepts_the_kernel_wording_of_the_refusal():
    assert _pids_case(CONTROL_OK, {**REFUSED, "stderr": "sh: Resource temporarily unavailable\n"}).passed


def test_pids_case_fails_when_the_shell_stops_for_another_reason():
    result = _pids_case(CONTROL_OK, {"stdout": "spawned=3\n", "stderr": "", "status": "nonzero_exit"})
    assert not result.passed


def test_pids_case_fails_on_an_unrelated_error_message():
    assert not _pids_case(CONTROL_OK, {**REFUSED, "stderr": "sh: Cannot allocate memory\n"}).passed


def test_pids_case_fails_when_nothing_limits_the_processes():
    assert not _pids_case(CONTROL_OK, {"stdout": "spawned=64\nall-spawned\nprocs=66\n"}).passed


def test_pids_case_fails_when_more_processes_start_than_allowed():
    assert not _pids_case(CONTROL_OK, {**REFUSED, "stdout": "spawned=17\n"}).passed


@pytest.mark.parametrize(
    "control",
    [
        {"stdout": "spawned=3\n", "status": "nonzero_exit"},
        {"stdout": "spawned=8\nall-spawned\n"},
        {"stdout": "spawned=8\nall-spawned\nprocs=2\n"},
        {"status": "error"},
    ],
)
def test_pids_case_fails_when_the_control_run_does_not_work(control):
    result = _pids_case(control, REFUSED)
    assert not result.passed and "control run" in result.detail


def test_host_env_case_cleans_its_canary(monkeypatch):
    import os

    _case(conformance.host_env_not_visible, stdout="")
    assert "RH_SANDBOX_CONFORMANCE_CANARY" not in os.environ


def test_host_env_case_detects_the_canary():
    class Leaky(ScriptedBackend):
        def run(self, spec):
            import os

            self.stdout = "\n".join(f"{k}={v}" for k, v in os.environ.items() if k.startswith("RH_SANDBOX"))
            return super().run(spec)

    backend = Leaky()
    assert not conformance.host_env_not_visible(lambda: backend, IMAGE).passed


def test_runtime_case_compares_docker_inspect_with_the_settings():
    assert _case(conformance.runtime_matches_config, host={"Runtime": "runc"}).passed
    assert not _case(conformance.runtime_matches_config, host={"Runtime": "kata"}).passed
    assert not _case(conformance.runtime_matches_config, host={"Runtime": "runc"}, level="gvisor").passed


def test_no_new_privileges_falls_back_to_the_configuration_only_on_gvisor():
    proof = {"Runtime": "runsc", "SecurityOpt": ["no-new-privileges:true"]}
    assert _case(conformance.no_new_privileges, stdout="Name: sh\n", host=proof).passed
    assert not _case(conformance.no_new_privileges, stdout="Name: sh\n", host={**proof, "Runtime": "runc"}).passed
    assert not _case(conformance.no_new_privileges, stdout="Name: sh\n", host={"Runtime": "runsc", "SecurityOpt": []}).passed


def test_crashing_backend_fails_every_case():
    class Crash(ScriptedBackend):
        def run(self, spec):
            raise RuntimeError("down")

    report = conformance.run_full_suite(Crash, image=IMAGE)
    assert len(report) == len(conformance.ALL_CASE_NAMES) and not any(item["passed"] for item in report)
    assert {item["group"] for item in report} == {"base", "hardening"} and all("duration_ms" in item for item in report)


def test_fake_backend_passes_the_base_cases_it_can_answer():
    backend, _ = _backend()
    assert conformance.rejects_invalid_spec(lambda: backend, IMAGE).passed
    assert conformance.limits_required(lambda: backend, IMAGE).passed
    assert conformance.env_secret_names_rejected(lambda: backend, IMAGE).passed
    assert conformance.unsupported_network_rejected(lambda: backend, IMAGE).passed


# ---------------------------------------------------------------------------
# D14: execution gate
# ---------------------------------------------------------------------------

VERSION = "29.9.2"


def _ready_settings(**overrides) -> Settings:
    return _settings(sandbox_execution_enabled=True, **overrides)


def _evaluate(session_factory, settings, *, row=None, **kwargs):
    async def scenario():
        async with session_factory() as session:
            if row is not None:
                session.add(row)
                await session.commit()
            return await gate.evaluate(session, settings, **kwargs)

    return asyncio.run(scenario())


def _row(settings, **overrides) -> SandboxConformanceRun:
    values = {
        "run_id": "sbc_1",
        "backend_id": "docker",
        "backend_version": docker_config.BACKEND_VERSION,
        "runtime": "runc",
        "config_fingerprint": gate.config_fingerprint(settings, docker_server_version=VERSION),
        "passed": True,
        "cases": [],
        "docker_server_version": VERSION,
        "triggered_by": "test",
        "created_at": datetime.now(timezone.utc),
    }
    return SandboxConformanceRun(**{**values, **overrides})


def test_gate_is_ready_only_when_every_condition_holds(session_factory):
    settings = _ready_settings()
    status = _evaluate(session_factory, settings, row=_row(settings))
    assert status.ready and status.reasons == () and status.latest_run_id == "sbc_1"


def test_gate_flag_off(session_factory):
    settings = _settings()
    status = _evaluate(session_factory, settings, row=_row(settings))
    assert not status.ready and status.reasons == (gate.EXECUTION_DISABLED,)


def test_gate_backend_not_docker(session_factory):
    status = _evaluate(session_factory, Settings(_env_file=None, sandbox_execution_enabled=True))
    assert not status.ready and status.reasons == (gate.BACKEND_NOT_DOCKER,)


def test_gate_default_settings_are_closed(session_factory):
    status = _evaluate(session_factory, Settings(_env_file=None))
    assert status.reasons == (gate.EXECUTION_DISABLED, gate.BACKEND_NOT_DOCKER)


def test_gate_without_conformance(session_factory):
    assert _evaluate(session_factory, _ready_settings()).reasons == (gate.CONFORMANCE_MISSING,)


def test_gate_failed_conformance(session_factory):
    settings = _ready_settings()
    assert _evaluate(session_factory, settings, row=_row(settings, passed=False)).reasons == (gate.CONFORMANCE_FAILED,)


def test_gate_fingerprint_mismatch_after_a_setting_changes(session_factory):
    settings = _ready_settings()
    changed = _ready_settings(sandbox_images={"python": IMAGE})
    assert _evaluate(session_factory, changed, row=_row(settings)).reasons == (gate.FINGERPRINT_MISMATCH,)


def test_gate_closes_when_the_live_docker_version_changes(session_factory):
    settings = _ready_settings()
    status = _evaluate(session_factory, settings, row=_row(settings), docker_server_version="30.0.0")
    assert status.reasons == (gate.FINGERPRINT_MISMATCH,)


def test_gate_stale_conformance(session_factory):
    settings = _ready_settings()
    old = datetime.now(timezone.utc) - timedelta(hours=169)
    assert _evaluate(session_factory, settings, row=_row(settings, created_at=old)).reasons == (gate.CONFORMANCE_STALE,)


def test_gate_max_age_is_configurable(session_factory):
    settings = _ready_settings(sandbox_conformance_max_age_hours=1)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    assert _evaluate(session_factory, settings, row=_row(settings, created_at=old)).reasons == (gate.CONFORMANCE_STALE,)


def test_gate_uses_only_the_latest_run(session_factory):
    settings = _ready_settings()
    earlier = datetime.now(timezone.utc) - timedelta(hours=1)

    async def scenario():
        async with session_factory() as session:
            session.add(_row(settings, run_id="sbc_old", passed=True, created_at=earlier))
            session.add(_row(settings, run_id="sbc_new", passed=False))
            await session.commit()
            return await gate.evaluate(session, settings)

    status = asyncio.run(scenario())
    assert status.reasons == (gate.CONFORMANCE_FAILED,) and status.latest_run_id == "sbc_new"


def test_gate_refuses_a_backend_below_the_minimum_isolation(session_factory):
    settings = _ready_settings(sandbox_min_isolation_default="gvisor")
    status = _evaluate(session_factory, settings, row=_row(settings))
    assert status.reasons == (gate.ISOLATION_BELOW_MINIMUM,)


def test_gate_reports_invalid_settings(session_factory):
    settings = _ready_settings(sandbox_docker_runtime="kata")
    assert _evaluate(session_factory, settings).reasons == ("runtime_invalid",)
    bad_user = _ready_settings(sandbox_docker_user="0:0")
    assert "docker_user_invalid" in _evaluate(session_factory, bad_user).reasons


def test_gate_shows_the_high_risk_relaxation(session_factory):
    settings = _ready_settings(sandbox_min_isolation_high_risk="container")
    status = _evaluate(session_factory, settings, row=_row(settings))
    assert status.ready and status.relaxed_high_risk is True and status.to_dict()["relaxed_high_risk"] is True


@pytest.mark.parametrize(
    "change",
    [
        {"sandbox_docker_runtime": "runsc"},
        {"sandbox_conformance_image": "busybox@sha256:" + "e" * 64},
        {"sandbox_images": {"python": IMAGE}},
        {"sandbox_max_timeout_seconds": 60},
        {"sandbox_max_output_bytes": 2048},
        {"sandbox_docker_user": "1000:1000"},
    ],
)
def test_fingerprint_follows_every_input(change):
    base = gate.config_fingerprint(_settings(), docker_server_version=VERSION)
    assert gate.config_fingerprint(_settings(**change), docker_server_version=VERSION) != base


def test_fingerprint_follows_the_docker_version_and_is_stable():
    settings = _settings()
    first = gate.config_fingerprint(settings, docker_server_version=VERSION)
    assert first == gate.config_fingerprint(_settings(), docker_server_version=VERSION) and len(first) == 64
    assert first != gate.config_fingerprint(settings, docker_server_version="30.0.0")


# ---------------------------------------------------------------------------
# D16 / D19: architecture contracts
# ---------------------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def test_only_the_backend_module_imports_the_docker_sdk():
    users = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "app").rglob("*.py")
        if any(name == "docker" or name.startswith("docker.") for name in _imports(path))
    }
    assert users == {"app/services/sandbox/docker_backend.py"}


def test_the_docker_sdk_is_imported_lazily():
    tree = ast.parse((ROOT / "app/services/sandbox/docker_backend.py").read_text(encoding="utf-8"))
    assert not any(isinstance(node, ast.Import) and any(alias.name == "docker" for alias in node.names) for node in tree.body)


def test_the_docker_sdk_is_not_a_runtime_requirement_of_the_api():
    assert "docker" not in (ROOT / "requirements.txt").read_text(encoding="utf-8").lower().split()
    assert "docker==" in (ROOT / "requirements-sandbox.txt").read_text(encoding="utf-8")


def test_import_contract_keeps_the_backend_out_of_the_api_and_worker():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'forbidden_modules = ["app.services.sandbox.docker_backend"]' in text
    for module in ("app.api", "app.ui", "app.workers.consumer", "app.services.executor"):
        assert f'"{module}"' in text
