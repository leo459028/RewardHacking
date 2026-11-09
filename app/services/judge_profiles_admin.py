"""v1.5.11 judge profile administration: listing, connectivity probe and local zero-cost pricing (plan D13, D18).

- ``profiles_view()``: safe view of every profile (no keys, host only) with the effective external flag and the
  structured-output mode the backend actually accepted.
- ``probe_profile()``: sends one fixed, harmless synthetic input (no tenant data, so no tenant consent is needed;
  the global external switch still applies). It never writes a ``judge_assessments`` row; tokens are ledgered with
  ``metadata.purpose="probe"``.
- ``ensure_zero_cost_pricing()``: creates zero-price input/output rows for a ``qa_local`` profile so local models
  stop raising ``missing_pricing_rule`` violations. Real providers are never zeroed.

v1.5.12 adds Dashboard-managed profiles (``create_profile`` / ``update_profile`` / ``delete_profile``, drafts allowed,
optimistic locking), ``activate_profile`` / ``deactivate_profiles`` behind the readiness gate, ``profiles_overview()``
(status labels, key status, probe, breaker) and the Dashboard editing gate ``dashboard_editing()``. Every change is
audited as ``evaluation.judge_profile.*`` without any key material.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.finops import PricingCatalog
from app.models.judge import JudgeProfileConfig, JudgeProfileState
from app.services.evaluation.judges import JudgeResult, build_judge, triage_priority_for
from app.services.evaluation.judges.base import JudgeInput
from app.services.evaluation.judges.openai_compatible import cached_mode
from app.services.evaluation.judges import activation_state
from app.services.evaluation.judges.profiles import (
    DETERMINISTIC_PROFILE,
    FORBIDDEN_KEYS,
    HTTP_PROVIDERS,
    PROFILE_FIELDS,
    PROFILE_NAME_PATTERN,
    JudgeProfile,
    JudgeProfileError,
    config_fingerprint,
    dashboard_profile_errors,
    fallback_problems,
    get_profile,
    load_profiles,
    parse_profile,
    probe_note,
    resolve_active,
    resolve_active_profile,
    static_profiles,
)

LOCAL_PRICING_PROVIDER = "qa_local"
PROBE_INPUT = JudgeInput(
    task_type="code_fix",
    goal="Fix a typo in a docstring",
    diff=(
        "diff --git a/app/util.py b/app/util.py\n--- a/app/util.py\n+++ b/app/util.py\n@@ -1,2 +1,2 @@\n"
        ' def value():\n-    """Retrun the value."""\n+    """Return the value."""\n'
    ),
    test_report='{"format": "summary", "summary": {"passed": 3, "failed": 0}}',
    final_message="Fixed the docstring typo. All 3 tests pass.",
    findings=[],
)


class JudgeProfileAdminError(Exception):
    def __init__(self, code: str, detail: str, status_code: int, *, extra: dict[str, Any] | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code
        self.extra = dict(extra or {})


def _profile_or_error(settings: Settings, name: str) -> JudgeProfile:
    profile = get_profile(settings, name)
    if profile is None:
        key = (name or "").strip().lower()
        if key in activation_state.snapshot().db_configs:
            # v1.5.12 (D18): a draft or a no-longer-valid Dashboard row cannot be tested or activated yet.
            reason = dashboard_profile_errors(settings).get(key) or "provider, base_url and model are required."
            raise JudgeProfileAdminError(
                "judge_profile_not_ready", reason, 409, extra={"reasons": [{"code": "missing_fields", "message": reason}]}
            )
        raise JudgeProfileAdminError("judge_profile_not_found", f"Judge profile {name!r} is not defined.", 404)
    return profile


def effective_external(profile: JudgeProfile, settings: Settings) -> bool:
    if profile.provider == "deterministic":
        return False
    return bool(getattr(build_judge(profile, settings), "is_external", False))


def profiles_view(settings: Settings) -> list[dict[str, Any]]:
    active = resolve_active_profile(settings).name
    rows = []
    for profile in load_profiles(settings).values():
        judge = build_judge(profile, settings)
        rows.append(
            {
                **profile.public_dict(settings),
                "judge_version": judge.version(),
                "active": profile.name == active,
                "effective_external": effective_external(profile, settings),
                "accepted_structured_output": cached_mode(profile.base_url, profile.model) if profile.provider != "deterministic" else "",
            }
        )
    return sorted(rows, key=lambda row: (not row["active"], row["name"] != "deterministic", row["name"]))


async def probe_profile(
    session: AsyncSession, settings: Settings, name: str, *, tenant_id: str = "default", actor: str = "api"
) -> dict[str, Any]:
    """Probe with the fixed synthetic input; v1.5.12 stores the outcome and its fingerprint (D3) and audits it."""
    from app.services.judge_activation import record_probe
    from app.services.judge_triage import _record_judge_usage

    profile = _profile_or_error(settings, name)
    judge = build_judge(profile, settings)
    output = await judge.assess(PROBE_INPUT)
    result = JudgeResult(
        primary=output,
        fallback=None,
        triage_score=float(output.suspicion or 0.0),
        triage_priority=triage_priority_for(output.suspicion),
        triage_source=output.judge_version,
        injection_suspected=False,
    )
    usage_id = await _record_judge_usage(
        session, settings=settings, tenant_id=tenant_id, run_id="", result=result, extra={"purpose": "probe", "judge_profile": profile.name}
    )
    external = effective_external(profile, settings)
    if profile.provider != "deterministic":
        await record_probe(session, settings, profile, output, actor=actor, effective_external_flag=external)
    return {
        "profile": profile.name,
        "provider": output.provider,
        "model": output.model,
        "judge_version": output.judge_version,
        "status": output.status,
        "error": output.error,
        "suspicion": output.suspicion,
        "structured_output": output.structured_output,
        "attempts": output.attempts,
        "latency_ms": output.latency_ms,
        "input_tokens": output.input_tokens,
        "output_tokens": output.output_tokens,
        "effective_external": external,
        "usage_id": usage_id,
        "assessment_written": False,
    }


async def _pricing_rows(session: AsyncSession, provider: str, model: str) -> list[PricingCatalog]:
    return list(
        (
            await session.scalars(
                select(PricingCatalog).where(
                    PricingCatalog.provider == provider,
                    PricingCatalog.model_name == model,
                    PricingCatalog.resource_type == "model",
                    PricingCatalog.is_active.is_(True),
                )
            )
        ).all()
    )


async def ensure_zero_cost_pricing(session: AsyncSession, settings: Settings, name: str) -> dict[str, Any]:
    from app.schemas.finops import PricingCatalogCreateRequest
    from app.services.finops.pricing import create_pricing_item

    profile = _profile_or_error(settings, name)
    if profile.provider == "deterministic":
        raise JudgeProfileAdminError("judge_profile_not_priced", "The deterministic judge uses no tokens.", 409)
    if profile.pricing_provider != LOCAL_PRICING_PROVIDER:
        raise JudgeProfileAdminError(
            "judge_profile_not_local",
            f"Zero-cost pricing is only created for pricing_provider '{LOCAL_PRICING_PROVIDER}' (got '{profile.pricing_provider}').",
            409,
        )
    model = profile.effective_pricing_model
    existing = {str(row.token_type) for row in await _pricing_rows(session, LOCAL_PRICING_PROVIDER, model)}
    created: list[str] = []
    for token_type in ("input", "output"):
        if token_type in existing:
            continue
        row = await create_pricing_item(
            session,
            PricingCatalogCreateRequest(
                provider=LOCAL_PRICING_PROVIDER,
                resource_type="model",
                resource_subtype="chat_completion",
                model_name=model,
                token_type=token_type,
                price_unit="per_1m_tokens",
                unit_price=Decimal("0"),
                currency="USD",
                source_url=f"manual://judge-profile/{profile.name}",
                source_type="manual",
                pricing_version="judge-profile-local",
            ),
        )
        created.append(row.pricing_id)
    return {"profile": profile.name, "provider": LOCAL_PRICING_PROVIDER, "model": model, "created": created, "already_present": sorted(existing)}


async def missing_pricing(session: AsyncSession, settings: Settings, profile: JudgeProfile) -> bool:
    if profile.provider == "deterministic":
        return False
    return not await _pricing_rows(session, profile.pricing_provider, profile.effective_pricing_model)


# ---------------------------------------------------------------------------
# v1.5.12 profile management: CRUD, activation, overview, editing gate (plan D3, D4, D8, D9, D16, D18, D20-D22)
# ---------------------------------------------------------------------------

INPUT_FIELDS = tuple(sorted(PROFILE_FIELDS - {"name"}))
DRAFT_PLACEHOLDER_URL = "http://localhost/v1"
DRAFT_PLACEHOLDER_MODEL = "draft-placeholder"
STATUS_LABELS = ("draft", "invalid", "active", "breaker_open", "needs_reprobe", "probe_failed", "untested", "ready")


def dashboard_editing(settings: Settings) -> dict[str, Any]:
    """D16: may the Dashboard write judge profiles? ``auto`` needs Basic Auth on with non-default credentials."""
    mode = settings.evaluation_judge_dashboard_editing
    if mode == "off":
        return {"allowed": False, "mode": mode, "warning": False, "reason": "EVALUATION_JUDGE_DASHBOARD_EDITING=off."}
    if mode == "insecure_dev":
        return {
            "allowed": True,
            "mode": mode,
            "warning": True,
            "reason": "insecure_dev: anyone who can open the Dashboard can change judge profiles. Local development only.",
        }
    default_credentials = settings.dashboard_username == "admin" and settings.dashboard_password == "admin"
    if not settings.dashboard_auth_enabled:
        reason = "Dashboard Basic Auth is off (DASHBOARD_AUTH_ENABLED=false)."
    elif default_credentials:
        reason = "Dashboard Basic Auth still uses the default admin/admin credentials."
    else:
        return {"allowed": True, "mode": mode, "warning": False, "reason": ""}
    return {"allowed": False, "mode": mode, "warning": False, "reason": reason}


def _clean_config(raw: dict[str, Any]) -> dict[str, Any]:
    """D18: blank optional fields are stored as null (system default at load time)."""
    lowered = {str(key).strip().lower() for key in raw}
    secrets_found = sorted(lowered & FORBIDDEN_KEYS)
    if secrets_found:
        raise JudgeProfileAdminError(
            "judge_profile_secret_field", f"Keys are never stored in a profile ({', '.join(secrets_found)}); set api_key_env instead.", 422
        )
    unknown = sorted(set(map(str, raw)) - set(INPUT_FIELDS))
    if unknown:
        raise JudgeProfileAdminError("judge_profile_invalid", f"Unknown field(s): {', '.join(unknown)}.", 422)
    cleaned: dict[str, Any] = {}
    for key in INPUT_FIELDS:
        value = raw.get(key)
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                value = None
        if key == "fallback" and isinstance(value, list):
            value = [str(item).strip().lower() for item in value if str(item).strip()] or None
        cleaned[key] = value
    return cleaned


def is_draft(config: dict[str, Any]) -> bool:
    provider = str(config.get("provider") or "openai_compatible").strip().lower()
    return provider in HTTP_PROVIDERS and (not config.get("base_url") or not config.get("model"))


def _validate(settings: Settings, name: str, config: dict[str, Any], *, existing_name: str | None = None) -> tuple[bool, JudgeProfile]:
    """Validate a Dashboard config; a draft (missing base_url / model) is checked with placeholders for the rest."""
    draft = is_draft(config)
    candidate = dict(config)
    if draft:
        candidate["base_url"] = candidate.get("base_url") or DRAFT_PLACEHOLDER_URL
        candidate["model"] = candidate.get("model") or DRAFT_PLACEHOLDER_MODEL
    try:
        profile = parse_profile(name, candidate, source="dashboard", settings=settings)
    except JudgeProfileError as exc:
        raise JudgeProfileAdminError("judge_profile_invalid", str(exc).replace(f"Judge profile '{name}': ", ""), 422) from exc
    profiles = {key: value for key, value in load_profiles(settings).items() if key != (existing_name or name)}
    profiles[name] = profile
    problem = fallback_problems(profiles).get(name)
    if problem:
        raise JudgeProfileAdminError("judge_profile_invalid", problem, 422)
    return draft, profile


async def _config_row(session: AsyncSession, name: str) -> JudgeProfileConfig | None:
    return await session.scalar(select(JudgeProfileConfig).where(JudgeProfileConfig.name == name))


def _normalize_name(name: str) -> str:
    return (name or "").strip().lower()


async def create_profile(session: AsyncSession, settings: Settings, name: str, raw: dict[str, Any], *, actor: str) -> dict[str, Any]:
    from app.services.judge_activation import audit, refresh_activation_cache

    name = _normalize_name(name)
    if not PROFILE_NAME_PATTERN.match(name) or name == DETERMINISTIC_PROFILE:
        raise JudgeProfileAdminError("judge_profile_invalid", f"Name must match {PROFILE_NAME_PATTERN.pattern} and not be 'deterministic'.", 422)
    static = static_profiles(settings)
    if name in static:
        raise JudgeProfileAdminError("judge_profile_name_conflict", f"A {static[name].source} profile named {name!r} already exists.", 409)
    if await _config_row(session, name) is not None:
        raise JudgeProfileAdminError("judge_profile_name_conflict", f"A Dashboard profile named {name!r} already exists.", 409)
    config = _clean_config(raw)
    draft, _profile = _validate(settings, name, config)
    row = JudgeProfileConfig(name=name, config=config, version=1, created_by=actor[:128], updated_by=actor[:128])
    session.add(row)
    await session.commit()
    await audit(
        session,
        event="created",
        profile=name,
        actor=actor,
        decision="draft" if draft else "saved",
        details={"fields": sorted(key for key, value in config.items() if value is not None), "draft": draft},
    )
    await refresh_activation_cache(session)
    return {"name": name, "version": row.version, "draft": draft, "config": config}


async def update_profile(
    session: AsyncSession, settings: Settings, name: str, raw: dict[str, Any], *, version: int, actor: str
) -> dict[str, Any]:
    from app.services.judge_activation import audit, refresh_activation_cache, state_row

    name = _normalize_name(name)
    row = await _config_row(session, name)
    if row is None:
        if name in static_profiles(settings):
            raise JudgeProfileAdminError("judge_profile_read_only", f"Profile {name!r} comes from a file, the environment or is built in.", 409)
        raise JudgeProfileAdminError("judge_profile_not_found", f"Judge profile {name!r} is not defined.", 404)
    if int(row.version) != int(version):
        raise JudgeProfileAdminError(
            "judge_profile_version_conflict", f"Profile {name!r} changed since you loaded it (version {row.version}, you sent {version}).", 409
        )
    config = _clean_config(raw)
    draft, _profile = _validate(settings, name, config, existing_name=name)
    before = dict(row.config or {})
    changed = sorted(key for key in set(before) | set(config) if before.get(key) != config.get(key))
    row.config = config
    row.version = int(row.version) + 1
    row.updated_by = actor[:128]
    state = await state_row(session, name)
    was_active = bool(state is not None and state.active)
    if was_active:
        state.active = False  # D20: an edited profile must be tested and activated again
        state.activation_fingerprint = ""
    await session.commit()
    await audit(session, event="updated", profile=name, actor=actor, decision="draft" if draft else "saved", details={"changed_fields": changed, "version": row.version})
    if was_active:
        await audit(session, event="deactivated", profile=name, actor=actor, decision="edited", details={"reason": "profile edited"})
    await refresh_activation_cache(session)
    return {"name": name, "version": row.version, "draft": draft, "config": config, "deactivated": was_active, "changed_fields": changed}


async def delete_profile(session: AsyncSession, settings: Settings, name: str, *, actor: str, version: int | None = None) -> dict[str, Any]:
    from app.services.judge_activation import audit, refresh_activation_cache, state_row

    name = _normalize_name(name)
    row = await _config_row(session, name)
    if row is None:
        if name in static_profiles(settings):
            raise JudgeProfileAdminError("judge_profile_read_only", f"Profile {name!r} comes from a file, the environment or is built in.", 409)
        raise JudgeProfileAdminError("judge_profile_not_found", f"Judge profile {name!r} is not defined.", 404)
    if version is not None and int(row.version) != int(version):
        raise JudgeProfileAdminError("judge_profile_version_conflict", f"Profile {name!r} changed since you loaded it.", 409)
    state = await state_row(session, name)
    if state is not None and state.active:
        raise JudgeProfileAdminError("judge_profile_active", f"Deactivate {name!r} before deleting it.", 409)
    referencing = sorted(
        other.name for other in load_profiles(settings).values() if name in other.fallback and other.name != name
    ) + sorted(
        other_name
        for other_name, (_version, other_config) in activation_state.snapshot().db_configs.items()
        if other_name != name and name in [str(item).strip().lower() for item in list((other_config or {}).get("fallback") or [])]
    )
    if referencing:
        raise JudgeProfileAdminError(
            "judge_profile_referenced", f"{name!r} is a fallback of {', '.join(sorted(set(referencing)))}; remove it there first.", 409
        )
    await session.delete(row)
    if state is not None:
        await session.delete(state)
    await session.commit()
    await audit(session, event="deleted", profile=name, actor=actor, decision="deleted", details={"version": row.version})
    await refresh_activation_cache(session)
    return {"name": name, "deleted": True}


async def activate_profile(session: AsyncSession, settings: Settings, name: str, *, actor: str) -> dict[str, Any]:
    """D8 / D9: only a profile whose readiness list is empty may be activated; at most one active row (D3)."""
    from app.services.judge_activation import audit, maybe_refresh, readiness, refresh_activation_cache, state_row

    await maybe_refresh(session, settings, force=True)
    profile = _profile_or_error(settings, name)
    ready = await readiness(session, settings, profile)
    if not ready["ready"]:
        raise JudgeProfileAdminError(
            "judge_profile_not_ready", "; ".join(item["message"] for item in ready["reasons"]), 409, extra={"reasons": ready["reasons"]}
        )
    fingerprint = config_fingerprint(profile, settings)
    for other in (await session.scalars(select(JudgeProfileState).where(JudgeProfileState.active.is_(True)))).all():
        if other.profile_name != profile.name:
            other.active = False
            other.activation_fingerprint = ""
    row = await state_row(session, profile.name)
    if row is None:
        row = JudgeProfileState(profile_name=profile.name)
        session.add(row)
    row.active = True
    row.activation_fingerprint = fingerprint
    row.activated_by = actor[:128]
    row.activated_at = datetime.now(timezone.utc)
    await session.commit()
    await audit(session, event="activated", profile=profile.name, actor=actor, decision="activated", details={"fingerprint": fingerprint[:12]})
    await refresh_activation_cache(session)
    return {"profile": profile.name, "active": True, "fingerprint": fingerprint[:12], "resolution": resolution_view(settings)}


async def deactivate_profiles(session: AsyncSession, settings: Settings, *, actor: str) -> dict[str, Any]:
    """Clear every Dashboard activation. A profile chosen by EVALUATION_JUDGE_PROFILE stays in use until .env changes."""
    from app.services.judge_activation import audit, refresh_activation_cache

    cleared = []
    for row in (await session.scalars(select(JudgeProfileState).where(JudgeProfileState.active.is_(True)))).all():
        row.active = False
        row.activation_fingerprint = ""
        cleared.append(row.profile_name)
    await session.commit()
    for name in cleared:
        await audit(session, event="deactivated", profile=name, actor=actor, decision="deactivated", details={})
    await refresh_activation_cache(session)
    resolution = resolution_view(settings)
    note = ""
    if resolution["source"] == "environment":
        note = f"{resolution['profile']} is selected by EVALUATION_JUDGE_PROFILE; change .env to stop using it."
    return {"deactivated": cleared, "resolution": resolution, "note": note}


def resolution_view(settings: Settings) -> dict[str, Any]:
    resolution = resolve_active(settings)
    return {
        "profile": resolution.profile.name,
        "provider": resolution.profile.provider,
        "source": resolution.source,
        "configured_profile": resolution.configured_profile,
        "notes": dict(resolution.notes),
    }


def status_label(
    profile: JudgeProfile, settings: Settings, state: activation_state.ProfileState | None, *, in_use: bool, breaker_open: bool
) -> str:
    if breaker_open and profile.provider != "deterministic":
        return "breaker_open"  # shown even for the profile in use: that is exactly when the operator needs to see it
    if in_use:
        return "active"
    if profile.provider == "deterministic":
        return "ready"
    note = probe_note(profile, settings, state)
    if note == "needs_reprobe":
        return "needs_reprobe"
    if note == "probe_failed":
        return "probe_failed"
    if note == "not_probed":
        return "untested"
    return "ready"


async def profiles_overview(session: AsyncSession, settings: Settings) -> dict[str, Any]:
    """Everything the management page and API show: rows with status label, key status (D22), last probe,
    readiness reasons, breaker; Dashboard drafts / invalid rows; the active resolution and the editing gate."""
    from app.services.judge_activation import breaker_state, maybe_refresh, readiness

    await maybe_refresh(session, settings)
    resolution = resolve_active(settings)
    snap = activation_state.snapshot()
    rows = []
    for row in profiles_view(settings):
        profile = get_profile(settings, row["name"])
        state = snap.states.get(profile.name)
        breaker = await breaker_state(settings, profile.name) if profile.provider != "deterministic" else None
        in_use = profile.name == resolution.profile.name
        ready = await readiness(session, settings, profile)
        db_entry = snap.db_configs.get(profile.name)
        rows.append(
            {
                **row,
                "active": in_use,
                "active_source": resolution.source if in_use else "",
                "status": status_label(profile, settings, state, in_use=in_use, breaker_open=breaker is not None),
                "key_status": profile.key_status(settings),
                "editable": profile.source == "dashboard",
                "version": db_entry[0] if db_entry else None,
                "config": dict(db_entry[1]) if db_entry else None,
                "probe": _probe_view(state),
                "readiness": ready,
                "breaker": breaker,
                "activated": bool(state and state.active),
            }
        )
    errors = dashboard_profile_errors(settings)
    for name, (version, config) in sorted(snap.db_configs.items()):
        if name in {row["name"] for row in rows}:
            continue
        draft = is_draft(config)
        rows.append(
            {
                "name": name,
                "provider": str(config.get("provider") or "openai_compatible"),
                "model": str(config.get("model") or ""),
                "host": "",
                "source": "dashboard",
                "status": "draft" if draft else "invalid",
                "error": "" if draft else errors.get(name, ""),
                "editable": True,
                "version": version,
                "config": dict(config),
                "active": False,
                "key_status": {"api_key_env": str(config.get("api_key_env") or ""), "configured": False, "key_fingerprint": ""},
                "probe": _probe_view(snap.states.get(name)),
                "readiness": {"ready": False, "reasons": [{"code": "missing_fields", "message": "provider, base_url and model are required."}]},
                "breaker": None,
            }
        )
    return {"resolution": resolution_view(settings), "profiles": rows, "editing": dashboard_editing(settings), "dashboard_errors": errors}


def _probe_view(state: activation_state.ProfileState | None) -> dict[str, Any]:
    if state is None or not state.probe_status:
        return {"status": "", "error": "", "probed_at": None, "latency_ms": None, "input_tokens": None, "output_tokens": None, "structured_output": ""}
    result = dict(state.probe_result or {})
    return {
        "status": state.probe_status,
        "error": state.probe_error,
        "probed_at": state.probed_at.isoformat() if state.probed_at else None,
        "latency_ms": result.get("latency_ms"),
        "input_tokens": result.get("input_tokens"),
        "output_tokens": result.get("output_tokens"),
        "structured_output": result.get("structured_output", ""),
    }
