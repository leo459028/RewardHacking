# Project Working Guidelines & Agent Specification

This document provides operational guidelines and engineering standards for AI agents and human engineers working on this repository. Please adhere to these guidelines first before referring to `README.md`.

---

## Scope of Application

These specifications apply across the entire repository. Unless a subdirectory provides a more specific `AGENTS.md`, all code modifications, test additions, and documentation updates must comply with this document.

---

## 1. Planning & Proposal Requirements

Any feature addition, risk decision modification, data query update, audit logging change, or plan impacting user-visible behavior must include accessible UI or Dashboard entry points for querying and testing.
If a change is purely documentation, tests, code formatting, or internal refactoring with no user-visible behavioral impact, explain explicitly why UI/Dashboard verification is not applicable.

Every proposal must contain:
- Functional goals and concrete acceptance criteria.
- UI / Dashboard verification instructions (navigation, inputs, expected visual state).
- Implementation design and list of affected modules.
- Verification test suite and exact executable shell commands.
- List of documentation files to be updated after implementation.
- Required test data and metrics to be recorded into `docs/test_report.md`.

Do not write vague conceptual sketches; ensure the plan provides actionable step-by-step instructions that subsequent engineers can execute directly.

---

## 2. Implementation Requirements

- **Maintain Architectural Continuity**: When extending or fixing existing releases, follow established architectural boundaries and dataflows unless an optimal paradigm shift is justified.
- **Read Before Modifying**: Inspect existing source files and test suites before making changes.
- **Preserve Unrelated Changes**: Never revert or discard existing user modifications in the working tree. If unrelated dirty changes exist, preserve them and only modify files within the required task scope.

---

## 3. Testing Requirements

- **Comprehensive Verification**: Execute complete automated test suites (`pytest -q`, `ruff check app tests scripts`, `python scripts/security_check.py`) after implementation.
- **Docker Verification**: If Docker is required, remind the user to start Docker Desktop before executing Docker-dependent checks.
- **Mandatory Browser E2E Verification (`docs/manual_test.md` & `scripts/browser_manual_test.py`)**: Every new release or change impacting user-visible routes, forms, APIs, or Dashboard views MUST:
  1. Update `docs/manual_test.md` and `scripts/browser_manual_test.py` to include scenarios covering the new release features.
  2. Launch a real browser and execute the complete interactive walkthrough (`python scripts/browser_manual_test.py` or equivalent browser automation) against the running stack.
  3. Verify `0` Console errors, `0` Page errors, `0` failed HTTP requests (`>=400`), and `0` formatting artifacts (such as `0E-8`).
- **Record Actual Results**: All actual test execution results (including pytest, Docker runtime, and Browser E2E test outputs and artifact IDs) must be recorded in `docs/test_report.md`. Entries must capture: execution date, exact commands, passed counts, Dashboard/Browser verification status, Docker verification status, and known limitations. Never record speculative or theoretical results.

---

## 4. Documentation Synchronization Requirements

- **Post-Test Synchronization**: Verify that code and documentation remain synchronized after all tests pass.
- **Synchronized Truth**: Documents must not merely describe theoretical intent; they must accurately reflect current implementation, routes, database schemas, metadata, Dashboard views, test counts, and boundaries.
- **README Maintenance**: When updating `README.md`, do not arbitrarily alter target use cases, non-applicable scenarios, or limitations unless explicitly instructed by the user.
- **Preserve Historical Context**: Do not discard historical sections unless they are obsolete, conflicting, or misleading. Prefer appending release sections, version badges, or historical notes so future contributors can trace architectural evolution.
- **Focused Updates**: Only update affected or out-of-date documentation files; avoid modifying unrelated files solely for synchronization.

---

## 5. Git Operations Policy

- **No Unauthorized Git Mutations**: Do not execute `git commit`, `git push`, or `git reset` unless explicitly commanded by the user.
- **Pre-Commit Disclosure**: State clearly which files will be staged and verify that unrelated changes are not commingled.
- **Protect Working Tree**: If the working tree contains pre-existing user changes, preserve them untouched.

---

## 6. Special Project Notices

- **Primary Entrance**: The root `README.md` serves as the primary gateway for users and external contributors.
- **Image Generation**: If generating diagrams, infographics, or mockups, default to ChatGPT Image2, save assets with versioned identifiers under `docs/assets/`, and avoid overwriting historical assets.
- **Transparent Communication**: Clearly explain what was accomplished, what was tested, and which limitations or known boundaries remain.
