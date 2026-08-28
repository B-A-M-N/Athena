"""HTTP/SSE API interface for Athena.

The HTTP API is a thin, interface-neutral transport (INV-007) over
:class:`~athena.service.service.AthenaService`. HTTP requests become Tasks
(BHV-002) and events stream out over SSE. Importing this package does NOT
require starlette/uvicorn (optional ``api`` extra) — those are lazily imported
by the factory and runner.
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
