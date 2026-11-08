"""v1.5.12 judge profile templates for the Dashboard form (plan D19).

Templates only pre-fill the form; nothing is saved until an operator saves it. Model names are examples: always
check the provider's current documentation. Keys are never part of a template, only the *name* of the environment
variable that should hold one.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

MODEL_HINT = "Example only - use the model name from the provider's current documentation."

_LOCAL_DEFAULTS: dict[str, Any] = {
    "execution": "background",
    "pricing_provider": "qa_local",
    "structured_output": "auto",
    "timeout_seconds": 120,
    "max_input_chars": 8000,
}
_CLOUD_DEFAULTS: dict[str, Any] = {
    "external": True,
    "max_retries": 2,
    "structured_output": "auto",
    "timeout_seconds": 30,
}

TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "lm-studio",
        "label": "LM Studio (local)",
        "kind": "local",
        "hints": {"base_url": "Use the port LM Studio shows under API Usage (inside Docker use host.docker.internal)."},
        "config": {**_LOCAL_DEFAULTS, "provider": "openai_compatible", "base_url": "http://host.docker.internal:1234/v1", "model": ""},
    },
    {
        "id": "ollama",
        "label": "Ollama (local)",
        "kind": "local",
        "hints": {"base_url": "Ollama's OpenAI-compatible endpoint (default port 11434)."},
        "config": {**_LOCAL_DEFAULTS, "provider": "openai_compatible", "base_url": "http://host.docker.internal:11434/v1", "model": ""},
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "kind": "cloud",
        "hints": {"model": MODEL_HINT},
        "config": {
            **_CLOUD_DEFAULTS,
            "provider": "openai_compatible",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-5-mini",
            "api_key_env": "OPENAI_API_KEY",
            "pricing_provider": "openai",
        },
    },
    {
        "id": "gemini",
        "label": "Google Gemini (OpenAI-compatible endpoint)",
        "kind": "cloud",
        "hints": {"model": MODEL_HINT},
        "config": {
            **_CLOUD_DEFAULTS,
            "provider": "openai_compatible",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "model": "gemini-2.5-flash",
            "api_key_env": "GEMINI_API_KEY",
            "pricing_provider": "google",
        },
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "kind": "cloud",
        "hints": {"model": MODEL_HINT},
        "config": {
            **_CLOUD_DEFAULTS,
            "provider": "openai_compatible",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "api_key_env": "DEEPSEEK_API_KEY",
            "pricing_provider": "deepseek",
        },
    },
    {
        "id": "anthropic",
        "label": "Anthropic (native Messages API)",
        "kind": "cloud",
        "hints": {
            "model": MODEL_HINT,
            "max_output_tokens": "Current Claude models think before answering and thinking uses output tokens; keep this generous.",
        },
        "config": {
            **_CLOUD_DEFAULTS,
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com",
            "model": "claude-sonnet-5-5",
            "api_key_env": "ANTHROPIC_API_KEY",
            "pricing_provider": "anthropic",
            "max_output_tokens": 4000,
            "timeout_seconds": 60,
        },
    },
)


def list_templates() -> list[dict[str, Any]]:
    return deepcopy(list(TEMPLATES))


def get_template(template_id: str) -> dict[str, Any] | None:
    for template in TEMPLATES:
        if template["id"] == template_id:
            return deepcopy(template)
    return None
