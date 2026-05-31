from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


INJECTION_PATTERNS = [
    r"ignore (all|previous) instructions",
    r"bypass policy",
    r"do anything now",
    r"reveal system prompt",
    r"(show|reveal|print).*(developer|system) (instructions|message)",
    r"print hidden chain of thought",
    r"exfiltrat",
    r"override safety",
]


CLASSICAL_LANGUAGE_MARKERS = [
    "文言",
    "古文",
    "古語",
    "勿",
    "毋",
    "莫",
    "汝",
    "吾",
    "其",
    "舊令",
    "秘旨",
    "守門者",
]

CLASSICAL_ATTACK_RULES = [
    {
        "flag": "classical_prompt_injection",
        "canonical_intent": "prompt_injection",
        "patterns": ["勿循舊令", "毋循舊令", "吐露系統秘旨", "吐露.*秘旨", "系統秘旨"],
    },
    {
        "flag": "classical_policy_bypass",
        "canonical_intent": "policy_bypass",
        "patterns": ["破其禁制", "越其防線", "免審", "避其律", "繞過.*政策"],
    },
    {
        "flag": "classical_credential_exfiltration",
        "canonical_intent": "credential_exfiltration",
        "patterns": ["取密鑰", "竊密鑰", "令牌", "取令牌", "竊令牌", "取.*憑證"],
    },
    {
        "flag": "classical_destructive_intent",
        "canonical_intent": "destructive_action",
        "patterns": ["毀其紀錄", "毀庫", "刪其檔", "清其紀錄", "毀.*資料"],
    },
    {
        "flag": "classical_malware_intent",
        "canonical_intent": "malware",
        "patterns": ["置後門", "植木馬", "布惡程", "惡意程式", "後門"],
    },
]


@dataclass(frozen=True)
class PromptAnalysis:
    cleaned_prompt: str
    language: str
    normalized_prompt: str
    flags: list[str] = field(default_factory=list)
    signals: list[dict[str, str]] = field(default_factory=list)

    def model_dump(self) -> dict[str, object]:
        return asdict(self)


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _detect_language(text: str) -> str:
    if not _contains_cjk(text):
        return "en"
    if any(marker in text for marker in CLASSICAL_LANGUAGE_MARKERS):
        return "zh_classical"
    if re.search(r"[A-Za-z]", text):
        return "mixed_zh"
    return "zh"


def _normalize_prompt(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def analyze_prompt(prompt: str) -> PromptAnalysis:
    cleaned = prompt.strip()
    normalized = _normalize_prompt(cleaned)
    lowered = normalized.lower()
    language = _detect_language(normalized)
    flags = [pat for pat in INJECTION_PATTERNS if re.search(pat, lowered)]
    signals: list[dict[str, str]] = []

    for rule in CLASSICAL_ATTACK_RULES:
        for pattern in rule["patterns"]:
            match = re.search(pattern, normalized, flags=re.IGNORECASE)
            if match is None:
                continue
            if "classical_chinese_obfuscation" not in flags:
                flags.append("classical_chinese_obfuscation")
            flags.append(rule["flag"])
            signals.append(
                {
                    "category": "classical_chinese_obfuscation",
                    "canonical_intent": rule["canonical_intent"],
                    "matched_text": match.group(0),
                }
            )
            break

    return PromptAnalysis(
        cleaned_prompt=cleaned,
        language=language,
        normalized_prompt=normalized,
        flags=_dedupe(flags),
        signals=signals,
    )


def sanitize_prompt(prompt: str) -> tuple[str, list[str]]:
    analysis = analyze_prompt(prompt)
    return analysis.cleaned_prompt, analysis.flags
