"""W3C Trace Context (Level 1) parsing, generation and in-process propagation (v1.6.0 D1-D3).

Dependency-free and always active, whether or not OpenTelemetry export is enabled. The OTel layer
(``app.core.tracing``) only exports spans; correlation across API, Kafka and worker never depends on it.

``traceparent`` = ``{version:2}-{trace_id:32}-{parent_id:16}-{flags:2}`` (lowercase hex).
"""

from __future__ import annotations

import contextlib
import contextvars
import re
import secrets
from dataclasses import dataclass, replace
from typing import Iterable, Iterator

TRACEPARENT_HEADER = "traceparent"
TRACESTATE_HEADER = "tracestate"
TRACESTATE_MAX_BYTES = 512
_TRACEPARENT_RE = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})(-.*)?$")
_INTERNAL_TRACE_ID_RE = re.compile(r"^trace_([0-9a-f]{32})$")
_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str
    flags: str = "01"
    tracestate: str = ""
    # The value stored in ``trace_id`` columns and echoed as ``X-Trace-Id`` (D2). Usually ``trace_<trace_id>``;
    # a client-supplied non-W3C ``X-Trace-Id`` is kept here unchanged for backward compatibility.
    display_id: str = ""
    # Span id of this span's parent ("" for a root or a context rebuilt from an id only); used by the OTel exporter.
    parent_span_id: str = ""

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.flags}"

    @property
    def internal_id(self) -> str:
        return f"trace_{self.trace_id}"

    @property
    def public_id(self) -> str:
        return self.display_id or self.internal_id

    def child(self) -> "TraceContext":
        """A new span in the same trace (this context becomes the parent)."""
        return replace(self, span_id=new_span_id(), parent_span_id=self.span_id)

    def headers(self) -> dict[str, str]:
        out = {TRACEPARENT_HEADER: self.traceparent}
        if self.tracestate:
            out[TRACESTATE_HEADER] = self.tracestate
        return out


def new_trace_id() -> str:
    while True:
        value = secrets.token_hex(16)
        if value != _ZERO_TRACE:
            return value


def new_span_id() -> str:
    while True:
        value = secrets.token_hex(8)
        if value != _ZERO_SPAN:
            return value


def new_root(display_id: str = "") -> TraceContext:
    return TraceContext(trace_id=new_trace_id(), span_id=new_span_id(), display_id=display_id)


def _clean_tracestate(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw or len(raw.encode("utf-8")) > TRACESTATE_MAX_BYTES:
        return ""
    return raw


def parse_traceparent(value: str | None, tracestate: str | None = None) -> TraceContext | None:
    """Parse a ``traceparent`` header; return ``None`` when it is invalid (callers then start a new trace)."""
    raw = (value or "").strip()
    match = _TRACEPARENT_RE.match(raw)
    if not match:
        return None
    version, trace_id, span_id, flags, extra = match.groups()
    if version == "ff" or trace_id == _ZERO_TRACE or span_id == _ZERO_SPAN:
        return None
    if version == "00" and extra:
        return None  # version 00 has exactly four fields
    return TraceContext(trace_id=trace_id, span_id=span_id, flags=flags, tracestate=_clean_tracestate(tracestate))


def trace_id_from_internal(value: str | None) -> str | None:
    """``trace_<32 hex>`` → ``<32 hex>``; anything else (legacy ids, uuids) → ``None``."""
    match = _INTERNAL_TRACE_ID_RE.match((value or "").strip())
    if not match or match.group(1) == _ZERO_TRACE:
        return None
    return match.group(1)


def context_from_http(traceparent: str | None, tracestate: str | None, x_trace_id: str | None) -> TraceContext:
    """D2: the server span for an incoming HTTP request (``traceparent`` wins; otherwise a new trace)."""
    legacy = (x_trace_id or "").strip()
    parent = parse_traceparent(traceparent, tracestate)
    if parent is not None:
        return replace(parent.child(), display_id=legacy)
    internal = trace_id_from_internal(legacy)
    if internal is not None:
        return TraceContext(trace_id=internal, span_id=new_span_id(), display_id=legacy)
    return new_root(display_id=legacy)


def _header_value(headers: Iterable[tuple[str, bytes | str]] | None, name: str) -> str | None:
    for key, value in headers or ():
        if str(key).lower() == name:
            return value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value)
    return None


def context_from_message(headers: Iterable[tuple[str, bytes | str]] | None, event_trace_id: str | None) -> TraceContext:
    """D3: the consumer span for a Kafka message (header → ``BaseEvent.trace_id`` → new trace)."""
    parent = parse_traceparent(_header_value(headers, TRACEPARENT_HEADER), _header_value(headers, TRACESTATE_HEADER))
    if parent is not None:
        return parent.child()
    internal = trace_id_from_internal(event_trace_id)
    if internal is not None:
        return TraceContext(trace_id=internal, span_id=new_span_id())
    return new_root()


def kafka_headers(context: TraceContext) -> list[tuple[str, bytes]]:
    return [(key, value.encode("utf-8")) for key, value in context.headers().items()]


# ---------------------------------------------------------------------------
# In-process propagation
# ---------------------------------------------------------------------------

_CURRENT: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar("rh_trace_context", default=None)


def current() -> TraceContext | None:
    return _CURRENT.get()


@contextlib.contextmanager
def use(context: TraceContext | None) -> Iterator[TraceContext | None]:
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)
