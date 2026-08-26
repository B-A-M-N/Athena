"""Ephemeral capability synthesis + proof-carrying promotion (fusion #4/#5).

Athena doesn't just generate skills-as-text: it can synthesize a TEMPORARY
EXECUTABLE CAPABILITY from a proven execution trace and register it through
the same registry -> policy -> executor path as native capabilities.

Lifecycle:
    ad-hoc execution -> synthesized helper -> sandbox validation ->
    effect classification -> EPHEMERAL capability (task-scoped) ->
    repeated success -> SkillCandidate with proof -> explicit promotion.

A promoted synthetic capability is PROOF-CARRYING: it keeps its validation
record, usage count, provenance, and effect envelope, so later retrieval
knows "executed successfully N times under these conditions", not merely
"advice I once wrote".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass

from athena.protocol.capabilities import (
    CapabilityDescriptor,
    CapabilityOrigin,
    CapabilityRequest,
    CapabilityResult,
    CapabilityResultStatus,
    EffectClass,
)
from athena.protocol.ids import new_id

__all__ = ["SyntheticCapability", "SynthesisEngine"]

_logger = logging.getLogger("athena.synthesis")

def _child_code(cap_code_repr: str) -> str:
    """Build the sandboxed child-process program for one capability."""
    return (
        "import json, sys\n"
        'ARGS = json.loads(sys.stdin.read() or "{}")\n'
        "NS = {}\n"
        f"exec({cap_code_repr}, NS)\n"
        'result = NS["run"](ARGS)\n'
        'print("__RESULT__" + json.dumps(result))\n'
    )


@dataclass
class SyntheticCapability:
    """A generated, validated, task-scoped executable capability."""

    id: str
    name: str
    description: str
    code: str                       # python source defining `def run(args)`
    input_schema: dict
    effects: frozenset              # declared effect envelope
    task_id: str | None
    provenance: dict                # originating task/call ids
    validation: dict                # test results from sandbox run
    uses: int = 0
    successes: int = 0
    failures: int = 0


class SynthesisEngine:
    """Registers temporary capabilities born from execution traces."""

    def __init__(self, *, restricted_env: bool = True) -> None:
        self._restricted_env = restricted_env
        self._synthetic: dict[str, SyntheticCapability] = {}

    def _child_env(self) -> dict:
        if not self._restricted_env:
            return {**os.environ, "PYTHONIOENCODING": "utf-8"}
        allowed = ("PATH", "PYTHONIOENCODING", "LANG", "LC_ALL", "TMPDIR",
                   "HOME", "VIRTUAL_ENV")
        env = {k: os.environ[k] for k in allowed if k in os.environ}
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def synthesize(
        self,
        *,
        name: str,
        description: str,
        code: str,
        input_schema: dict | None = None,
        effects: set | frozenset | None = None,
        task_id: str | None = None,
        provenance: dict | None = None,
        validation_cases: list[dict] | None = None,
    ) -> SyntheticCapability:
        """Create (but do not yet trust) a synthetic capability.

        ``validation_cases`` are [{"args": {...}, "expect_output_contains": ...}]
        executed in a subprocess sandbox before the capability becomes callable.
        """
        return SyntheticCapability(
            id=f"synth_{name}",
            name=name,
            description=description,
            code=code,
            input_schema=input_schema or {"type": "object", "properties": {}},
            effects=frozenset(effects or {EffectClass.READ_LOCAL}),
            task_id=task_id,
            provenance=provenance or {},
            validation={},
        )

    async def validate(
        self, cap: SyntheticCapability, cases: list[dict],
        *, timeout: float = 15.0,
    ) -> SyntheticCapability:
        """Run each case in an isolated interpreter; record evidence."""
        loop = asyncio.get_running_loop()
        passed = 0
        details = []

        child = _child_code(repr(cap.code))

        def _run_case(case: dict):
            proc = subprocess.run(
                [sys.executable, "-c", child],
                input=json.dumps(case.get("args") or {}),
                capture_output=True, text=True, timeout=timeout,
                env=self._child_env())
            return proc.stdout, proc.stderr, proc.returncode

        for i, case in enumerate(cases or []):
            try:
                out, err, rc = await loop.run_in_executor(None, _run_case, case)
                ok = rc == 0
                marker = "__RESULT__"
                value = None
                if ok and marker in out:
                    try:
                        line = out.split(marker, 1)[1].splitlines()[0]
                        value = json.loads(line)
                    except (IndexError, json.JSONDecodeError):
                        ok = False
                expect = case.get("expect_output_contains")
                if expect is not None:
                    if value is None:
                        ok = False
                    else:
                        ok = ok and str(expect).lower() in json.dumps(value).lower()
                passed += 1 if ok else 0
                details.append({"case": i, "passed": ok,
                                "value": value, "rc": rc,
                                **({"stderr": err[-300:]} if err else {})})
            except Exception as exc:
                details.append({"case": i, "passed": False, "error": str(exc)})

        total = len(details)
        cap.validation = {
            "cases_total": total, "cases_passed": passed,
            "all_passed": total > 0 and passed == total,
            "details": details,
        }
        return cap

    # ------------------------------------------------------------------
    # Registration into the live registry (ephemeral, task-scoped)
    # ------------------------------------------------------------------
    def register_ephemeral(self, registry, cap: SyntheticCapability) -> bool:
        """Admit a VALIDATED synthetic capability through the normal path."""
        if not cap.validation.get("all_passed"):
            _logger.warning("refusing unvalidated synthetic %s", cap.name)
            return False

        child = _child_code(repr(cap.code))

        class _Executor:
            def __init__(self, engine_ref):
                self.engine = engine_ref

            descriptor = CapabilityDescriptor(
                id=cap.id,
                description=f"[synthetic] {cap.description} "
                            f"(validated {cap.validation['cases_passed']}/"
                            f"{cap.validation['cases_total']})",
                input_schema=cap.input_schema,
                effects=frozenset(cap.effects),
                origin=CapabilityOrigin.NATIVE,
                version="0",
            )

            async def invoke(self, request, output_accumulator=None, context=None):
                # P1-18: enforce task scoping — a task-scoped synthetic must
                # not be callable by another task.
                if cap.task_id and request.task_id != cap.task_id:
                    return CapabilityResult(
                        request.call_id, request.capability_id,
                        CapabilityResultStatus.FAILED,
                        error=f"synthetic capability {cap.id} is scoped to "
                              f"task {cap.task_id}")
                loop = asyncio.get_running_loop()

                def _run():
                    payload = json.dumps(dict(request.arguments or {}))
                    return subprocess.run(
                        [sys.executable, "-c", child],
                        input=payload, capture_output=True,
                        text=True, timeout=30,
                        env=self.engine._child_env())

                proc = await loop.run_in_executor(None, _run)
                ok = proc.returncode == 0 and "__RESULT__" in proc.stdout
                cap.uses += 1
                if ok:
                    cap.successes += 1
                    value = json.loads(
                        proc.stdout.split("__RESULT__", 1)[1].splitlines()[0])
                    return CapabilityResult(
                        request.call_id, request.capability_id,
                        CapabilityResultStatus.OK,
                        output=json.dumps(value))
                cap.failures += 1
                return CapabilityResult(
                    request.call_id, request.capability_id,
                    CapabilityResultStatus.FAILED,
                    error=(proc.stderr or "synthetic failed")[-500:])

        registry.register(_Executor(engine_ref=self))
        self._synthetic[cap.id] = cap
        _logger.info("ephemeral capability registered: %s", cap.id)
        return True

    def proof_for(self, cap_id: str) -> dict | None:
        """Proof-carrying summary for a synthetic capability."""
        cap = self._synthetic.get(cap_id)
        if cap is None:
            return None
        return {
            "id": cap.id,
            "validation": cap.validation,
            "uses": cap.uses,
            "successes": cap.successes,
            "failures": cap.failures,
            "provenance": cap.provenance,
            "effects": sorted(getattr(e, "value", str(e)) for e in cap.effects),
        }

    def to_skill_candidate(self, cap_id: str):
        """Convert a repeatedly-successful synthetic into a SkillCandidate."""
        cap = self._synthetic.get(cap_id)
        if cap is None or cap.uses < 2 or cap.successes < cap.uses:
            return None
        from athena.skills.candidates import SkillCandidate
        from athena.skills.models import Skill

        skill = Skill(
            id=new_id("skill"),
            name=cap.name,
            description=cap.description,
            body=f"```python\n{cap.code}\n```",
            version=1,
        )
        return SkillCandidate(
            draft=skill,
            source_task_id=cap.task_id or "",
            target_skill=None,
            rationale="synthesized capability with proven usage history",
            evidence=tuple([
                f"validated {cap.validation.get('cases_passed')}/"
                f"{cap.validation.get('cases_total')} sandbox cases",
                f"{cap.successes}/{cap.uses} live invocations succeeded",
            ]),
            confidence=min(0.4 + 0.2 * cap.successes, 0.95),
        )
