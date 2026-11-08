"""OpenAI-compatible chat completions (v1.5.10 / v1.5.11 behaviour, unchanged; plan D12).

``POST {base_url}/chat/completions`` with ``Authorization: Bearer``; ``response_format`` carries the structured-output
mode (``json_schema`` → ``json_object`` → none). Works with OpenAI, Gemini's OpenAI endpoint, DeepSeek, Groq, Mistral,
xAI, OpenRouter, LM Studio, Ollama and vLLM.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.services.evaluation.judges.adapters.base import ParsedResponse, as_int, mentions_format
from app.services.evaluation.judges.parsing import judge_output_schema


class OpenAICompatibleAdapter:
    name = "openai_compatible"
    retryable_status = frozenset({429, 502, 503, 504})

    def __init__(self, *, base_url: str, model: str, api_key: str = "", max_output_tokens: int = 600, reasoning_effort: str = ""):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.model = (model or "").strip()
        self._api_key = api_key or ""
        self.max_output_tokens = int(max_output_tokens)
        self.reasoning_effort = reasoning_effort or ""

    def supported_modes(self) -> tuple[str, ...]:
        return ("json_schema", "json_object", "none")

    def build_request(self, messages: list[dict[str, str]], mode: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "judge_triage", "strict": True, "schema": judge_output_schema()},
            }
        elif mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        return f"{self.base_url}/chat/completions", headers, body

    def parse_response(self, payload: Any) -> ParsedResponse:
        usage = payload.get("usage") if isinstance(payload, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        tokens = {"input_tokens": as_int(usage.get("prompt_tokens")), "output_tokens": as_int(usage.get("completion_tokens"))}
        try:
            message = payload["choices"][0]["message"]
            content = message["content"]
        except (KeyError, IndexError, TypeError):
            return ParsedResponse(error="missing_choice_content", **tokens)
        reasoning = message.get("reasoning_content") if isinstance(message, dict) else ""
        if not isinstance(content, str) or not content.strip():
            # e.g. a reasoning model that spent max_tokens on reasoning_content and never answered
            return ParsedResponse(error="empty_content", reasoning=str(reasoning or ""), **tokens)
        return ParsedResponse(content=content, reasoning=str(reasoning or ""), **tokens)

    def format_rejected(self, response: httpx.Response) -> bool:
        return mentions_format(response)
