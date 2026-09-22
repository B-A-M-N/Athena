"""Provider-prefix cache observation for the inference broker."""

from __future__ import annotations

import logging
from typing import Any

from athena.context.compiler import CompiledContext
from athena.models.router import ModelSelection
from athena.protocol.tasks import TaskSpec

__all__ = ["observe_prefix"]

_logger = logging.getLogger("athena.kernel")


async def observe_prefix(
    broker: Any,
    task: TaskSpec,
    compiled: CompiledContext,
    selection: ModelSelection,
) -> dict[str, Any]:
    """Observe one rendered prefix and emit its durable cache boundary."""
    from athena.models.compat.caching import (
        PrefixTracker,
        PromptEnvelope,
        build_cache_key,
        cache_message_payload,
    )

    kernel = broker._k
    session_key = task.session_id or task.id
    namespace = kernel._trusted_cache_namespace(task)
    metadata = kernel._inference_metadata(selection)
    key = (namespace, session_key, selection.provider)
    profile_id = str(metadata.get("provider_profile_id", selection.provider))
    tracker = kernel._prefix_trackers.setdefault(key, PrefixTracker())
    if tracker.last_prefix_fp is None and kernel._events is not None:
        try:
            latest = getattr(kernel._events, "latest_for_session", None)
            event = (
                await latest(session_key, "InferencePrefixObserved") if callable(latest) else None
            )
            if event is not None and event.payload.get("cache_namespace") in {None, namespace}:
                payload = dict(event.payload or {})
                tracker.last_prefix_fp = payload.get("prefix_fingerprint")
                tracker.last_full_fp = payload.get("full_fingerprint")
                tracker.components_fp = dict(payload.get("components_fp") or {})
            else:
                # Compatibility with older event-store adapters.
                for event in reversed(await kernel._events.list_for_session(session_key)):
                    if event.type != "InferencePrefixObserved":
                        continue
                    payload = dict(event.payload or {})
                    if payload.get("cache_namespace") not in {None, namespace}:
                        continue
                    tracker.last_prefix_fp = payload.get("prefix_fingerprint")
                    tracker.last_full_fp = payload.get("full_fingerprint")
                    tracker.components_fp = dict(payload.get("components_fp") or {})
                    break
        except Exception as exc:  # rationale: cache telemetry restore is best effort
            _logger.debug("prefix tracker restore failed for %s: %s", session_key, exc)

    stable_messages = tuple(getattr(compiled, "cache_prefix_messages", ()) or ())
    stable_payload = [cache_message_payload(message) for message in stable_messages]
    dynamic_messages = compiled.messages[len(stable_messages) :]
    tools_payload = [
        {
            "name": descriptor.id,
            "description": descriptor.description or f"Athena capability {descriptor.id}",
            "parameters": descriptor.input_schema or {"type": "object", "properties": {}},
        }
        for descriptor in compiled.capability_definitions
    ]
    envelope = PromptEnvelope(
        stable_prefix=[stable_payload, tools_payload],
        append_history=[message.id for message in dynamic_messages],
        dynamic_suffix=[cache_message_payload(message) for message in dynamic_messages],
    )
    observed = tracker.observe(
        envelope,
        components={
            "stable_context": stable_payload,
            "tools": tools_payload,
            "model": selection.model,
            "provider_profile": metadata.get("provider_profile_fingerprint", profile_id),
            "compatibility_policy": {
                "profile": metadata.get("compatibility_profile", "auto"),
                "repair": metadata.get("tool_repair_mode", "safe"),
                "correction_cycles": metadata.get("max_tool_correction_cycles", 0),
                "protocol": metadata.get("protocol", "openai-compat"),
            },
        },
    )
    cache_key = build_cache_key(
        namespace=namespace,
        provider=selection.provider,
        model=selection.model,
        profile_fingerprint=str(metadata.get("provider_profile_fingerprint", profile_id)),
        prefix_fingerprint=observed["prefix_fp"],
    )
    output = {
        "prefix_fingerprint": observed["prefix_fp"],
        "full_fingerprint": tracker.last_full_fp,
        "components_fp": dict(tracker.components_fp),
        "boundary": observed.get("boundary"),
        "cache_boundary": observed.get("boundary"),
        "cache_session_key": cache_key,
        "cache_namespace": namespace,
        "cache_prefix_message_count": len(stable_messages),
    }
    await kernel._emit("InferencePrefixObserved", output, task)
    return output
