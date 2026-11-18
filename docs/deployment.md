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

Each GitHub Release publishes one image for `linux/amd64`:

```text
ghcr.io/leo459028/rewardhacking:vX.Y.Z
ghcr.io/leo459028/rewardhacking:latest
```

`vX.Y.Z` names one release. `latest` moves with every release, so production deployments should pin a version or, better, a digest (`ghcr.io/leo459028/rewardhacking@sha256:...`).

**The image contains no secrets, signing keys, default accounts, local configuration or tests.** It runs as UID / GID `10001`, and root owns the program files. `scripts/image_content_check.py` scans every image before it is pushed.

### 1. Prepare a Directory

Copy [`deploy/docker-compose.release.example.yml`](../deploy/docker-compose.release.example.yml) to an empty directory as `docker-compose.yml`, then create `.env` and `config/` next to it.

| Variable | Required | Purpose |
| :--- | :--- | :--- |
| `POSTGRES_PASSWORD` | Yes | Database password used by the example file. It builds `POSTGRES_DSN`; set `POSTGRES_DSN` yourself for an external database |
| `REDIS_URL`, `KAFKA_BOOTSTRAP_SERVERS` | Set by the example | Point them at your own services when you do not use the bundled ones |
| `APP_ENV` | Yes | `prod` for production |
| `AUTH_ENABLED` | Yes | `true`; API clients then need keys |
| `DASHBOARD_AUTH_MODE` | Yes | `session` (personal accounts) |
| `DASHBOARD_CSRF_SECRET` | Yes with more than one API replica | Random value shared by the replicas |
| `AUDIT_CHAIN_SIGNING_KEY_FILE` | Yes in `prod` | For example `config/audit_chain_key.pem` |
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

### 5. Create the First Administrator

There is no default account. Create the first admin from the command line ([`runbook.md`](runbook.md) §13); the password is read from an environment variable and must be changed at first sign-in:

```bash
docker compose exec -e RH_ADMIN_PASSWORD api python -m app.jobs.users create --username admin --role admin --password-env RH_ADMIN_PASSWORD
```

### 6. Inspect the SBOM and Provenance

```bash
docker buildx imagetools inspect ghcr.io/leo459028/rewardhacking:vX.Y.Z --format '{{ json .SBOM }}'
docker buildx imagetools inspect ghcr.io/leo459028/rewardhacking:vX.Y.Z --format '{{ json .Provenance }}'
```

Images are not signed yet; signing is planned for `v1.6.5`.

---

## Production Readiness & Hardening Checklist

When preparing for production deployment (`APP_ENV=prod`):

1. **Authentication Enforcement**:
   - Set `AUTH_ENABLED=true` and `DASHBOARD_AUTH_MODE=session`, then create the first Dashboard admin with `python -m app.jobs.users create --role admin ...` (`docs/runbook.md` §13).
   - Remove bootstrap fallback keys (`API_KEYS`) and provision hashed tenant keys via `/v1/tenants/{tenant_id}/api-keys`.
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
