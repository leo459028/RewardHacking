# Developer Guide & Local Setup

> **Guide Version**: `v1.5.2`  
> **Python Version**: `>=3.11`  
> **Linter**: `ruff`  
> **Test Runner**: `pytest`

This document details the project structure, developer workflows, coding standards, and testing procedures for engineers contributing to **RewardHacking**.

---

## Repository Structure

```text
RewardHacking/
├── app/
│   ├── api/            FastAPI route handlers and dependency injectors
│   ├── core/           Configuration settings (pydantic-settings) and structured logging
│   ├── infra/          Kafka, Redis, and PostgreSQL asynchronous engine clients
│   ├── models/         SQLAlchemy ORM models for runs, checkpoints, and FinOps
│   ├── observability/  Prometheus metrics declarations and HTTP middleware
│   ├── schemas/        Pydantic validation schemas for requests and responses
│   ├── seeds/          Official model pricing YAML catalog seeds
│   ├── services/       Policy engine, prompt sanitizer, evaluator, control plane, FinOps
│   ├── ui/             Jinja2 HTML templates and operational dashboard routes
│   └── workers/        Kafka asynchronous event consumer and bounded tool executor
├── docs/               System documentation and operational guides
├── migrations/         Alembic database migration versions and environment scripts
├── scripts/            Utility scripts (pricing catalog importer, security checker)
├── tests/              Pytest test suite (API, unit, regression, red-team corpus)
├── .env.example        Environment configuration template
├── docker-compose.yml  Multi-container Docker orchestration manifest
├── Makefile            Common development shortcut targets
└── pyproject.toml      Project metadata, dependencies, and build configuration
```

---

## Local Development Workflow

### 1. Environment Setup
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

### 2. Running Backing Services
You can run backing services using Docker Compose while executing Python code locally:
```bash
docker compose up -d postgres redis kafka zookeeper
alembic upgrade head
```

### 3. Running the Server Locally
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 4. Running Quality Checks
Before submitting code, always run the linter, security scan, and test suite:
```bash
# Code style and linting
ruff check app tests scripts

# Security and secret leakage scan
python scripts/security_check.py

# Complete test suite
pytest -q
```

---

## Adding Database Migrations

When modifying SQLAlchemy models under `app/models/`:

1. Create a new migration revision:
   ```bash
   alembic revision -m "add_new_feature_table"
   ```
2. Implement `upgrade()` and `downgrade()` in the generated script under `migrations/versions/`.
3. Apply the migration:
   ```bash
   alembic upgrade head
   ```
4. Verify database state:
   ```bash
   alembic current
   ```

---

## Dashboard Development Notes

- Forms submitted via HTML dashboard routes use `application/x-www-form-urlencoded` parsed via `request.form()`. The runtime includes `python-multipart` to support this.
- When creating or modifying dashboard routes, ensure both the `GET` view and the corresponding `POST` action are tested in `tests/` to guarantee reliable operation.
- If introducing new Python packages, ensure both `requirements.txt` and `pyproject.toml` are updated synchronously.
