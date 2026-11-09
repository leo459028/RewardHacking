"""v1.6.0 P2b: optional OpenTelemetry export (D5-D8) through an in-memory exporter (no network)."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("opentelemetry.sdk.trace")

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from opentelemetry.trace import SpanKind  # noqa: E402

from app.core import trace_context, tracing  # noqa: E402
from app.workers.consumer import process_message  # noqa: E402

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN = "00f067aa0ba902b7"
CANARY = "rh-secret-canary-0001"
ENABLED = SimpleNamespace(otel_enabled=True, otel_trace_ui_url_template="http://localhost:16686/trace/{trace_id}")


@pytest.fixture
def exporter():
    memory = InMemorySpanExporter()
    assert tracing.configure(ENABLED, exporter=memory, service_name="rh-test") is True
    yield memory
    tracing.shutdown()


@pytest.fixture
def traced_client(client):
    memory = InMemorySpanExporter()
    assert tracing.configure(ENABLED, exporter=memory, service_name="rh-test") is True
    yield client, memory
    tracing.shutdown()


def _hex(value: int, width: int) -> str:
    return format(value, f"0{width}x")


def test_disabled_settings_keep_tracing_off():
    assert tracing.configure(SimpleNamespace(otel_enabled=False)) is False
    assert tracing.enabled() is False


def test_missing_package_keeps_tracing_off(monkeypatch):
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", None)
    assert tracing.configure(ENABLED) is False
    assert tracing.enabled() is False


def test_exported_ids_match_trace_context(exporter):
    with tracing.start_span("outer") as outer:
        with tracing.start_span("inner", kind=tracing.SPAN_KIND_CLIENT) as inner:
            pass
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert _hex(spans["outer"].context.trace_id, 32) == outer.context.trace_id
    assert _hex(spans["outer"].context.span_id, 16) == outer.context.span_id
    assert _hex(spans["inner"].context.span_id, 16) == inner.context.span_id
    assert spans["inner"].parent.span_id == spans["outer"].context.span_id
    assert spans["outer"].parent is None
    assert spans["inner"].kind == SpanKind.CLIENT


def test_allowlist_drops_unknown_keys_and_non_scalars(exporter):
    from app.observability.metrics import TRACING_DROPPED_ATTRIBUTES_TOTAL

    before = TRACING_DROPPED_ATTRIBUTES_TOTAL._value.get()
    with tracing.start_span("s") as span:
        span.set_attributes({"rh.run_id": "run_1", "prompt": CANARY, "rh.status": {"x": 1}, "rh.sandbox.backend": "simulated", "rh.task_type": "x" * 500})
    attrs = dict(exporter.get_finished_spans()[0].attributes)
    assert attrs["rh.run_id"] == "run_1" and attrs["rh.sandbox.backend"] == "simulated"
    assert "prompt" not in attrs and "rh.status" not in attrs
    assert len(attrs["rh.task_type"]) == tracing.MAX_ATTRIBUTE_STRING
    assert TRACING_DROPPED_ATTRIBUTES_TOTAL._value.get() == before + 2


def test_span_links(exporter):
    trigger = trace_context.TraceContext(trace_id=TRACE, span_id=SPAN)
    with tracing.start_span("background", links=[trigger]):
        pass
    span = exporter.get_finished_spans()[0]
    assert len(span.links) == 1 and _hex(span.links[0].context.span_id, 16) == SPAN
    assert _hex(span.context.trace_id, 32) != TRACE  # a link, not a parent: new trace


def test_exception_marks_span_error(exporter):
    with pytest.raises(RuntimeError):
        with tracing.start_span("boom"):
            raise RuntimeError("x")
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code.name == "ERROR" and span.attributes["error.type"] == "RuntimeError"


def test_http_submit_and_worker_form_one_trace(traced_client, low_risk_payload, session_factory):
    client, memory = traced_client
    payload = dict(low_risk_payload)
    payload["goal"] = f"{payload['goal']} {CANARY}"
    response = client.post("/v1/agent/submit", json=payload, headers={"traceparent": f"00-{TRACE}-{SPAN}-01"})
    assert response.status_code == 200
    request_event = client.event_publisher.events[0]
    headers = [(key, value.encode()) for key, value in request_event["headers"].items()]

    async def consume():
        return await process_message(
            request_event["event"], headers, session_factory=session_factory,
            state_store=client.state_store, event_publisher=client.event_publisher, topic="agent.requests",
        )

    asyncio.run(consume())
    spans = memory.get_finished_spans()
    by_name = {}
    for span in spans:
        by_name.setdefault(span.name, span)
    for name in ("POST /v1/agent/submit", "agent.requests publish", "agent.requests process", "worker.process_run",
                 "executor.execute", "evaluation.evaluate"):
        assert name in by_name, f"missing span {name}: {sorted(by_name)}"
    assert {_hex(span.context.trace_id, 32) for span in spans} == {TRACE}
    server = by_name["POST /v1/agent/submit"]
    assert server.kind == SpanKind.SERVER and _hex(server.parent.span_id, 16) == SPAN
    producer = by_name["agent.requests publish"]
    consumer = by_name["agent.requests process"]
    assert producer.kind == SpanKind.PRODUCER and consumer.kind == SpanKind.CONSUMER
    assert consumer.parent.span_id == producer.context.span_id
    assert by_name["worker.process_run"].parent.span_id == consumer.context.span_id
    assert by_name["worker.process_run"].attributes["rh.status"] == "completed"
    assert response.headers["traceparent"].split("-")[2] == _hex(server.context.span_id, 16)
    for span in spans:  # D6: nothing from the prompt / goal ever reaches a span
        blob = span.name + repr(dict(span.attributes)) + repr([dict(e.attributes or {}) for e in span.events])
        assert CANARY not in blob


def test_status_reports_exporting(exporter, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://user:pw@jaeger:4318")
    status = tracing.status(ENABLED)
    assert status["exporting"] is True and status["exporter_host"] == "jaeger"
    assert "pw" not in repr(status)
