"""OpenAI-compatible chat-completions judge (v1.5.10).

Works with hosted APIs and local servers that speak ``POST {base_url}/chat/completions``
(for example vLLM or Ollama). Guardrails:
- external endpoints are refused unless ``EVALUATION_JUDGE_ALLOW_EXTERNAL=true`` (D10, parsed hostnames);
- a hard timeout (D6); any failure returns a non-``ok`` status so the caller falls back;
- the API key is sent only as an ``Authorization`` header and never logged or returned.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

import httpx

from app.services.evaluation.judges.base import JudgeInput, JudgeOutput
from app.services.evaluation.judges.parsing import JudgeOutputError, judge_output_schema, parse_judge_output
from app.services.evaluation.judges.profiles import JudgeProfile
from app.services.evaluation.judges.prompt import build_messages, prompt_version
from app.services.mcp_gateway import is_external_endpoint


AUTO_MODE_ORDER: tuple[str, ...] = ("json_schema", "json_object", "none")
FORMAT_REJECTION_STATUS = frozenset({400, 422})
FORMAT_REJECTION_MARKERS: tuple[str, ...] = ("response_format", "json_schema", "json_object", "structured", "schema", "format")
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
RETRY_BASE_SECONDS = 1.0
RETRY_MAX_SECONDS = 8.0
RETRY_AFTER_MAX_SECONDS = 10.0
# Per-process memory of the structured-output mode each (base_url, model) accepted (D7).
_MODE_CACHE: dict[tuple[str, str], str] = {}


class OpenAICompatibleJudge:
    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 8.0,
        max_output_tokens: int = 600,
        allow_external: bool = False,
        internal_suffixes: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        profile: JudgeProfile | None = None,
    ):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.model = (model or "").strip()
        self._api_key = api_key or ""
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_tokens = int(max_output_tokens)
        self.allow_external = bool(allow_external)
        self.internal_suffixes = list(internal_suffixes or [])
        self._transport = transport
        # v1.5.11: named profiles prefix judge_version; the synthesized legacy ``default`` keeps v1.5.10 strings (D3, D6).
        self.profile = profile
        self.profile_label = profile.name if profile is not None else ""
        self._version_prefix = profile.name if profile is not None and not profile.legacy else ""
        self.structured_output = profile.structured_output if profile is not None else "json_object"
        self.max_retries = profile.max_retries if profile is not None else 0
        self.strip_reasoning = profile.strip_reasoning if profile is not None else True

    def version(self) -> str:
        base = f"{self.name}:{self.model or 'unset'}:{prompt_version()}"
        return f"{self._version_prefix}:{base}" if self._version_prefix else base

    @property
    def is_external(self) -> bool:
        return is_external_endpoint("streamable_http", self.base_url, self.internal_suffixes)

    def _output(self, status: str, **kwargs: Any) -> JudgeOutput:
        return JudgeOutput(
            provider=self.name,
            model=self.model,
            judge_version=self.version(),
            prompt_version=prompt_version(),
            status=status,
            profile=self.profile_label,
            **kwargs,
        )

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self._transport) as client:
            return await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)

    # -- v1.5.11 structured output modes (D7) ------------------------------------------------------------

    @property
    def _cache_key(self) -> tuple[str, str]:
        return (self.base_url, self.model)

    def _mode_sequence(self) -> list[str]:
        """``auto`` tries json_schema → json_object → none, starting from the mode that worked last time."""
        if self.structured_output != "auto":
            return [self.structured_output]
        cached = _MODE_CACHE.get(self._cache_key)
        if cached in AUTO_MODE_ORDER:
            return list(AUTO_MODE_ORDER[AUTO_MODE_ORDER.index(cached):])
        return list(AUTO_MODE_ORDER)

    def _body(self, messages: list[dict[str, str]], mode: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
        }
        if mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "judge_triage", "strict": True, "schema": judge_output_schema()},
            }
        elif mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        return body

    # -- v1.5.11 bounded retries (D9) ------------------------------------------------------------------------

    async def _send(self, body: dict[str, Any], deadline: float) -> tuple[httpx.Response | None, str, int]:
        """POST with retries on 429 / 502 / 503 / 504 and connection errors, all inside the profile timeout.

        Returns ``(response, error, requests_made)``; ``error`` is ``timeout`` or an exception class name when no
        response was obtained. Timeouts are never retried (a slow local model would only be hit again).
        """
        loop = asyncio.get_running_loop()
        attempt = 0
        requests_made = 0
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None, "timeout", requests_made
            requests_made += 1
            last_response: httpx.Response | None = None
            last_error = ""
            try:
                response = await asyncio.wait_for(self._post(body), timeout=remaining)
            except (asyncio.TimeoutError, httpx.TimeoutException):
                return None, "timeout", requests_made
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
                if attempt >= self.max_retries:
                    return None, last_error, requests_made
                delay = min(RETRY_BASE_SECONDS * 2**attempt, RETRY_MAX_SECONDS)
            else:
                if response.status_code not in RETRYABLE_STATUS or attempt >= self.max_retries:
                    return response, "", requests_made
                last_response = response
                delay = _retry_after(response) or min(RETRY_BASE_SECONDS * 2**attempt, RETRY_MAX_SECONDS)
            if loop.time() + delay >= deadline:
                # Not enough time left for another attempt: report what the last attempt produced.
                return last_response, last_error if last_response is None else "", requests_made
            await asyncio.sleep(delay)
            attempt += 1

    async def assess(self, judge_input: JudgeInput) -> JudgeOutput:
        if not self.base_url or not self.model:
            return self._output("not_configured", error="base_url_or_model_missing")
        if self.is_external and not self.allow_external:
            return self._output("blocked_external", error="external_endpoint_not_allowed")
        messages = build_messages(judge_input, secrets.token_hex(8))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_seconds
        started = time.perf_counter()
        attempts = 0
        modes = self._mode_sequence()
        for index, mode in enumerate(modes):
            response, error, made = await self._send(self._body(messages, mode), deadline)
            attempts += made
            latency_ms = int((time.perf_counter() - started) * 1000)
            meta = {"latency_ms": latency_ms, "structured_output": mode, "attempts": attempts}
            if response is None:
                return self._output("timeout" if error == "timeout" else "error", error=error, **meta)
            if (
                self.structured_output == "auto"
                and index < len(modes) - 1
                and response.status_code in FORMAT_REJECTION_STATUS
                and _format_rejected(response)
            ):
                continue  # the backend does not support this response_format; degrade (not counted as a retry)
            if response.status_code >= 400:
                return self._output("error", error=f"http_{response.status_code}", **meta)
            return self._parse_response(response, mode, meta)
        return self._output("error", error="no_structured_output_mode", attempts=attempts)

    def _parse_response(self, response: httpx.Response, mode: str, meta: dict[str, Any]) -> JudgeOutput:
        try:
            payload = response.json()
        except ValueError:
            return self._output("invalid_output", error="response_not_json", **meta)
        usage = payload.get("usage") if isinstance(payload, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        tokens = {"input_tokens": int(usage.get("prompt_tokens") or 0), "output_tokens": int(usage.get("completion_tokens") or 0)}
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return self._output("invalid_output", error="missing_choice_content", **tokens, **meta)
        if not isinstance(content, str) or not content.strip():
            # e.g. a reasoning model that spent max_tokens on reasoning_content and never answered
            return self._output("invalid_output", error="empty_content", **tokens, **meta)
        try:
            parsed = parse_judge_output(content, strip_reasoning=self.strip_reasoning)
        except JudgeOutputError as exc:
            return self._output("invalid_output", error=str(exc), **tokens, **meta)
        if self.structured_output == "auto":
            _MODE_CACHE[self._cache_key] = mode
        return self._output(
            "ok",
            suspicion=parsed["suspicion"],
            categories=parsed["categories"],
            rationale=parsed["rationale"],
            evidence_refs=parsed["evidence_refs"],
            **tokens,
            **meta,
        )


def _retry_after(response: httpx.Response) -> float:
    value = response.headers.get("Retry-After", "")
    try:
        seconds = float(value)
    except ValueError:
        return 0.0
    return max(0.0, min(seconds, RETRY_AFTER_MAX_SECONDS))


def _format_rejected(response: httpx.Response) -> bool:
    """Heuristic: a 400 / 422 whose error mentions the structured-output request (backends word this differently)."""
    try:
        text = response.text.lower()
    except Exception:  # noqa: BLE001
        return False
    return any(marker in text for marker in FORMAT_REJECTION_MARKERS)


def reset_mode_cache() -> None:
    _MODE_CACHE.clear()


def cached_mode(base_url: str, model: str) -> str:
    return _MODE_CACHE.get(((base_url or "").strip().rstrip("/"), (model or "").strip()), "")
