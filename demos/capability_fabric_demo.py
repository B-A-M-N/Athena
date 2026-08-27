"""Offline VHS showcase for Athena's programmable capability fabric.

The demo deliberately uses real Athena protocol/validation primitives but no
provider, network, database, or host mutation.  It is presentation glue, not a
second runtime: the event stream is projected through the same
``DualPaneSurface`` used by the CLI, so the recording shows the actual operator
surface rather than a parallel storyboard UI.

Two modes:

* Default (live): runs the demo once and prints to stdout.  Suitable for
  ``vhs``.  The pauses are deliberate so the operator can read each card and
  see the right-hand OI stream update.

* ``--emit-fixture FILE``: runs the same scenes and writes a canonical
  jsonl event stream to ``FILE``.  The fixture is the regression-test source
  of truth for the projection.

* ``--replay FILE --speed N``: replays a previously emitted fixture through
  the same event sequence and ``DualPaneSurface`` used in live mode.
  ``--speed`` controls the deliberate pauses (1.0 == live mode; >1 == faster;
  0 == no pauses).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from athena.capabilities.registry import validate_schema
from athena.capabilities.synthesis import infer_input_schema
from athena.models.compat.toolrepair import RepairOutcome, ToolInputRepairer
from athena.protocol.messages import CapabilityCallBlock, TextBlock
from athena.protocol.models import (
    ModelDelta,
    ModelEvent,
    ModelEventType,
    ModelRequest,
    ModelResponse,
    ModelResponseAccumulator,
    ToolCallCandidate,
)
from athena.research.models import EvidenceObject, SourceRecord
from athena.synthesis.engine import _schema_for_values
from athena.affordances.validation import GeneratedSourceValidator, ValidationTier
from athena.cli.dual_pane import DualPaneSurface
from athena.protocol.events import make_event


# The recording is intentionally readable: each of the eight gaps between the
# nine stages is long enough to understand the current card and right-hand
# stream.  VHS captures real time, so this produces a roughly 90-second demo.
LIVE_SCENE_CADENCE_S = 10.0
EVENT_PAUSE_S = 0.45


@dataclass(frozen=True)
class DemoData:
    input_schema: dict
    output_schema: dict
    source_checks: tuple[str, ...]
    repaired: dict
    repair_rules: tuple[str, ...]
    response_block_types: tuple[str, ...]
    source: SourceRecord
    evidence: EvidenceObject


def build_data() -> DemoData:
    fixtures = [
        {"args": {"paths": ["src/athena"], "mode": "strict"}},
        {"args": {"paths": ["tests"], "mode": "strict"}},
    ]
    input_schema = infer_input_schema(fixtures)
    # The capability contract can carry an explicit, unambiguous compatibility
    # alias. This lets the demo show real repair behavior without inventing a
    # value or using a permissive fallback schema.
    input_schema["x-athena-aliases"] = {"paths": ["files"]}

    generated_code = (
        "def run(args):\n"
        "    paths = args['paths']\n"
        "    return {'files_checked': len(paths), 'violations': 0}\n"
    )
    source_validation = GeneratedSourceValidator().validate(
        generated_code, tier=ValidationTier.TASK
    )
    source_checks = tuple(
        f"{check.name}:{check.status}" for check in source_validation.checks
    )

    output_schema = _schema_for_values([
        {"files_checked": 601, "violations": 17},
        {"files_checked": 412, "violations": 0},
    ])

    raw_arguments = '{"files": ["src/athena"], "mode": "strict"}'
    candidate = ToolCallCandidate.parse(
        "call_repair_01",
        "project.verify_manifest",
        raw_arguments,
        provider_profile_id="local-openai-compatible",
        model_id="athena-local-14b",
    )
    repaired, receipt = ToolInputRepairer().repair(
        call_id=candidate.call_id,
        tool_name=candidate.capability_id,
        arguments=candidate.raw_arguments,
        input_schema=input_schema,
        validate_fn=validate_schema,
        provider_profile_id=candidate.provider_profile_id,
        model_id=candidate.model_id,
    )
    if receipt.outcome != RepairOutcome.REPAIRED or not isinstance(repaired, dict):
        raise RuntimeError(f"demo repair unexpectedly failed: {receipt.to_dict()}")

    request = ModelRequest(
        messages=(),
        model="athena-local-14b",
        provider="openai-compat",
        request_id="demo-turn-01",
    )
    accumulator = ModelResponseAccumulator(request)
    call = CapabilityCallBlock(
        type="capability_call",
        call_id="call_repair_01",
        capability_id="project.verify_manifest",
        arguments=repaired,
        candidate=candidate,
    )
    accumulator.ingest(ModelEvent(
        type=ModelEventType.DELTA,
        request_id=request.request_id,
        delta=ModelDelta(request_id=request.request_id, text="I found a repeatable check."),
    ))
    accumulator.ingest(ModelEvent(
        type=ModelEventType.DELTA,
        request_id=request.request_id,
        delta=ModelDelta(request_id=request.request_id, block=call),
    ))
    accumulator.ingest(ModelEvent(
        type=ModelEventType.DONE,
        request_id=request.request_id,
        response=ModelResponse(
            request_id=request.request_id,
            model=request.model,
            provider=request.provider,
            blocks=(TextBlock(type="text", text="I found a repeatable check."),),
        ),
    ))
    response = accumulator.finish()

    content = b"official manifest rule: every release artifact must be verified"
    content_hash = hashlib.sha256(content).hexdigest()
    source = SourceRecord.for_uri(
        "artifact://sha256/demo-manifest",
        title="Project release manifest",
        source_type="documentation",
        authority_class="primary",
        content_hash=content_hash,
        artifact_uri="artifact://sha256/demo-manifest",
        task_id="demo-task",
        project_id="athena-demo",
    )
    evidence = EvidenceObject.for_content(
        source_id=source.id,
        extracted_claim="Release artifacts require manifest verification.",
        exact_supporting_excerpt="official manifest rule: every release artifact must be verified",
        locator={"section": "release", "line": 1},
        evidence_type="quote",
        authority_class=source.authority_class,
        extraction_method="deterministic-demo",
        task_id="demo-task",
    )
    return DemoData(
        input_schema=input_schema,
        output_schema=output_schema,
        source_checks=source_checks,
        repaired=repaired,
        repair_rules=tuple(receipt.rules),
        response_block_types=tuple(type(block).__name__ for block in response.blocks),
        source=source,
        evidence=evidence,
    )


def _clip(value: object, width: int) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= width else text[: max(0, width - 3)] + "..."


def _schema_summary(schema: dict) -> str:
    properties = schema.get("properties", {})
    fields = ", ".join(
        f"{name}:{spec.get('type', 'union')}" for name, spec in properties.items()
    )
    return "{" + fields + "}"


@dataclass
class SceneScript:
    """One chapter in the fixture-backed capability-fabric narrative."""

    title: str
    status: str
    appended_events: list[tuple[str, str]] = field(default_factory=list)
    appended_right: list[tuple[str, str]] = field(default_factory=list)
    progress: int = 0


def build_scenes(data: DemoData) -> list[SceneScript]:
    """Return the canonical chapter list.  Same shape for live and replay."""
    return [
        SceneScript(
            title="BOOT // one reasoning authority",
            status="READY",
            appended_events=[
                ("[ok]", "AgentKernel online"),
                ("[ok]", "objective: verify the next release"),
            ],
            appended_right=[
                ("Task", "demo-task"),
                ("Project", "athena-demo"),
                ("Model", "athena-local-14b"),
                ("Authority", "policy-derived"),
            ],
            progress=2,
        ),
        SceneScript(
            title="DISCOVER // inspect affordances before inventing tools",
            status="GAP FOUND",
            appended_events=[
                ("[ok]", "reflect: search visible affordances"),
                ("[ok]", "native: filesystem / execute / research"),
                ("[ok]", "gap: no project manifest verifier"),
            ],
            appended_right=[
                ("Scope", "SCRATCH -> TASK"),
                ("Fabric", "native + generated + external"),
                ("Strategy", "construct deterministic helper"),
            ],
            progress=4,
        ),
        SceneScript(
            title="RESEARCH // acquire knowledge, not another agent",
            status="EVIDENCE READY",
            appended_events=[
                ("[ok]", "research gap recorded"),
                ("[ok]", "source policy: local snapshot"),
                ("[ok]", "evidence: release rule located"),
            ],
            appended_right=[
                ("Source", "primary / artifact-backed"),
                ("Evidence", _clip(data.evidence.extracted_claim, 30)),
                ("Hash", ((data.source.content_hash or "")[:16] + "...") if data.source.content_hash else "(no-hash)"),
            ],
            progress=6,
        ),
        SceneScript(
            title="CONSTRUCT // turn repeated reasoning into computation",
            status="MACHINERY DRAFT",
            appended_events=[
                ("[ok]", "write project.verify_manifest"),
                ("[ok]", "input contract inferred from fixtures"),
                ("[ok]", "output contract inferred from observations"),
            ],
            appended_right=[
                ("Input", _schema_summary(data.input_schema)),
                ("Output", _schema_summary(data.output_schema)),
                ("Lifetime", "task-local"),
            ],
            progress=8,
        ),
        SceneScript(
            title="VALIDATE // independent checks before registration",
            status="ADMISSIBLE",
            appended_events=[
                ("[ok]", "parse + interface check"),
                ("[ok]", "security-pattern check"),
                ("[ok]", "Ruff format + lint"),
                ("[ok]", "schema compile + bounded fixture"),
            ],
            appended_right=[
                ("Checks", ", ".join(data.source_checks)),
                ("Schema", "exact JSON Schema / no {} fallback"),
                ("Proof", "validation evidence attached"),
            ],
            progress=11,
        ),
        SceneScript(
            title="REPAIR // compatibility fixes calls, never source authority",
            status="REPAIRED",
            appended_events=[
                ("[ok]", "provider emits raw candidate bytes"),
                ("[ok]", "double-encoded JSON detected"),
                ("[ok]", "one bounded repair pass"),
                ("[ok]", "exact schema revalidation"),
            ],
            appended_right=[
                ("Raw", '"{\\"paths\\": [...] }"'),
                ("Repair", ", ".join(data.repair_rules)),
                ("Canonical", json.dumps(data.repaired, separators=(",", ":"))),
            ],
            progress=14,
        ),
        SceneScript(
            title="OPERATE // mixed model/tool transcript stays durable",
            status="GOVERNED",
            appended_events=[
                ("[ok]", "assistant text persisted"),
                ("[ok]", "tool call persisted with call_id"),
                ("[ok]", "policy computes effective authority"),
                ("[ok]", "task-local capability registered"),
            ],
            appended_right=[
                ("Blocks", " + ".join(data.response_block_types)),
                ("Declared", "READ_LOCAL + WRITE_LOCAL"),
                ("Effective", "READ_LOCAL + EXECUTE"),
            ],
            progress=16,
        ),
        SceneScript(
            title="OBSERVE // computation compresses reality for reasoning",
            status="EVIDENCE BOUND",
            appended_events=[
                ("[ok]", "verify_manifest runs in Task pipeline"),
                ("[ok]", "stdout becomes structured observation"),
                ("[ok]", "claim linked to evidence + artifact"),
                ("[ok]", "world state updated"),
            ],
            appended_right=[
                ("Result", "601 files / 17 violations"),
                ("Reality", "observed, not merely narrated"),
                ("Next", "reason -> verify -> adapt"),
            ],
            progress=18,
        ),
        SceneScript(
            title="LEARN // preserve what proved useful",
            status="FABRIC EXTENDED",
            appended_events=[
                ("[ok]", "task overlay expires at terminal state"),
                ("[ok]", "valuable helper can promote to PROJECT"),
                ("[ok]", "proof, hashes, dependencies, provenance retained"),
                ("[ok]", "future task discovers the affordance"),
            ],
            appended_right=[
                ("Lifecycle", "SCRATCH -> TASK -> PROJECT"),
                ("Retained", "capability + skill + workflow"),
                ("Invariant", "one kernel / one policy wall"),
                ("Outcome", "unfamiliar environment, still operable"),
            ],
            progress=20,
        ),
    ]


def _event(event_type: str, payload: dict[str, Any] | None = None):
    """Create a task-scoped event for the real CLI projection."""
    return make_event(
        event_type,
        payload,
        task_id="demo-task",
        session_id="demo-session",
    )


def _model_turn(text: str) -> list[Any]:
    """Return the real model boundary events for one explanatory turn."""
    return [
        _event("ModelRequestStarted", {
            "provider": "fake",
            "model": "fake-1",
            "role": "primary",
        }),
        _event("ModelDelta", {"text": text}),
        _event("ModelResponseCompleted", {
            "provider": "fake",
            "model": "fake-1",
            "role": "primary",
        }),
    ]


def _capability_turn(
    capability_id: str,
    arguments: dict[str, Any],
    output: str,
    *,
    runtime: str = "python",
) -> list[Any]:
    """Return a capability/execution sequence rendered by the real surface."""
    return [
        _event("CapabilityRequested", {
            "capability_id": capability_id,
            "arguments": arguments,
        }),
        _event("CapabilityStarted", {"capability_id": capability_id}),
        _event("ExecutionStarted", {"runtime": runtime}),
        _event("StdoutChunk", {"data": output}),
        _event("ExecutionExited", {
            "runtime": runtime,
            "exit_status": "success",
            "exit_code": 0,
        }),
        _event("CapabilityCompleted", {"capability_id": capability_id}),
    ]


def _events_for_stage(stage: int, data: DemoData) -> list[Any]:
    """Translate one fixture chapter into actual application events."""
    schema_in = _schema_summary(data.input_schema)
    schema_out = _schema_summary(data.output_schema)
    repaired = json.dumps(data.repaired, separators=(",", ":"))
    checks = ", ".join(data.source_checks)

    if stage == 1:
        return [
            _event("TaskStarted", {"objective": "verify the next release"}),
            *_model_turn(
                "I’ll inspect the workspace, find the smallest safe path, "
                "and keep the work inside this durable task."
            ),
        ]
    if stage == 2:
        return [
            *_model_turn(
                "I found the native filesystem, execution, and research "
                "affordances, but no project manifest verifier."
            ),
            *_capability_turn(
                "affordances.list",
                {"operation": "list", "scope": "task"},
                "native: filesystem, execute, research\nmissing: project.verify_manifest\n",
            ),
        ]
    if stage == 3:
        return [
            *_model_turn(
                "The release rule is local evidence, so this run can stay "
                "offline while binding the result to a source snapshot."
            ),
            *_capability_turn(
                "research.snapshot",
                {"operation": "snapshot", "uri": "artifact://sha256/demo-manifest"},
                f"source: {data.source.title}\nevidence: {data.evidence.extracted_claim}\n",
            ),
            _event("ArtifactCreated", {
                "name": "demo-manifest",
                "uri": "artifact://sha256/demo-manifest",
            }),
        ]
    if stage == 4:
        return [
            *_model_turn(
                "I’ll construct a task-local verifier with strict input and "
                "output contracts, then exercise it before registration."
            ),
            *_capability_turn(
                "synthesis.create",
                {
                    "operation": "create",
                    "name": "project.verify_manifest",
                    "scope": "task",
                    "input": schema_in,
                    "output": schema_out,
                },
                f"generated: project.verify_manifest\ninput: {schema_in}\noutput: {schema_out}\n",
            ),
        ]
    if stage == 5:
        return [
            *_model_turn(
                "Before registration, Athena runs independent source, schema, "
                "and bounded fixture checks."
            ),
            *_capability_turn(
                "synthesis.validate",
                {"operation": "validate", "name": "project.verify_manifest"},
                f"checks: {checks}\nschema: exact JSON Schema\nfixtures: bounded smoke passed\n",
            ),
        ]
    if stage == 6:
        return [
            *_model_turn(
                "The provider sent a compatible call with the legacy `files` "
                "field; repair may fix its shape, never the implementation."
            ),
            *_capability_turn(
                "project.verify_manifest",
                {"files": ["src/athena"], "mode": "strict"},
                f"raw: files=[src/athena]\nrepair: {', '.join(data.repair_rules)}\ncanonical: {repaired}\nschema: revalidated\n",
            ),
        ]
    if stage == 7:
        return [
            *_model_turn(
                "The helper requests execution under inherited task authority; "
                "this action needs your approval."
            ),
            _event("ApprovalRequested", {
                "approval_id": "demo-approval-01",
                "capability_id": "execute",
                "scopes": ["call", "task", "session"],
            }),
            _event("ApprovalResolved", {
                "approval_id": "demo-approval-01",
                "decision": "approved",
                "scope": "task",
            }),
            *_capability_turn(
                "execute",
                {"language": "python", "code": "verify_manifest(paths)"},
                "policy: task scope\nexecution: allowed\nhelper: project.verify_manifest\n",
            ),
        ]
    if stage == 8:
        return [
            *_model_turn(
                "Execution produced a structured observation, not just a "
                "narrated claim."
            ),
            *_capability_turn(
                "worldstate.record",
                {"operation": "record", "claim": "release artifacts require verification"},
                "files_checked=601\nviolations=17\nclaim: evidence-bound\n",
            ),
        ]
    if stage == 9:
        return [
            *_model_turn(
                "This task-local capability proved useful. Athena can retain "
                "it as a candidate with proof, hashes, and provenance."
            ),
            *_capability_turn(
                "skills.promote_candidate",
                {"operation": "promote", "from": "TASK", "to": "PROJECT"},
                "candidate: project.verify_manifest\nproof: validation + execution\nprovenance: demo-task\n",
            ),
            _event("TaskCompleted", {"status": "COMPLETE"}),
        ]
    return []


async def _run_surface(
    scenes: list[SceneScript],
    data: DemoData,
    *,
    speed: float,
) -> None:
    """Project the event sequence through the production CLI surface."""
    surface = DualPaneSurface(details=False)
    print("Athena console (type /help for commands; Ctrl-D to exit)")
    print("athena> verify the next release using the available affordances")
    pause = 0.0 if speed == 0 else EVENT_PAUSE_S / max(speed, 0.0001)
    cadence = 0.0 if speed == 0 else LIVE_SCENE_CADENCE_S / max(speed, 0.0001)

    for index, _scene in enumerate(scenes, start=1):
        for event in _events_for_stage(index, data):
            await surface.render_event(event)
            if pause:
                await asyncio.sleep(pause)
        if index < len(scenes) and cadence:
            await asyncio.sleep(cadence)

    # Leave the completed task and returned prompt on screen long enough to
    # read, just as the real REPL does before its next input call.
    await asyncio.sleep(3.0 / max(speed, 1.0) if speed else 0.0)
    surface.finish()
    print("\n[task demo-task -> COMPLETE]")
    print("athena> ", end="", flush=True)


def run_live(scenes: list[SceneScript], data: DemoData, *, speed: float) -> None:
    asyncio.run(_run_surface(scenes, data, speed=speed))


def _iter_fixture(path: str) -> Iterable[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def emit_fixture(scenes: list[SceneScript], path: str) -> None:
    """Write the canonical jsonl fixture: one event per scene.

    The schema is intentionally small and explicit.  The shape is the
    contract: the event sequence is the input to the production CLI surface.
    """
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "kind": "demo_meta",
            "schema": "athena.capability_fabric.scene/v1",
            "scene_count": len(scenes),
        }) + "\n")
        for index, scene in enumerate(scenes, start=1):
            handle.write(json.dumps({
                "kind": "scene",
                "stage": index,
                "title": scene.title,
                "status": scene.status,
                "progress": scene.progress,
                "appended_events": [list(item) for item in scene.appended_events],
                "appended_right": [list(item) for item in scene.appended_right],
            }) + "\n")


def run_replay(scenes: list[SceneScript], data: DemoData, *, speed: float, source: str) -> None:
    """Replay fixture chapters through the same event sequence as live mode."""
    replay_scenes: list[SceneScript] = []
    for record in _iter_fixture(source):
        kind = record.get("kind")
        if kind == "demo_meta":
            continue
        if kind != "scene":
            continue
        replay_scenes.append(SceneScript(
            title=record["title"],
            status=record["status"],
            appended_events=[tuple(item) for item in record.get("appended_events", [])],
            appended_right=[tuple(item) for item in record.get("appended_right", [])],
            progress=int(record.get("progress", 0)),
        ))
    asyncio.run(_run_surface(replay_scenes or scenes, data, speed=speed))


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Athena capability-fabric offline demo / fixture emitter / replay.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--emit-fixture",
        metavar="PATH",
        help="Run the live demo, write a canonical jsonl fixture to PATH, exit.",
    )
    mode.add_argument(
        "--replay",
        metavar="PATH",
        help="Replay a jsonl fixture through the same projection.  Implies --speed if unset.",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=None,
        help=(
            "Replay/live cadence multiplier.  >1 = faster, 0 = no inter-scene pause.  "
            "Live default is 1.0 (= LIVE_SCENE_CADENCE_S per scene).  "
            "Replay default is 1.0 (a readable ~90-second recording)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    data = build_data()
    scenes = build_scenes(data)

    if args.emit_fixture:
        emit_fixture(scenes, args.emit_fixture)
        return 0

    if args.replay:
        speed = args.speed if args.speed is not None else 1.0
        run_replay(scenes, data, speed=speed, source=args.replay)
        return 0

    # Live mode.  Speed=1.0 by default; --speed 0 means "no waits at all" for
    # raw bench runs.
    speed = args.speed if args.speed is not None else 1.0
    run_live(scenes, data, speed=speed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
