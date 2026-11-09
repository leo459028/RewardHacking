---
name: rh-testing-conventions
description: RewardHacking test-writing conventions for review / approval / honeypot / attestation / LLM judge / sampling / judge activation paths, plus the zero-DeprecationWarning rule. Use before writing or fixing tests in tests/ that touch evaluation, reviews, judges, sampling, approvals or tracing.
---

# Testing conventions

Full details: `docs/testing_guide.md` § Agent Test Conventions (read the bullet for the path you are testing).

Most common reminders:

- Every pytest run must pass with `-W error::DeprecationWarning`; Pydantic decimals use `PlainSerializer(format_decimal_plain, ...)`, never `json_encoders`.
- Never call a real LLM: drive `openai_compatible` / `anthropic` judges through `httpx.MockTransport` (`app.services.evaluation.judges._TEST_TRANSPORT`).
- `tests/conftest.py` is autouse-heavy: random review sampling is off (`EVALUATION_SAMPLE_RANDOM_RATE=0`), judge activation trusts environment profiles without a probe (use the `activation_gate` fixture to test the real gate), the startup probe is disabled and the judge breaker store is in memory.
- Tests must not depend on the developer's `.env` or `config/judge_profiles.yaml`: build `Settings(_env_file=None, ...)` and pass `evaluation_judge_profiles_file=""`.
- Background triage: wait with `client.portal.call(judge_background.drain_background, 15.0)`, not HTTP polling.
- Tracing (v1.6.0): `client.event_publisher.events[i]["headers"]["traceparent"]` holds the propagated trace; drive the worker with `app.workers.consumer.process_message(...)`.
- Fake secrets in tests: keep them short or add them to the `scripts/security_check.py` allowlist (the scan reads the whole git history).
- New test files follow the release digits: `tests/test_v16x_<feature>.py`.
