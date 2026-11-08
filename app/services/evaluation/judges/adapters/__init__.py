"""v1.5.12 provider adapters for HTTP judges (plan D12)."""

from __future__ import annotations

from app.services.evaluation.judges.adapters.anthropic import AnthropicAdapter
from app.services.evaluation.judges.adapters.base import ParsedResponse, ProviderAdapter
from app.services.evaluation.judges.adapters.openai_compatible import OpenAICompatibleAdapter

ADAPTERS: dict[str, type] = {"openai_compatible": OpenAICompatibleAdapter, "anthropic": AnthropicAdapter}

__all__ = ["ADAPTERS", "AnthropicAdapter", "OpenAICompatibleAdapter", "ParsedResponse", "ProviderAdapter"]
