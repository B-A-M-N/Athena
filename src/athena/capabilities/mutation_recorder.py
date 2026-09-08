"""Durable mutation recording — extracted from CapabilityDispatcher (P1-10).

Mechanism, not a second authority. Persisting a mutation receipt, stamping
its durable boundary onto the result, and notifying the observation
callback are bookkeeping the dispatcher already decided to do. Every seam —
the mutation store, the event emitter, the mutation observer — resolves
through the owning :class:`CapabilityDispatcher` instance (``self._d``),
and the dispatcher keeps delegate methods, so behavior, event order, and
instance-attribute patching are unchanged from when these bodies lived on
the dispatcher.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from athena.protocol.capabilities import CapabilityRequest, CapabilityResult
from athena.protocol.events import EV

if TYPE_CHECKING:
    from athena.capabilities.dispatcher import CapabilityDispatcher

__all__ = ["MutationRecorder"]


class MutationRecorder:
    """Mutation bookkeeping mechanism owned by CapabilityDispatcher."""

    def __init__(self, dispatcher: CapabilityDispatcher) -> None:
        self._d = dispatcher

    async def _record_mutation(
        self,
        request: CapabilityRequest,
        result: CapabilityResult,
    ) -> None:
        if self._d._mutation_store is None:
            return
        mutation = (result.metadata or {}).get("mutation")
        if not mutation:
            return
        mutation_id = mutation.get("mutation_id")
        mutation_sequence = None
        if mutation_id and self._d._mutation_store is not None:
            mutation_sequence = await self._d._mutation_sequence_for(
                self._d._mutation_store, mutation_id
            )
        if mutation.get("mutation_id"):
            event = await self._d._emit(
                EV["MUTATION_RECORDED"],
                {
                    "call_id": request.call_id,
                    "capability_id": request.capability_id,
                    "resource": mutation.get("resource"),
                    "operation": mutation.get("operation"),
                    "mutation_id": mutation.get("mutation_id"),
                    "mutation_sequence": mutation_sequence,
                },
                request.task_id,
                causal_id=request.call_id,
            )
            self._d._attach_mutation_boundary(
                result,
                event_sequence=getattr(event, "sequence", None),
                mutation_sequence=mutation_sequence,
            )
            if self._d._mutation_observer is not None:
                await self._d._mutation_observer(
                    request.task_id,
                    mutation.get("resource", ""),
                    mutation.get("mutation_id"),
                    getattr(event, "sequence", None),
                    mutation_sequence,
                )
            return
        try:
            mid = await self._d._mutation_store.record(
                task_id=request.task_id,
                resource=mutation.get("resource", ""),
                operation=mutation.get("operation", ""),
                before_state=mutation.get("before_hash"),
                after_state=mutation.get("after_hash"),
                reversible=bool(mutation.get("reversible", False)),
                before_ref=mutation.get("before_ref"),
                inverse=mutation.get("inverse"),
                metadata={
                    "capability_call_id": request.call_id,
                    "capability_id": request.capability_id,
                },
            )
        except Exception as e:
            await self._d._emit(
                "MUTATION_RECORD_FAILED",
                {
                    "call_id": request.call_id,
                    "capability_id": request.capability_id,
                    "resource": mutation.get("resource"),
                    "operation": mutation.get("operation"),
                    "error": str(e),
                },
                request.task_id,
                causal_id=request.call_id,
            )
            import logging

            logging.getLogger("athena.dispatcher").warning(
                "mutation record failed: %s",
                e,
                exc_info=True,
            )
            raise

        mutation_sequence = await self._d._mutation_sequence_for(self._d._mutation_store, mid)
        event = await self._d._emit(
            EV["MUTATION_RECORDED"],
            {
                "call_id": request.call_id,
                "capability_id": request.capability_id,
                "resource": mutation.get("resource"),
                "operation": mutation.get("operation"),
                "mutation_id": mid,
                "mutation_sequence": mutation_sequence,
            },
            request.task_id,
            causal_id=request.call_id,
        )
        self._d._attach_mutation_boundary(
            result,
            event_sequence=getattr(event, "sequence", None),
            mutation_sequence=mutation_sequence,
        )
        if self._d._mutation_observer is not None:
            await self._d._mutation_observer(
                request.task_id,
                mutation.get("resource", ""),
                mid,
                getattr(event, "sequence", None),
                mutation_sequence,
            )

    @staticmethod
    async def _mutation_sequence_for(store, mutation_id: str) -> int | None:
        """Read a sequence when the configured store supports the extension.

        A few embedders provide a compatible pre-sequence MutationStore. They
        must retain the mutation path without losing the newer world-state
        boundary metadata.
        """
        sequence_for = getattr(store, "sequence_for", None)
        if sequence_for is None:
            return None
        return await sequence_for(mutation_id)

    @staticmethod
    def _attach_mutation_boundary(
        result: CapabilityResult,
        *,
        event_sequence: int | None,
        mutation_sequence: int | None,
    ) -> None:
        """Expose the durable mutation boundary to downstream orchestration."""
        metadata = dict(result.metadata or {})
        metadata["mutation_event_sequence"] = event_sequence
        metadata["mutation_sequence"] = mutation_sequence
        object.__setattr__(result, "metadata", metadata)
