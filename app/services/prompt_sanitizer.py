import re


INJECTION_PATTERNS = [
    r"ignore (all|previous) instructions",
    r"bypass policy",
    r"do anything now",
    r"reveal system prompt",
    r"print hidden chain of thought",
    r"exfiltrat",
    r"override safety",
]


def sanitize_prompt(prompt: str) -> tuple[str, list[str]]:
    lowered = prompt.lower()
    flags = [pat for pat in INJECTION_PATTERNS if re.search(pat, lowered)]
    return prompt.strip(), flags
