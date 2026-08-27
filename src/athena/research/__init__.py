"""Durable research and evidence fabric.

Research is an Athena capability/workflow concern, not a second reasoning
loop.  The package stores source versions, evidence objects, and open research
gaps so the AgentKernel can reason from durable, inspectable support instead
of relying on an old transcript.
"""

from athena.research.models import (
    EvidenceObject,
    ResearchGap,
    SourceRecord,
)
from athena.research.policy import SourcePolicy, SourcePolicyError
from athena.research.store import ResearchStore

__all__ = [
    "EvidenceObject",
    "ResearchGap",
    "ResearchStore",
    "SourcePolicy",
    "SourcePolicyError",
    "SourceRecord",
]
