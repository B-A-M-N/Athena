"""HTTP/SSE API interface for Athena.

The HTTP API is a thin, interface-neutral transport (INV-007) over
:class:`~athena.service.service.AthenaService`. HTTP requests become Tasks
(BHV-002) and events stream out over SSE. Starlette and Uvicorn are core
Athena dependencies; they are imported lazily so importing this package does
not start a server.
"""

from __future__ import annotations

from .app import HTTPError, build_agent_request, create_app, json_response
from .sse import EventEncoder, encode_frame, run, sse_stream

__all__ = [
    "create_app",
    "run",
    "build_agent_request",
    "json_response",
    "HTTPError",
    "sse_stream",
    "encode_frame",
    "EventEncoder",
]
