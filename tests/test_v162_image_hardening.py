"""v1.6.2 P1: the published image holds no secrets and the delivery files cannot regress (D1-D9). No Docker needed."""

from __future__ import annotations

import io
import json
import re
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import image_content_check as scan  # noqa: E402

ALLOW_LIST = [
    "!app/",
    "!migrations/",
    "!alembic.ini",
    "!requirements.txt",
    "!requirements-otel.txt",
    "!requirements-sandbox.txt",
    "!README.md",
    "!LICENSE",
    "!config/*.example.yaml",
]
SHA_PINNED = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _workflow(name: str) -> dict:
    data = yaml.safe_load(_read(f".github/workflows/{name}"))
    data["on"] = data.pop(True)  # YAML 1.1 reads the key `on` as a boolean
    return data


def _instructions(dockerfile: str) -> list[str]:
    joined = re.sub(r"\\\n", " ", dockerfile)
    return [line.strip() for line in joined.splitlines() if line.strip() and not line.strip().startswith("#")]


def _final_stage(dockerfile: str) -> list[str]:
    lines = _instructions(dockerfile)
    start = max(i for i, line in enumerate(lines) if line.upper().startswith("FROM "))
    return lines[start:]


def dockerfile_violations(dockerfile: str) -> list[str]:
    """The D8 rules, as a function so a tampered Dockerfile can be shown to fail them."""
    problems = []
    lines = _instructions(dockerfile)
    for line in lines:
        upper = line.upper()
        if upper.startswith("ADD "):
            problems.append("ADD is not allowed")
        if upper.startswith("FROM ") and "@sha256:" not in line:
            problems.append(f"base image is not pinned by digest: {line}")
        if upper.startswith("COPY ") and "--from=" not in line:
            sources = line.split()[1:-1]
            for source in sources:
                clean = source.rstrip("/")
                if clean in {".", "tests", "scripts", "docs"} or clean.startswith("tests/"):
                    problems.append(f"forbidden COPY source: {source}")
                if clean == "config" or (clean.startswith("config/") and not clean.endswith(".example.yaml")):
                    problems.append(f"forbidden COPY source: {source}")
    final = _final_stage(dockerfile)
    users = [line.split()[1] for line in final if line.upper().startswith("USER ")]
    if not users or not re.fullmatch(r"[1-9]\d*(:[1-9]\d*)?", users[-1]):
        problems.append("the final stage must end with a numeric non-root USER")
    for line in final:
        if line.upper().startswith("RUN ") and re.search(r"build-essential|\bcurl\b|\bgcc\b", line):
            problems.append(f"build tools in the final stage: {line}")
    return problems


# ---------------------------------------------------------------------------
# D1 .dockerignore, D2 / D3 Dockerfile
# ---------------------------------------------------------------------------


def test_dockerignore_is_an_exact_allow_list():
    lines = [line for line in _read(".dockerignore").splitlines() if line.strip()]
    assert lines[0] == "*"
    assert [line for line in lines if line.startswith("!")] == ALLOW_LIST
    assert lines[-2:] == ["**/__pycache__", "**/*.pyc"]


def test_repository_dockerfile_has_no_violations():
    assert dockerfile_violations(_read("Dockerfile")) == []


@pytest.mark.parametrize(
    "tamper",
    [
        "COPY config ./config",
        "COPY config/ ./config/",
        "COPY config/audit_chain_key.pem ./config/",
        "COPY tests ./tests",
        "COPY . .",
        "ADD app.tar.gz /app",
        "RUN apt-get install -y build-essential",
        "RUN apt-get install -y curl",
    ],
)
def test_tampered_dockerfile_is_caught(tamper):
    text = _read("Dockerfile").replace("WORKDIR /app\nUSER", f"WORKDIR /app\n{tamper}\nUSER")
    assert text != _read("Dockerfile")
    assert dockerfile_violations(text)


@pytest.mark.parametrize("user", ["USER root", "USER 0", "USER 0:0", "USER app"])
def test_root_or_named_user_is_caught(user):
    text = _read("Dockerfile").replace("USER 10001:10001", user)
    assert any("non-root USER" in problem for problem in dockerfile_violations(text))


def test_unpinned_base_image_is_caught():
    text = re.sub(r"@sha256:[0-9a-f]{64}", "", _read("Dockerfile"))
    assert any("not pinned" in problem for problem in dockerfile_violations(text))


def test_dockerfile_start_command_honours_auto_migrate():
    text = _read("Dockerfile")
    assert "${RH_AUTO_MIGRATE:-true}" in text and "set -e" in text and "exec uvicorn" in text
    assert "HEALTHCHECK" in text and "/v1/healthz" in text


def test_worker_disables_the_api_health_check():
    for name in ("docker-compose.yml", "deploy/docker-compose.release.example.yml"):
        assert yaml.safe_load(_read(name))["services"]["worker"]["healthcheck"] == {"disable": True}


def test_runtime_requirements_hold_no_test_tools():
    runtime = _read("requirements.txt")
    for tool in ("pytest", "ruff", "import-linter", "aiosqlite"):
        assert tool not in runtime
    dev = _read("requirements-dev.txt")
    assert "-r requirements.txt" in dev
    for tool in ("pytest", "pytest-asyncio", "ruff", "import-linter", "aiosqlite"):
        assert tool in dev


# ---------------------------------------------------------------------------
# D6 / D7 workflows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["ci.yml", "release-image.yml"])
def test_every_action_is_pinned_to_a_commit(name):
    workflow = _workflow(name)
    uses = [step["uses"] for job in workflow["jobs"].values() for step in job["steps"] if "uses" in step]
    assert uses
    assert [item for item in uses if not SHA_PINNED.match(item)] == []
    assert workflow["permissions"] == {"contents": "read"}


def test_release_workflow_triggers():
    triggers = _workflow("release-image.yml")["on"]
    assert triggers["release"] == {"types": ["published"]}
    assert "ref" in triggers["workflow_dispatch"]["inputs"]
    assert "push" not in triggers


def test_release_workflow_scans_before_it_pushes():
    jobs = _workflow("release-image.yml")["jobs"]
    verify = [step.get("run", "") for step in jobs["verify"]["steps"]]
    assert any("image_content_check.py" in run for run in verify)
    assert jobs["publish"]["needs"] == "verify"


def test_manual_runs_never_push():
    jobs = _workflow("release-image.yml")["jobs"]
    assert jobs["publish"]["if"] == "github.event_name == 'release'"
    builds = [s for s in jobs["verify"]["steps"] if "build-push-action" in s.get("uses", "")]
    assert builds and all(step["with"]["push"] is False for step in builds)
    assert "packages" not in jobs["verify"].get("permissions", {})


def test_publish_job_tags_sbom_and_provenance():
    publish = _workflow("release-image.yml")["jobs"]["publish"]
    assert publish["permissions"] == {"contents": "read", "packages": "write"}
    step = next(s for s in publish["steps"] if "build-push-action" in s.get("uses", ""))
    assert step["with"]["push"] is True and step["with"]["sbom"] is True
    assert step["with"]["provenance"] == "mode=max"
    assert "github.event.release.tag_name" in step["with"]["tags"] and ":latest" in step["with"]["tags"]


def test_release_tag_must_match_the_project_version():
    steps = _workflow("release-image.yml")["jobs"]["verify"]["steps"]
    check = next(step for step in steps if step.get("name") == "Check release tag")
    assert "pyproject.toml" in check["run"] and "^v[0-9]+" in check["run"]


def test_cve_report_does_not_block():
    steps = _workflow("release-image.yml")["jobs"]["verify"]["steps"]
    step = next(s for s in steps if "scan-action" in s.get("uses", ""))
    assert step["continue-on-error"] is True and step["with"]["fail-build"] is False


def test_ci_builds_and_scans_the_image_without_pushing():
    steps = _workflow("ci.yml")["jobs"]["image"]["steps"]
    runs = [step.get("run", "") for step in steps]
    assert any(run.startswith("docker build") for run in runs)
    assert any("image_content_check.py" in run for run in runs)
    assert not any("docker push" in run for run in runs)


# ---------------------------------------------------------------------------
# D4 / D9 release compose and deployment guide
# ---------------------------------------------------------------------------


def test_release_compose_uses_the_image_read_only():
    services = yaml.safe_load(_read("deploy/docker-compose.release.example.yml"))["services"]
    for name in ("api", "worker"):
        service = services[name]
        assert "build" not in service and service["image"].startswith("${RH_IMAGE:-ghcr.io/")
        assert service["read_only"] is True and service["tmpfs"] == ["/tmp"]
        assert "./config:/app/config:ro" in service["volumes"]


def test_release_compose_has_no_literal_password():
    services = yaml.safe_load(_read("deploy/docker-compose.release.example.yml"))["services"]
    assert services["postgres"]["environment"]["POSTGRES_PASSWORD"].startswith("${POSTGRES_PASSWORD:?")
    assert "${POSTGRES_PASSWORD:?" in services["api"]["environment"]["POSTGRES_DSN"]


def test_deployment_guide_covers_the_published_image():
    guide = _read("docs/deployment.md")
    for phrase in ("## Using the Published Image", "RH_AUTO_MIGRATE", "AUDIT_CHAIN_SIGNING_KEY_FILE", "app.jobs.users create",
                   "imagetools inspect", "UID `10001`", "no secrets"):
        assert phrase in guide
    assert "zookeeper" not in guide.lower()


# ---------------------------------------------------------------------------
# D5 image_content_check.py on generated `docker save` archives
# ---------------------------------------------------------------------------


def _layer(files: dict[str, bytes | None]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _archive(tmp_path: Path, layers: list[dict[str, bytes | None]], user: str = "10001:10001") -> Path:
    path = tmp_path / "image.tar"
    names = [f"blobs/sha256/layer{i}" for i in range(len(layers))]
    members = {"blobs/sha256/config": json.dumps({"config": {"User": user}}).encode()}
    members.update({name: _layer(files) for name, files in zip(names, layers)})
    members["manifest.json"] = json.dumps([{"Config": "blobs/sha256/config", "Layers": names}]).encode()
    with tarfile.open(path, "w") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return path


def _rules(report: dict) -> set[str]:
    return {finding["rule"] for finding in report["findings"]}


CLEAN = {
    "app/main.py": b"print('hello')\n",
    "app/config/judge_profiles.example.yaml": b"profiles: []\n",
    "etc/ssl/certs/ca.pem": b"-----BEGIN CERTIFICATE-----\n",
    "opt/venv/lib/python3.12/site-packages/certifi/cacert.pem": b"-----BEGIN CERTIFICATE-----\n",
    "usr/lib/ssl/cert.pem": b"-----BEGIN CERTIFICATE-----\n",
}


def test_clean_image_passes(tmp_path):
    report = scan.scan_archive(_archive(tmp_path, [CLEAN]))
    assert report["status"] == "pass" and report["findings"] == [] and report["user"] == "10001:10001"


@pytest.mark.parametrize(
    "path",
    ["app/config/audit_chain_key.pem", "app/server.key", "root/.ssh/id_rsa", "root/keys/store.p12", "app/.env",
     "app/.env.e2e", "app/.git/config"],
)
def test_secret_files_fail(tmp_path, path):
    report = scan.scan_archive(_archive(tmp_path, [{**CLEAN, path: b"x"}]))
    assert "secret_file" in _rules(report)


def test_private_key_in_a_ca_path_fails(tmp_path):
    key = b"-----BEGIN RSA PRIVATE KEY-----\n"
    report = scan.scan_archive(_archive(tmp_path, [{**CLEAN, "etc/ssl/certs/evil.pem": key}]))
    assert "secret_file" in _rules(report)


def test_secret_deleted_in_a_later_layer_still_fails(tmp_path):
    layers = [{**CLEAN, "app/deploy.key": b"x"}, {"app/.wh.deploy.key": b""}]
    report = scan.scan_archive(_archive(tmp_path, layers))
    finding = next(item for item in report["findings"] if item["rule"] == "secret_file")
    assert finding["deleted_later"] is True and finding["layer"] == 0


def test_secret_content_fails_without_echoing_the_value(tmp_path, capsys):
    fake = "AKIA" + "ABCDEFGHIJKLMNOP"
    archive = _archive(tmp_path, [{**CLEAN, "app/settings.py": f"KEY = '{fake}'\n".encode()}])
    assert scan.main(["--tar", str(archive)]) == 1
    output = capsys.readouterr().out
    assert "secret_content" in output and fake not in output


def test_allowlisted_and_binary_content_passes(tmp_path):
    files = {**CLEAN, "app/fixture.py": b"token = 'sk-abcdefghijklmnop1234'\n", "app/blob.bin": b"\0AKIAABCDEFGHIJKLMNOP"}
    assert scan.scan_archive(_archive(tmp_path, [files]))["findings"] == []


@pytest.mark.parametrize("path", ["app/tests/test_a.py", "app/tests"])
def test_tests_in_the_final_filesystem_fail(tmp_path, path):
    assert "app_tests" in _rules(scan.scan_archive(_archive(tmp_path, [{**CLEAN, path: b"x"}])))


@pytest.mark.parametrize("path", ["app/config/judge_profiles.yaml", "app/config/nested/a.example.yaml"])
def test_local_config_fails(tmp_path, path):
    assert "app_config" in _rules(scan.scan_archive(_archive(tmp_path, [{**CLEAN, path: b"x"}])))


def test_whiteout_removes_tests_from_the_final_filesystem(tmp_path):
    layers = [{**CLEAN, "app/tests/test_a.py": b"x"}, {"app/.wh.tests": b""}]
    assert scan.scan_archive(_archive(tmp_path, layers))["findings"] == []


def test_opaque_directory_hides_lower_files(tmp_path):
    layers = [{**CLEAN, "app/tests/test_a.py": b"x"}, {"app/tests/.wh..wh..opq": b""}]
    assert scan.scan_archive(_archive(tmp_path, layers))["findings"] == []


def test_a_file_restored_after_a_whiteout_fails(tmp_path):
    layers = [{**CLEAN, "app/tests/a.py": b"x"}, {"app/.wh.tests": b""}, {"app/tests/b.py": b"x"}]
    assert "app_tests" in _rules(scan.scan_archive(_archive(tmp_path, layers)))


@pytest.mark.parametrize("user", ["", "root", "0", "0:0", "root:root"])
def test_root_user_fails(tmp_path, user):
    assert "root_user" in _rules(scan.scan_archive(_archive(tmp_path, [CLEAN], user=user)))


def test_compiler_fails(tmp_path):
    assert "compiler" in _rules(scan.scan_archive(_archive(tmp_path, [{**CLEAN, "usr/bin/gcc": b"x"}])))


def test_exit_codes_and_single_json_line(tmp_path, capsys):
    assert scan.main(["--tar", str(_archive(tmp_path, [CLEAN]))]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["status"] == "pass"
    broken = tmp_path / "broken.tar"
    broken.write_bytes(b"not a tar")
    assert scan.main(["--tar", str(broken)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "error"
