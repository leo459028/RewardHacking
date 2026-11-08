"""Native Anthropic Messages API adapter (v1.5.12, plan D12 as amended after checking the current API docs).

- ``POST {base_url}/v1/messages`` with ``x-api-key`` and ``anthropic-version: 2026-10-21``; the system prompt is the
  top-level ``system`` field and ``max_tokens`` is required.
- Structured output uses ``output_config.format`` (``json_schema``). Forced ``tool_choice`` is *not* used: current
  models (Claude Opus 5.5, Sonnet 5.5, Fable 5.1) reject it with a 400. The schema is sent without numeric / length
  constraints (unsupported there); ``parse_judge_output()`` still validates the answer strictly.
- ``temperature`` is never sent (API version ``2026-10-21`` rejects sampling parameters on every model).
- Thinking may be always on (Claude Opus 5.5): ``thinking`` blocks are skipped and only ``text`` blocks are the answer.
  ``reasoning_effort`` maps to ``output_config.effort`` (``none`` / ``minimal`` → ``low``).
- 529 (overloaded) is retried like 429 / 5xx, honouring ``Retry-After``.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.services.evaluation.judges.adapters.base import ParsedResponse, as_int, mentions_format
from app.services.evaluation.judges.parsing import judge_output_schema

ANTHROPIC_VERSION = "2026-10-21"
UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
    }
)
EFFORT_MAP = {"none": "low", "minimal": "low", "low": "low", "medium": "medium", "high": "high"}


def _strip_schema(node: Any) -> Any:
    if isinstance(node, dict):
        return {key: _strip_schema(value) for key, value in node.items() if key not in UNSUPPORTED_SCHEMA_KEYS}
    if isinstance(node, list):
        return [_strip_schema(item) for item in node]
    return node


def anthropic_output_schema() -> dict[str, Any]:
    return _strip_schema(judge_output_schema())


class AnthropicAdapter:
    name = "anthropic"
    retryable_status = frozenset({429, 500, 502, 503, 504, 529})

    def __init__(self, *, base_url: str, model: str, api_key: str = "", max_output_tokens: int = 600, reasoning_effort: str = ""):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.model = (model or "").strip()
        self._api_key = api_key or ""
        self.max_output_tokens = int(max_output_tokens)
        self.reasoning_effort = reasoning_effort or ""

    @property
    def url(self) -> str:
        # Accept both "https://api.anthropic.com" (documented template) and ".../v1".
        return f"{self.base_url}/messages" if self.base_url.endswith("/v1") else f"{self.base_url}/v1/messages"

    def supported_modes(self) -> tuple[str, ...]:
        return ("json_schema", "none")

    def build_request(self, messages: list[dict[str, str]], mode: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        headers = {"Content-Type": "application/json", "anthropic-version": ANTHROPIC_VERSION}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        system = "\n\n".join(item["content"] for item in messages if item.get("role") == "system")
        conversation = [{"role": item["role"], "content": item["content"]} for item in messages if item.get("role") != "system"]
        body: dict[str, Any] = {"model": self.model, "max_tokens": self.max_output_tokens, "messages": conversation}
        if system:
            body["system"] = system
        output_config: dict[str, Any] = {}
        effort = EFFORT_MAP.get(self.reasoning_effort, "")
        if effort:
            output_config["effort"] = effort
        if mode == "json_schema":
            output_config["format"] = {"type": "json_schema", "schema": anthropic_output_schema()}
        if output_config:
            body["output_config"] = output_config
        return self.url, headers, body

    def parse_response(self, payload: Any) -> ParsedResponse:
        if not isinstance(payload, dict):
            return ParsedResponse(error="missing_content")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        tokens = {"input_tokens": as_int(usage.get("input_tokens")), "output_tokens": as_int(usage.get("output_tokens"))}
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            return ParsedResponse(error="missing_content", **tokens)
        texts: list[str] = []
        reasoning: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
                reasoning.append(block["thinking"])
        stop_reason = str(payload.get("stop_reason") or "")
        content = "".join(texts)
        if stop_reason == "refusal":
            return ParsedResponse(error="refusal", reasoning="\n".join(reasoning), **tokens)
        if not content.strip():
            return ParsedResponse(error="max_tokens" if stop_reason == "max_tokens" else "empty_content", reasoning="\n".join(reasoning), **tokens)
        return ParsedResponse(content=content, reasoning="\n".join(reasoning), **tokens)

    def format_rejected(self, response: httpx.Response) -> bool:
        return mentions_format(response)
