# Deployment & Production Hardening

> **Deployment Version**: `v1.6.2`
> **Previous Versions**: `v1.5.2`, `v1.5.1`, `v1.5.0`, `v1.0.0`

This document covers running **RewardHacking** with Docker Compose from source, running the published container image, and production hardening.

---

## Docker Compose Quickstart

The project provides a self-contained `docker-compose.yml` orchestrating all required services:
- **`api`**: FastAPI HTTP gateway and Jinja2 operational dashboard.
- **`worker`**: Background worker consuming agent requests from Kafka.
- **`postgres`**: Relational audit trail and state store.
- **`redis`**: State caching, tool circuit breakers, and rate limit quotas.
- **`kafka`**: Event bus in KRaft mode (`kafka-init` creates the topics once).

### 1. Initialize Configuration
```bash
cp .env.example .env
```

### 2. Build and Launch
```bash
docker compose up -d --build
```
*Note: The API container runs `alembic upgrade head` before serving HTTP traffic unless `RH_AUTO_MIGRATE=false`.*

### 3. Verify Health & Logs
```bash
# Check container status
docker compose ps

# Verify API and DB connectivity
curl -f http://localhost:8000/v1/healthz
curl -f http://localhost:8000/v1/readyz

# Tail service logs
docker compose logs -f api worker
```

### 4. Teardown
```bash
docker compose down
```

The local `postgres` service keeps its data in an anonymous volume. `docker compose down` followed by `up` starts with an empty database, so use `docker compose stop` to keep local data.

---

## Hybrid Mode: Infrastructure Containers Only

To run the API or worker natively in Python while leveraging Docker for databases and queues:

```bash
# Install the application and the development tools
pip install -r requirements-dev.txt

# Launch only backing services
docker compose up -d postgres redis kafka kafka-init

# Run database migrations locally
alembic upgrade head

# Seed model pricing catalog
python scripts/import_pricing_catalog.py --apply --file app/seeds/model_pricing_seed.yaml

# Start API gateway locally
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Using the Published Image

Each GitHub Release publishes two public images for `linux/amd64`: the application, and the sandbox image that platform re-runs execute in.

```text
ghcr.io/leo459028/rewardhacking:vX.Y.Z
ghcr.io/leo459028/rewardhacking:latest
ghcr.io/leo459028/rewardhacking-sandbox-python:vX.Y.Z
ghcr.io/leo459028/rewardhacking-sandbox-python:latest
```

`vX.Y.Z` names one release. `latest` follows the release GitHub marks as Latest (never a prerelease or a later fix of an older series), so production deployments should pin a version or, better, a digest (`ghcr.io/leo459028/rewardhacking@sha256:...`).

**The image contains no secrets, signing keys, default accounts, local configuration or tests.** It runs as UID / GID `10001`, and root owns the program files. `scripts/image_content_check.py` scans every image before it is pushed. The images also contain third-party open-source software: its notices and license texts are in `/licenses/THIRD_PARTY_NOTICES.txt` inside each image, and [`third_party_licenses.md`](third_party_licenses.md) explains them and how to obtain the source.

### 1. Prepare a Directory

Copy [`deploy/docker-compose.release.example.yml`](../deploy/docker-compose.release.example.yml) to an empty directory as `docker-compose.yml`, then create `.env` and `config/` next to it.

| Variable | Required | Purpose |
| :--- | :--- | :--- |
| `POSTGRES_PASSWORD` | Yes | Database password used by the example file. It builds `POSTGRES_DSN`; set `POSTGRES_DSN` yourself for an external database |
| `REDIS_URL`, `KAFKA_BOOTSTRAP_SERVERS` | Set by the example | Point them at your own services when you do not use the bundled ones |
| `APP_ENV` | Yes | `prod` for production |
| `AUTH_ENABLED` | Yes | `true`; API clients then need keys |
| `API_KEYS` | Yes: empty | Bootstrap keys read from the environment. Since `v1.6.3` they are accepted only when `APP_ENV` is `dev` or `test` and the program default is empty; **up to `v1.6.2`, leaving it unset fell back to the development key `demo-key` with the admin role**. Keep it empty (the example file does) and create hashed tenant keys instead (step 5) |
| `DASHBOARD_AUTH_MODE` | Yes | `session` (personal accounts) |
| `DASHBOARD_CSRF_SECRET` | Yes with more than one API replica | Random value shared by the replicas |
| `AUDIT_CHAIN_SIGNING_KEY_FILE` | Yes in production | For example `config/audit_chain_key.pem` |
| `AUDIT_CHAIN_PUBLIC_KEYS_FILE` | After a key rotation | Public keys that still verify older checkpoints |
| `RH_AUTO_MIGRATE` | No (default `true`) | See step 4 |
| `RH_IMAGE` | No | Image the example file runs; pin it to a version or digest |

The complete list is in [`configuration.md`](configuration.md).

### 2. Mount Configuration and Keys Read-Only

The example mounts `./config` at `/app/config` read-only. Put the audit chain key and an optional `judge_profiles.yaml` there. UID `10001` must be able to read them, and nobody else should:

```bash
chown -R 10001:10001 config
chmod 750 config
chmod 640 config/*
```

Keep the private key out of the database and out of any image (see [`security.md`](security.md), Tamper-Evident Audit Chain).

### 3. Start

```bash
docker compose pull
docker compose up -d
docker compose ps
curl -f http://localhost:8000/v1/readyz
```

The example runs `api` and `worker` with a read-only root filesystem and a `tmpfs` at `/tmp`.

### 4. Database Migrations

With `RH_AUTO_MIGRATE=true` (the default) the API container runs `alembic upgrade head` before it starts, and a failed migration stops the start. **With more than one API replica, only one instance may migrate**: two concurrent upgrades on a new database can collide. Leave it `true` on one instance, or run a one-off job, and set `RH_AUTO_MIGRATE=false` on the others:

```bash
docker compose run --rm -e RH_AUTO_MIGRATE=false api alembic upgrade head
```

The `worker` never runs migrations.

Automatic migration means the API's database account can change the schema. Where that is not acceptable, set `RH_AUTO_MIGRATE=false` everywhere, run the one-off job above with an account that owns the schema, and give the API and the worker an account that can only read and write rows. A separate migration account is planned for the `v1.6.6` installer.

### 5. Create the First Administrator

There is no default account. Create the first admin from the command line ([`runbook.md`](runbook.md) §13); the password is read from an environment variable and must be changed at first sign-in:

```bash
docker compose exec -e RH_ADMIN_PASSWORD api python -m app.jobs.users create --username admin --role admin --password-env RH_ADMIN_PASSWORD
```

Then create the API keys your clients need as hashed tenant keys (`/dashboard/governance`, or `POST /v1/tenants/{tenant_id}/api-keys`). A deployment without a Dashboard admin can create its first key from the command line; the plaintext is printed once:

```bash
docker compose exec -T api python -m app.jobs.api_keys create --tenant default --role admin --name first-admin-key
```

Confirm that `/dashboard/operations` shows `bootstrap_keys_in_prod` as `pass`. Upgrading from `v1.6.2` or earlier: `env_created_api_keys` lists database keys that an environment key (possibly `demo-key`) created, and `unattributed_api_keys` those created while the API needed no key (`AUTH_ENABLED=false`, the old default); see [`runbook.md`](runbook.md) §17. A new production install also fails `audit_chain_integrity` until the first checkpoint exists (`python -m app.jobs.audit_chain --checkpoint`, [`runbook.md`](runbook.md) §14) and `safety_regression_evidence` until evidence is recorded.

### 6. Inspect the SBOM and Provenance

```bash
docker buildx imagetools inspect ghcr.io/leo459028/rewardhacking:vX.Y.Z --format '{{ json .SBOM }}'
docker buildx imagetools inspect ghcr.io/leo459028/rewardhacking:vX.Y.Z --format '{{ json .Provenance }}'
```

Images are not signed yet; signing is planned for `v1.6.6`.

---

### 7. Running the Image Directly (`docker run`)

Since `v1.6.3` the image sets `APP_ENV=prod`. Compose `env_file` and `environment` entries override it, so the Compose Quickstart (`cp .env.example .env`, `APP_ENV=dev`) is unchanged. When you start the image yourself with `docker run`, it behaves as production unless you say otherwise:

- Pass the connection settings (`POSTGRES_DSN`, `REDIS_URL`, `KAFKA_BOOTSTRAP_SERVERS`) and the production settings in the table above.
- `AUTH_ENABLED` defaults to `true` and `DASHBOARD_AUTH_MODE` to `session`. With any `APP_ENV` other than `dev` or `test`, **the API refuses to start** when `AUTH_ENABLED=false` or the Dashboard sign-in mode resolves to `off`, and it refuses environment keys from `API_KEYS` (401).
- For a local trial only, set `APP_ENV=dev` explicitly:

```bash
docker run --rm -p 8000:8000 --env-file .env -e APP_ENV=dev ghcr.io/leo459028/rewardhacking:vX.Y.Z
```

## Optional: Sandbox Runner (`v1.6.2`)

`sandbox-runner` is the only service that talks to Docker. It is off by default.

1. Pull the conformance image and set the sandbox variables in `.env` (see [`configuration.md`](configuration.md)): `SANDBOX_BACKEND=docker`, `SANDBOX_EXECUTION_ENABLED=true`.
2. On Linux set `DOCKER_SOCKET_GID` to the group of `/var/run/docker.sock`.
3. Start it with `docker compose --profile sandbox up -d`, then check `/dashboard/sandbox`: the gate must read `ready`.

The Docker socket gives root-level access to the host, so run the runner on a dedicated host or VM in production and use `SANDBOX_DOCKER_RUNTIME=runsc` for untrusted code. The published `rewardhacking-sandbox-python` image holds `pytest` and `coverage`, runs as UID 65534 and can be mirrored to an internal registry and configured by digest.

## Production Readiness & Hardening Checklist

When preparing for production deployment (`APP_ENV=prod`):

1. **Authentication Enforcement**:
   - Set `AUTH_ENABLED=true` and `DASHBOARD_AUTH_MODE=session`, then create the first Dashboard admin with `python -m app.jobs.users create --role admin ...` (`docs/runbook.md` §13).
   - Leave `API_KEYS` empty and provision hashed tenant keys with `python -m app.jobs.api_keys create` or `/v1/tenants/{tenant_id}/api-keys`. Since `v1.6.3` the program default is empty and environment keys are refused outside `dev` / `test`; up to `v1.6.2` an unset variable fell back to the development key `demo-key` (tenant `default`, role admin), so set it empty explicitly when you still run `v1.6.2`.
   - Since `v1.6.3` the API refuses to start in production with `AUTH_ENABLED=false` or Dashboard sign-in `off`. After an upgrade from `v1.6.2` or earlier, review `env_created_api_keys` and `unattributed_api_keys` on `/dashboard/operations` ([`runbook.md`](runbook.md) §17).
2. **Operations Gate Clearance**:
   - In `prod`, the `/dashboard/operations` gate enforces fail-closed checks:
     - No unresolved critical incidents.
     - Dead Letter Queue (DLQ) count = 0.
     - Fresh backup drill evidence submitted within 30 days.
     - Secret rotation evidence submitted within 90 days.
     - Deterministic safety regression test suite passing.
3. **External Secret Management**:
   - Inject `POSTGRES_DSN`, Redis passwords, and API secrets dynamically via AWS Secrets Manager, GCP Secret Manager, or HashiCorp Vault.
4. **Container Isolation for Agent Tools**:
   - If extending the bounded executor to run shell commands or arbitrary python scripts, wrap workers in isolated container boundaries (gVisor or Kata Containers).
