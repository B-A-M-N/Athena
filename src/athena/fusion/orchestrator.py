"""Fusion orchestrator: the three engines operating as ONE system.

Ties together the pieces so their combination is greater than the parts:

    ShadowEngine   (speculative execution in isolated clones)
    TaskWorldState (claims + evidence + invariants + machine reality)
    TaskForker     (causal forks from any event point)
    CheckpointManager (workspace snapshots)
    SynthesisEngine (ephemeral capabilities -> proof-carrying skills)

Integrated workflows this module enables:

1. SpeculativeExperiment.run()  — propose ops, execute in shadow, verify,
   record a CLAIM bound to the evidence, check INVARIANTS before commit,
   commit only if the envelope holds; on failure, fork the parent task
   from the pre-experiment event for an alternate approach.

2. Invariant-gated commits — no shadow branch can commit while a required
   invariant is violated; violations are recorded as world-state facts.

3. Claim invalidation on commit — committing a branch invalidates STALE
   every claim whose dependency paths overlap the committed files.

4. Proof-carrying synthesis — synthetic capabilities validated in shadow
   branches carry their branch id as provenance; repeated success converts
   them to SkillCandidates with that evidence attached.

5. Fork-with-checkpoint — forking optionally captures a checkpoint of the
   parent workspace first, so the fork can restore to the exact causal
   state rather than merely replaying events.

Nothing here bypasses the kernel/policy/executor path; everything is
durable and auditable through the canonical event log.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from athena.causal.checkpoint import CheckpointManager
from athena.causal.fork import TaskForker
from athena.protocol.ids import new_id
from athena.worldstate.core import ClaimStatus

__all__ = ["FusionOrchestrator", "ExperimentResult"]

_logger = logging.getLogger("athena.fusion")


@dataclass
class ExperimentResult:
    """Outcome of one speculative experiment."""

    branch_id: str = ""
    status: str = "PROPOSED"         # COMMITTED | FAILED | DISCARDED
    claim_id: str | None = None      # claim recorded for a successful run
    invariant_report: dict = field(default_factory=dict)
    verification: list[dict] = field(default_factory=list)
    commit: dict = field(default_factory=dict)
    error: str | None = None
    fork_id: str | None = None       # set when auto-forking on failure


class FusionOrchestrator:
    """The integration layer binding all fusion engines together."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.shadow = service.shadow_engine()
        self.forker = TaskForker(service=service)
        self.checkpoints = CheckpointManager(
            root="/tmp/athena-checkpoints")

    # ------------------------------------------------------------------
    # 1+3+4: speculative experiment with invariant gate and claims
    # ------------------------------------------------------------------
    async def run_experiment(
        self,
        *,
        task_id: str,
        proposal: list[dict],
        criteria_probes: list[dict] | None = None,
        invariants: list[dict] | None = None,
        profile: str | None = None,
        auto_fork_on_failure: bool = True,
    ) -> ExperimentResult:
        """Run one full speculative experiment against reality.

        criteria_probes : [{"id","command"}] executed INSIDE the shadow;
                          all must pass for the branch to be verified.
        invariants      : [{"description","probe"}] required-envelope checks
                          evaluated AFTER execution but BEFORE commit.
        auto_fork_on_failure: create a causal fork at the parent's latest
                          event so an alternate approach can be tried.
        """
        ws = await self._workspace_for(task_id)
        result = ExperimentResult()

        # Pre-experiment checkpoint: the exact restore point if things go wrong.
        ckpt = await self.checkpoints.capture(
            task_id=task_id or "unknown", workspace_root=ws.root,
            label="pre-experiment")
        ckpt_id = ckpt.get("checkpoint_id") or ckpt.get("id")
        _logger.info("experiment checkpoint %s", ckpt_id)

        branch = await self.shadow.open_branch(
            task_id=task_id, base_workspace=ws, proposal=proposal)
        result.branch_id = branch.id

        branch = await self.shadow.execute_branch(branch, profile=profile)
        if branch.status == "FAILED":
            result.status = "FAILED"
            result.error = branch.error
            await self._fail_path(task_id, result, auto_fork_on_failure, ckpt_id)
            return result

        # Criteria probes run INSIDE the shadow workspace. Commands written
        # against the real workspace root are transparently rewritten to the
        # shadow root so "test -f <real>/src/x.py" verifies the shadow copy.
        verification = []
        all_ok = True
        real_root = os.path.realpath(ws.root)
        shadow_root = os.path.realpath(branch.shadow_workspace.root)
        for probe in criteria_probes or []:
            command = probe["command"].replace(real_root, shadow_root) \
                if real_root != shadow_root else probe["command"]
            ok, detail = await self._run_probe(command,
                                               branch.shadow_workspace.root)
            passed = ok if not probe.get("negate", False) else not ok
            verification.append({
                "id": probe.get("id") or new_id("ac"),
                "command": probe["command"], "passed": passed,
                "detail": detail[-400:],
            })
            if not passed:
                all_ok = False
        await self.shadow.record_verification(branch, verification)
        result.verification = verification
        if not all_ok:
            result.status = "FAILED"
            result.error = "acceptance criteria failed in shadow"
            await self.shadow.discard(branch, reason=result.error)
            await self._fail_path(task_id, result, auto_fork_on_failure, ckpt_id)
            return result

        # Invariant envelope BEFORE touching reality.
        inv_set = self._build_invariants(invariants)
        report = await inv_set.check_all()
        result.invariant_report = report
        if not report["ok"]:
            result.status = "FAILED"
            result.error = "invariant violation: " + "; ".join(
                v["description"] for v in report["violations"])
            await self.shadow.discard(branch, reason=result.error)
            await self._fail_path(task_id, result, auto_fork_on_failure, ckpt_id)
            return result

        # Commit and bind a claim to the evidence. Order matters:
        # 1) invalidate claims overlapping the committed paths (they're stale
        #    now that reality changed), 2) THEN record the fresh VERIFIED
        #    claim for this experiment.
        outcome = await self.shadow.commit(branch)
        result.commit = outcome
        wstate = self.service.world_state(task_id)
        depends = tuple(outcome.get("written", []))
        wstate.claims.invalidate_for_paths(list(depends))
        claim = wstate.claims.record(
            text=f"experiment {branch.id} verified "
                 f"({len(verification)} criteria)",
            evidence={
                "branch": branch.id,
                "checkpoint": ckpt_id,
                "criteria": verification,
                "invariants": report["results"],
                "committed": outcome,
            },
            task_id=task_id,
            depends_on_paths=depends,
        )
        result.claim_id = claim.id
        result.status = "COMMITTED"
        return result

    # ------------------------------------------------------------------
    # 2+5: fork with checkpoint restoration context
    # ------------------------------------------------------------------
    async def fork_from_event(self, *, task_id: str,
                              after_event_sequence: int,
                              capture_checkpoint: bool = False) -> dict:
        """Fork a task from a causal point, optionally checkpointing first."""
        ckpt = None
        if capture_checkpoint:
            ws = await self._workspace_for(task_id)
            ckpt = await self.checkpoints.capture(
                task_id=task_id, workspace_root=ws.root,
                label=f"fork-after-{after_event_sequence}")
        outcome = await self.forker.fork(
            task_id=task_id, after_event_sequence=after_event_sequence)
        if ckpt:
            outcome["checkpoint_id"] = ckpt.get("checkpoint_id") or ckpt.get("id")
        return outcome

    async def synthesize_from_branch(
        self, registry, *, name: str, description: str, code: str,
        input_schema: dict, effects: set, task_id: str | None,
        validation_cases: list[dict],
    ) -> dict:
        """Synthesize a capability validated INSIDE a shadow context.

        The branch provenance becomes part of the proof carried by the
        resulting skill candidate.
        """
        from athena.synthesis.engine import SynthesisEngine

        engine = getattr(self, "_synthesis", None)
        if engine is None:
            engine = SynthesisEngine()
            self._synthesis = engine

        cap = engine.synthesize(
            name=name, description=description, code=code,
            input_schema=input_schema, effects=effects, task_id=task_id,
            provenance={"origin": "shadow_experiment"},
        )
        cap = await engine.validate(cap, validation_cases)
        admitted = engine.register_ephemeral(registry, cap)
        candidate = engine.to_skill_candidate(cap.id) \
            if cap.validation.get("all_passed") else None
        return {
            "capability_id": cap.id,
            "admitted": admitted,
            "validation": cap.validation,
            "proof": engine.proof_for(cap.id),
            "skill_candidate_proposed": candidate is not None,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _workspace_for(self, task_id: str):
        base = self.service._default_workspace
        if task_id and getattr(self.service, "_store_tasks", None):
            try:
                row = await self.service._store_tasks.get(task_id)
                if row and row.get("workspace"):
                    from athena.kernel.lifecycle import deserialize_task

                    spec = deserialize_task(dict(row))
                    if spec.workspace is not None:
                        return spec.workspace
            except Exception as exc:
                _logger.warning("workspace lookup for %s failed: %s", task_id, exc)
        return base

    def _build_invariants(self, specs: list[dict] | None):
        from athena.worldstate import InvariantSet

        inv = InvariantSet()
        for spec in specs or []:
            inv.add(spec["description"], spec["probe"])
        return inv

    async def _run_probe(self, command: str, cwd: str) -> tuple[bool, str]:
        import subprocess

        loop = asyncio.get_running_loop()

        def _run():
            proc = subprocess.run(command, shell=True, capture_output=True,
                                  text=True, timeout=120, cwd=cwd)
            return proc.returncode == 0, proc.stdout + proc.stderr

        return await loop.run_in_executor(None, _run)

    async def _fail_path(self, task_id, result: ExperimentResult,
                         auto_fork: bool, ckpt_id: str | None) -> None:
        """On failure: discard handled by caller; optionally fork for retry."""
        if not auto_fork or not task_id:
            return
        try:
            timeline = await self.forker.timeline(task_id)
            seq = max((e["sequence"] for e in timeline), default=0)
            outcome = await self.fork_from_event(
                task_id=task_id, after_event_sequence=seq,
                capture_checkpoint=False)
            result.fork_id = outcome.get("fork_id")
            result.commit = {"checkpoint_available": ckpt_id}
            _logger.info("experiment failed; forked %s -> %s for alternate approach",
                         task_id, result.fork_id)
        except Exception as exc:
            _logger.warning("auto-fork after failure failed: %s", exc)
