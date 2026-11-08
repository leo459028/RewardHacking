"""v1.5.12 provider adapter contract (plan D12).

An adapter owns everything that differs between HTTP judge APIs: the URL, authentication headers, request body for a
structured-output mode, and where the answer / reasoning / token usage live in the response. Everything shared
(timeouts, bounded retries with ``Retry-After``, structured-output degradation, the URL guard, strict output parsing)
stays in :class:`app.services.evaluation.judges.openai_compatible.HttpJudge`. A new provider (Azure OpenAI, Gemini
native, ...) is a new adapter, not a new judge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

FORMAT_REJECTION_STATUS = frozenset({400, 422})
FORMAT_REJECTION_MARKERS: tuple[str, ...] = (
    "response_format",
    "output_config",
    "json_schema",
    "json_object",
    "structured",
    "schema",
    "format",
)


@dataclass(frozen=True)
class ParsedResponse:
    """What an adapter extracted from a 2xx response. ``error`` set means no usable answer."""

    content: str = ""
    reasoning: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""


class ProviderAdapter(Protocol):
    name: str
    retryable_status: frozenset[int]

    def supported_modes(self) -> tuple[str, ...]:
        """Structured-output modes in ``auto`` order (strictest first, ``none`` last)."""
        ...

    def build_request(self, messages: list[dict[str, str]], mode: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        """``(url, headers, json_body)``. The API key appears only in ``headers``."""
        ...

    def parse_response(self, payload: Any) -> ParsedResponse: ...

    def format_rejected(self, response: httpx.Response) -> bool:
        """Did the backend reject the structured-output request (so ``auto`` may degrade)?"""
        ...


def mentions_format(response: httpx.Response) -> bool:
    """Heuristic shared by adapters: a 400 / 422 whose body mentions the structured-output request."""
    if response.status_code not in FORMAT_REJECTION_STATUS:
        return False
    try:
        text = response.text.lower()
    except Exception:  # noqa: BLE001
        return False
    return any(marker in text for marker in FORMAT_REJECTION_MARKERS)


def as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
