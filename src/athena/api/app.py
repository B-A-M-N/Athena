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

import base64
import binascii
import enum
import inspect
import json
import logging
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from athena.api.decoders import (
    DecodeError,
    decode_budget,
    decode_capability_policy,
    decode_criteria,
    decode_model_policy,
    decode_mutation_mode,
    decode_workspace,
)
from athena.protocol.tasks import AgentRequest, AutonomyLevel

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids forced runtime dep
    from athena.service.service import AthenaService  # noqa: F401

logger = logging.getLogger(__name__)

__all__ = ["HTTPError", "build_agent_request", "create_app", "json_response"]


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


def _loopback_client(host: object) -> bool:
    """Accept only loopback clients at the application boundary."""
    import ipaddress

    value = str(host or "").casefold().rstrip(".")
    if value == "localhost":
        return True
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        # In-process ASGI test transports often use a symbolic client name.
        # A real socket transport supplies an IP address and is checked below.
        return not value
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(address.is_loopback or (mapped is not None and mapped.is_loopback))


class _LocalOnlyMiddleware:
    """Prevent an accidentally remote ASGI bind from exposing the operator API."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            client = scope.get("client")
            host = client[0] if isinstance(client, (tuple, list)) and client else None
            if host is not None and not _loopback_client(host):
                from starlette.responses import JSONResponse

                response = JSONResponse(
                    {
                        "code": "local_only",
                        "message": "Athena's operator API accepts loopback clients only",
                    },
                    status_code=403,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


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
        raise HTTPError(
            400, "validation_error", "field 'prompt' is required and must be a non-empty string"
        )

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

    task_id = body.get("task_id")
    if task_id is not None and not isinstance(task_id, str):
        raise HTTPError(400, "validation_error", "field 'task_id' must be a string or null")

    requested = body.get("requested_capabilities")
    if requested is not None and (
        not isinstance(requested, (list, tuple))
        or not all(isinstance(item, str) and item.strip() for item in requested)
    ):
        raise HTTPError(
            400,
            "validation_error",
            "field 'requested_capabilities' must be an array of non-empty strings",
        )

    raw_attachments = body.get("attachments", body.get("context_refs", ()))
    if raw_attachments is None:
        raw_attachments = ()
    if not isinstance(raw_attachments, (list, tuple)):
        raise HTTPError(400, "validation_error", "attachments must be an array")
    attachments: list[Any] = []
    for item in raw_attachments:
        if isinstance(item, str):
            attachments.append({"kind": "file", "ref": item})
        elif isinstance(item, Mapping):
            if not item.get("ref") and not item.get("uri"):
                raise HTTPError(400, "validation_error", "each attachment needs ref or uri")
            attachments.append(dict(item))
        else:
            raise HTTPError(400, "validation_error", "each attachment must be an object or string")

    try:
        workspace = decode_workspace(body.get("workspace"))
        capability_policy = (
            None
            if body.get("capability_policy") is None
            else decode_capability_policy(body.get("capability_policy"))
        )
        resource_budget = decode_budget(body.get("resource_budget"))
        model_policy = decode_model_policy(body.get("model_policy"))
        mutation_mode = decode_mutation_mode(body.get("mutation_mode"))
        acceptance_criteria = decode_criteria(body.get("acceptance_criteria"))
    except DecodeError as exc:
        raise HTTPError(400, "validation_error", str(exc))

    deadline = body.get("deadline")
    if deadline is not None:
        if not isinstance(deadline, str):
            raise HTTPError(
                400, "validation_error", "field 'deadline' must be an ISO string or null"
            )
        try:
            deadline = datetime.fromisoformat(deadline)
        except ValueError as exc:
            raise HTTPError(
                400, "validation_error", "field 'deadline' must be an ISO datetime"
            ) from exc

    return AgentRequest(
        prompt=prompt,
        session_id=body.get("session_id"),
        task_id=task_id,
        autonomy=autonomy_value,
        workspace=workspace,
        model_policy=model_policy,
        capability_policy=capability_policy,
        resource_budget=resource_budget,
        deadline=deadline,
        mutation_mode=mutation_mode,
        acceptance_criteria=acceptance_criteria,
        attachments=tuple(attachments),
        requested_capabilities=(frozenset(requested) if requested is not None else None),
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
    if isinstance(exc, errs.ServiceNotReady):
        return HTTPError(503, code, message, **data)
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


async def _read_voice_audio(
    request: Any, service: Any
) -> tuple[bytes, str, str, Mapping[str, Any]]:
    """Read bounded raw-audio or JSON/base64 voice input."""
    config = getattr(service, "config", None)
    voice_config = getattr(config, "voice", None)
    max_bytes = int(getattr(voice_config, "max_input_bytes", 25 * 1024 * 1024))
    content_type = str(request.headers.get("content-type", "audio/wav"))
    media_type = content_type.split(";", 1)[0].strip().lower()
    wire_limit = (
        (max_bytes * 4 + 2) // 3 + 16 * 1024 if media_type == "application/json" else max_bytes
    )
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            # JSON/base64 adds roughly one third over the decoded audio. The
            # post-decode check below remains authoritative.
            if int(content_length) > wire_limit:
                from athena.protocol.errors import VoiceInputError

                raise VoiceInputError("voice input exceeds the configured size limit")
        except ValueError:
            pass
    metadata: Mapping[str, Any] = {}
    if media_type == "application/json":
        raw_body = await _read_request_bytes(request, wire_limit)
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            from athena.protocol.errors import VoiceInputError

            raise VoiceInputError("voice JSON body is invalid", cause=exc) from exc
        if not isinstance(body, Mapping):
            from athena.protocol.errors import VoiceInputError

            raise VoiceInputError("voice JSON body must be an object")
        encoded = body.get("audio_base64")
        if not isinstance(encoded, str) or not encoded:
            from athena.protocol.errors import VoiceInputError

            raise VoiceInputError("voice JSON requires a non-empty audio_base64 field")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            from athena.protocol.errors import VoiceInputError

            raise VoiceInputError("audio_base64 is not valid base64", cause=exc) from exc
        mime_type = str(body.get("mime_type") or "audio/wav")
        filename = str(body.get("filename") or "voice-input")
        metadata = body
    else:
        data = await _read_request_bytes(request, max_bytes)
        mime_type = media_type or "audio/wav"
        filename = str(request.headers.get("x-filename") or "voice-input")
    if len(data) > max_bytes:
        from athena.protocol.errors import VoiceInputError

        raise VoiceInputError("voice input exceeds the configured size limit")
    return data, mime_type, filename, metadata


async def _read_request_bytes(request: Any, limit: int) -> bytes:
    """Read a request body incrementally with a hard wire-size ceiling."""
    if limit <= 0:
        raise ValueError("request body limit must be positive")
    stream = getattr(request, "stream", None)
    if callable(stream):
        chunks: list[bytes] = []
        size = 0
        async for chunk in stream():
            data = bytes(chunk)
            size += len(data)
            if size > limit:
                from athena.protocol.errors import VoiceInputError

                raise VoiceInputError("voice input exceeds the configured size limit")
            chunks.append(data)
        return b"".join(chunks)
    body = await request.body()
    if len(body) > limit:
        from athena.protocol.errors import VoiceInputError

        raise VoiceInputError("voice input exceeds the configured size limit")
    return body


async def _voice_audio_response(service: Any, synthesis: Any) -> Any:
    from starlette.responses import Response

    artifacts = getattr(service, "_artifacts", None)
    if artifacts is None:
        from athena.protocol.errors import VoiceUnavailable

        raise VoiceUnavailable("artifact store is unavailable for voice output")
    data = await artifacts.load(synthesis.artifact)
    fmt = str(getattr(synthesis, "response_format", "mp3"))
    mime = getattr(synthesis.artifact, "mime_type", None) or "audio/mpeg"
    return Response(
        content=data,
        media_type=mime,
        headers={
            "X-Athena-Artifact-URI": str(synthesis.artifact.uri),
            "Content-Disposition": f'inline; filename="athena-voice.{fmt}"',
        },
    )


def _voice_health_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        del request
        health = service.voice_health()
        status = 200 if health.get("state") in {"ready", "disabled"} else 503
        return json_response({"voice": health}, status=status)

    return handler


def _voice_transcribe_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        try:
            data, mime_type, filename, metadata = await _read_voice_audio(request, service)
            transcript = await service.transcribe_voice(
                data,
                mime_type=mime_type,
                filename=filename,
                language=(str(metadata["language"]) if metadata.get("language") else None),
            )
            return json_response({"transcript": transcript.to_dict()})
        except HTTPError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport error translation boundary
            raise _status_for_error(exc)

    return handler


def _voice_turn_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        try:
            data, mime_type, filename, metadata = await _read_voice_audio(request, service)
            transcript = await service.transcribe_voice(
                data,
                mime_type=mime_type,
                filename=filename,
                language=(str(metadata["language"]) if metadata.get("language") else None),
            )
            session_id = metadata.get("session_id")
            task_id = metadata.get("task_id")
            if session_id is not None and not isinstance(session_id, str):
                raise HTTPError(400, "validation_error", "session_id must be a string or null")
            if task_id is not None and not isinstance(task_id, str):
                raise HTTPError(400, "validation_error", "task_id must be a string or null")
            autonomy = AutonomyLevel.SUPERVISED
            if metadata.get("autonomy") is not None:
                try:
                    autonomy = AutonomyLevel(str(metadata["autonomy"]))
                except ValueError as exc:
                    raise HTTPError(400, "validation_error", "invalid autonomy") from exc
            voice_attachment: Any = {
                "kind": "voice",
                "ref": transcript.artifact.uri,
                "source_id": transcript.artifact.id,
                "summary": "immutable audio captured for this voice turn",
            }
            request_envelope = AgentRequest(
                prompt=transcript.text,
                session_id=session_id,
                task_id=task_id,
                autonomy=autonomy,
                # Keep the captured audio as provenance without making the
                # reasoning model consume it a second time after transcription.
                attachments=(voice_attachment,),
                metadata={
                    "voice_input_artifact": transcript.artifact.uri,
                    "voice_transcription_provider": transcript.provider,
                    "voice_transcription_model": transcript.model,
                },
            )
            task = await service.submit(request_envelope, wait=False)
            task_id_value = _get_task_id(task)
            return json_response(
                {
                    "task_id": task_id_value,
                    "session_id": getattr(task, "session_id", None) or session_id,
                    "status": "QUEUED",
                    "transcript": transcript.to_dict(),
                    "voice_result_endpoint": f"/v1/tasks/{task_id_value}/voice",
                },
                status=202,
            )
        except HTTPError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport error translation boundary
            raise _status_for_error(exc)

    return handler


def _voice_synthesize_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        try:
            body = await request.json()
            if not isinstance(body, Mapping) or not isinstance(body.get("text"), str):
                raise HTTPError(400, "validation_error", "field 'text' is required")
            synthesis = await service.synthesize_voice(
                body["text"],
                voice=(str(body["voice"]) if body.get("voice") else None),
                response_format=(
                    str(body["response_format"]) if body.get("response_format") else None
                ),
            )
            return await _voice_audio_response(service, synthesis)
        except HTTPError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport error translation boundary
            raise _status_for_error(exc)

    return handler


def _task_voice_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        try:
            body: Mapping[str, Any] = {}
            if request.headers.get("content-length", "0") != "0":
                try:
                    decoded = await request.json()
                    if isinstance(decoded, Mapping):
                        body = decoded
                except Exception:
                    body = {}
            synthesis = await service.synthesize_task_result(
                request.path_params["task_id"],
                voice=(str(body["voice"]) if body.get("voice") else None),
                response_format=(
                    str(body["response_format"]) if body.get("response_format") else None
                ),
            )
            return await _voice_audio_response(service, synthesis)
        except HTTPError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport error translation boundary
            raise _status_for_error(exc)

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
            except Exception:  # noqa: BLE001 - status polling is best effort
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


def _steer_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        task_id = request.path_params["task_id"]
        body = await request.json()
        if not isinstance(body, Mapping) or not isinstance(body.get("text"), str):
            raise HTTPError(400, "validation_error", "field 'text' must be a string")
        source_task_id = body.get("source_task_id")
        if source_task_id is not None and not isinstance(source_task_id, str):
            raise HTTPError(400, "validation_error", "field 'source_task_id' must be a string")
        try:
            record = await service.steer_task(
                task_id,
                body["text"],
                source_task_id=source_task_id,
            )
        except ValueError as exc:
            raise HTTPError(400, "validation_error", str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - thin translation boundary
            raise _status_for_error(exc)
        return json_response({"steering": _serializable(record)}, status=202)

    return handler


def _approve_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        approval_id = request.path_params["approval_id"]
        body = await request.json()
        # P1-57: approval is security-sensitive. Require an actual JSON
        # boolean; string "false" must never coerce to True.
        granted = body.get("granted")
        if not isinstance(granted, bool):
            return json_response({"error": "'granted' must be a JSON boolean"}, status=400)
        scope = body.get("scope")
        if scope is not None and not isinstance(scope, str):
            return json_response({"error": "'scope' must be a string or null"}, status=400)
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


def _input_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        task_id = request.path_params["task_id"]
        body = await request.json()
        if not isinstance(body, Mapping) or "input" not in body:
            raise HTTPError(400, "validation_error", "field 'input' is required")
        value = body["input"]
        try:
            provider = getattr(service, "provide_input", None)
            if callable(provider):
                outcome = await provider(task_id, value)
            else:
                task = await service.get_task(task_id)
                if task is None:
                    raise KeyError(task_id)
                outcome = await service.resume(getattr(task, "session_id", None), prompt=str(value))
        except KeyError:
            raise _task_not_found(task_id)
        except Exception as exc:  # noqa: BLE001
            raise _status_for_error(exc)
        return json_response({"task_id": _get_task_id(outcome), "accepted": True})

    return handler


def _get_session_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        session_id = request.path_params["session_id"]
        try:
            sessions = await service.list_sessions()
        except Exception as exc:  # noqa: BLE001
            raise _status_for_error(exc)
        match = next(
            (
                item
                for item in sessions
                if str(item.get("id") if isinstance(item, Mapping) else getattr(item, "id", ""))
                == session_id
            ),
            None,
        )
        if match is None:
            raise HTTPError(404, "session_not_found", f"session {session_id!r} not found")
        return json_response({"session": _serializable(match)})

    return handler


def _close_session_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        session_id = request.path_params["session_id"]
        try:
            closed = await service.close_session(session_id)
        except Exception as exc:  # noqa: BLE001
            raise _status_for_error(exc)
        if not closed:
            raise HTTPError(404, "session_not_found", f"session {session_id!r} not found")
        return json_response({"session_id": session_id, "closed": True}, status=200)

    return handler


def _models_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        registry = getattr(service, "_model_registry", None)
        if registry is None:
            return json_response({"models": []})
        try:
            models = await registry.list_models()
        except Exception as exc:  # noqa: BLE001
            raise _status_for_error(exc)
        return json_response({"models": _serializable(models)})

    return handler


def _capabilities_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        registry = getattr(service, "_registry", None)
        try:
            descriptors = registry.list_descriptors() if registry is not None else []
        except Exception as exc:  # noqa: BLE001
            raise _status_for_error(exc)
        return json_response({"capabilities": _serializable(descriptors)})

    return handler


def _health_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        started = bool(getattr(service, "_started", False))
        database_ok = getattr(service, "_db", None) is not None
        database_error = None
        db = getattr(service, "_db", None)
        if database_ok and db is not None:
            try:
                await db.fetch_one("SELECT 1")
            except Exception as exc:  # noqa: BLE001 - readiness must expose DB failure
                database_ok = False
                database_error = str(exc)
        worker = getattr(service, "_worker", None)
        worker_health = worker.health() if worker is not None and hasattr(worker, "health") else {}
        refresh_profile = getattr(service, "refresh_capability_profile", None)
        if callable(refresh_profile):
            try:
                refreshed = refresh_profile()
                if inspect.isawaitable(refreshed):
                    await refreshed
            except Exception:
                # runtime_health below remains the diagnostic source; the
                # readiness predicate will fail closed on the resulting state.
                pass
        runtime_health = service.runtime_health() if hasattr(service, "runtime_health") else {}
        scheduler = getattr(service, "_scheduler", None)
        scheduler_health = runtime_health.get("scheduler") or (
            scheduler.health() if scheduler is not None and hasattr(scheduler, "health") else {}
        )
        scheduler_running = scheduler is not None and bool(
            getattr(scheduler, "is_running", lambda: False)()
        )
        scheduler_state = str(scheduler_health.get("health") or "")
        startup = service.startup_health() if hasattr(service, "startup_health") else None
        capability_profile = runtime_health.get("capability_profile") or {}
        resources = runtime_health.get("resources") or {}
        execution_recovery = runtime_health.get("execution_recovery") or {}
        execution_recovery_state = execution_recovery.get(
            "state", getattr(service, "_recovery_status", "healthy")
        )
        # Optional startup integrations may be degraded while the core
        # service remains ready. Required profile/resource failures are live
        # contract failures and therefore gate new work and readiness.
        startup_ok = startup is None or startup.get("status") in {"ok", "degraded"}
        checks = {
            "service": started,
            "database": database_ok,
            "worker": (
                getattr(service, "_worker_task", None) is not None
                and not getattr(service._worker_task, "done", lambda: True)()
            ),
            "scheduler": (
                scheduler_running
                and (not scheduler_health or scheduler_state in {"healthy", "recovering"})
            ),
            "providers": _providers_ready(getattr(service, "_model_registry", None)),
            "worker_persistence": worker_health.get("status", "ok") == "ok",
            "recovery": getattr(service, "_recovery_status", "healthy") in {"healthy", "recovered"},
            "capability_profile": capability_profile.get("status", "ok") == "ok",
            "resource_teardown": resources.get("unresolved_count", 0) == 0,
            "execution_recovery": execution_recovery_state in {"healthy", "recovered"},
        }
        if startup is not None:
            checks["startup"] = startup_ok
        ready = all(checks.values())
        details = {
            "checks": checks,
            "worker": worker_health,
            "scheduler": scheduler_health,
            "subsystems": runtime_health,
            "provider_readiness": _provider_readiness(getattr(service, "_model_registry", None)),
        }
        if startup is not None:
            details["startup"] = startup
        if database_error is not None:
            details["database_error"] = database_error
        return json_response(
            {"status": "ok" if ready else "degraded", **details},
            status=200 if ready else 503,
        )

    return handler


def _providers_ready(registry: Any) -> bool:
    """Return provider readiness without relying on object truthiness."""
    if registry is None:
        return False
    readiness = getattr(registry, "readiness", None)
    if callable(readiness):
        try:
            return readiness().get("state") == "ready"
        except Exception:
            return False
    names = getattr(registry, "names", None)
    if callable(names):
        return bool(names())
    # Preserve compatibility with small transport-test doubles that expose a
    # truthy registry object but no ProviderRegistry.names() method.
    return bool(registry)


def _provider_readiness(registry: Any) -> dict[str, Any]:
    if registry is None:
        return {"state": "unconfigured", "providers": {}}
    readiness = getattr(registry, "readiness", None)
    if callable(readiness):
        try:
            value = readiness()
            return dict(value) if isinstance(value, dict) else {"state": "unverified"}
        except Exception as exc:
            return {"state": "degraded", "error": str(exc), "providers": {}}
    names = getattr(registry, "names", None)
    return {
        "state": "configured" if callable(names) and names() else "unconfigured",
        "providers": {},
    }


def _live_handler(service: Any) -> Any:
    async def handler(request: Any) -> Any:
        return json_response({"status": "alive"}, status=200)

    return handler


def _ready_handler(service: Any) -> Any:
    return _health_handler(service)


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
            "Starlette is required to serve the HTTP API. Install Athena's "
            "base dependencies before starting the API."
        ) from exc

    routes = [
        Route("/v1/tasks", _submit_handler(service), methods=["POST"]),
        Route("/v1/tasks/{task_id}", _get_task_handler(service), methods=["GET"]),
        Route("/v1/tasks/{task_id}/result", _get_result_handler(service), methods=["GET"]),
        Route("/v1/tasks/{task_id}/voice", _task_voice_handler(service), methods=["POST"]),
        Route("/v1/tasks/{task_id}/cancel", _cancel_handler(service), methods=["POST"]),
        Route("/v1/tasks/{task_id}/interrupt", _interrupt_handler(service), methods=["POST"]),
        Route("/v1/tasks/{task_id}/steer", _steer_handler(service), methods=["POST"]),
        Route("/v1/tasks/{task_id}/events", _events_handler(service), methods=["GET"]),
        Route("/v1/tasks/{task_id}/input", _input_handler(service), methods=["POST"]),
        Route("/v1/approvals/{approval_id}", _approve_handler(service), methods=["POST"]),
        Route("/v1/sessions", _list_sessions_handler(service), methods=["GET"]),
        Route("/v1/sessions/{session_id}", _get_session_handler(service), methods=["GET"]),
        Route("/v1/sessions/{session_id}", _close_session_handler(service), methods=["DELETE"]),
        Route("/v1/sessions/{session_id}/resume", _resume_handler(service), methods=["POST"]),
        Route("/v1/models", _models_handler(service), methods=["GET"]),
        Route("/v1/voice", _voice_health_handler(service), methods=["GET"]),
        Route("/v1/voice/transcribe", _voice_transcribe_handler(service), methods=["POST"]),
        Route("/v1/voice/turn", _voice_turn_handler(service), methods=["POST"]),
        Route("/v1/voice/synthesize", _voice_synthesize_handler(service), methods=["POST"]),
        Route("/v1/capabilities", _capabilities_handler(service), methods=["GET"]),
        Route("/v1/health", _health_handler(service), methods=["GET"]),
        Route("/v1/live", _live_handler(service), methods=["GET"]),
        Route("/v1/ready", _ready_handler(service), methods=["GET"]),
    ]

    app = Starlette(routes=routes, lifespan=_lifespan(service))
    _install_exception_handlers(app)
    # Keep local-only policy in the app itself as well as in the canonical
    # uvicorn runner. Embedders that preserve the socket client address cannot
    # accidentally turn an unauthenticated operator surface into a network API.
    return _LocalOnlyMiddleware(app)


def _lifespan(service: Any) -> Any:
    """Starlette lifespan that starts/stops the bound AthenaService.

    The default in-memory service built by ``create_app`` is never started
    otherwise (P1-40), so enabling it in the ASGI lifespan is what makes requests
    reach a fully initialised service. The start is a no-op when the caller has
    already started the service (idempotent); absence of the methods is tolerated
    so a minimal duck-type service can still be served.
    """

    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _manager(_app: Any = None) -> AsyncIterator[None]:
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
                except Exception:
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
    record_method = getattr(value, "to_record", None)
    if callable(record_method):
        return _serializable(record_method())
    try:
        return _serializable(getattr(value, "__dict__", value))
    except Exception:  # noqa: BLE001 - serialization fallback must not mask output
        return str(value)
