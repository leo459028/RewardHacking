"""Span API used by the application (v1.6.0 D5-D8).

P2a ships the no-op implementation only: ``start_span()`` creates a child ``TraceContext`` and makes it current,
so W3C propagation works end to end without any optional dependency. P2b adds the OpenTelemetry exporter behind
``OTEL_ENABLED`` (``pip install .[otel]``) without changing these call sites. This module is the only place that will
ever import OpenTelemetry.
"""

from __future__ import annotations

import contextlib
from typing import Any, Iterator

from app.core import trace_context

SPAN_KIND_SERVER = "server"
SPAN_KIND_CLIENT = "client"
SPAN_KIND_PRODUCER = "producer"
SPAN_KIND_CONSUMER = "consumer"
SPAN_KIND_INTERNAL = "internal"


class Span:
    """Handle returned by ``start_span``. The no-op version records nothing."""

    def __init__(self, name: str, context: trace_context.TraceContext, kind: str) -> None:
        self.name = name
        self.context = context
        self.kind = kind

    def set_attributes(self, attributes: dict[str, Any]) -> None:  # noqa: ARG002 - filled in by P2b
        return None

    def add_link(self, context: trace_context.TraceContext | None) -> None:  # noqa: ARG002 - filled in by P2b
        return None


@contextlib.contextmanager
def start_span(
    name: str,
    *,
    kind: str = SPAN_KIND_INTERNAL,
    context: trace_context.TraceContext | None = None,
    new_span: bool = True,
) -> Iterator[Span]:
    """Start a span as a child of ``context`` (or of the current context, or a new root) and make it current.

    ``new_span=False`` uses ``context`` itself as the span (for callers that already derived the span context, such
    as the HTTP middleware and the Kafka consumer).
    """
    parent = context or trace_context.current()
    if parent is None:
        span_context = trace_context.new_root()
    elif new_span:
        span_context = parent.child()
    else:
        span_context = parent
    with trace_context.use(span_context):
        yield Span(name, span_context, kind)
