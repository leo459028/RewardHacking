# Contributing to RewardHacking

Thank you for your interest in contributing to **RewardHacking**! We welcome contributions from the community to help make autonomous AI agents safer, more verifiable, and economically accountable.

Please read this document before submitting issues, feature proposals, or pull requests.

---

## Code of Conduct

All contributors and participants are expected to adhere to our [Code of Conduct](CODE_OF_CONDUCT.md). Please report unacceptable behavior to the project maintainers.

---

## Getting Started

### Prerequisites

- **Python**: 3.11 or higher
- **Docker & Docker Compose**: Required for running the complete service stack (FastAPI, Postgres, Redis, Kafka)
- **Git**

### Local Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/your-username/RewardHacking.git
   cd RewardHacking
   ```

2. **Create a virtual environment**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up local environment variables**:
   ```bash
   cp .env.example .env
   ```

5. **Run the local Docker stack** (optional, recommended for integration tests):
   ```bash
   docker compose up -d --build
   ```

---

## Development & Coding Standards

### Core Architectural Principles

1. **Security Fail-Closed**: In case of policy evaluation errors or missing credentials, execution plans must fail closed (escalate or deny).
2. **Deterministic Evaluation**: Policy checks, risk scoring, and FinOps usage accounting must be deterministic and reproducible.
3. **Audit Trail Completeness**: Every agent submission, decision, execution attempt, and approval action must be permanently logged in the PostgreSQL audit trail with an associated `X-Trace-Id`.
4. **No Side-Effects in Dry-Run**: Bounded execution simulations and dry-run replay attempts must never perform destructive external tool mutations.

### Code Style & Formatting

We use [Ruff](https://github.com/astral-sh/ruff) for fast Python linting and code quality checks.

- Verify code style before committing:
  ```bash
  ruff check app tests scripts
  ```
- Ensure comprehensive type annotations and clear function docstrings.
- Never hardcode secrets, API keys, or credentials. Run the built-in secret scanner:
  ```bash
  python scripts/security_check.py
  ```

---

## Testing Guidelines

Every code modification must be accompanied by corresponding tests or verification commands.

1. **Run the test suite**:
   ```bash
   pytest -q
   ```
2. **Database migrations**:
   If modifying models, generate and test Alembic migrations:
   ```bash
   alembic revision --autogenerate -m "describe change"
   alembic upgrade head
   ```
3. **Verify the Operational Dashboard & Browser E2E Suite**:
   Ensure web routes and templates render cleanly without template syntax errors or console errors by following [`docs/manual_test.md`](docs/manual_test.md) and running the automated browser walkthrough:
   ```bash
   python scripts/browser_manual_test.py
   ```
   - Check Overview: `http://localhost:8000/dashboard`
   - Check Operations Gate: `http://localhost:8000/dashboard/operations`
   - Check FinOps Costs & Budgets: `http://localhost:8000/dashboard/costs` & `http://localhost:8000/dashboard/admin/finops/budgets`

---

## Submitting Pull Requests

1. **Branch naming**: Use descriptive branch names:
   - `feat/feature-name`
   - `fix/bug-description`
   - `docs/documentation-update`
2. **Keep PRs focused**: Avoid bundling unrelated changes into a single pull request.
3. **Fill out the PR template**: Provide a concise summary, motivation, and verification commands.
4. **CI Checks**: Ensure that all GitHub Actions workflow checks (Lint, Security Scan, Pytest) pass.

---

## Reporting Security Issues

Please **DO NOT** submit public GitHub issues for security vulnerabilities. Instead, refer to our [Security Policy](SECURITY.md) for responsible disclosure instructions.
