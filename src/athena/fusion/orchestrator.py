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

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from athena.causal.checkpoint import CheckpointManager
from athena.causal.fork import TaskForker
from athena.protocol.capabilities import CapabilityRequestOrigin
from athena.protocol.ids import new_id

__all__ = ["ExperimentResult", "FusionOrchestrator"]

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
        state_root = getattr(service, "_runtime_state_root", None)
        self.checkpoints = CheckpointManager(
            root=(
                os.path.join(state_root, "checkpoints")
                if state_root else "/tmp/athena-checkpoints"
            ))
        self.forker = TaskForker(service=service, checkpoint_manager=self.checkpoints)

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

        criteria_probes : [{"id","command"}] executed INSIDE the shadow via
                          the dispatcher ('execute' capability, same
                          capability/policy path as proposals); all must pass
                          for the branch to be verified.
        invariants      : [{"description","command"|"probe"}] required-envelope
                          checks evaluated AFTER execution but BEFORE commit;
                          declarative "command" specs run inside the shadow
                          through the dispatcher too.
        auto_fork_on_failure: create a causal fork at the parent's latest
                          event so an alternate approach can be tried.
        """
        ws = await self._workspace_for(task_id)
        result = ExperimentResult()
        # Capture the PRE-EXPERIMENT event position (P1-40): a failure fork
        # must branch from BEFORE the experiment ran, not from after it.
        pre_experiment_sequence = 0
        try:
            timeline = await self.forker.timeline(task_id)
            pre_experiment_sequence = max(
                (e["sequence"] for e in timeline), default=0)
        except Exception as exc:  # noqa: BLE001 - timeline lookup is best-effort before experiment
            _logger.warning("pre-experiment timeline failed: %s", exc)

        # Pre-experiment checkpoint: the exact restore point if things go wrong.
        ckpt = await self.checkpoints.capture(
            task_id=task_id or "unknown", workspace_root=ws.root,
            label="pre-experiment")
        ckpt_id = ckpt.get("checkpoint_id") or ckpt.get("id")
        _logger.info("experiment checkpoint %s", ckpt_id)

        branch = await self.shadow.open_branch(
            task_id=task_id, base_workspace=ws, proposal=proposal,
            profile=profile)
        self.shadow.attach_checkpoint(branch, ckpt_id)
        result.branch_id = branch.id

        branch = await self.shadow.execute_branch(branch, profile=profile)
        if branch.status == "FAILED":
            result.status = "FAILED"
            result.error = branch.error
            await self._fail_path(task_id, result, auto_fork_on_failure, ckpt_id,
                                  pre_experiment_sequence)
            return result

        # Criteria probes run INSIDE the shadow workspace through the SAME
        # capability/policy path as proposals (item 16): each probe is an
        # 'execute' dispatch bound to branch.shadow_workspace. Commands
        # written against the real workspace root are transparently rewritten
        # to the shadow root so "test -f <real>/src/x.py" verifies the shadow
        # copy.
        verification = []
        all_ok = True
        for probe in criteria_probes or []:
            command = self._rewrite_to_shadow(probe["command"], branch)
            ok, detail = await self._dispatch_probe(
                command, branch.shadow_workspace, profile, task_id=task_id)
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
            await self._fail_path(task_id, result, auto_fork_on_failure, ckpt_id,
                                  pre_experiment_sequence)
            return result

        # Invariant envelope BEFORE touching reality.
        inv_set = self._build_invariants(invariants, branch=branch,
                                         profile=profile, task_id=task_id)
        report = await inv_set.check_all()
        result.invariant_report = report
        if not report["ok"]:
            result.status = "FAILED"
            result.error = "invariant violation: " + "; ".join(
                v["description"] for v in report["violations"])
            await self.shadow.discard(branch, reason=result.error)
            await self._fail_path(task_id, result, auto_fork_on_failure, ckpt_id,
                                  pre_experiment_sequence)
            return result

        # Commit and bind a claim to the evidence. Order matters:
        # 1) invalidate claims overlapping the committed paths (they're stale
        #    now that reality changed), 2) THEN record the fresh VERIFIED
        #    claim for this experiment.
        outcome = await self.shadow.commit(branch)
        result.commit = outcome
        if outcome.get("status") != "committed":
            result.status = "FAILED"
            result.error = str(
                outcome.get("error")
                or outcome.get("reason")
                or f"shadow commit did not complete: {outcome.get('status', 'unknown')}"
            )
            await self._fail_path(
                task_id, result, auto_fork_on_failure, ckpt_id,
                pre_experiment_sequence,
            )
            return result
        wstate = self.service.world_state(task_id)
        depends = tuple(outcome.get("written", []))
        wstate.claims.invalidate_for_paths(list(depends))
        event_sequence = 0
        events = getattr(self.service, "_store_events", None)
        if events is not None:
            try:
                event_sequence = await events.last_sequence(task_id)
            except Exception as exc:  # noqa: BLE001 - telemetry lookup cannot block commit planning
                _logger.warning("claim event boundary lookup failed: %s", exc)
        claim = wstate.claims.record(
            text=f"experiment {branch.id} verified "
                 f"({len(verification)} criteria)",
            evidence={
                "branch": branch.id,
                "checkpoint": ckpt_id,
                "criteria": verification,
                "invariants": report["results"],
                "committed": outcome,
                "event_sequence": event_sequence,
                "mutation_sequence": max(
                    (int(item.get("mutation_sequence"))
                     for item in outcome.get("mutation_results", [])
                     if item.get("mutation_sequence") is not None),
                    default=0,
                ),
                "workspace_revision": await self.checkpoints.fingerprint(ws.root),
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
                              capture_checkpoint: bool = False,
                              checkpoint_id: str | None = None) -> dict:
        """Fork a task from a causal point, optionally checkpointing first."""
        ckpt = None
        if capture_checkpoint:
            ws = await self._workspace_for(task_id)
            ckpt = await self.checkpoints.capture(
                task_id=task_id, workspace_root=ws.root,
                label=f"fork-after-{after_event_sequence}")
            checkpoint_id = ckpt.get("checkpoint_id") or ckpt.get("id")
        outcome = await self.forker.fork(
            task_id=task_id,
            after_event_sequence=after_event_sequence,
            workspace_checkpoint_id=checkpoint_id,
        )
        if checkpoint_id:
            outcome["checkpoint_id"] = checkpoint_id
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

        engine = getattr(self.service, "_synthesis", None)
        if engine is None:
            engine = SynthesisEngine()
            # Services create the shared engine during startup. This fallback
            # keeps the orchestrator usable with lightweight test doubles.
            self.service._synthesis = engine

        cap = engine.synthesize(
            name=name, description=description, code=code,
            input_schema=input_schema, effects=effects, task_id=task_id,
            provenance={"origin": "shadow_experiment"},
        )
        cap = await engine.validate(cap, validation_cases)
        # Generated machinery belongs in the effective task overlay.  Falling
        # back to the supplied global registry remains supported for older
        # callers/tests, but the service path never exposes it globally.
        surface = getattr(self.service, "_fabric", None) or registry
        admitted = engine.register_ephemeral(surface, cap)
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
            except Exception as exc:  # noqa: BLE001 - workspace fallback keeps orchestration available
                _logger.warning("workspace lookup for %s failed: %s", task_id, exc)
        return base

    async def _dispatch_probe(self, code: str, workspace, profile: str | None,
                              task_id: str | None = None) -> tuple[bool, str]:
        """Run one probe through the capability/policy path (item 16).

        Dispatches an ``execute`` request bound to the given (shadow)
        workspace so probes pass policy like every other operation instead
        of bypassing via raw subprocess.
        """
        from athena.capabilities.dispatcher import SuspendedCall
        from athena.protocol.capabilities import CapabilityRequest

        dispatcher = getattr(self.service, "_dispatcher", None)
        if dispatcher is None:
            raise RuntimeError("service has no capability dispatcher bound")
        req = CapabilityRequest(
            capability_id="execute",
            arguments={"language": "shell", "code": code},
            task_id=task_id,
            call_id=new_id("call"),
            origin=CapabilityRequestOrigin.TRUSTED_ORCHESTRATION,
        )
        result = await dispatcher.dispatch(req, workspace=workspace,
                                           profile=profile)
        if isinstance(result, SuspendedCall):
            return False, "probe requires approval; suspended"
        if isinstance(result, Exception):
            return False, str(result)
        ok = getattr(result.status, "value", str(result.status)) == "ok"
        detail = (getattr(result, "output", None) or "") \
            + (getattr(result, "error", None) or "")
        return ok, detail

    def _rewrite_to_shadow(self, command: str, branch) -> str:
        """Rewrite real paths to the sandbox-visible shadow mount.

        Shadow verification runs through the ``shadow`` execution backend.
        Inside its mount namespace the branch root is ``/workspace``; the
        host-side temporary path is intentionally not visible.  Rewriting
        only to the host shadow directory makes an absolute probe look right
        in the parent process but fail inside the actual sandbox.
        """
        real_root = os.path.realpath(branch.base_workspace.root)
        shadow_root = os.path.realpath(branch.shadow_workspace.root)
        if real_root == shadow_root:
            return command
        return command.replace(real_root, "/workspace")

    def _build_invariants(self, specs: list[dict] | None, *,
                          branch=None, profile: str | None = None,
                          task_id: str | None = None):
        """Build an InvariantSet from declarative or callable specs.

        Declarative spec: {"description", "command"} — the probe runs that
        command inside the shadow workspace via the dispatcher (same
        capability/policy path as everything else).
        """
        from athena.worldstate import InvariantSet

        inv = InvariantSet(
            task_id=task_id,
            store=getattr(self.service, "_world_state_store", None),
        )
        for spec in specs or []:
            probe = spec.get("probe")
            if probe is not None:
                raise ValueError(
                    "fusion invariants must use declarative command specs; "
                    "arbitrary Python probes are not durable"
                )
            if probe is None and spec.get("command") and branch is not None:
                code = self._rewrite_to_shadow(spec["command"], branch)

                async def cmd_probe(cmd=code):
                    ok, _ = await self._dispatch_probe(
                        cmd, branch.shadow_workspace, profile, task_id=task_id)
                    return ok

                probe = cmd_probe
            if probe is None:
                raise ValueError(
                    f"invariant spec needs 'command' or 'probe': {spec!r}")
            inv.add(
                spec["description"], probe,
                definition={
                    "type": "command",
                    "command": spec.get("command"),
                    "required": bool(spec.get("required", True)),
                },
            )
        return inv

    async def _fail_path(self, task_id, result: ExperimentResult,
                         auto_fork: bool, ckpt_id: str | None,
                         pre_experiment_sequence: int | None = None) -> None:
        """On failure: discard handled by caller; optionally fork for retry.

        Forks branch from the PRE-EXPERIMENT event position (P1-40): the
        alternate approach must not inherit the failed experiment's events.
        """
        if not auto_fork or not task_id:
            return
        try:
            seq = pre_experiment_sequence
            if seq is None:
                timeline = await self.forker.timeline(task_id)
                seq = max((e["sequence"] for e in timeline), default=0)
            outcome = await self.fork_from_event(
                task_id=task_id, after_event_sequence=seq,
                capture_checkpoint=False, checkpoint_id=ckpt_id)
            result.fork_id = outcome.get("fork_id")
            result.commit = {"checkpoint_available": ckpt_id}
            _logger.info("experiment failed; forked %s -> %s for alternate approach",
                         task_id, result.fork_id)
        except Exception as exc:  # noqa: BLE001 - auto-fork is recovery best-effort
            _logger.warning("auto-fork after failure failed: %s", exc)
