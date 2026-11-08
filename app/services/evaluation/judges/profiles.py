"""v1.5.11 named judge profiles (plan D1–D6, D16).

A profile bundles everything one judge backend needs: provider, endpoint, model, the *name* of the
environment variable that holds its API key, structured-output mode, timeout, input budget, pricing
attribution, external-endpoint policy and execution mode. Switching from a local 2B model to a 14B /
27B model or to a cloud API is a configuration change, and assessments from different profiles stay
comparable because ``judge_version`` names the profile.

Sources, later ones overriding earlier ones by name:

1. built-in ``deterministic`` (reserved, cannot be overridden);
2. ``default``, synthesized from the v1.5.10 ``EVALUATION_JUDGE_*`` settings (``legacy=True``: it keeps the
   v1.5.10 ``judge_version`` strings and the ``json_object`` request format, so behaviour is unchanged when
   no profile is configured);
3. the YAML file ``EVALUATION_JUDGE_PROFILES_FILE``;
4. the JSON setting ``EVALUATION_JUDGE_PROFILES``.

Secrets never live in a profile: a profile that contains ``api_key`` (or similar) is rejected at load time.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from app.core.config import Settings

PROFILE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
PROVIDERS: tuple[str, ...] = ("deterministic", "openai_compatible")
STRUCTURED_OUTPUT_MODES: tuple[str, ...] = ("auto", "json_schema", "json_object", "none")
EXECUTION_MODES: tuple[str, ...] = ("inline", "background")
EXTERNAL_MODES: tuple[str, ...] = ("auto", "true", "false")
MAX_RETRIES_LIMIT = 5
DETERMINISTIC_PROFILE = "deterministic"
DEFAULT_PROFILE = "default"
LEGACY_API_KEY_ENV = "EVALUATION_JUDGE_API_KEY"
# Field names that would hold a secret. ``api_key_env`` (the variable *name*) is the only allowed form.
FORBIDDEN_KEYS = frozenset({"api_key", "apikey", "api-key", "key", "token", "secret", "password", "authorization", "bearer"})
PROFILE_FIELDS = frozenset(
    {
        "name",
        "provider",
        "base_url",
        "model",
        "api_key_env",
        "structured_output",
        "timeout_seconds",
        "max_input_chars",
        "max_output_tokens",
        "pricing_provider",
        "pricing_model",
        "external",
        "execution",
        "max_retries",
        "strip_reasoning",
    }
)


class JudgeProfileError(ValueError):
    """Invalid judge profile configuration (raised at startup)."""


@dataclass(frozen=True)
class JudgeProfile:
    name: str
    provider: str = "openai_compatible"
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    structured_output: str = "auto"
    timeout_seconds: float = 8.0
    max_input_chars: int = 24000
    max_output_tokens: int = 600
    pricing_provider: str = "custom"
    pricing_model: str = ""
    external: str = "auto"
    execution: str = "inline"
    max_retries: int = 0
    strip_reasoning: bool = True
    legacy: bool = False
    source: str = "builtin"

    @property
    def effective_pricing_model(self) -> str:
        return self.pricing_model or self.model

    @property
    def host(self) -> str:
        try:
            return (urlsplit(self.base_url).hostname or "").lower()
        except ValueError:
            return ""

    def api_key(self, settings: Settings) -> str:
        """Read the key only at call time; it is never stored on the profile."""
        if not self.api_key_env:
            return ""
        if self.api_key_env == LEGACY_API_KEY_ENV:
            return settings.evaluation_judge_api_key.get_secret_value() or os.environ.get(LEGACY_API_KEY_ENV, "")
        return os.environ.get(self.api_key_env, "")

    def api_key_configured(self, settings: Settings) -> bool:
        return bool(self.api_key(settings))

    def public_dict(self, settings: Settings) -> dict[str, Any]:
        """Safe view for APIs and the Dashboard: no key, no URL path or query string."""
        return {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "host": self.host,
            "api_key_env": self.api_key_env,
            "api_key_configured": self.api_key_configured(settings),
            "structured_output": self.structured_output,
            "timeout_seconds": self.timeout_seconds,
            "max_input_chars": self.max_input_chars,
            "max_output_tokens": self.max_output_tokens,
            "pricing_provider": self.pricing_provider,
            "pricing_model": self.effective_pricing_model,
            "external": self.external,
            "execution": self.execution,
            "max_retries": self.max_retries,
            "strip_reasoning": self.strip_reasoning,
            "legacy": self.legacy,
            "source": self.source,
        }


DETERMINISTIC = JudgeProfile(name=DETERMINISTIC_PROFILE, provider="deterministic", structured_output="none", source="builtin")


def legacy_default_profile(settings: Settings) -> JudgeProfile:
    """``default`` from the v1.5.10 settings: same request format, version strings and limits (D3)."""
    return JudgeProfile(
        name=DEFAULT_PROFILE,
        provider=settings.effective_judge_provider,
        base_url=settings.evaluation_judge_base_url,
        model=settings.evaluation_judge_model,
        api_key_env=LEGACY_API_KEY_ENV,
        structured_output="json_object",
        timeout_seconds=float(settings.evaluation_judge_timeout_seconds),
        max_input_chars=int(settings.evaluation_judge_max_input_chars),
        max_output_tokens=int(settings.evaluation_judge_max_output_tokens),
        pricing_provider="custom",
        external="auto",
        execution="inline",
        max_retries=0,
        strip_reasoning=True,
        legacy=True,
        source="settings",
    )


def _pricing_providers() -> set[str]:
    from app.schemas.finops import Provider

    return {item.value for item in Provider}


def _number(name: str, field: str, value: Any, *, kind: type, minimum: float, maximum: float | None = None) -> Any:
    if isinstance(value, bool):
        raise JudgeProfileError(f"Judge profile '{name}': {field} must be a number.")
    try:
        parsed = kind(value)
    except (TypeError, ValueError) as exc:
        raise JudgeProfileError(f"Judge profile '{name}': {field} must be a number.") from exc
    if parsed < minimum or (maximum is not None and parsed > maximum):
        bound = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise JudgeProfileError(f"Judge profile '{name}': {field} must be {bound}.")
    return parsed


def parse_profile(name: str, raw: Any, *, source: str, base: JudgeProfile | None = None) -> JudgeProfile:
    if not isinstance(raw, dict):
        raise JudgeProfileError(f"Judge profile '{name}' must be a mapping.")
    lowered = {str(key).strip().lower() for key in raw}
    secrets_found = sorted(lowered & FORBIDDEN_KEYS)
    if secrets_found:
        raise JudgeProfileError(
            f"Judge profile '{name}' contains secret field(s) {secrets_found}; store the key in an environment "
            "variable and reference it with api_key_env."
        )
    unknown = sorted(set(map(str, raw)) - PROFILE_FIELDS)
    if unknown:
        raise JudgeProfileError(f"Judge profile '{name}' has unknown field(s) {unknown}.")
    if "name" in raw and str(raw["name"]).strip().lower() != name:
        raise JudgeProfileError(f"Judge profile '{name}' declares a different name ({raw['name']!r}).")
    if not PROFILE_NAME_PATTERN.match(name):
        raise JudgeProfileError(f"Judge profile name {name!r} must match {PROFILE_NAME_PATTERN.pattern}.")
    if name == DETERMINISTIC_PROFILE:
        raise JudgeProfileError("The 'deterministic' judge profile is built in and cannot be redefined.")

    data = dict(raw)
    provider = str(data.get("provider", base.provider if base else "openai_compatible")).strip().lower()
    if provider not in PROVIDERS:
        raise JudgeProfileError(f"Judge profile '{name}': provider must be one of {', '.join(PROVIDERS)}.")
    structured = str(data.get("structured_output", "auto")).strip().lower()
    if structured not in STRUCTURED_OUTPUT_MODES:
        raise JudgeProfileError(f"Judge profile '{name}': structured_output must be one of {', '.join(STRUCTURED_OUTPUT_MODES)}.")
    execution = str(data.get("execution", "inline")).strip().lower()
    if execution not in EXECUTION_MODES:
        raise JudgeProfileError(f"Judge profile '{name}': execution must be one of {', '.join(EXECUTION_MODES)}.")
    external_raw = data.get("external", "auto")
    external = ("true" if external_raw else "false") if isinstance(external_raw, bool) else str(external_raw).strip().lower()
    if external not in EXTERNAL_MODES:
        raise JudgeProfileError(f"Judge profile '{name}': external must be auto, true or false.")
    api_key_env = str(data.get("api_key_env", "") or "").strip()
    if api_key_env and not ENV_NAME_PATTERN.match(api_key_env):
        raise JudgeProfileError(f"Judge profile '{name}': api_key_env must be an environment variable name (A-Z, 0-9, _).")
    pricing_provider = str(data.get("pricing_provider", "custom")).strip().lower()
    if pricing_provider not in _pricing_providers():
        raise JudgeProfileError(f"Judge profile '{name}': pricing_provider {pricing_provider!r} is not a FinOps provider.")
    base_url = str(data.get("base_url", "") or "").strip().rstrip("/")
    model = str(data.get("model", "") or "").strip()
    if provider == "openai_compatible" and (not base_url or not model):
        raise JudgeProfileError(f"Judge profile '{name}': openai_compatible needs base_url and model.")
    strip_reasoning = data.get("strip_reasoning", True)
    if not isinstance(strip_reasoning, bool):
        raise JudgeProfileError(f"Judge profile '{name}': strip_reasoning must be true or false.")

    return JudgeProfile(
        name=name,
        provider=provider,
        base_url=base_url,
        model=model[:128],
        api_key_env=api_key_env,
        structured_output=structured,
        timeout_seconds=_number(name, "timeout_seconds", data.get("timeout_seconds", 30), kind=float, minimum=0.1, maximum=600),
        max_input_chars=_number(name, "max_input_chars", data.get("max_input_chars", 12000), kind=int, minimum=500, maximum=500000),
        max_output_tokens=_number(name, "max_output_tokens", data.get("max_output_tokens", 600), kind=int, minimum=16, maximum=32768),
        pricing_provider=pricing_provider,
        pricing_model=str(data.get("pricing_model", "") or "").strip()[:128],
        external=external,
        execution=execution,
        max_retries=_number(name, "max_retries", data.get("max_retries", 0), kind=int, minimum=0, maximum=MAX_RETRIES_LIMIT),
        strip_reasoning=strip_reasoning,
        legacy=False,
        source=source,
    )


def _entries(data: Any, origin: str) -> dict[str, Any]:
    """Accept ``{profiles: [...]}``, a list of mappings with ``name``, or a ``name -> mapping`` dict."""
    if data is None:
        return {}
    if isinstance(data, dict) and "profiles" in data and set(data) <= {"profiles", "version"}:
        data = data["profiles"]
    if isinstance(data, list):
        entries: dict[str, Any] = {}
        for item in data:
            if not isinstance(item, dict) or not str(item.get("name", "")).strip():
                raise JudgeProfileError(f"Every judge profile in {origin} needs a name.")
            key = str(item["name"]).strip().lower()
            if key in entries:
                raise JudgeProfileError(f"Duplicate judge profile {key!r} in {origin}.")
            entries[key] = item
        return entries
    if isinstance(data, dict):
        return {str(key).strip().lower(): value for key, value in data.items()}
    raise JudgeProfileError(f"{origin} must contain a list or mapping of judge profiles.")


def _read_file(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JudgeProfileError(f"Cannot read judge profiles file {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise JudgeProfileError(f"Judge profiles file {path} is not valid YAML: {exc}") from exc
    return _entries(data, str(path))


_CACHE: dict[tuple, dict[str, JudgeProfile]] = {}


def _signature(settings: Settings) -> tuple:
    path = Path(settings.evaluation_judge_profiles_file) if settings.evaluation_judge_profiles_file else None
    file_sig: tuple = ("", None, None)
    if path is not None:
        try:
            stat = path.stat()
            file_sig = (str(path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            file_sig = (str(path), None, None)
    legacy = legacy_default_profile(settings)
    return (
        file_sig,
        json.dumps(settings.evaluation_judge_profiles or {}, sort_keys=True, default=str),
        (legacy.provider, legacy.base_url, legacy.model, legacy.timeout_seconds, legacy.max_input_chars, legacy.max_output_tokens),
    )


def load_profiles(settings: Settings) -> dict[str, JudgeProfile]:
    """All profiles by name. Raises :class:`JudgeProfileError` on invalid configuration."""
    signature = _signature(settings)
    cached = _CACHE.get(signature)
    if cached is not None:
        return cached
    profiles: dict[str, JudgeProfile] = {DETERMINISTIC_PROFILE: DETERMINISTIC, DEFAULT_PROFILE: legacy_default_profile(settings)}
    path_text = settings.evaluation_judge_profiles_file
    if path_text and Path(path_text).is_file():
        for name, raw in _read_file(Path(path_text)).items():
            profiles[name] = parse_profile(name, raw, source="file")
    for name, raw in _entries(settings.evaluation_judge_profiles or {}, "EVALUATION_JUDGE_PROFILES").items():
        profiles[name] = parse_profile(name, raw, source="env")
    if len(_CACHE) > 16:
        _CACHE.clear()
    _CACHE[signature] = profiles
    return profiles


def get_profile(settings: Settings, name: str) -> JudgeProfile | None:
    return load_profiles(settings).get((name or "").strip().lower())


def active_profile_known(settings: Settings) -> bool:
    return get_profile(settings, settings.evaluation_judge_profile) is not None


def resolve_active_profile(settings: Settings) -> JudgeProfile:
    """The live-triage profile; an unknown name falls back to ``deterministic`` (health reports it, D4)."""
    return get_profile(settings, settings.evaluation_judge_profile) or DETERMINISTIC
