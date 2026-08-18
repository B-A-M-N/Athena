"""HTTP API (§95 HTTP API).

This is a plain **interface** around :class:`~athena.service.service.AthenaService`
(BUILDSPEC §94). It contains no agent loop (INV-001), owns no session store
(INV-003) and is transport-neutral (INV-007): every HTTP request becomes a Task
via ``service.submit`` (BHV-002) and events stream out over SSE.

Starlette is an **optional** dependency. It is imported lazily so this module
can be imported without the ``api`` extra installed; the factory raises a clear
error if Starlette is missing.
"""

from __future__ import annotations

import enum
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Mapping

from athena.api.decoders import DecodeError, decode_model_policy, decode_workspace
from athena.protocol.tasks import AgentRequest, AutonomyLevel

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids forced runtime dep
    from athena.service.service import AthenaService  # noqa: F401

logger = logging.getLogger(__name__)

__all__ = ["create_app", "build_agent_request", "json_response", "HTTPError"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class HTTPError(Exception):
    """An HTTP error with a status code, raised by thin handlers.

    Handlers stay thin and delegate to AthenaService; they translate the
    service-level outcome into an HTTP status here.
    """

    def __init__(self, status: int, code: str, message: str, **data: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.data = data

    def body(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.data}


class _JSONEncoder(json.JSONEncoder):
    """JSON encoder that renders protocol objects (dataclasses, enums, etc.)."""

    def default(self, o: Any) -> Any:
        import dataclasses

        if isinstance(o, enum.Enum):
            return o.value
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, Mapping):
            return dict(o)
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            return dataclasses.asdict(o)
        dict_method = getattr(o, "_asdict", None)
        if callable(dict_method):
            return self.default(dict_method())
        if hasattr(o, "__dict__"):
            return self.default(o.__dict__)
        return super().default(o)


def json_response(data: Any, *, status: int = 200) -> Any:
    """Build a Starlette JSON response without a hard dependency at import time.

    The JSON is serialized here so we control datetime/enum/Decimal rendering
    across framework versions; we hand the framework a pre-encoded string via
    ``Response`` (avoiding JSONResponse's re-serialization which double-encodes
    a string body).
    """
    from starlette.responses import Response

    body = json.dumps(data, cls=_JSONEncoder, ensure_ascii=False, allow_nan=False)
    return Response(
        content=body.encode("utf-8"),
        status_code=status,
        media_type="application/json",
    )


# ------------------------------------------------------------------------- #
# Request -> Task translation
# ------------------------------------------------------------------------- #
def build_agent_request(body: Mapping[str, Any]) -> AgentRequest:
    """Build an :class:`AgentRequest` from an HTTP JSON body.

    Invalid or missing required fields raise :class:`HTTPError` with a 400 so
    handlers stay thin.
    """
    body = dict(body or {})
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPError(400, "validation_error", "field 'prompt' is required and must be a non-empty string")

    autonomy = body.get("autonomy")
    if autonomy is not None:
        if not isinstance(autonomy, str):
            raise HTTPError(400, "validation_error", "field 'autonomy' must be a string")
        try:
            autonomy_value: AutonomyLevel = AutonomyLevel(autonomy)
        except ValueError:
            allowed = ", ".join(a.value for a in AutonomyLevel)
            raise HTTPError(
                400,
                "validation_error",
                f"field 'autonomy' must be one of: {allowed}",
            )
    else:
        autonomy_value = AutonomyLevel.SUPERVISED

    metadata = body.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}

    try:
        workspace = decode_workspace(body.get("workspace"))
        model_policy = decode_model_policy(body.get("model_policy"))
    except DecodeError as exc:
        raise HTTPError(400, "validation_error", str(exc))

    return AgentRequest(
        prompt=prompt,
        session_id=body.get("session_id"),
        autonomy=autonomy_value,
        workspace=workspace,
        model_policy=model_policy,
        metadata=dict(metadata),
    )


# ------------------------------------------------------------------------- #
# Error mapping (service errors -> HTTP status)
# ------------------------------------------------------------------------- #
def _status_for_error(exc: BaseException) -> HTTPError:
    """Map an AthenaService/AthenaError into an HTTPError with a status."""
    from athena.protocol import errors as errs

    code = getattr(exc, "code", "internal_error")
    message = getattr(exc, "message", str(exc))
    data = dict(getattr(exc, "data", {}) or {})

    if isinstance(exc, errs.IllegalStateTransition):
        return HTTPError(409, code, message, **data)
    if isinstance(exc, errs.Cancelled):
        return HTTPError(409, code, message, **data)
    if isinstance(exc, errs.ProviderError):
        return HTTPError(502, code, message, **data)
    if isinstance(exc, errs.ExecutionError):
        return HTTPError(502, code, message, **data)
    if isinstance(exc, errs.CapabilityError):
        return HTTPError(502, code, message, **data)
    if isinstance(exc, errs.PolicyDenied):
        return HTTPError(403, code, message, **data)
    if isinstance(exc, errs.ApprovalExpired):
        return HTTPError(409, code, message, **data)
    if isinstance(exc, errs.RecoveryError):
        return HTTPError(503, code, message, **data)
    if isinstance(exc, errs.RequestCancelled):
        return HTTPError(409, code, message, **data)
    if isinstance(exc, errs.TaskError):
        return HTTPError(409, code, message, **data)

    status = getattr(exc, "http_status", None)
    if status is not None:
        return HTTPError(int(status), code, message, **data)
    return HTTPError(500, code, message, **data)


def _task_not_found(task_id: str) -> HTTPError:
    return HTTPError(404, "task_not_found", f"task {task_id!r} not found")


# ------------------------------------------------------------------------- #
# Handlers
# ------------------------------------------------------------------------- #
def _submit_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        body = await request.json()
        agent_request = build_agent_request(body)
        try:
            task = await service.submit(agent_request, wait=False)
        except HTTPError:
            raise
        except Exception as exc:  # noqa: BLE001 - thin translation boundary
            raise _status_for_error(exc)
        task_id = _get_task_id(task)
        session_id = getattr(task, "session_id", None) or agent_request.session_id
        return json_response(
            {"task_id": task_id, "session_id": session_id, "status": "QUEUED"},
            status=202,
        )

    return handler


def _get_task_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        task_id = request.path_params["task_id"]
        try:
            task = await service.get_task(task_id)
        except Exception as exc:  # noqa: BLE001
            raise _status_for_error(exc)
        if task is None:
            raise _task_not_found(task_id)
        return json_response(
            {
                "task": {
                    "task_id": _get_task_id(task),
                    "status": _get_status(task),
                    "created_at": _iso(getattr(task, "created_at", None)),
                },
                "result": _maybe_result(task),
            }
        )

    return handler


def _get_result_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        task_id = request.path_params["task_id"]
        try:
            result = await service.get_result(task_id)
        except Exception as exc:  # noqa: BLE001
            raise _status_for_error(exc)
        if result is None:
            try:
                task_status = await _task_status(service, task_id)
            except Exception:
                task_status = None
            if task_status is None:
                raise _task_not_found(task_id)
            raise HTTPError(409, "result_not_ready", f"result not ready for task {task_id!r}")
        return json_response({"result": result}, status=200)

    return handler


def _cancel_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        task_id = request.path_params["task_id"]
        try:
            await service.cancel(task_id)
        except Exception as exc:  # noqa: BLE001
            raise _status_for_error(exc)
        return json_response({"task_id": task_id, "status": "cancelled"}, status=200)

    return handler


def _interrupt_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        task_id = request.path_params["task_id"]
        try:
            await service.interrupt(task_id)
        except Exception as exc:  # noqa: BLE001
            raise _status_for_error(exc)
        return json_response({"task_id": task_id, "status": "interrupted"}, status=200)

    return handler


def _approve_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        approval_id = request.path_params["approval_id"]
        body = await request.json()
        granted = bool(body.get("granted", False))
        scope = body.get("scope")
        try:
            await service.approve(approval_id, granted=granted, scope=scope)
        except Exception as exc:  # noqa: BLE001
            raise _status_for_error(exc)
        return json_response({"approval_id": approval_id, "granted": granted}, status=200)

    return handler


def _list_sessions_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        try:
            sessions = await service.list_sessions()
        except Exception as exc:  # noqa: BLE001
            raise _status_for_error(exc)
        return json_response({"sessions": _serializable(sessions)}, status=200)

    return handler


def _resume_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        session_id = request.path_params["session_id"]
        body = await request.json()
        prompt = (body or {}).get("prompt")
        try:
            task = await service.resume(session_id, prompt=prompt)
        except Exception as exc:  # noqa: BLE001
            raise _status_for_error(exc)
        return json_response({"task_id": _get_task_id(task), "session_id": session_id}, status=200)

    return handler


def _health_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        return json_response({"status": "ok"}, status=200)

    return handler


# ------------------------------------------------------------------------ #
# Factory
# ------------------------------------------------------------------------ #
def create_app(service: Any = None) -> Any:
    """Build the Starlette ASGI application bound to ``service``.

    ``service`` must be an :class:`~athena.service.service.AthenaService` (or a
    compatible duck-type). Starlette is imported lazily; if it is not installed
    an import error with a clear message is raised.
    """
    if service is None:
        try:
            # Default discovery: build the in-memory AthenaService the way the
            # task-recommended smoke path does.
            from athena.service.service import AthenaService

            service = AthenaService.in_memory()
        except ImportError as exc:
            raise HTTPError(
                500,
                "service_unavailable",
                "no AthenaService provided and `athena.service.athena_service` is not importable",
            ) from exc

    try:
        from starlette.applications import Starlette
        from starlette.routing import Route
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "Starlette is required to serve the HTTP API. Install it with "
            "`pip install -e '.[api]'` (provides uvicorn + starlette)."
        ) from exc

    routes = [
        Route("/v1/tasks", _submit_handler(service), methods=["POST"]),
        Route("/v1/tasks/{task_id}", _get_task_handler(service), methods=["GET"]),
        Route("/v1/tasks/{task_id}/result", _get_result_handler(service), methods=["GET"]),
        Route("/v1/tasks/{task_id}/cancel", _cancel_handler(service), methods=["POST"]),
        Route("/v1/tasks/{task_id}/interrupt", _interrupt_handler(service), methods=["POST"]),
        Route("/v1/tasks/{task_id}/events", _events_handler(service), methods=["GET"]),
        Route("/v1/approvals/{approval_id}", _approve_handler(service), methods=["POST"]),
        Route("/v1/sessions", _list_sessions_handler(service), methods=["GET"]),
        Route("/v1/sessions/{session_id}/resume", _resume_handler(service), methods=["POST"]),
        Route("/v1/health", _health_handler(service), methods=["GET"]),
    ]

    app = Starlette(routes=routes, lifespan=_lifespan(service))
    _install_exception_handlers(app)
    return app


def _lifespan(service: Any) -> Any:
    """Starlette lifespan that starts/stops the bound AthenaService.

    The default in-memory service built by ``create_app`` is never started
    otherwise (P1-40), so enabling it in the ASGI lifespan is what makes requests
    reach a fully initialised service. The start is a no-op when the caller has
    already started the service (idempotent); absence of the methods is tolerated
    so a minimal duck-type service can still be served.
    """

    from contextlib import asynccontextmanager
    from collections.abc import AsyncIterator

    @asynccontextmanager
    async def _manager() -> AsyncIterator[None]:
        start = getattr(service, "start", None)
        if callable(start):
            await start()
        try:
            yield
        finally:
            stop = getattr(service, "stop", None)
            if callable(stop):
                try:
                    await stop()
                except Exception:  # noqa: BLE001 - shutdown must not raise out
                    logger.exception("error stopping AthenaService during shutdown")

    return _manager


def _install_exception_handlers(app: Any) -> None:
    """Register a JSON error handler for HTTPError and generic exceptions."""
    from starlette.requests import Request

    async def http_error_handler(request: Request, exc: HTTPError) -> Any:
        return json_response(exc.body(), status=exc.status)

    async def generic_error_handler(request: Request, exc: Exception) -> Any:
        logger.exception("unhandled error handling %s %s", request.method, request.url.path)
        return json_response(
            {"code": "internal_error", "message": "internal error"},
            status=500,
        )

    app.add_exception_handler(HTTPError, http_error_handler)
    app.add_exception_handler(Exception, generic_error_handler)


# ------------------------------------------------------------------------- #
# Small helpers
# ------------------------------------------------------------------------- #
def _events_handler(service: Any) -> Any:
    from .sse import sse_stream

    async def handler(request: Any) -> Any:
        task_id = request.path_params["task_id"]
        last_event_id = request.headers.get("last-event-id")
        return await sse_stream(service, task_id, last_event_id=last_event_id)

    return handler


def _get_task_id(task: Any) -> str:
    if task is None:
        return ""
    if isinstance(task, str):
        return task
    for attr in ("task_id", "id"):
        value = getattr(task, attr, None)
        if value:
            return str(value)
    if isinstance(task, Mapping):
        for key in ("task_id", "id"):
            value = task.get(key)
            if value:
                return str(value)
    return str(task)


def _get_status(task: Any) -> Any:
    if task is None:
        return None
    value = getattr(task, "status", None)
    if value is None and isinstance(getattr(task, "metadata", None), Mapping):
        value = getattr(task, "metadata", {}).get("status")
    if value is None and isinstance(task, Mapping):
        value = task.get("status") or (task.get("metadata") or {}).get("status")
    if value is None:
        return None
    return getattr(value, "value", value)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _maybe_result(task: Any) -> Any:
    """Return a result if the task already carries one, else None."""
    return getattr(task, "result", None)


async def _task_status(service: Any, task_id: str) -> Any:
    task = await service.get_task(task_id)
    if task is None:
        return None
    return _get_status(task)


def _serializable(value: Any) -> Any:
    """Best-effort conversion to a JSON-serializable structure."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(k): _serializable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_serializable(v) for v in value]
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    try:
        return _serializable(getattr(value, "__dict__", value))
    except Exception:
        return str(value)