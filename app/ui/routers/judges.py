"""v1.5.12 Dashboard: judge profile management (`/dashboard/judge-profiles`, plan D16-D19).

- Lists every profile with its source, status label, key status (name / set / 6-hex fingerprint only), last probe,
  readiness reasons and breaker; the header shows which judge live triage really uses and why.
- Create / edit Dashboard profiles from templates (blank optional fields = system default, drafts allowed), test the
  connection, activate / deactivate, delete. Keys are never entered here: only the environment variable *name*.
- Writes need the editing gate (``EVALUATION_JUDGE_DASHBOARD_EDITING``, D16) and a same-origin ``Origin`` / ``Referer``
  (D17). A full CSRF token scheme and in-page key entry wait for Dashboard RBAC (v1.6.x).
- A judge only orders review work: nothing on this page changes verdicts, review priorities or statuses (D23).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.infra.postgres import get_session
from app.services.evaluation.judges.profiles import EXECUTION_MODES, PROVIDERS, REASONING_EFFORTS, STRUCTURED_OUTPUT_MODES
from app.services.evaluation.judges.templates import get_template, list_templates
from app.services.judge_activation import REASON_MESSAGES
from app.services.judge_profiles_admin import (
    INPUT_FIELDS,
    JudgeProfileAdminError,
    activate_profile,
    create_profile,
    dashboard_editing,
    deactivate_profiles,
    delete_profile,
    probe_profile,
    profiles_overview,
    update_profile,
)
from app.ui.routes import dashboard_auth, templates
from app.ui.security import actor_for, needs

router = APIRouter(tags=["dashboard"], dependencies=[Depends(dashboard_auth)])
PAGE = "/dashboard/judge-profiles"
DASHBOARD_ACTOR = "dashboard-admin"
STATUS_TEXT = {
    "draft": "Draft | 草稿",
    "invalid": "Invalid | 設定無效",
    "untested": "Untested | 待測試",
    "probe_failed": "Probe failed | 測試失敗",
    "ready": "Ready | 可啟用",
    "active": "Active | 使用中",
    "needs_reprobe": "Needs re-test | 需重新測試",
    "breaker_open": "Offline (breaker) | 離線（熔斷）",
}
SOURCE_TEXT = {"dashboard": "Dashboard", "environment": "environment (.env)", "default": "default"}
# System defaults shown as grey placeholders (D18); they mirror parse_profile().
DEFAULT_HINTS = {
    "provider": "openai_compatible",
    "structured_output": "auto",
    "timeout_seconds": "30",
    "max_input_chars": "12000",
    "max_output_tokens": "600",
    "pricing_provider": "custom",
    "pricing_model": "(same as model)",
    "external": "auto",
    "execution": "inline",
    "max_retries": "0",
    "strip_reasoning": "true",
    "reasoning_effort": "(not sent)",
    "fallback": "(none; deterministic is always last)",
    "daily_token_cap": "(global cap only)",
    "api_key_env": "e.g. OPENAI_API_KEY (must end with _API_KEY)",
    "base_url": "e.g. http://host.docker.internal:1234/v1",
    "model": "required",
}


def _redirect(*, message: str = "", error: str = "", edit: str = "") -> RedirectResponse:
    query = f"message={quote(message)}" if message else f"error={quote(error)}"
    if edit:
        query += f"&edit={quote(edit)}"
    return RedirectResponse(f"{PAGE}?{query}", status_code=303)


def same_origin(request: Request) -> bool:
    """D17: the form must come from this host (``Origin``, else ``Referer``). Missing both is refused."""
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    if not source or source == "null":
        return False
    try:
        origin_host = urlsplit(source).netloc.lower()
    except ValueError:
        return False
    host = (request.headers.get("host") or request.url.netloc or "").lower()
    return bool(origin_host) and origin_host == host


def _guard(request: Request, *, editing: bool = True) -> RedirectResponse | None:
    if not same_origin(request):
        return None  # handled by caller as 403
    if editing:
        gate = dashboard_editing(get_settings())
        if not gate["allowed"]:
            return _redirect(error=f"Editing judge profiles is disabled: {gate['reason']}")
    return None


def _forbidden() -> HTMLResponse:
    return HTMLResponse("Cross-origin form submission refused (Origin / Referer must match this host).", status_code=403)


def _config_from_form(form: Any) -> dict[str, Any]:
    """Raw strings from the form; blank = system default. The service validates everything (D18)."""
    config: dict[str, Any] = {}
    for key in INPUT_FIELDS:
        value = str(form.get(key, "") or "").strip()
        if value == "":
            config[key] = None
        elif key == "strip_reasoning":
            config[key] = value.lower() in {"true", "1", "yes", "on"}
        elif key == "fallback":
            config[key] = [item.strip() for item in value.split(",") if item.strip()]
        else:
            config[key] = value
    return config


def _form_values(source: dict[str, Any] | None) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in INPUT_FIELDS:
        value = (source or {}).get(key)
        if value is None:
            values[key] = ""
        elif isinstance(value, bool):
            values[key] = "true" if value else "false"
        elif isinstance(value, list):
            values[key] = ", ".join(str(item) for item in value)
        else:
            values[key] = str(value)
    return values


@router.get(PAGE, response_class=HTMLResponse, dependencies=needs("judge.view", view="judge_profiles"))
async def dashboard_judge_profiles(request: Request, session: AsyncSession = Depends(get_session)):
    params = request.query_params
    settings = get_settings()
    overview = await profiles_overview(session, settings)
    by_name = {row["name"]: row for row in overview["profiles"]}
    editing_row = by_name.get(params.get("edit", "")) if params.get("edit") else None
    if editing_row is not None and not editing_row.get("editable"):
        editing_row = None
    template = get_template(params.get("template", "")) if params.get("template") else None
    form_source = (editing_row or {}).get("config") or (template or {}).get("config")
    return templates.TemplateResponse(
        request,
        "judge_profiles.html",
        {
            "active": "judge-profiles",
            "overview": overview,
            "resolution": overview["resolution"],
            "editing": overview["editing"],
            "rows": overview["profiles"],
            "status_text": STATUS_TEXT,
            "source_text": SOURCE_TEXT,
            "reason_messages": REASON_MESSAGES,
            "templates_list": list_templates(),
            "template": template,
            "editing_row": editing_row,
            "show_form": bool(editing_row or template or params.get("new")),
            "form": _form_values(form_source),
            "hints": {**DEFAULT_HINTS, **((template or {}).get("hints") or {})},
            "choices": {
                "provider": PROVIDERS,
                "structured_output": STRUCTURED_OUTPUT_MODES,
                "execution": EXECUTION_MODES,
                "reasoning_effort": REASONING_EFFORTS,
                "external": ("auto", "true", "false"),
            },
            "judge_mode": settings.evaluation_judge_mode,
            "message": params.get("message", ""),
            "error": params.get("error", ""),
        },
    )


@router.post(PAGE, response_class=HTMLResponse, dependencies=needs("judge.profile.write", view="judge_profiles"))
async def dashboard_judge_profile_create(request: Request, session: AsyncSession = Depends(get_session)):
    if not same_origin(request):
        return _forbidden()
    if (blocked := _guard(request)) is not None:
        return blocked
    form = await request.form()
    name = str(form.get("name", "") or "").strip().lower()
    try:
        result = await create_profile(session, get_settings(), name, _config_from_form(form), actor=actor_for(request, DASHBOARD_ACTOR))
    except JudgeProfileAdminError as exc:
        return _redirect(error=f"{name or 'profile'}: {exc.detail}")
    state = "saved as a draft (fill in the required fields to test it)" if result["draft"] else "saved; test the connection next"
    return _redirect(message=f"Profile {name} {state}.")


@router.post(PAGE + "/deactivate", response_class=HTMLResponse, dependencies=needs("judge.profile.write", view="judge_profiles"))
async def dashboard_judge_profile_deactivate(request: Request, session: AsyncSession = Depends(get_session)):
    if not same_origin(request):
        return _forbidden()
    if (blocked := _guard(request)) is not None:
        return blocked
    result = await deactivate_profiles(session, get_settings(), actor=actor_for(request, DASHBOARD_ACTOR))
    in_use = result["resolution"]["profile"]
    message = f"Dashboard activation cleared; live triage now uses {in_use}."
    return _redirect(message=f"{message} {result['note']}".strip())


@router.post(PAGE + "/{name}/update", response_class=HTMLResponse, dependencies=needs("judge.profile.write", view="judge_profiles"))
async def dashboard_judge_profile_update(request: Request, name: str, session: AsyncSession = Depends(get_session)):
    if not same_origin(request):
        return _forbidden()
    if (blocked := _guard(request)) is not None:
        return blocked
    form = await request.form()
    try:
        version = int(str(form.get("version", "0") or "0"))
        result = await update_profile(session, get_settings(), name, _config_from_form(form), version=version, actor=actor_for(request, DASHBOARD_ACTOR))
    except ValueError:
        return _redirect(error="Missing profile version; reload the page.", edit=name)
    except JudgeProfileAdminError as exc:
        return _redirect(error=f"{name}: {exc.detail}", edit=name)
    note = " It was active and has been deactivated: test and activate it again." if result["deactivated"] else ""
    return _redirect(message=f"Profile {name} updated (version {result['version']}).{note}")


@router.post(PAGE + "/{name}/delete", response_class=HTMLResponse, dependencies=needs("judge.profile.write", view="judge_profiles"))
async def dashboard_judge_profile_delete(request: Request, name: str, session: AsyncSession = Depends(get_session)):
    if not same_origin(request):
        return _forbidden()
    if (blocked := _guard(request)) is not None:
        return blocked
    form = await request.form()
    version_text = str(form.get("version", "") or "")
    try:
        await delete_profile(session, get_settings(), name, actor=actor_for(request, DASHBOARD_ACTOR), version=int(version_text) if version_text else None)
    except JudgeProfileAdminError as exc:
        return _redirect(error=f"{name}: {exc.detail}")
    return _redirect(message=f"Profile {name} deleted.")


@router.post(PAGE + "/{name}/probe", response_class=HTMLResponse, dependencies=needs("judge.profile.operate", view="judge_profiles"))
async def dashboard_judge_profile_probe(request: Request, name: str, session: AsyncSession = Depends(get_session)):
    if not same_origin(request):
        return _forbidden()
    if (blocked := _guard(request)) is not None:
        return blocked
    try:
        result = await probe_profile(session, get_settings(), name, actor=actor_for(request, DASHBOARD_ACTOR))
    except JudgeProfileAdminError as exc:
        return _redirect(error=f"{name}: {exc.detail}")
    if result["status"] != "ok":
        return _redirect(error=f"Connection test of {name} failed: {result['status']} ({result['error'] or 'no detail'}), {result['latency_ms']} ms.")
    return _redirect(
        message=(
            f"Connection test of {name} succeeded: {result['latency_ms']} ms, {result['input_tokens']}/{result['output_tokens']} tokens, "
            f"structured output {result['structured_output'] or 'n/a'}. It is not used until you activate it."
        )
    )


@router.post(PAGE + "/{name}/activate", response_class=HTMLResponse, dependencies=needs("judge.profile.write", view="judge_profiles"))
async def dashboard_judge_profile_activate(request: Request, name: str, session: AsyncSession = Depends(get_session)):
    if not same_origin(request):
        return _forbidden()
    if (blocked := _guard(request)) is not None:
        return blocked
    try:
        await activate_profile(session, get_settings(), name, actor=actor_for(request, DASHBOARD_ACTOR))
    except JudgeProfileAdminError as exc:
        return _redirect(error=f"Cannot activate {name}: {exc.detail}")
    return _redirect(message=f"{name} is now used for live triage (Dashboard activation).")
