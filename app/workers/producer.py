from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.schemas.events import BaseEvent


def make_event(event_type: str, run_id: str, payload: dict) -> BaseEvent:
    return BaseEvent(
        event_id=str(uuid4()),
        trace_id=str(uuid4()),
        run_id=run_id,
        event_type=event_type,
        ts=datetime.now(timezone.utc).isoformat(),
        payload=payload,
    )
