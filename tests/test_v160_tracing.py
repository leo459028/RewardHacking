"""v1.6.0 P2a: W3C trace context, HTTP middleware and Kafka propagation (D1-D3). OTel export is P2b."""

from __future__ import annotations

import asyncio
import re

import pytest

from app.core import trace_context, tracing
from app.core.trace_context import (
    TraceContext,
    context_from_http,
    context_from_message,
    kafka_headers,
    parse_traceparent,
    trace_id_from_internal,
)
from app.workers.consumer import process_message

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN = "00f067aa0ba902b7"
VALID = f"00-{TRACE}-{SPAN}-01"
TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


# ---------------------------------------------------------------------------
# D1: parsing and generation
# ---------------------------------------------------------------------------


def test_parse_valid_traceparent():
    ctx = parse_traceparent(VALID, "vendor=abc")
    assert ctx == TraceContext(trace_id=TRACE, span_id=SPAN, flags="01", tracestate="vendor=abc")
    assert ctx.traceparent == VALID
    assert ctx.internal_id == f"trace_{TRACE}"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "garbage",
        f"00-{'0' * 32}-{SPAN}-01",  # all-zero trace id
        f"00-{TRACE}-{'0' * 16}-01",  # all-zero parent id
        f"ff-{TRACE}-{SPAN}-01",  # forbidden version
        f"00-{TRACE.upper()}-{SPAN}-01",  # uppercase hex
        f"00-{TRACE[:-1]}-{SPAN}-01",  # short trace id
        f"00-{TRACE}-{SPAN}-01-extra",  # version 00 has exactly four fields
    ],
)
def test_parse_rejects_invalid_traceparent(value):
    assert parse_traceparent(value) is None


def test_future_version_with_extra_fields_is_accepted():
    ctx = parse_traceparent(f"01-{TRACE}-{SPAN}-01-future")
    assert ctx is not None and ctx.trace_id == TRACE


def test_oversized_tracestate_is_dropped():
    ctx = parse_traceparent(VALID, "k=" + "v" * 600)
    assert ctx is not None and ctx.tracestate == ""


def test_generated_ids_are_valid_and_child_keeps_trace():
    root = trace_context.new_root()
    assert TRACEPARENT_RE.match(root.traceparent)
    child = root.child()
    assert child.trace_id == root.trace_id and child.span_id != root.span_id


def test_trace_id_from_internal():
    assert trace_id_from_internal(f"trace_{TRACE}") == TRACE
    for legacy in ("trace-v08-submit", "trace_abc", str(TRACE), f"trace_{'0' * 32}", None):
        assert trace_id_from_internal(legacy) is None


# ---------------------------------------------------------------------------
# D2: HTTP middleware
# ---------------------------------------------------------------------------


def test_context_from_http_precedence():
    from_header = context_from_http(VALID, None, "legacy-id")
    assert from_header.trace_id == TRACE and from_header.span_id != SPAN
    assert from_header.public_id == "legacy-id"  # a client X-Trace-Id is still echoed

    internal = context_from_http(None, None, f"trace_{TRACE}")
    assert internal.trace_id == TRACE

    fresh = context_from_http(None, None, None)
    assert fresh.public_id == f"trace_{fresh.trace_id}"


def test_http_without_headers_returns_matching_ids(client):
    response = client.get("/v1/healthz")
    traceparent = response.headers["traceparent"]
    assert TRACEPARENT_RE.match(traceparent)
    assert response.headers["X-Trace-Id"] == f"trace_{traceparent.split('-')[1]}"


def test_http_traceparent_is_continued(client):
    response = client.get("/v1/healthz", headers={"traceparent": VALID})
    _, trace_id, span_id, flags = response.headers["traceparent"].split("-")
    assert trace_id == TRACE and span_id != SPAN and flags == "01"
    assert response.headers["X-Trace-Id"] == f"trace_{TRACE}"


def test_http_legacy_x_trace_id_is_echoed_unchanged(client):
    response = client.get("/v1/healthz", headers={"X-Trace-Id": "trace-legacy-abc"})
    assert response.headers["X-Trace-Id"] == "trace-legacy-abc"
    assert TRACEPARENT_RE.match(response.headers["traceparent"])


def test_invalid_traceparent_starts_a_new_trace(client):
    response = client.get("/v1/healthz", headers={"traceparent": f"00-{'0' * 32}-{SPAN}-01"})
    assert response.headers["traceparent"].split("-")[1] != "0" * 32


# ---------------------------------------------------------------------------
# D3: Kafka propagation
# ---------------------------------------------------------------------------


def test_submit_event_carries_the_request_trace(client, low_risk_payload):
    response = client.post("/v1/agent/submit", json=low_risk_payload, headers={"traceparent": VALID})
    assert response.status_code == 200
    published = client.event_publisher.events[0]
    assert published["event"]["trace_id"] == f"trace_{TRACE}"
    header = parse_traceparent(published["headers"]["traceparent"])
    assert header is not None and header.trace_id == TRACE
    # the producer span is a new child, not the server span itself
    assert header.span_id != response.headers["traceparent"].split("-")[2]
    run = client.get(f"/v1/runs/{response.json()['run_id']}").json()
    assert run["request_payload"]["metadata"]["trace_id"] == f"trace_{TRACE}"


def test_legacy_x_trace_id_still_saved_on_submit(client, low_risk_payload):
    response = client.post("/v1/agent/submit", json=low_risk_payload, headers={"X-Trace-Id": "trace-v160-legacy"})
    run = client.get(f"/v1/runs/{response.json()['run_id']}").json()
    assert run["request_payload"]["metadata"]["trace_id"] == "trace-v160-legacy"
    published = client.event_publisher.events[0]
    assert published["event"]["trace_id"] == "trace-v160-legacy"
    assert TRACEPARENT_RE.match(published["headers"]["traceparent"])


def test_context_from_message_precedence():
    from_header = context_from_message([("traceparent", VALID.encode())], "trace_" + "1" * 32)
    assert from_header.trace_id == TRACE and from_header.span_id != SPAN

    from_event = context_from_message([], f"trace_{TRACE}")
    assert from_event.trace_id == TRACE

    fresh = context_from_message(None, "3f2c-legacy-uuid")
    assert TRACEPARENT_RE.match(fresh.traceparent)


def test_kafka_headers_are_bytes():
    ctx = TraceContext(trace_id=TRACE, span_id=SPAN, tracestate="a=b")
    assert kafka_headers(ctx) == [("traceparent", VALID.encode()), ("tracestate", b"a=b")]


def test_worker_continues_trace_and_publishes_with_it(client, low_risk_payload, session_factory):
    response = client.post("/v1/agent/submit", json=low_risk_payload, headers={"traceparent": VALID})
    request_event = client.event_publisher.events[0]
    headers = [(key, value.encode()) for key, value in request_event["headers"].items()]

    async def consume():
        return await process_message(
            request_event["event"],
            headers,
            session_factory=session_factory,
            state_store=client.state_store,
            event_publisher=client.event_publisher,
            topic="agent.requests",
        )

    updated = asyncio.run(consume())
    assert updated is not None and updated.status == "completed"
    downstream = client.event_publisher.events[1:]
    assert downstream, "worker should publish policy / execution / evaluation events"
    for item in downstream:
        assert parse_traceparent(item["headers"]["traceparent"]).trace_id == TRACE
    assert response.json()["run_id"] == updated.run_id


def test_message_without_run_id_is_skipped(session_factory):
    from app.services.events import InMemoryEventPublisher
    from app.services.run_state import InMemoryRunStateStore

    publisher = InMemoryEventPublisher()

    async def consume():
        return await process_message(
            {"payload": {}},
            None,
            session_factory=session_factory,
            state_store=InMemoryRunStateStore(),
            event_publisher=publisher,
            topic="agent.requests",
        )

    assert asyncio.run(consume()) is None
    assert publisher.events == []


def test_publish_outside_any_span_uses_event_trace_id():
    from app.services.events import InMemoryEventPublisher

    publisher = InMemoryEventPublisher()
    asyncio.run(publisher.publish("t", "k", {"event_id": "e", "trace_id": f"trace_{TRACE}", "run_id": "r", "event_type": "x", "ts": "now"}))
    assert parse_traceparent(publisher.events[0]["headers"]["traceparent"]).trace_id == TRACE


# ---------------------------------------------------------------------------
# No-op tracing layer (OTel export is P2b)
# ---------------------------------------------------------------------------


def test_start_span_sets_and_restores_current_context():
    assert trace_context.current() is None
    with tracing.start_span("outer") as outer:
        assert trace_context.current() == outer.context
        with tracing.start_span("inner") as inner:
            assert inner.context.trace_id == outer.context.trace_id
            assert inner.context.span_id != outer.context.span_id
        assert trace_context.current() == outer.context
    assert trace_context.current() is None


def test_only_tracing_module_imports_opentelemetry():
    # Third-party packages (redis-py's observability module) may import opentelemetry on their own when it is
    # installed, so check our own sources instead of sys.modules.
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    offenders = [
        str(path.relative_to(root)) for path in root.rglob("*.py")
        if "opentelemetry" in path.read_text(encoding="utf-8") and path.name != "tracing.py"
    ]
    assert offenders == []
    assert tracing.enabled() is False
