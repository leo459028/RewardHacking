"""Model family normalisation for the same-family warning (v1.5.10, D9).

A judge from the same model family as the monitored agent may share its blind spots. The warning is
advisory: some teams only have one provider. Families come from the FinOps pricing catalog when the
caller supplies them, otherwise from name prefixes. Fine-tuned or self-hosted lineage is not detectable.
"""

from __future__ import annotations

import re

_FAMILY_PREFIXES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(gpt|chatgpt|o[1-9]\b|o[1-9]-|text-davinci|codex)"), "openai-gpt"),
    (re.compile(r"^claude"), "anthropic-claude"),
    (re.compile(r"^(gemini|gemma)"), "google-gemini"),
    (re.compile(r"^(llama|meta-llama|codellama)"), "meta-llama"),
    (re.compile(r"^(qwen|qwq)"), "alibaba-qwen"),
    (re.compile(r"^deepseek"), "deepseek"),
    (re.compile(r"^(mistral|mixtral|codestral|ministral|magistral|devstral)"), "mistral"),
    (re.compile(r"^(kimi|moonshot)"), "moonshot-kimi"),
    (re.compile(r"^(glm|chatglm|zai)"), "zhipu-glm"),
    (re.compile(r"^(minimax|abab)"), "minimax"),
    (re.compile(r"^(grok)"), "xai-grok"),
)


def normalize_model_name(name: str) -> str:
    value = (name or "").strip().lower()
    # Drop provider / namespace prefixes such as "openai/gpt-4.1" or "us.anthropic.claude-...".
    if "/" in value:
        value = value.rsplit("/", 1)[-1]
    for prefix in ("us.", "eu.", "apac.", "global."):
        if value.startswith(prefix):
            value = value[len(prefix):]
    for vendor in ("anthropic.", "meta.", "mistral.", "amazon.", "google.", "openai."):
        if value.startswith(vendor):
            value = value[len(vendor):]
    return value


def model_family(name: str, catalog_family: str = "") -> str:
    """Catalog family wins; otherwise a prefix-derived family; ``""`` when unknown."""
    if (catalog_family or "").strip():
        return catalog_family.strip().lower()
    value = normalize_model_name(name)
    if not value:
        return ""
    for pattern, family in _FAMILY_PREFIXES:
        if pattern.search(value):
            return family
    return ""


def same_family(judge_model: str, agent_model: str, *, judge_catalog_family: str = "", agent_catalog_family: str = "") -> bool:
    left = model_family(judge_model, judge_catalog_family)
    right = model_family(agent_model, agent_catalog_family)
    return bool(left) and left == right
