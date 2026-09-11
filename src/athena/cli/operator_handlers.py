"""Handlers for the typed operator command surface.

The handlers deliberately remain thin: they validate only the command-level
contract, call the existing AthenaService facade, and render its durable
projection.  Policy, ownership, mutation, and recovery authority stays in the
service layer.
"""

from __future__ import annotations

import sys
from typing import Any


async def cmd_inference_recoveries(o: Any, service: Any) -> int:
    """Inspect or explicitly resolve uncertain provider attempts."""
    import json

    actions = list(o.args or [])
    if not actions or actions[0] in {"list", "status"}:
        rows = await service.list_provider_outcome_recoveries()
        print(json.dumps(rows, indent=2, default=str, sort_keys=True))
        return 0
    if actions[0] in {"show", "inspect"} and len(actions) >= 2:
        row = await service.get_provider_outcome_recovery(actions[1])
        if row is None:
            print(f"inference attempt not found: {actions[1]}", file=sys.stderr)
            return 1
        print(json.dumps(row, indent=2, default=str, sort_keys=True))
        return 0
    if actions[0] in {"close", "close-liability"} and len(actions) >= 2:
        note = o.recovery_note or " ".join(actions[2:])
        if not note.strip():
            print("close requires --note NOTE", file=sys.stderr)
            return 2
        try:
            row = await service.close_provider_liability(actions[1], note=note)
        except (KeyError, RuntimeError, ValueError) as exc:
            print(f"inference liability closeout failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(row, indent=2, default=str, sort_keys=True))
        return 0
    if actions[0] == "resolve" and len(actions) >= 2:
        raw_resolution = o.recovery_resolution or (actions[2] if len(actions) >= 3 else "")
        resolution = {
            "failed": "confirmed_failed",
            "confirmed-failed": "confirmed_failed",
            "succeeded": "confirmed_succeeded",
            "confirmed-success": "confirmed_succeeded",
            "retry": "retry_authorized",
            "retry-authorized": "retry_authorized",
            "abandon": "abandoned_with_liability",
            "abandoned": "abandoned_with_liability",
            "abandoned-with-liability": "abandoned_with_liability",
        }.get(raw_resolution.strip().lower(), raw_resolution.strip().lower())
        if not resolution:
            print("resolve requires --resolution failed|succeeded|retry|abandon", file=sys.stderr)
            return 2
        note = o.recovery_note or (" ".join(actions[3:]) if len(actions) >= 3 else "")
        if not note.strip():
            print("resolve requires --note NOTE", file=sys.stderr)
            return 2
        try:
            current = await service.get_provider_outcome_recovery(actions[1])
            if current is None:
                print(f"inference attempt not found: {actions[1]}", file=sys.stderr)
                return 1
            consequences = {
                "confirmed_failed": "close the attempt, release its reservation, and fail the task",
                "retry_authorized": "release its reservation and launch one new provider attempt",
                "confirmed_succeeded": "account the supplied cost, release its reservation, and fail the task because the response is unavailable",
                "abandoned_with_liability": "fail the task while retaining provider liability for manual closeout",
            }
            print(
                "resolution plan: "
                + consequences.get(resolution, "validate and apply the disposition")
            )
            row = await service.resolve_provider_outcome(
                actions[1],
                resolution=resolution,
                note=note,
                provider_response_id=o.recovery_provider_response_id,
                actual_cost=o.recovery_actual_cost,
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            print(f"inference recovery failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(row, indent=2, default=str, sort_keys=True))
        return 0
    print(
        "athena inference-recoveries: use list, inspect ATTEMPT_ID, "
        "close ATTEMPT_ID --note NOTE, or resolve ATTEMPT_ID "
        "--resolution failed|succeeded|retry|abandon --note NOTE "
        "[--provider-response-id ID] [--actual-cost COST]",
        file=sys.stderr,
    )
    return 2


async def cmd_artifacts(o: Any, service: Any) -> int:
    import json

    action = (o.args[0] if o.args else "list").casefold()
    if action != "list":
        print("athena artifacts: use list", file=sys.stderr)
        return 2
    rows = await service.operator_artifacts(limit=int(getattr(o, "operator_limit", 50)))
    print(json.dumps(rows, indent=2, default=str, sort_keys=True))
    return 0


async def cmd_candidates(o: Any, service: Any) -> int:
    import json

    action = (o.args[0] if o.args else "list").casefold()
    task_id = getattr(o, "operator_task_id", None)
    if action == "list":
        value = await service.operator_candidates(task_id)
        print(json.dumps(value, indent=2, default=str, sort_keys=True))
        return 0
    if len(o.args) < 2:
        print(f"athena candidates {action}: missing candidate id", file=sys.stderr)
        return 2
    candidate_id = o.args[1]
    if action == "inspect":
        value = await service.operator_candidate_item(candidate_id, task_id)
        if value is None:
            print(f"candidate not found or not visible: {candidate_id}", file=sys.stderr)
            return 1
        print(json.dumps(value, indent=2, default=str, sort_keys=True))
        return 0
    if action == "promote":
        scope = getattr(o, "operator_scope", None)
        if not scope:
            print("athena candidates promote: missing --scope", file=sys.stderr)
            return 2
        value = await service.operator_promote_candidate(
            candidate_id,
            target_scope=scope,
            task_id=task_id,
        )
        print(json.dumps(value, indent=2, default=str, sort_keys=True))
        return 0 if value.get("status") == "promoted" else 1
    if action == "deprecate":
        value = await service.operator_deprecate_candidate(candidate_id, task_id=task_id)
        print(json.dumps(value, indent=2, default=str, sort_keys=True))
        return 0 if value.get("status") == "deprecated" else 1
    print(
        "athena candidates: use list, inspect ID, promote ID --scope SCOPE, or deprecate ID",
        file=sys.stderr,
    )
    return 2


async def cmd_mutations(o: Any, service: Any) -> int:
    import json

    action = (o.args[0] if o.args else "list").casefold()
    if action == "list":
        rows = await service.operator_diff(limit=int(getattr(o, "operator_limit", 25)))
        print(json.dumps(rows, indent=2, default=str, sort_keys=True))
        return 0
    if action == "undo" and len(o.args) >= 2:
        value = await service.undo_mutation(o.args[1])
        print(json.dumps(value, indent=2, default=str, sort_keys=True))
        return 0 if value.get("error") is None else 1
    print("athena mutations: use list or undo MUTATION_ID", file=sys.stderr)
    return 2


async def cmd_permissions(service: Any) -> int:
    import json

    print(json.dumps(await service.operator_permissions(), indent=2, default=str, sort_keys=True))
    return 0


async def cmd_capabilities(service: Any) -> int:
    import json

    registry = getattr(service, "_registry", None)
    rows = registry.list_descriptors() if registry is not None else []
    print(json.dumps(rows, indent=2, default=str, sort_keys=True))
    return 0


async def cmd_context(o: Any, service: Any) -> int:
    import json

    action = (o.args[0] if o.args else "show").casefold()
    if action != "show":
        print("athena context: use show [--session-id SESSION_ID]", file=sys.stderr)
        return 2
    value = await service.operator_context_summary(getattr(o, "operator_session_id", None))
    print(json.dumps(value, indent=2, default=str, sort_keys=True))
    return 0


async def cmd_generated_capabilities(o: Any, service: Any) -> int:
    import json

    action = (o.args[0] if o.args else "list").casefold()
    task_id = getattr(o, "operator_task_id", None)
    if action == "list":
        value = await service.operator_generated_capabilities(task_id)
        print(json.dumps(value, indent=2, default=str, sort_keys=True))
        return 0
    if len(o.args) < 2:
        print(f"athena generated-capabilities {action}: missing capability id", file=sys.stderr)
        return 2
    capability_id = o.args[1]
    if action == "show":
        value = await service.operator_generated_capability(capability_id, task_id)
    elif action == "promote" and len(o.args) >= 3:
        value = await service.operator_promote_generated_capability(
            capability_id, o.args[2], task_id
        )
    elif action == "deprecate":
        value = await service.operator_deprecate_generated_capability(capability_id, task_id)
    else:
        print(
            "athena generated-capabilities: use list, show ID, promote ID SCOPE, or deprecate ID",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(value, indent=2, default=str, sort_keys=True))
    return 0 if value.get("status", "ok") not in {"error", "failed"} else 1
