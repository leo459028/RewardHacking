"""Pre-execution prompt screening (prompt-guard/2.0).

Deterministic and model-free. Screening is a **risk signal, not a security boundary**: every rule here
can be evaded by rephrasing, so enforcement rests on controls that do not read the prompt (tool
allow-lists, MCP argument validation, approval gates, budgets). See ``docs/security.md``.

Pipeline:

1. Normalisation for matching only (the stored prompt is unchanged): NFKC, removal of format and
   combining characters (zero-width spaces, bidi controls, variation selectors), Simplified ->
   Traditional for the characters the rules use, and a "compact" view that keeps only letters,
   digits and CJK so interleaved punctuation or spaces cannot split a phrase.
2. English injection patterns (unchanged since v0.2; the flag is the pattern itself).
3. Classical Chinese phrase list (v1.1.0), now matched on the compact view.
4. Modern Chinese rules (``zh_*`` flags).
5. ``sensitive_term_mention``: a bare sensitive noun without an attack verb. It still needs review,
   but is no longer labelled as classical exfiltration or malware intent.
6. ``classical_style_task_mismatch``: classical-style text on a task type that has write, query or
   deploy tools. A heuristic; marker-free classical text is not detected.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field

GUARD_VERSION = "prompt-guard/2.0"

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
        "patterns": ["取密鑰", "竊密鑰", "取令牌", "竊令牌", "取.*憑證"],
    },
    {
        "flag": "classical_destructive_intent",
        "canonical_intent": "destructive_action",
        "patterns": ["毀其紀錄", "毀庫", "刪其檔", "清其紀錄", "毀.*資料"],
    },
    {
        "flag": "classical_malware_intent",
        "canonical_intent": "malware",
        "patterns": ["置後門", "植木馬", "布惡程"],
    },
]

# Modern Chinese rules run on the compact view (no punctuation or spaces, Traditional characters,
# lower-case ASCII), so gaps such as ``.{0,8}`` count letters, digits and CJK only.
_ZH_SCOPE = "(?:先前|之前|以上|上述|上面|前面|前述|原本|原先|原有|既有|所有|全部|一切|任何|系統|開發者|安全)"
_ZH_CREDENTIAL_NOUNS = "(?:金鑰|密鑰|私鑰|apikey|權杖|令牌|accesstoken|token|密碼|帳密|環境變數|secret|credential|憑證)"
_ZH_DATA = (
    "(?:資料庫|資料表|備份|稽核紀錄|稽核記錄|稽核日誌|日誌|紀錄|記錄|帳號|使用者資料|客戶資料|儲存庫|正式環境|生產環境|正式資料|生產資料)"
)
_ZH_DESTROY_WORDS = "刪除|刪掉|刪光|清空|清除|銷毀|摧毀|抹除|移除|毀掉"

ZH_MODERN_RULES = [
    {
        "flag": "zh_prompt_injection",
        "canonical_intent": "prompt_injection",
        "patterns": [
            r"(?:忽略|無視|不要理會|別理會|不用理會|不必理會|忘記|忘掉|拋棄|捨棄|不要遵守|不用遵守|無需遵守|停止遵守|不再遵守|不要管|別管).{0,6}"
            + _ZH_SCOPE
            + r".{0,6}(?:指示|指令|規則|規定|提示|命令|限制|約束)",
            r"(?:顯示|輸出|印出|列出|洩漏|洩露|透露|告訴我|給我|提供|複述|重複|說出|吐露|公開).{0,8}"
            r"(?:系統提示|系統指令|系統訊息|開發者訊息|開發者指令|隱藏指令|隱藏提示|初始指令|原始指令|systemprompt)",
            r"(?:進入|切換到?|啟用|開啟|啟動).{0,6}(?:開發者模式|越獄模式|無限制模式|dan模式)",
            r"你現在是.{0,12}(?:不受限制|沒有限制|無限制|不受約束|沒有任何限制)",
        ],
    },
    {
        "flag": "zh_policy_bypass",
        "canonical_intent": "policy_bypass",
        "patterns": [
            r"(?:繞過|略過|跳過|規避|迴避|關閉|停用|解除|突破|避開|免除).{0,8}"
            r"(?:安全機制|安全檢查|安全限制|安全防護|安全審查|審核|審查|核准|審批|政策|防護|護欄|管控|權限檢查|權限驗證|權限控管"
            r"|稽核|監控|內容過濾|安全過濾|身分驗證|身份驗證|憑證驗證|存取限制|人工覆核|人工核准)",
            r"(?:不需要|不用|不必|無需).{0,4}(?:審核|核准|審批|人工覆核|人工核准)",
        ],
    },
    {
        "flag": "zh_credential_exfiltration",
        "canonical_intent": "credential_exfiltration",
        "patterns": [
            r"(?:竊取|盜取|偷取|偷走|外傳|外洩|洩漏|洩露|dump|傾印).{0,8}" + _ZH_CREDENTIAL_NOUNS,
            _ZH_CREDENTIAL_NOUNS + r".{0,8}(?:外傳|上傳到|傳到|送到|寄到|貼到|發到|傳給|寄給)",
            r"(?:印出|顯示|列出|讀取|輸出|蒐集|收集|匯出|導出).{0,3}(?:所有|全部|全數|一切|每個|每一個).{0,8}" + _ZH_CREDENTIAL_NOUNS,
        ],
    },
    {
        "flag": "zh_destructive_intent",
        "canonical_intent": "destructive_action",
        "patterns": [
            r"(?:" + _ZH_DESTROY_WORDS + r"|drop|truncate|格式化).{0,4}(?:所有|全部|整個|全數|一切|正式|生產|線上|production|prod).{0,10}" + _ZH_DATA,
            _ZH_DATA + r".{0,10}(?:全部|全數|通通|統統|全都|整個)(?:" + _ZH_DESTROY_WORDS + r")",
        ],
    },
    {
        "flag": "zh_malware_intent",
        "canonical_intent": "malware",
        "patterns": [
            r"(?:植入|埋入|安插|留下|留個|加入|寫入|建立|安裝|置入|藏入|隱藏|偷偷加).{0,6}"
            r"(?:後門|木馬|惡意程式|惡意代碼|惡意軟體|勒索軟體|鍵盤側錄|挖礦程式|反向shell|reverseshell|webshell)",
        ],
    },
]

# Bare nouns that flagged a request as a classical attack before prompt-guard/2.0. They still send
# the request to review, under an accurate label and without the high-risk classical weighting.
SENSITIVE_TERMS = ("令牌", "後門", "惡意程式")
_STRONG_INTENT_FLAGS = {
    "classical_credential_exfiltration",
    "classical_malware_intent",
    "zh_credential_exfiltration",
    "zh_malware_intent",
}

# Classical-style heuristic. "Strong" markers are pronouns and particles that are rare in modern
# prose; "weak" ones also occur in formal modern writing, so at least one strong marker is required.
STYLE_MISMATCH_TASK_TYPES = frozenset({"code_fix", "data_query", "workflow"})
_STYLE_MIN_CJK = 8
_STYLE_MAX_MODERN_RATIO = 0.03
_STYLE_MIN_SCORE = 3
_MODERN_FUNCTION_CHARS = frozenset("的了是在們這那嗎吧呢個和把被會沒麼")
_CLASSICAL_STRONG = re.compile(r"[汝吾矣焉哉兮歟曰毋]|爾等|(?<![請切])勿|莫(?![名非過大])")
_CLASSICAL_WEAK_CHARS = frozenset("其之乃於以而則者也")

# Simplified -> Traditional for the characters the rules and markers use (not a general converter).
_S2T = str.maketrans(
    {
        "旧": "舊", "统": "統", "钥": "鑰", "记": "記", "录": "錄", "纪": "紀", "资": "資", "档": "檔",
        "删": "刪", "毁": "毀", "后": "後", "门": "門", "马": "馬", "恶": "惡", "证": "證", "凭": "憑",
        "绕": "繞", "过": "過", "线": "線", "审": "審", "库": "庫", "窃": "竊", "输": "輸", "显": "顯",
        "开": "開", "发": "發", "规": "規", "则": "則", "设": "設", "泄": "洩", "无": "無", "视": "視",
        "环": "環", "变": "變", "数": "數", "据": "據", "销": "銷", "备": "備", "志": "誌", "传": "傳",
        "导": "導", "码": "碼", "权": "權", "关": "關", "闭": "閉", "检": "檢", "护": "護", "写": "寫",
        "装": "裝", "这": "這", "个": "個", "们": "們", "吗": "嗎", "么": "麼", "会": "會", "没": "沒",
        "请": "請", "尔": "爾", "欤": "歟", "于": "於", "讯": "訊", "获": "獲", "盗": "盜", "献": "獻",
        "账": "帳", "户": "戶", "储": "儲", "产": "產", "隐": "隱", "对": "對", "话": "話", "机": "機",
        "监": "監", "滤": "濾", "验": "驗", "认": "認", "复": "覆", "弃": "棄", "舍": "捨", "抛": "拋",
        "给": "給", "诉": "訴", "说": "說", "启": "啟", "换": "換", "狱": "獄", "约": "約", "倾": "傾",
        "贴": "貼", "汇": "匯", "读": "讀", "软": "軟", "键": "鍵", "盘": "盤", "侧": "側", "矿": "礦",
        "动": "動", "进": "進", "为": "為",
    }
)


@dataclass(frozen=True)
class PromptAnalysis:
    cleaned_prompt: str
    language: str
    normalized_prompt: str
    flags: list[str] = field(default_factory=list)
    signals: list[dict[str, str]] = field(default_factory=list)
    guard_version: str = GUARD_VERSION

    def model_dump(self) -> dict[str, object]:
        return asdict(self)


def _contains_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


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


def _match_views(text: str) -> tuple[str, str]:
    """Return ``(spaced, compact)`` views used only for matching.

    ``spaced`` keeps single spaces (English patterns). ``compact`` keeps letters, digits and CJK only,
    in Traditional characters, so ``勿、循、舊、令`` and ``勿<ZWSP>循旧令`` both read ``勿循舊令``.
    """
    folded = unicodedata.normalize("NFKC", text)
    visible = "".join(ch for ch in folded if unicodedata.category(ch) not in {"Cf", "Mn"})
    spaced = _normalize_prompt(visible).lower().translate(_S2T)
    compact = "".join(ch for ch in spaced if ch.isalnum())
    return spaced, compact


def classical_style(text: str) -> tuple[bool, str]:
    """Heuristic: does ``text`` (a spaced match view) read as classical Chinese? Returns ``(hit, marker)``."""
    cjk = [ch for ch in text if "一" <= ch <= "鿿"]
    if len(cjk) < _STYLE_MIN_CJK:
        return False, ""
    if sum(ch in _MODERN_FUNCTION_CHARS for ch in cjk) / len(cjk) >= _STYLE_MAX_MODERN_RATIO:
        return False, ""
    strong = _CLASSICAL_STRONG.findall(text)
    if not strong:
        return False, ""
    weak = sum(ch in _CLASSICAL_WEAK_CHARS for ch in cjk)
    if len(strong) * 2 + weak < _STYLE_MIN_SCORE:
        return False, ""
    return True, strong[0]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def analyze_prompt(prompt: str, task_type: str | None = None) -> PromptAnalysis:
    """Screen ``prompt``. ``task_type`` enables the classical-style / task-type mismatch signal."""
    cleaned = prompt.strip()
    normalized = _normalize_prompt(cleaned)
    spaced, compact = _match_views(cleaned)
    language = _detect_language(normalized.translate(_S2T))
    flags = [pat for pat in INJECTION_PATTERNS if re.search(pat, spaced)]
    signals: list[dict[str, str]] = []

    for rule in CLASSICAL_ATTACK_RULES:
        for pattern in rule["patterns"]:
            match = re.search(pattern, compact, flags=re.IGNORECASE)
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

    for rule in ZH_MODERN_RULES:
        for pattern in rule["patterns"]:
            match = re.search(pattern, compact, flags=re.IGNORECASE)
            if match is None:
                continue
            flags.append(rule["flag"])
            signals.append(
                {
                    "category": "zh_modern_instruction",
                    "canonical_intent": rule["canonical_intent"],
                    "matched_text": match.group(0),
                }
            )
            break

    if not _STRONG_INTENT_FLAGS.intersection(flags):
        for term in SENSITIVE_TERMS:
            if term in compact:
                flags.append("sensitive_term_mention")
                signals.append({"category": "sensitive_term", "canonical_intent": "needs_review", "matched_text": term})
                break

    if task_type in STYLE_MISMATCH_TASK_TYPES:
        hit, marker = classical_style(spaced)
        if hit:
            flags.append("classical_style_task_mismatch")
            signals.append({"category": "classical_style", "canonical_intent": "style_task_mismatch", "matched_text": marker})

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
