"""Event Streaming over SSE (§96 Event Streaming).

``GET /v1/tasks/{task_id}/events`` returns a Server-Sent-Events stream whose
frames serialize :class:`~athena.protocol.events.Event` objects from
``service.stream_events(task_id)`` into the wire format::

    event: <type>
    data: <json>

    ...

Each frame carries an ``id`` (the event sequence / event id) so that a
reconnecting client may supply ``Last-Event-ID`` and the stream resumes from
that cursor when the event store supports replay (best effort).
"""

from __future__ import annotations

import json
import ipaddress
import logging
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, AsyncIterator, Mapping

__all__ = ["sse_stream", "encode_frame", "EventEncoder"]

logger = logging.getLogger(__name__)


class EventEncoder(json.JSONEncoder):
    """JSON encoder for SSE payloads (dataclasses, enums, datetimes, decimals)."""

    def default(self, o: Any) -> Any:
        import dataclasses

        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, Enum):
            return o.value
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, Mapping):
            return dict(o)
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            return dataclasses.asdict(o)
        if hasattr(o, "__dict__"):
            return dict(o.__dict__)
        return super().default(o)


def encode_frame(event: Any) -> str:
    """Serialize a single :class:`Event` to an SSE frame string.

    The ``data`` block is the JSON-serialized event. We expose ``event`` set
    to the event's ``type`` so clients can dispatch on it.
    """
    payload = _event_to_dict(event)
    event_type = payload.get("type")
    event_id = payload.get("id") or payload.get("sequence")
    lines = []
    if event_type:
        lines.append(f"event: {event_type}")
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append("data: " + json.dumps(payload, cls=EventEncoder))
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def _event_to_dict(event: Any) -> dict[str, Any]:
    """Extract a plain dict from either an Event dataclass or a dict-like."""
    if isinstance(event, Mapping):
        return dict(event)
    fields = (
        "id",
        "type",
        "sequence",
        "timestamp",
        "task_id",
        "session_id",
        "schema_version",
        "payload",
        "causal_id",
    )
    out: dict[str, Any] = {}
    for name in fields:
        value = getattr(event, name, None)
        if value is not None:
            out[name] = value
    if "payload" not in out:
        out["payload"] = {}
    return out


def _open_stream(service: Any, task_id: str, last_event_id: str | None) -> Any:
    """Open ``service.stream_events`` with best-effort replay cursor support.

    ``Last-Event-ID`` is forwarded to the service (INV-007 — we never replay
    events ourselves). AthenaService's ``stream_events`` takes
    ``after_sequence``; other services may expose ``cursor``. We try both and
    finally fall back to the plain call so the handler stays framework-neutral.
    """
    if last_event_id is None or last_event_id in ("", "done"):
        return service.stream_events(task_id)
    try:
        after = int(last_event_id)
    except (TypeError, ValueError):
        after = None
    if after is not None:
        try:
            return service.stream_events(task_id, after_sequence=after)
        except TypeError:
            pass
    try:
        return service.stream_events(task_id, cursor=last_event_id)
    except TypeError:
        return service.stream_events(task_id)


async def sse_stream(
    service: Any,
    task_id: str,
    *,
    last_event_id: str | None = None,
) -> Any:
    """Return a Starlette :class:`StreamingResponse` emitting SSE frames.

    One frame per ``Event`` yielded by ``service.stream_events(task_id)``. When
    reconnection metadata (``Last-Event-ID``) is present it is forwarded to the
    service as a replay cursor so the store can resume from that point (best
    effort). We never replay events ourselves (INV-007); the service owns replay.
    """
    from starlette.responses import StreamingResponse

    async def event_source() -> AsyncIterator[str]:
        generator = _open_stream(service, task_id, last_event_id)
        if not hasattr(generator, "__aiter__"):
            maybe = await generator
            generator = maybe
        async for event in generator:
            yield encode_frame(event)
        yield 'id: done\ndata: {"done": true}\n\n'

    response = StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    return response


def run(
    app: Any = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    server_config: Mapping[str, Any] | None = None,
) -> None:
    """Serve the HTTP API with uvicorn.

    Both ``service``/``app`` resolution and uvicorn itself are lazy so importing
    this module never starts a server. If uvicorn is missing a clear error is
    raised.
    """
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "uvicorn is required to serve the HTTP API. Install Athena's base "
            "dependencies before starting the API."
        ) from exc

    config = dict(server_config or {})
    app = app or _build_default_app()
    selected_host = str(config.get("host", host))
    if not _is_loopback_host(selected_host):
        raise RuntimeError(
            "Athena's beta API is local-only; bind to 127.0.0.1, ::1, or "
            "localhost until an authenticated API boundary is configured"
        )
    uvicorn.run(app, host=selected_host, port=int(config.get("port", port)))


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _build_default_app() -> Any:
    from .app import create_app

    return create_app(None)


if __name__ == "__main__":
    run()
