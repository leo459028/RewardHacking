# Deployment & Production Hardening

> **Deployment Version**: `v1.5.2`  
> **Previous Versions**: `v1.5.1`, `v1.5.0`, `v1.0.0`

This document provides deployment instructions for running the **RewardHacking** stack via Docker Compose, configuring isolated local infrastructure, and production hardening recommendations.

---

## Docker Compose Quickstart

The project provides a self-contained `docker-compose.yml` orchestrating all required services:
- **`api`**: FastAPI HTTP gateway and Jinja2 operational dashboard.
- **`worker`**: Background worker consuming agent requests from Kafka.
- **`postgres`**: Relational audit trail and state store.
- **`redis`**: State caching, tool circuit breakers, and rate limit quotas.
- **`kafka` & `zookeeper`**: Distributed event bus for async decoupling.

### 1. Initialize Configuration
```bash
cp .env.example .env
```

### 2. Build and Launch
```bash
docker compose up -d --build
```
*Note: The API container entrypoint automatically executes `alembic upgrade head` before serving HTTP traffic.*

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

---

## Hybrid Mode: Infrastructure Containers Only

To run the API or worker natively in Python while leveraging Docker for databases and queues:

```bash
# Launch only backing services
docker compose up -d postgres redis kafka zookeeper

# Run database migrations locally
alembic upgrade head

# Seed model pricing catalog
python scripts/import_pricing_catalog.py --apply --file app/seeds/model_pricing_seed.yaml

# Start API gateway locally
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Production Readiness & Hardening Checklist

When preparing for production deployment (`APP_ENV=prod`):

1. **Authentication Enforcement**:
   - Set `AUTH_ENABLED=true` and `DASHBOARD_AUTH_ENABLED=true`.
   - Never rely on default passwords (`admin:admin`).
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
