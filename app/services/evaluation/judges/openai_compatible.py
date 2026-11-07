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
from app.services.evaluation.judges.parsing import JudgeOutputError, parse_judge_output
from app.services.evaluation.judges.prompt import build_messages, prompt_version
from app.services.mcp_gateway import is_external_endpoint


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
    ):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.model = (model or "").strip()
        self._api_key = api_key or ""
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_tokens = int(max_output_tokens)
        self.allow_external = bool(allow_external)
        self.internal_suffixes = list(internal_suffixes or [])
        self._transport = transport

    def version(self) -> str:
        return f"{self.name}:{self.model or 'unset'}:{prompt_version()}"

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
            **kwargs,
        )

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self._transport) as client:
            return await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)

    async def assess(self, judge_input: JudgeInput) -> JudgeOutput:
        if not self.base_url or not self.model:
            return self._output("not_configured", error="base_url_or_model_missing")
        if self.is_external and not self.allow_external:
            return self._output("blocked_external", error="external_endpoint_not_allowed")
        body = {
            "model": self.model,
            "messages": build_messages(judge_input, secrets.token_hex(8)),
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        started = time.perf_counter()
        try:
            response = await asyncio.wait_for(self._post(body), timeout=self.timeout_seconds)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            return self._output("timeout", latency_ms=int((time.perf_counter() - started) * 1000), error="timeout")
        except httpx.HTTPError as exc:
            return self._output("error", latency_ms=int((time.perf_counter() - started) * 1000), error=type(exc).__name__)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            return self._output("error", latency_ms=latency_ms, error=f"http_{response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            return self._output("invalid_output", latency_ms=latency_ms, error="response_not_json")
        usage = payload.get("usage") if isinstance(payload, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        try:
            content = payload["choices"][0]["message"]["content"]
            parsed = parse_judge_output(str(content))
        except (KeyError, IndexError, TypeError):
            return self._output("invalid_output", latency_ms=latency_ms, input_tokens=input_tokens, output_tokens=output_tokens, error="missing_choice_content")
        except JudgeOutputError as exc:
            return self._output("invalid_output", latency_ms=latency_ms, input_tokens=input_tokens, output_tokens=output_tokens, error=str(exc))
        return self._output(
            "ok",
            suspicion=parsed["suspicion"],
            categories=parsed["categories"],
            rationale=parsed["rationale"],
            evidence_refs=parsed["evidence_refs"],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
        )
