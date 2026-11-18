"""v1.6.2 real Docker checks (plan D44). Skipped unless ``RH_SANDBOX_LIVE=1``; needs Docker and the conformance image.

``SANDBOX_DOCKER_RUNTIME`` selects the runtime (``runc`` by default, ``runsc`` in the CI gVisor job).
"""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from dataclasses import replace

import pytest

from app.core.config import Settings
from app.services.sandbox import ExecutionSpec, Limits, Mount, conformance
from app.services.sandbox.docker_backend import DockerBackend

pytestmark = pytest.mark.skipif(os.environ.get("RH_SANDBOX_LIVE") != "1", reason="set RH_SANDBOX_LIVE=1 to run against real Docker")

LIMITS = Limits(timeout_seconds=20, cpu_millis=1000, memory_mb=128, pids=32, max_output_bytes=8192, max_output_files_bytes=65536)


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings(_env_file=None, sandbox_backend="docker", sandbox_docker_runtime=os.environ.get("SANDBOX_DOCKER_RUNTIME", "runc"))


@pytest.fixture(scope="module")
def client():
    import docker

    return docker.from_env()


@pytest.fixture
def factory(settings, client):
    return lambda: DockerBackend(settings, client=client)


def _tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _no_leftovers(client) -> None:
    assert client.containers.list(all=True, filters={"label": "rh.sandbox=1"}) == []
    assert client.volumes.list(filters={"label": "rh.sandbox=1"}) == []


@pytest.mark.parametrize("case", conformance.CASES, ids=lambda case: case.__name__)
def test_live_base_case(case, factory, settings, client):
    result = case(factory, settings.sandbox_conformance_image)
    assert result.passed, result.detail
    _no_leftovers(client)


@pytest.mark.parametrize("case", conformance.HARDENING_CASES, ids=lambda case: case.__name__)
def test_live_hardening_case(case, factory, settings, client):
    result = case(factory, settings.sandbox_conformance_image)
    assert result.passed, result.detail
    _no_leftovers(client)


def test_live_full_suite_passes(factory, settings):
    report = conformance.run_full_suite(factory, image=settings.sandbox_conformance_image)
    assert conformance.suite_passed(report), [item for item in report if not item["passed"]]


def test_live_artifact_round_trip_and_read_only_source(factory, settings, client):
    tar = _tar({"src/app.py": b"print('hi')\n", "tests/test_app.py": b"# test\n"})
    source = "artifact:sha256:" + hashlib.sha256(tar).hexdigest()
    spec = ExecutionSpec(
        tool_name="artifact",
        argv=("sh", "-c", "cat /workspace/src/app.py && cp /workspace/tests/test_app.py /workspace/out/copied.txt && echo done > /workspace/out/done.txt"),
        limits=LIMITS,
        image=settings.sandbox_conformance_image,
        mounts=(Mount(source_ref=source, target="/workspace", read_only=False),),
    )
    result, contents = factory().run_with_outputs(spec, artifacts={source: tar})
    assert result.status == "ok", result
    assert "print('hi')" in result.stdout
    assert contents == {"out/copied.txt": b"# test\n", "out/done.txt": b"done\n"}
    assert {item.path for item in result.output_files} == set(contents)
    read_only = replace(spec, argv=("sh", "-c", "touch /workspace/out/x"), mounts=(Mount(source_ref=source, target="/workspace"),))
    assert factory().run(read_only, artifacts={source: tar}).status == "nonzero_exit"
    _no_leftovers(client)


def test_live_output_flood_is_bounded(factory, settings, client):
    script = "i=0; while [ $i -lt 2000 ]; do echo xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx; i=$((i+1)); done"
    spec = ExecutionSpec(tool_name="flood", argv=("sh", "-c", script), limits=replace(LIMITS, max_output_bytes=256), image=settings.sandbox_conformance_image)
    result = factory().run(spec)
    assert result.status == "ok" and result.truncated and len(result.stdout.encode()) <= 256
    assert result.stdout_sha256 == hashlib.sha256((("x" * 48 + "\n") * 2000).encode()).hexdigest()
    _no_leftovers(client)


def test_live_missing_image_is_reported(factory, client):
    spec = ExecutionSpec(tool_name="missing", argv=("true",), limits=LIMITS, image="registry.invalid/rh/none@sha256:" + "0" * 64)
    result = factory().run(spec)
    assert (result.status, result.rejection_reasons) == ("error", ("image_missing",))
    _no_leftovers(client)
