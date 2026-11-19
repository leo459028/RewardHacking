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
import security_check  # noqa: E402

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
    "!docker/sandbox-python/third_party_notices.py",  # v1.6.2: builds the third-party notices; removed after use
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


def test_publish_job_pushes_only_the_images_it_scanned():
    steps = _workflow("release-image.yml")["jobs"]["publish"]["steps"]
    scans = [index for index, step in enumerate(steps) if "image_content_check.py" in step.get("run", "")]
    pushes = [index for index, step in enumerate(steps) if step.get("with", {}).get("push") is True]
    assert len(scans) == 2 and len(pushes) == 2 and max(scans) < min(pushes)
    assert not [step["name"] for step in steps if "cache-from" in step.get("with", {})]
    confirm = next(step for step in steps if step.get("name") == "Confirm the published images are the scanned images")
    assert steps.index(confirm) > max(pushes) and ".RootFS.Layers" in confirm["run"]


def test_manual_runs_never_push():
    jobs = _workflow("release-image.yml")["jobs"]
    assert jobs["publish"]["if"] == "github.event_name == 'release'"
    builds = [s for s in jobs["verify"]["steps"] if "build-push-action" in s.get("uses", "")]
    assert builds and all(step["with"]["push"] is False for step in builds)
    assert "packages" not in jobs["verify"].get("permissions", {})


def test_publish_job_tags_sbom_and_provenance():
    publish = _workflow("release-image.yml")["jobs"]["publish"]
    assert publish["permissions"] == {"contents": "read", "packages": "write"}
    step = next(s for s in publish["steps"] if s.get("with", {}).get("push") is True)
    assert step["with"]["sbom"] is True
    assert step["with"]["provenance"] == "mode=max"
    assert "github.event.release.tag_name" in step["with"]["tags"] and ":latest" in step["with"]["tags"]


def test_latest_moves_only_for_the_latest_release():
    steps = _workflow("release-image.yml")["jobs"]["publish"]["steps"]
    lookup = next(step for step in steps if step.get("id") == "image")
    assert "releases/latest" in lookup["run"] and 'latest_release" = "$TAG"' in lookup["run"] and "|| true" in lookup["run"]
    for step in (s for s in steps if s.get("with", {}).get("push") is True):
        latest = [line for line in step["with"]["tags"].splitlines() if ":latest" in line]
        assert latest and all("steps.image.outputs.latest == 'true'" in line for line in latest)


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
        assert service["cap_drop"] == ["ALL"] and service["security_opt"] == ["no-new-privileges:true"]
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


FAKE_TOKEN = "sk-" + "a" * 24
FAKE_ACCESS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
KEY_HEADER = "-----BEGIN RSA " + "PRIVATE KEY-----"
PASSWORD_LINE = "password" + " = 'real-value-123'"
MOCK_PASSWORD_LINE = "password" + " = 'mock-value-123'"


def test_mock_password_and_binary_content_pass(tmp_path):
    files = {**CLEAN, "app/fixture.py": MOCK_PASSWORD_LINE.encode(), "app/blob.bin": b"\0" + FAKE_ACCESS_KEY.encode()}
    assert scan.scan_archive(_archive(tmp_path, [files]))["findings"] == []


@pytest.mark.parametrize("name", ["app/signing.pfx", "app/store.jks", "app/release.keystore", "app/UPPER.PEM"])
def test_more_key_file_types_fail_the_image_scan(tmp_path, name):
    assert "secret_file" in _rules(scan.scan_archive(_archive(tmp_path, [{**CLEAN, name: b"x"}])))


@pytest.mark.parametrize("path", ["C:" + "\\Users\\someone\\project", "D:" + "/Users/" + "someone/x", "/Users/" + "someone/x", "/home/" + "someone/x"])
def test_a_developer_home_path_fails_the_image_scan(tmp_path, path):
    files = {**CLEAN, "app/report.md": f"saved to {path}\n".encode()}
    assert "local_path" in _rules(scan.scan_archive(_archive(tmp_path, [files])))


def test_an_app_file_too_large_to_read_fails_the_image_scan(tmp_path):
    files = {**CLEAN, "app/huge.txt": b"x" * (scan.MAX_TEXT_BYTES + 1)}
    assert "unscanned_file" in _rules(scan.scan_archive(_archive(tmp_path, [files])))


@pytest.mark.parametrize(
    "section",
    [{"Env": [f"OPENAI_API_KEY={FAKE_TOKEN}"]}, {"Labels": {"note": FAKE_TOKEN}}],
)
def test_a_secret_in_the_image_config_fails_the_scan(section):
    assert [item["rule"] for item in scan.check_config({"config": section})] == ["config_secret"]


def test_a_secret_in_the_build_history_fails_the_scan():
    config = {"config": {}, "history": [{"created_by": f"RUN /bin/sh -c echo {FAKE_TOKEN}"}]}
    assert [item["rule"] for item in scan.check_config(config)] == ["config_secret"]


def test_the_base_image_settings_pass_the_config_scan():
    config = {"config": {"Env": ["PATH=/usr/bin", "GPG_KEY=7169605F62C751356D054A26A821E680E5FA6305"], "Labels": None}, "history": [{"created_by": "RUN pip install x"}]}
    assert scan.check_config(config) == []


def test_a_token_marked_as_mock_still_fails_the_image_scan(tmp_path):
    files = {**CLEAN, "app/fixture.py": f"token = '{FAKE_TOKEN}'  # mock\n".encode()}
    assert "secret_content" in _rules(scan.scan_archive(_archive(tmp_path, [files])))


@pytest.mark.parametrize("word", ["mock", "dummy", "demo-key", "key-a"])
@pytest.mark.parametrize(
    ("value", "rule"),
    [(FAKE_TOKEN, "OpenAI API Key"), (FAKE_ACCESS_KEY, "AWS Access Key ID"), (KEY_HEADER, "Private Key")],
)
def test_allowlist_words_never_excuse_a_token_or_private_key(value, rule, word):
    assert security_check.line_findings(f"{value}  # {word}") == [rule]


def test_allowlist_words_only_excuse_the_password_rule():
    assert security_check.line_findings(PASSWORD_LINE) == [security_check.PASSWORD_RULE]
    assert security_check.line_findings(MOCK_PASSWORD_LINE) == []


def test_a_reviewed_fixture_line_passes_only_when_it_is_unchanged():
    line = 'key = b"' + KEY_HEADER + '\\n"'
    assert security_check.line_findings(f"    {line}") == []
    assert security_check.line_findings(line + "  # mock") == ["Private Key"]
    assert security_check.line_findings(line.replace("RSA", "EC")) == ["Private Key"]


def test_reviewed_fixture_lines_are_digests_with_a_reason():
    assert security_check.KNOWN_FIXTURE_LINES
    for digest, reason in security_check.KNOWN_FIXTURE_LINES.items():
        assert re.fullmatch(r"[0-9a-f]{64}", digest) and reason.strip()


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
