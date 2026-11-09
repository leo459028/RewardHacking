"""v1.5.11 named judge profiles (plan D1–D6, D16), extended in v1.5.12 (activation, hardening, fallback).

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
4. the JSON setting ``EVALUATION_JUDGE_PROFILES``;
5. (v1.5.12) Dashboard-managed profiles from ``judge_profile_configs`` (source ``dashboard``). A Dashboard name may not
   collide with sources 1-4; a colliding or no-longer-valid row is skipped and reported, never fatal (D4).

Secrets never live in a profile: a profile that contains ``api_key`` (or similar) is rejected at load time.

v1.5.12 hardening for every source (D10, D11): ``api_key_env`` must end in ``_API_KEY`` or be listed in
``EVALUATION_JUDGE_KEY_ENV_ALLOWLIST`` (so a profile cannot send ``DATABASE_URL`` anywhere), and ``base_url`` must be a
plain http(s) URL without userinfo / query / fragment, never a cloud metadata address, and ``https`` for any host that
is treated as external.

Which profile live triage uses is decided by :func:`resolve_active` (D2): a Dashboard activation whose fingerprint
still matches, else the ``EVALUATION_JUDGE_PROFILE`` profile if it has a matching successful probe, else
``deterministic``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from app.core.config import Settings
from app.services.evaluation.judges import activation_state

PROFILE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
# v1.5.12 (D10): only variables that are obviously API keys, or ones an operator listed explicitly.
KEY_ENV_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*_API_KEY$")
PROVIDERS: tuple[str, ...] = ("deterministic", "openai_compatible", "anthropic")
HTTP_PROVIDERS: tuple[str, ...] = ("openai_compatible", "anthropic")
STRUCTURED_OUTPUT_MODES: tuple[str, ...] = ("auto", "json_schema", "json_object", "none")
EXECUTION_MODES: tuple[str, ...] = ("inline", "background")
EXTERNAL_MODES: tuple[str, ...] = ("auto", "true", "false")
MAX_RETRIES_LIMIT = 5
MAX_FALLBACKS = 2
# v1.5.12 (D11): cloud instance-metadata endpoints are never a judge, whatever the scheme or host policy.
METADATA_HOSTS = frozenset({"169.254.169.254", "fd00:ec2::254", "metadata.google.internal"})
# v1.5.11 P7: sent as the OpenAI-style ``reasoning_effort`` request field only when set. LM Studio honours
# "none" for Qwen3.5 (reasoning models otherwise spend max_output_tokens thinking and return empty content).
REASONING_EFFORTS: tuple[str, ...] = ("", "none", "minimal", "low", "medium", "high")
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
        "reasoning_effort",
        "fallback",
        "daily_token_cap",
    }
)


class JudgeProfileError(ValueError):
    """Invalid judge profile configuration (raised at startup)."""


def key_digest(value: str) -> str:
    """First 16 hex of the key's sha256 ("" when unset). Used only inside fingerprints and key status (D5, D22)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] if value else ""


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
    reasoning_effort: str = ""
    fallback: tuple[str, ...] = ()  # v1.5.12 (D13): tried in order when this profile fails; deterministic is always last
    daily_token_cap: int | None = None  # v1.5.12 (D15): per tenant, per profile, per UTC day; None = global cap only
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

    def key_status(self, settings: Settings) -> dict[str, Any]:
        """v1.5.12 (D22): the variable name, whether it is set and a 6-hex fingerprint. Never any key character."""
        return {
            "api_key_env": self.api_key_env,
            "configured": self.api_key_configured(settings),
            "key_fingerprint": key_digest(self.api_key(settings))[:6],
        }

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
            "reasoning_effort": self.reasoning_effort,
            "fallback": list(self.fallback),
            "daily_token_cap": self.daily_token_cap,
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


# ---------------------------------------------------------------------------
# v1.5.12 hardening rules (D10, D11)
# ---------------------------------------------------------------------------


def key_env_allowed(api_key_env: str, settings: Settings | None) -> bool:
    if not api_key_env or KEY_ENV_PATTERN.match(api_key_env):
        return True
    return settings is not None and api_key_env in settings.judge_key_env_allowlist


def url_is_external(base_url: str, external: str, settings: Settings) -> bool:
    """Same rule as the HTTP judges (v1.5.11 D14): ``external: true`` forces external; exact internal host names and
    internal suffixes are internal; anything unparseable is external. ``external: false`` cannot downgrade a host."""
    from app.services.network_policy import is_external_endpoint

    if external == "true":
        return True
    try:
        host = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    except ValueError:
        host = ""
    if host and host in set(settings.judge_internal_host_list):
        return False
    return is_external_endpoint("streamable_http", base_url, settings.mcp_internal_host_suffix_list)


def base_url_problem(base_url: str, *, external: bool) -> str:
    """Why ``base_url`` may not be used (D11), or "" when it is acceptable."""
    if not base_url:
        return ""
    try:
        parts = urlsplit(base_url)
        host = (parts.hostname or "").lower().rstrip(".")
        _ = parts.port  # raises ValueError for an invalid port
    except ValueError:
        return "base_url is not a valid URL."
    if parts.scheme not in {"http", "https"}:
        return "base_url must use http or https."
    if not host:
        return "base_url needs a host name."
    if "@" in parts.netloc:
        return "base_url must not contain user credentials."
    if parts.query or parts.fragment or "?" in base_url or "#" in base_url:
        return "base_url must not contain a query string or fragment."
    if host in METADATA_HOSTS:
        return "base_url points at a cloud metadata address, which is never allowed."
    if external and parts.scheme != "https":
        return "an external host must use HTTPS."
    return ""


def _number(name: str, field_name: str, value: Any, *, kind: type, minimum: float, maximum: float | None = None) -> Any:
    if isinstance(value, bool):
        raise JudgeProfileError(f"Judge profile '{name}': {field_name} must be a number.")
    try:
        parsed = kind(value)
    except (TypeError, ValueError) as exc:
        raise JudgeProfileError(f"Judge profile '{name}': {field_name} must be a number.") from exc
    if parsed < minimum or (maximum is not None and parsed > maximum):
        bound = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise JudgeProfileError(f"Judge profile '{name}': {field_name} must be {bound}.")
    return parsed


def _parse_fallback(name: str, raw: Any) -> tuple[str, ...]:
    if raw in (None, "", []):
        return ()
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        raise JudgeProfileError(f"Judge profile '{name}': fallback must be a list of profile names.")
    names: list[str] = []
    for item in raw:
        target = str(item).strip().lower()
        if not target or target == DETERMINISTIC_PROFILE:
            continue  # deterministic is always the implicit last step
        if not PROFILE_NAME_PATTERN.match(target):
            raise JudgeProfileError(f"Judge profile '{name}': fallback entry {target!r} is not a profile name.")
        if target == name:
            raise JudgeProfileError(f"Judge profile '{name}': fallback cannot point at itself.")
        if target in names:
            raise JudgeProfileError(f"Judge profile '{name}': fallback lists {target!r} twice.")
        names.append(target)
    if len(names) > MAX_FALLBACKS:
        raise JudgeProfileError(f"Judge profile '{name}': at most {MAX_FALLBACKS} fallback profiles.")
    return tuple(names)


def parse_profile(
    name: str, raw: Any, *, source: str, base: JudgeProfile | None = None, settings: Settings | None = None
) -> JudgeProfile:
    """Validate one profile.

    Every loading path passes ``settings`` so the v1.5.12 rules that depend on the host policy (HTTPS for external
    hosts, the key-variable allowlist) apply. Without settings the structural rules (scheme, userinfo, query,
    metadata address, ``*_API_KEY`` name) still apply; the HTTP judges re-check the URL before every request.
    """
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

    data = {key: value for key, value in raw.items() if value is not None}  # v1.5.12 (D18): null = system default
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
    if not key_env_allowed(api_key_env, settings):
        raise JudgeProfileError(
            f"Judge profile '{name}': api_key_env must end with _API_KEY or be listed in EVALUATION_JUDGE_KEY_ENV_ALLOWLIST."
        )
    pricing_provider = str(data.get("pricing_provider", "custom")).strip().lower()
    if pricing_provider not in _pricing_providers():
        raise JudgeProfileError(f"Judge profile '{name}': pricing_provider {pricing_provider!r} is not a FinOps provider.")
    base_url = str(data.get("base_url", "") or "").strip().rstrip("/")
    model = str(data.get("model", "") or "").strip()
    if provider in HTTP_PROVIDERS and (not base_url or not model):
        raise JudgeProfileError(f"Judge profile '{name}': {provider} needs base_url and model.")
    if base_url:
        external_host = settings is not None and url_is_external(base_url, external, settings)
        problem = base_url_problem(base_url, external=external_host)
        if problem:
            raise JudgeProfileError(f"Judge profile '{name}': {problem}")
    strip_reasoning = data.get("strip_reasoning", True)
    if not isinstance(strip_reasoning, bool):
        raise JudgeProfileError(f"Judge profile '{name}': strip_reasoning must be true or false.")
    reasoning_effort = str(data.get("reasoning_effort", "") or "").strip().lower()
    if reasoning_effort not in REASONING_EFFORTS:
        raise JudgeProfileError(f"Judge profile '{name}': reasoning_effort must be one of {', '.join(e or '(empty)' for e in REASONING_EFFORTS)}.")
    fallback = _parse_fallback(name, data.get("fallback"))
    cap_raw = data.get("daily_token_cap")
    daily_token_cap = None if cap_raw in (None, "") else _number(name, "daily_token_cap", cap_raw, kind=int, minimum=1, maximum=10**9)

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
        reasoning_effort=reasoning_effort,
        fallback=fallback,
        daily_token_cap=daily_token_cap,
        legacy=False,
        source=source,
    )


def fallback_problems(profiles: dict[str, JudgeProfile]) -> dict[str, str]:
    """Unknown fallback targets and cycles, by profile name (D13)."""
    problems: dict[str, str] = {}
    for name, profile in profiles.items():
        missing = [target for target in profile.fallback if target not in profiles]
        if missing:
            problems[name] = f"fallback target {missing[0]!r} is not defined."
            continue
        seen = {name}
        stack = list(profile.fallback)
        while stack:
            current = stack.pop()
            if current in seen:
                problems[name] = f"fallback chain loops back to {current!r}."
                break
            seen.add(current)
            target_profile = profiles.get(current)
            if target_profile is not None:
                stack.extend(target_profile.fallback)
    return problems


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
# Dashboard rows that could not be loaded (name collision or a rule they no longer satisfy), by profile name.
_DB_ERRORS: dict[tuple, dict[str, str]] = {}


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
        tuple(sorted(settings.judge_key_env_allowlist)),
        tuple(settings.judge_internal_host_list),
        tuple(settings.mcp_internal_host_suffix_list),
        activation_state.snapshot().db_signature(),
    )


def _load_static(settings: Settings) -> dict[str, JudgeProfile]:
    legacy = legacy_default_profile(settings)
    if legacy.base_url:
        problem = base_url_problem(legacy.base_url, external=url_is_external(legacy.base_url, legacy.external, settings))
        if problem:
            raise JudgeProfileError(f"EVALUATION_JUDGE_BASE_URL: {problem}")
    profiles: dict[str, JudgeProfile] = {DETERMINISTIC_PROFILE: DETERMINISTIC, DEFAULT_PROFILE: legacy}
    path_text = settings.evaluation_judge_profiles_file
    if path_text and Path(path_text).is_file():
        for name, raw in _read_file(Path(path_text)).items():
            profiles[name] = parse_profile(name, raw, source="file", settings=settings)
    for name, raw in _entries(settings.evaluation_judge_profiles or {}, "EVALUATION_JUDGE_PROFILES").items():
        profiles[name] = parse_profile(name, raw, source="env", settings=settings)
    problems = fallback_problems(profiles)
    if problems:
        name, problem = sorted(problems.items())[0]
        raise JudgeProfileError(f"Judge profile '{name}': {problem}")
    return profiles


def _merge_dashboard(profiles: dict[str, JudgeProfile], settings: Settings) -> dict[str, str]:
    errors: dict[str, str] = {}
    for name, (_version, raw) in sorted(activation_state.snapshot().db_configs.items()):
        if name in profiles:
            errors[name] = f"name collides with a {profiles[name].source} profile; the {profiles[name].source} profile is used."
            continue
        try:
            profiles[name] = parse_profile(name, raw, source="dashboard", settings=settings)
        except JudgeProfileError as exc:
            errors[name] = str(exc)
    while True:  # drop Dashboard profiles whose fallback chain is broken (they may depend on each other)
        broken = {name: problem for name, problem in fallback_problems(profiles).items() if profiles[name].source == "dashboard"}
        if not broken:
            break
        for name, problem in broken.items():
            errors[name] = problem
            profiles.pop(name)
    return errors


def load_profiles(settings: Settings) -> dict[str, JudgeProfile]:
    """All profiles by name. Raises :class:`JudgeProfileError` on invalid file / environment configuration."""
    signature = _signature(settings)
    cached = _CACHE.get(signature)
    if cached is not None:
        return cached
    profiles = _load_static(settings)
    errors = _merge_dashboard(profiles, settings)
    if len(_CACHE) > 16:
        _CACHE.clear()
        _DB_ERRORS.clear()
    _CACHE[signature] = profiles
    _DB_ERRORS[signature] = errors
    return profiles


def static_profiles(settings: Settings) -> dict[str, JudgeProfile]:
    """Built-in, settings, file and environment profiles only (Dashboard names must not collide with these, D4)."""
    return {name: profile for name, profile in load_profiles(settings).items() if profile.source != "dashboard"}


def dashboard_profile_errors(settings: Settings) -> dict[str, str]:
    load_profiles(settings)
    return dict(_DB_ERRORS.get(_signature(settings), {}))


def get_profile(settings: Settings, name: str) -> JudgeProfile | None:
    return load_profiles(settings).get((name or "").strip().lower())


def active_profile_known(settings: Settings) -> bool:
    return get_profile(settings, settings.evaluation_judge_profile) is not None


# ---------------------------------------------------------------------------
# v1.5.12 configuration fingerprints and active-profile resolution (D2, D5, D6)
# ---------------------------------------------------------------------------


def config_fingerprint(profile: JudgeProfile, settings: Settings) -> str:
    """sha256 over what decides whether a probe result still holds: endpoint, model, key (as a short digest),
    request format and output budget. Timeouts and prices are excluded on purpose (D5)."""
    payload = {
        "provider": profile.provider,
        "base_url": profile.base_url,
        "model": profile.model,
        "api_key_env": profile.api_key_env,
        "key_digest": key_digest(profile.api_key(settings)),
        "structured_output": profile.structured_output,
        "reasoning_effort": profile.reasoning_effort,
        "external": profile.external,
        "max_output_tokens": profile.max_output_tokens,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def probe_matches(profile: JudgeProfile, settings: Settings, state: activation_state.ProfileState | None) -> bool:
    """A successful probe recorded for exactly this configuration (key included)."""
    return bool(state and state.probe_status == "ok" and state.probe_fingerprint == config_fingerprint(profile, settings))


def probe_note(profile: JudgeProfile, settings: Settings, state: activation_state.ProfileState | None) -> str:
    """``not_probed`` / ``probe_failed`` / ``needs_reprobe`` (fingerprint changed) / "" (usable)."""
    if profile.provider == "deterministic":
        return ""
    if state is None or not state.probe_status:
        return "not_probed"
    if state.probe_status != "ok":
        return "probe_failed"
    if state.probe_fingerprint != config_fingerprint(profile, settings):
        return "needs_reprobe"
    return ""


@dataclass(frozen=True)
class ActiveResolution:
    """Which profile live triage uses and why. ``source``: ``dashboard`` / ``environment`` / ``default``."""

    profile: JudgeProfile
    source: str
    configured_profile: str = ""
    notes: dict[str, str] = field(default_factory=dict)  # profile name -> why it is not in use (needs_reprobe, ...)

    @property
    def needs_attention(self) -> bool:
        return bool(self.notes)


def resolve_active(settings: Settings) -> ActiveResolution:
    """D2: ① a Dashboard activation whose fingerprint still matches → ② the ``EVALUATION_JUDGE_PROFILE`` profile
    (non-deterministic) with a matching successful probe → ③ ``deterministic``. A stale activation counts as absent."""
    snap = activation_state.snapshot()
    profiles = load_profiles(settings)
    configured = settings.evaluation_judge_profile
    notes: dict[str, str] = {}
    for name, state in sorted(snap.states.items()):
        if not state.active:
            continue
        profile = profiles.get(name)
        if profile is None:
            notes[name] = "unknown_profile"
        elif profile.provider != "deterministic" and config_fingerprint(profile, settings) != state.activation_fingerprint:
            notes[name] = "needs_reprobe"
        else:
            return ActiveResolution(profile, "dashboard", configured, notes)
    env_profile = profiles.get(configured)
    if env_profile is None:
        notes.setdefault(configured, "unknown_profile")
    elif env_profile.provider != "deterministic":
        state = snap.states.get(env_profile.name)
        if activation_state.TRUST_ENV_PROFILE_WITHOUT_PROBE or probe_matches(env_profile, settings, state):
            return ActiveResolution(env_profile, "environment", configured, notes)
        notes.setdefault(env_profile.name, probe_note(env_profile, settings, state))
    else:
        # e.g. the synthesized ``default`` without a provider: rule-based, kept by name for continuity (v1.5.11).
        return ActiveResolution(env_profile, "default", configured, notes)
    return ActiveResolution(DETERMINISTIC, "default", configured, notes)


def resolve_active_profile(settings: Settings) -> JudgeProfile:
    """The live-triage profile (D2). Unknown, unprobed or changed profiles fall back to ``deterministic``."""
    return resolve_active(settings).profile
