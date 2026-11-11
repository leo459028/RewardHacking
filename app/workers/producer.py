from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.core import trace_context
from app.schemas.events import BaseEvent


def make_event(event_type: str, run_id: str, payload: dict) -> BaseEvent:
    # v1.6.0 D3: carry the current trace (``trace_<32 hex>`` or the client's legacy id); outside a request or
    # worker span the previous random uuid is kept.
    current = trace_context.current()
    return BaseEvent(
        event_id=str(uuid4()),
        trace_id=current.public_id if current is not None else str(uuid4()),
        run_id=run_id,
        event_type=event_type,
        ts=datetime.now(timezone.utc).isoformat(),
        payload=payload,
    )
