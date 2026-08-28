"""ACP integration subsystem (§93 ACP Integration).

ACP is a client interface around AthenaService. ``ACPAdapter`` is a thin
transport/translation layer: it converts inbound ACP requests to
:class:`~athena.protocol.tasks.TaskSpec`, submits them via TaskManager, streams
events/results back in ACP envelopes, and uses Athena's SessionRepository for
all session authority (INV-003). There is no agent loop and no independent
session store here.
"""

from __future__ import annotations

from athena.acp.adapter import (
    ACPAdapter,
    ACPEvent,
    ACPRequest,
    EV_TASK_ACCEPTED,
    EV_TASK_ERROR,
    EV_TASK_FINISHED,
    EV_TASK_MESSAGE,
    EV_TASK_STARTED,
    build_acp_provenance,
)

__all__ = [
    "ACPAdapter",
    "ACPRequest",
    "ACPEvent",
    "EV_TASK_ACCEPTED",
    "EV_TASK_STARTED",
    "EV_TASK_MESSAGE",
    "EV_TASK_FINISHED",
    "EV_TASK_ERROR",
    "build_acp_provenance",
]
