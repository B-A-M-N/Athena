"""The programmable affordance fabric.

The fabric is the common surface through which Athena discovers and invokes
native, generated, project, and external abilities.  It deliberately does not
reason: the AgentKernel chooses an affordance, while the existing dispatcher,
policy engine, and execution manager remain the authority for effects.
"""

from athena.affordances.fabric import CapabilityFabric
from athena.affordances.models import (
    AffordanceScope,
    DependencyRequirement,
    GeneratedCapability,
    ScratchAdapter,
    ScratchAnalyzer,
    ScratchHelper,
    ScratchProgram,
)
from athena.affordances.scratch import ScratchManager
from athena.affordances.store import GeneratedCapabilityStore
from athena.affordances.validation import (
    GeneratedSourceValidator,
    SourceValidation,
    ValidationCheck,
    ValidationTier,
)


def __getattr__(name):
    """Lazy compatibility exports avoid the models/workflows import cycle."""
    if name in {"Workflow", "WorkflowResult", "WorkflowStep", "WorkflowValidator"}:
        from athena.workflows import (
            Workflow,
            WorkflowResult,
            WorkflowStep,
            WorkflowValidator,
        )
        return {
            "Workflow": Workflow, "WorkflowResult": WorkflowResult,
            "WorkflowStep": WorkflowStep, "WorkflowValidator": WorkflowValidator,
        }[name]
    raise AttributeError(name)

__all__ = [
    "AffordanceScope",
    "CapabilityFabric",
    "DependencyRequirement",
    "GeneratedCapability",
    "GeneratedCapabilityStore",
    "GeneratedSourceValidator",
    "ScratchAdapter",
    "ScratchAnalyzer",
    "ScratchHelper",
    "ScratchManager",
    "ScratchProgram",
    "SourceValidation",
    "ValidationCheck",
    "ValidationTier",
    "Workflow",
    "WorkflowResult",
    "WorkflowStep",
    "WorkflowValidator",
]
