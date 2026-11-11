"""HTTP judges: OpenAI-compatible (v1.5.10) and, since v1.5.12, any provider adapter (plan D12).

:class:`HttpJudge` holds everything provider-independent; the adapter (``judges/adapters/``) builds the request and
reads the response. Guardrails:
- external endpoints are refused unless ``EVALUATION_JUDGE_ALLOW_EXTERNAL=true`` (D10, parsed hostnames);
- v1.5.12 (D11): the URL is re-checked before every request (no metadata address, credentials, query string, and
  HTTPS for external hosts), so a profile built outside the validated loaders cannot send evidence in clear text;
- a hard timeout (D6) with bounded retries honouring ``Retry-After`` (v1.5.11 D9); any failure returns a non-``ok``
  status so the caller falls back;
- the API key is sent only in an authentication header and never logged or returned.

``OpenAICompatibleJudge`` keeps its v1.5.10 name, constructor and ``judge_version`` strings; ``AnthropicJudge`` uses the
native Messages API adapter.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core import tracing
from app.services.evaluation.judges.adapters import ADAPTERS, ProviderAdapter
from app.services.evaluation.judges.adapters.base import FORMAT_REJECTION_MARKERS, FORMAT_REJECTION_STATUS  # noqa: F401 - re-export
from app.services.evaluation.judges.base import JudgeInput, JudgeOutput
from app.services.evaluation.judges.parsing import JudgeOutputError, parse_judge_output
from app.services.evaluation.judges.profiles import JudgeProfile, base_url_problem
from app.services.evaluation.judges.prompt import build_messages, prompt_version
from app.services.network_policy import is_external_endpoint

AUTO_MODE_ORDER: tuple[str, ...] = ("json_schema", "json_object", "none")
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})  # OpenAI-compatible default; adapters declare their own
RETRY_BASE_SECONDS = 1.0
RETRY_MAX_SECONDS = 8.0
RETRY_AFTER_MAX_SECONDS = 10.0
# Per-process memory of the structured-output mode each (base_url, model) accepted (D7).
_MODE_CACHE: dict[tuple[str, str], str] = {}


class HttpJudge:
    provider = "openai_compatible"

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
        internal_hosts: list[str] | None = None,
    ):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.model = (model or "").strip()
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_tokens = int(max_output_tokens)
        self.allow_external = bool(allow_external)
        self.internal_suffixes = list(internal_suffixes or [])
        # v1.5.11 (D14): exact host names treated as internal (default host.docker.internal). Exact match only, so
        # host.docker.internal.evil.com stays external. A profile can force "external" but never downgrade a
        # public host name to internal (external: false behaves like auto).
        self.internal_hosts = {host.strip().lower().rstrip(".") for host in (internal_hosts or []) if host.strip()}
        self.force_external = bool(profile is not None and profile.external == "true")
        self._transport = transport
        # v1.5.11: named profiles prefix judge_version; the synthesized legacy ``default`` keeps v1.5.10 strings (D3, D6).
        self.profile = profile
        self.profile_label = profile.name if profile is not None else ""
        self._version_prefix = profile.name if profile is not None and not profile.legacy else ""
        self.structured_output = profile.structured_output if profile is not None else "json_object"
        self.max_retries = profile.max_retries if profile is not None else 0
        self.strip_reasoning = profile.strip_reasoning if profile is not None else True
        self.reasoning_effort = profile.reasoning_effort if profile is not None else ""
        self.adapter: ProviderAdapter = ADAPTERS[self.provider](
            base_url=self.base_url,
            model=self.model,
            api_key=api_key or "",
            max_output_tokens=self.max_output_tokens,
            reasoning_effort=self.reasoning_effort,
        )
        self.name = self.adapter.name

    def version(self) -> str:
        base = f"{self.name}:{self.model or 'unset'}:{prompt_version()}"
        return f"{self._version_prefix}:{base}" if self._version_prefix else base

    @property
    def is_external(self) -> bool:
        if self.force_external:
            return True
        try:
            host = (urlsplit(self.base_url).hostname or "").lower().rstrip(".")
        except ValueError:
            host = ""
        if host and host in self.internal_hosts:
            return False
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

    async def _post(self, request: tuple[str, dict[str, str], dict[str, Any]]) -> httpx.Response:
        url, headers, body = request
        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self._transport) as client:
            return await client.post(url, json=body, headers=headers)

    # -- v1.5.11 structured output modes (D7) ------------------------------------------------------------

    @property
    def _cache_key(self) -> tuple[str, str]:
        return (self.base_url, self.model)

    def _mode_sequence(self) -> list[str]:
        """``auto`` tries the adapter's modes strictest first, starting from the mode that worked last time.

        An explicit mode the adapter does not speak (e.g. ``json_object`` on Anthropic) is sent as ``none``."""
        supported = list(self.adapter.supported_modes())
        if self.structured_output != "auto":
            return [self.structured_output if self.structured_output in supported else "none"]
        cached = _MODE_CACHE.get(self._cache_key)
        if cached in supported:
            return supported[supported.index(cached):]
        return supported

    # -- v1.5.11 bounded retries (D9) ------------------------------------------------------------------------

    async def _send(self, request: tuple[str, dict[str, str], dict[str, Any]], deadline: float) -> tuple[httpx.Response | None, str, int]:
        """POST with retries on the adapter's retryable statuses and connection errors, all inside the profile timeout.

        Returns ``(response, error, requests_made)``; ``error`` is ``timeout`` or an exception class name when no
        response was obtained. Timeouts are never retried (a slow local model would only be hit again).
        """
        loop = asyncio.get_running_loop()
        retryable = self.adapter.retryable_status
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
                response = await asyncio.wait_for(self._post(request), timeout=remaining)
            except (asyncio.TimeoutError, httpx.TimeoutException):
                return None, "timeout", requests_made
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
                if attempt >= self.max_retries:
                    return None, last_error, requests_made
                delay = min(RETRY_BASE_SECONDS * 2**attempt, RETRY_MAX_SECONDS)
            else:
                if response.status_code not in retryable or attempt >= self.max_retries:
                    return response, "", requests_made
                last_response = response
                delay = _retry_after(response) or min(RETRY_BASE_SECONDS * 2**attempt, RETRY_MAX_SECONDS)
            if loop.time() + delay >= deadline:
                # Not enough time left for another attempt: report what the last attempt produced.
                return last_response, last_error if last_response is None else "", requests_made
            await asyncio.sleep(delay)
            attempt += 1

    async def assess(self, judge_input: JudgeInput) -> JudgeOutput:
        """One judge call inside a GenAI client span (v1.6.0 D7). Only model, provider, token counts and status are
        attached; judge input and output never reach a span."""
        attributes = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": self.provider, "gen_ai.request.model": self.model}
        if self.profile is not None:
            attributes["rh.judge.profile"] = self.profile.name
        with tracing.start_span(f"chat {self.model}", kind=tracing.SPAN_KIND_CLIENT, attributes=attributes) as span:
            output = await self._assess(judge_input)
            span.set_attributes(
                {
                    "rh.judge.status": output.status,
                    "gen_ai.usage.input_tokens": output.input_tokens,
                    "gen_ai.usage.output_tokens": output.output_tokens,
                }
            )
            if output.status in {"timeout", "error"}:
                span.set_error(output.error or output.status)
            return output

    async def _assess(self, judge_input: JudgeInput) -> JudgeOutput:
        if not self.base_url or not self.model:
            return self._output("not_configured", error="base_url_or_model_missing")
        external = self.is_external
        if external and not self.allow_external:
            return self._output("blocked_external", error="external_endpoint_not_allowed")
        problem = base_url_problem(self.base_url, external=external)
        if problem:
            return self._output("blocked_insecure_url", error="insecure_base_url")
        messages = build_messages(judge_input, secrets.token_hex(8))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_seconds
        started = time.perf_counter()
        attempts = 0
        modes = self._mode_sequence()
        for index, mode in enumerate(modes):
            response, error, made = await self._send(self.adapter.build_request(messages, mode), deadline)
            attempts += made
            latency_ms = int((time.perf_counter() - started) * 1000)
            meta = {"latency_ms": latency_ms, "structured_output": mode, "attempts": attempts}
            if response is None:
                return self._output("timeout" if error == "timeout" else "error", error=error, **meta)
            if self.structured_output == "auto" and index < len(modes) - 1 and self.adapter.format_rejected(response):
                continue  # the backend does not support this structured-output request; degrade (not counted as a retry)
            if response.status_code >= 400:
                return self._output("error", error=f"http_{response.status_code}", **meta)
            return self._parse_response(response, mode, meta)
        return self._output("error", error="no_structured_output_mode", attempts=attempts)

    def _parse_response(self, response: httpx.Response, mode: str, meta: dict[str, Any]) -> JudgeOutput:
        try:
            payload = response.json()
        except ValueError:
            return self._output("invalid_output", error="response_not_json", **meta)
        parsed_response = self.adapter.parse_response(payload)
        tokens = {"input_tokens": parsed_response.input_tokens, "output_tokens": parsed_response.output_tokens}
        if parsed_response.error:
            return self._output("invalid_output", error=parsed_response.error, **tokens, **meta)
        try:
            parsed = parse_judge_output(parsed_response.content, strip_reasoning=self.strip_reasoning)
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


class OpenAICompatibleJudge(HttpJudge):
    provider = "openai_compatible"


class AnthropicJudge(HttpJudge):
    provider = "anthropic"


HTTP_JUDGES: dict[str, type[HttpJudge]] = {"openai_compatible": OpenAICompatibleJudge, "anthropic": AnthropicJudge}


def _retry_after(response: httpx.Response) -> float:
    value = response.headers.get("Retry-After", "")
    try:
        seconds = float(value)
    except ValueError:
        return 0.0
    return max(0.0, min(seconds, RETRY_AFTER_MAX_SECONDS))


def _format_rejected(response: httpx.Response) -> bool:
    """v1.5.11 helper kept for compatibility; adapters decide since v1.5.12."""
    from app.services.evaluation.judges.adapters.base import mentions_format

    return mentions_format(response)


def reset_mode_cache() -> None:
    _MODE_CACHE.clear()


def cached_mode(base_url: str, model: str) -> str:
    return _MODE_CACHE.get(((base_url or "").strip().rstrip("/"), (model or "").strip()), "")
