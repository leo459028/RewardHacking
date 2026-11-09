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


# ---------------------------------------------------------------------------
# P2b-2: GenAI judge span (D7), background span link (D4), exemplars (D8)
# ---------------------------------------------------------------------------


def test_judge_call_emits_genai_client_span(exporter):
    import json

    import httpx

    from app.services.evaluation.judges.base import JudgeInput
    from app.services.evaluation.judges.openai_compatible import OpenAICompatibleJudge

    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({"suspicion": 0.7, "categories": ["grader_tampering"], "rationale": CANARY})}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        })

    judge = OpenAICompatibleJudge(base_url="http://localhost:1234/v1", model="tiny-judge", transport=httpx.MockTransport(handler))
    output = asyncio.run(judge.assess(JudgeInput(diff=f"+x = '{CANARY}'\n", final_message=CANARY, findings=[])))
    assert output.status == "ok"
    span = next(s for s in exporter.get_finished_spans() if s.name == "chat tiny-judge")
    attrs = dict(span.attributes)
    assert span.kind == SpanKind.CLIENT
    assert attrs["gen_ai.operation.name"] == "chat" and attrs["gen_ai.request.model"] == "tiny-judge"
    assert attrs["gen_ai.provider.name"] == "openai_compatible"
    assert attrs["gen_ai.usage.input_tokens"] == 50 and attrs["gen_ai.usage.output_tokens"] == 10
    assert attrs["rh.judge.status"] == "ok"
    assert CANARY not in repr(attrs)


def test_failed_judge_call_marks_span_error(exporter):
    import httpx

    from app.services.evaluation.judges.base import JudgeInput
    from app.services.evaluation.judges.openai_compatible import OpenAICompatibleJudge

    judge = OpenAICompatibleJudge(
        base_url="http://localhost:1234/v1", model="tiny-judge", transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    output = asyncio.run(judge.assess(JudgeInput(diff="+x\n", final_message="", findings=[])))
    span = next(s for s in exporter.get_finished_spans() if s.name == "chat tiny-judge")
    assert output.status == "error" and span.status.status_code.name == "ERROR"


def test_background_triage_span_is_linked_not_parented(exporter, monkeypatch):
    from app.services import judge_background

    async def fake_inner(*args, **kwargs):
        return None

    monkeypatch.setattr(judge_background, "_run_background_inner", fake_inner)
    trigger = trace_context.TraceContext(trace_id=TRACE, span_id=SPAN)
    asyncio.run(judge_background._run_background("review_x", "local-small", SimpleNamespace(), lambda: None, trigger))
    span = next(s for s in exporter.get_finished_spans() if s.name == "judge.triage.background")
    assert span.parent is None
    assert _hex(span.context.trace_id, 32) != TRACE
    assert [_hex(link.context.span_id, 16) for link in span.links] == [SPAN]
    assert span.attributes["rh.review_id"] == "review_x"


def test_exemplar_only_while_exporting():
    ctx = trace_context.TraceContext(trace_id=TRACE, span_id=SPAN)
    tracing.shutdown()
    assert tracing.exemplar(ctx) is None
    memory = InMemorySpanExporter()
    tracing.configure(ENABLED, exporter=memory)
    try:
        assert tracing.exemplar(ctx) == {"trace_id": TRACE}
        assert tracing.exemplar(None) is None
    finally:
        tracing.shutdown()


def test_metrics_openmetrics_has_trace_exemplar(traced_client):
    client, _ = traced_client
    client.get("/v1/healthz", headers={"traceparent": f"00-{TRACE}-{SPAN}-01"})
    plain = client.get("/v1/metrics")
    assert plain.status_code == 200 and plain.headers["content-type"].startswith("text/plain")
    open_metrics = client.get("/v1/metrics", headers={"Accept": "application/openmetrics-text; version=1.0.0"})
    assert open_metrics.status_code == 200
    assert open_metrics.headers["content-type"].startswith("application/openmetrics-text")
    assert f'trace_id="{TRACE}"' in open_metrics.text
