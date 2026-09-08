"""JSON -> typed protocol decoders shared across boundary transports (INV-007).

The HTTP API and the ACP adapter both translate JSON-shaped request fragments
into :mod:`athena.protocol.tasks` objects. They MUST agree on decoding so a
client's intent is interpreted identically regardless of transport. Decoding
never broadens a provided limit: every field that is supplied is honoured, and
missing optional fields fall back to the protocol default (never "unlimited"
beyond the documented default).
"""

from __future__ import annotations

from typing import Any

from athena.protocol.task_codec import (
    TaskCodecError,
    decode_budget as _decode_budget,
    decode_capability_policy as _decode_capability_policy,
    decode_criteria,
    decode_model_policy as _decode_model_policy,
    decode_mutation_mode,
    decode_workspace as _decode_workspace,
)
from athena.protocol.tasks import (
    CapabilityPolicy,
    ModelPolicy,
    NetworkPolicy,
    ResourceBudget,
    WorkspaceSpec,
)

__all__ = [
    "DecodeError",
    "decode_workspace",
    "decode_capability_policy",
    "decode_criteria",
    "decode_model_policy",
    "decode_mutation_mode",
    "decode_budget",
]


DecodeError = TaskCodecError


def decode_workspace(
    raw: Any,
    *,
    id_fallback: str = "",
    network_default: NetworkPolicy = NetworkPolicy.ALLOW,
) -> WorkspaceSpec | None:
    """Decode a workspace fragment into a :class:`WorkspaceSpec`.

    ``root`` is required whenever a workspace object is supplied. Missing or
    empty ``root`` raises :class:`DecodeError` (a partial workspace is never
    silently widened).
    """
    return _decode_workspace(
        raw,
        id_fallback=id_fallback,
        network_default=network_default,
    )


def decode_capability_policy(raw: Any) -> CapabilityPolicy:
    """Decode a capability policy using the shared empty-list semantics.

    Empty ``allow`` and ``ask`` fields do not mean deny-all; with no deny
    entries they mean unrestricted. Callers that need a safe deny-all default
    must supply ``deny=("*",)`` explicitly.
    """
    return _decode_capability_policy(raw)


def decode_model_policy(raw: Any) -> ModelPolicy:
    """Decode a model policy fragment, always honoring supplied fields."""
    return _decode_model_policy(raw)


def decode_budget(raw: Any) -> ResourceBudget:
    """Decode a full :class:`ResourceBudget`, mapping EVERY field.

    A client's supplied limits are never dropped: every present field maps onto
    the corresponding budget attribute. Fields that the protocol models as
    optional (``None`` default) stay optional; scalar fields use the documented
    default (never "unlimited") when absent.
    """
    return _decode_budget(raw)
