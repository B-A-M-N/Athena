"""The kernel-owned Interpreter Reasoning Extension (audit P0.2).

ONE reasoning authority. This extension is not an agent, has no provider
handle, no subprocess, and no store. It:

1. receives execution observations from the computational body,
2. asks ONE bounded interpretive question of the SAME model the primary
   loop uses, through the kernel's single inference broker,
3. converts the answer into an ``InterpreterProposal`` (a capability id +
   arguments — no authority of its own),
4. hands the proposal to the kernel's normal dispatch path: repair →
   policy → approval → execution → durable evidence.

Why not just let the primary loop see runtime output? Because the body
can produce volumes (screen states, trace tails) that would bloat the
primary context; the extension condenses observation → proposal through
a metered, cancellable, role-tagged subturn instead, and every subturn is
visible in `athena inspect` as its own inference row.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from athena.interpreter.context import InterpreterContext
from athena.interpreter.protocol import (
    InterpreterObservation,
    InterpreterProposal,
)

__all__ = ["InterpreterExtension"]

_logger = logging.getLogger("athena.interpreter")

# Hard ceiling on observation payload size handed to the model — larger
# observations must already be artifactized by the producer.
_MAX_OBSERVATION_CHARS = 20_000


class InterpreterExtension:
    """Kernel-side fusion hook. Constructed and owned by the AgentKernel."""

    def __init__(
        self,
        *,
        inference_broker: Callable[..., Awaitable[Any]],
        max_subturns_per_observation: int = 1,
    ) -> None:
        # The broker is kernel._invoke (or a test double). Signature:
        #   await broker(task, state, compiled, *, role="interpreter",
        #                system_prompt=..., user_prompt=...) -> ModelResponse
        # The kernel owns model selection; the extension never selects.
        self._broker = inference_broker
        self._max_subturns = max_subturns_per_observation

    @property
    def max_subturns_per_observation(self) -> int:
        return self._max_subturns

    async def interpret(
        self, observation: InterpreterObservation, context: InterpreterContext
    ) -> InterpreterProposal | None:
        """Condense one observation into at most one capability proposal.

        Returns None when the observation does not warrant a subturn (e.g.
        truncated payload, cancellation already requested, or the model
        proposes nothing). Never raises past the broker contract: a broker
        failure is logged and yields None (no proposal) rather than killing
        the primary loop.
        """
        if context.cancel_requested():
            return None
        if not observation.kind:
            return None
        payload_text = self._render_observation(observation)
        if payload_text is None:
            return None

        system_prompt = (
            "You are the interpreter component of the Athena agent kernel. "
            "You translate execution observations into capability proposals. "
            "Respond with a single JSON object with keys "
            '"capability_id", "arguments", "rationale". '
            "Respond with an empty object {} when the observation warrants "
            "no further action."
        )
        user_prompt = (
            f"Observation kind: {observation.kind}\n"
            f"Task: {context.task_id}\n"
            "Observation payload (JSON):\n"
            f"{payload_text}\n\n"
            "Propose the single most useful next capability call, or {}."
        )

        response = await self._broker(
            context=context,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        proposal = self._parse_proposal(response)
        if proposal is None:
            _logger.debug(
                "interpreter subturn produced no proposal for %s",
                observation.kind,
            )
        return proposal

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _render_observation(self, observation: InterpreterObservation) -> str | None:
        try:
            text = json.dumps(observation.payload, default=str)
        except (TypeError, ValueError):
            text = str(observation.payload)
        if len(text) > _MAX_OBSERVATION_CHARS:
            # The producer should have artifactized; refuse rather than
            # truncate silently (a truncated tool payload invites guessing).
            _logger.warning(
                "interpreter observation %s exceeds %d chars; artifactize it",
                observation.kind, _MAX_OBSERVATION_CHARS,
            )
            return None
        return text

    def _parse_proposal(self, response: Any) -> InterpreterProposal | None:
        """Extract the first text block and parse it as the proposal JSON."""
        for block in getattr(response, "blocks", ()) or ():
            text = getattr(block, "text", None)
            if not text:
                continue
            candidate = text.strip()
            # Tolerate a fenced JSON block.
            if candidate.startswith("```"):
                candidate = candidate.strip("`")
                if candidate.lower().startswith("json"):
                    candidate = candidate[4:]
                candidate = candidate.strip()
            if not candidate.startswith("{"):
                continue
            try:
                parsed = json.loads(candidate)
            except ValueError:
                continue
            if not isinstance(parsed, dict) or not parsed:
                return None
            capability_id = parsed.get("capability_id")
            if not capability_id or not isinstance(capability_id, str):
                return None
            arguments = parsed.get("arguments") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            return InterpreterProposal(
                capability_id=capability_id,
                arguments=dict(arguments),
                rationale=str(parsed.get("rationale") or ""),
            )
        return None
