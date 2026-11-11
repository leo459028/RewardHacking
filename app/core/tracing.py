"""Span API used by the application (v1.6.0 D5-D8).

``start_span()`` always creates a child ``TraceContext`` and makes it current, so W3C propagation works with no
optional dependency (P2a). When ``OTEL_ENABLED=true`` and the ``otel`` extra is installed (``pip install .[otel]``),
``configure()`` also exports each span through OpenTelemetry (OTLP/HTTP; endpoint, service name and sampler come from
the standard ``OTEL_*`` environment variables). The exported span uses exactly the ids of the ``TraceContext`` (a preset
id generator), so ``traceparent`` headers, ``trace_id`` columns and the exported trace always agree.

This module is the only place that imports OpenTelemetry. Attribute keys pass an allowlist (D6); prompts, diffs,
test reports, judge input / output and keys must never be attached to a span.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from typing import Any, Iterator

from app.core import trace_context

logger = logging.getLogger(__name__)

SPAN_KIND_SERVER = "server"
SPAN_KIND_CLIENT = "client"
SPAN_KIND_PRODUCER = "producer"
SPAN_KIND_CONSUMER = "consumer"
SPAN_KIND_INTERNAL = "internal"

# D6: exact keys and prefixes that may be exported. Everything else is dropped and counted.
ALLOWED_ATTRIBUTE_KEYS = frozenset(
    {
        "rh.run_id", "rh.tenant_id", "rh.review_id", "rh.task_type", "rh.status", "rh.verdict", "rh.priority",
        "rh.tool_name", "rh.mcp_server_id", "rh.legacy_trace_id", "rh.attempt", "rh.tool_count",
        "rh.judge.profile", "rh.judge.status", "rh.judge.fallback_from",
        "http.request.method", "http.route", "http.response.status_code", "url.path",
        "messaging.system", "messaging.operation.type", "messaging.destination.name",
        "gen_ai.operation.name", "gen_ai.provider.name", "gen_ai.request.model", "gen_ai.response.model",
        "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.response.finish_reasons",
    }
)
ALLOWED_ATTRIBUTE_PREFIXES = ("rh.sandbox.", "rh.retention.")
MAX_ATTRIBUTE_STRING = 256

_PRESET_IDS: contextvars.ContextVar[tuple[int, int] | None] = contextvars.ContextVar("rh_preset_ids", default=None)


class _State:
    tracer: Any = None
    provider: Any = None
    otel: Any = None  # imported opentelemetry.trace module


_STATE = _State()


def enabled() -> bool:
    return _STATE.tracer is not None


def _allowed(key: str) -> bool:
    return key in ALLOWED_ATTRIBUTE_KEYS or key.startswith(ALLOWED_ATTRIBUTE_PREFIXES)


def filter_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Apply the allowlist; keep only scalar values; truncate strings. Dropped keys are counted, never logged."""
    from app.observability.metrics import TRACING_DROPPED_ATTRIBUTES_TOTAL

    kept: dict[str, Any] = {}
    for key, value in attributes.items():
        if not _allowed(key) or value is None:
            if value is not None:
                TRACING_DROPPED_ATTRIBUTES_TOTAL.inc()
            continue
        if isinstance(value, str):
            kept[key] = value[:MAX_ATTRIBUTE_STRING]
        elif isinstance(value, (bool, int, float)):
            kept[key] = value
        elif isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
            kept[key] = [item[:MAX_ATTRIBUTE_STRING] for item in value][:16]
        else:
            TRACING_DROPPED_ATTRIBUTES_TOTAL.inc()
    return kept


class Span:
    """Handle returned by ``start_span``; forwards to the OTel span when export is enabled."""

    def __init__(self, name: str, context: trace_context.TraceContext, kind: str, otel_span: Any = None) -> None:
        self.name = name
        self.context = context
        self.kind = kind
        self._otel_span = otel_span

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        kept = filter_attributes(attributes)
        if self._otel_span is not None and kept:
            self._otel_span.set_attributes(kept)

    def set_error(self, error_type: str) -> None:
        if self._otel_span is not None:
            otel = _STATE.otel
            self._otel_span.set_attribute("error.type", error_type[:MAX_ATTRIBUTE_STRING])
            self._otel_span.set_status(otel.Status(otel.StatusCode.ERROR))


def _otel_kind(kind: str) -> Any:
    otel = _STATE.otel
    return {
        SPAN_KIND_SERVER: otel.SpanKind.SERVER,
        SPAN_KIND_CLIENT: otel.SpanKind.CLIENT,
        SPAN_KIND_PRODUCER: otel.SpanKind.PRODUCER,
        SPAN_KIND_CONSUMER: otel.SpanKind.CONSUMER,
    }.get(kind, otel.SpanKind.INTERNAL)


def _span_context(otel: Any, ctx: trace_context.TraceContext, span_id: str) -> Any:
    return otel.SpanContext(
        trace_id=int(ctx.trace_id, 16),
        span_id=int(span_id, 16),
        is_remote=True,
        trace_flags=otel.TraceFlags(int(ctx.flags, 16)),
    )


@contextlib.contextmanager
def _otel_span(name: str, kind: str, span_context: trace_context.TraceContext, links: list[trace_context.TraceContext]):
    otel = _STATE.otel
    parent = None
    if span_context.parent_span_id:
        parent = otel.set_span_in_context(otel.NonRecordingSpan(_span_context(otel, span_context, span_context.parent_span_id)))
    otel_links = [otel.Link(_span_context(otel, link, link.span_id)) for link in links if link is not None]
    token = _PRESET_IDS.set((int(span_context.trace_id, 16), int(span_context.span_id, 16)))
    try:
        span = _STATE.tracer.start_span(name, context=parent, kind=_otel_kind(kind), links=otel_links)
    finally:
        _PRESET_IDS.reset(token)
    try:
        yield span
    except BaseException as exc:
        span.set_attribute("error.type", type(exc).__name__)
        span.set_status(otel.Status(otel.StatusCode.ERROR))
        raise
    finally:
        span.end()


@contextlib.contextmanager
def start_span(
    name: str,
    *,
    kind: str = SPAN_KIND_INTERNAL,
    context: trace_context.TraceContext | None = None,
    new_span: bool = True,
    attributes: dict[str, Any] | None = None,
    links: list[trace_context.TraceContext] | None = None,
) -> Iterator[Span]:
    """Start a span as a child of ``context`` (or of the current context, or a new root) and make it current.

    ``new_span=False`` uses ``context`` itself as the span (the HTTP middleware and the Kafka consumer already derived
    it). ``links`` connect asynchronous work to the span that triggered it (D4).
    """
    parent = context or trace_context.current()
    if parent is None:
        span_context = trace_context.new_root()
    elif new_span:
        span_context = parent.child()
    else:
        span_context = parent
    with trace_context.use(span_context):
        if _STATE.tracer is None:
            yield Span(name, span_context, kind)
            return
        with _otel_span(name, kind, span_context, list(links or [])) as otel_span:
            span = Span(name, span_context, kind, otel_span)
            if attributes:
                span.set_attributes(attributes)
            yield span


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _make_id_generator(base: Any) -> Any:
    class _PresetIdGenerator(base):  # type: ignore[misc, valid-type]
        """Use the TraceContext ids for the span being started; random ids otherwise."""

        def generate_span_id(self) -> int:
            preset = _PRESET_IDS.get()
            return preset[1] if preset else super().generate_span_id()

        def generate_trace_id(self) -> int:
            preset = _PRESET_IDS.get()
            return preset[0] if preset else super().generate_trace_id()

    return _PresetIdGenerator()


def configure(settings: Any, *, exporter: Any = None, service_name: str = "") -> bool:
    """Enable OTel export when ``settings.otel_enabled``; return whether export is active.

    ``exporter`` replaces the OTLP exporter (tests pass an in-memory exporter and get a synchronous processor).
    Missing packages or a failing exporter setup never break the app: tracing stays a no-op and a warning is logged.
    """
    shutdown()
    if not getattr(settings, "otel_enabled", False):
        return False
    try:
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
        from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
    except ImportError:
        logger.warning("OTEL_ENABLED=true but the otel extra is not installed (pip install .[otel]); tracing stays off")
        return False
    attributes = {"service.version": getattr(settings, "app_version", "") or ""}
    if service_name:
        attributes["service.name"] = service_name
    provider = TracerProvider(resource=Resource.create({k: v for k, v in attributes.items() if v}), id_generator=_make_id_generator(RandomIdGenerator))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        except Exception as exc:  # pragma: no cover - depends on the environment
            logger.warning("OTLP exporter setup failed (%s); tracing stays off", type(exc).__name__)
            return False
    _STATE.provider = provider
    _STATE.tracer = provider.get_tracer("rewardhacking")
    _STATE.otel = otel_trace
    return True


def shutdown() -> None:
    provider = _STATE.provider
    _STATE.tracer = None
    _STATE.provider = None
    _STATE.otel = None
    if provider is not None:
        try:
            provider.shutdown()
        except Exception:  # pragma: no cover - exporter shutdown failures are not actionable
            logger.debug("tracer provider shutdown failed", exc_info=True)


def exemplar(context: trace_context.TraceContext | None) -> dict[str, str] | None:
    """Prometheus exemplar labels linking a histogram observation to its trace (only while exporting, D8)."""
    if context is None or not enabled():
        return None
    return {"trace_id": context.trace_id}


def status(settings: Any) -> dict[str, Any]:
    """Tracing status for the Observability page and API (no endpoint secrets: host only)."""
    import os
    from urllib.parse import urlsplit

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or ""
    try:
        host = urlsplit(endpoint).hostname or ""
    except ValueError:
        host = ""
    return {
        "w3c_propagation": True,
        "otel_enabled_setting": bool(getattr(settings, "otel_enabled", False)),
        "exporting": enabled(),
        "exporter_host": host,
        "sampler": os.environ.get("OTEL_TRACES_SAMPLER", "parentbased_always_on"),
        "service_name": os.environ.get("OTEL_SERVICE_NAME", ""),
        "trace_ui_url_template": getattr(settings, "otel_trace_ui_url_template", "") or "",
        "dropped_attributes": dropped_attribute_count(),
    }


def dropped_attribute_count() -> int:
    """Attributes refused by the D6 allowlist in this process since start (Prometheus counter value)."""
    from prometheus_client import REGISTRY

    return int(REGISTRY.get_sample_value("tracing_dropped_attributes_total") or 0)
