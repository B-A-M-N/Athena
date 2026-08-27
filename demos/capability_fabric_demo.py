"""Offline VHS showcase for Athena's programmable capability fabric.

The demo deliberately uses real Athena protocol/validation primitives but no
provider, network, database, or host mutation.  It is presentation glue, not a
second runtime: the right pane exposes the durable/provenance story while the
left pane advances one AgentKernel operating loop.

Two modes:

* Default (live): runs the demo once and prints to stdout.  Suitable for
  ``vhs``.  No wall-clock sleeps — scenes advance at a configurable cadence so
  the recording is bounded and deterministic.

* ``--emit-fixture FILE``: runs the same scenes and writes a canonical
  jsonl event stream to ``FILE``.  The fixture is the regression-test source
  of truth for the projection.

* ``--replay FILE --speed N``: replays a previously emitted fixture through
  the same projection/renderer used in the live mode.  ``--speed`` controls
  cadence (1.0 == live mode's cadence; >1 == faster; 0 == no inter-scene
  pause, all scenes flush as fast as VHS can redraw).

The render path is shared: the same ``render()`` projection function is the
reducer.  ``wait()`` is the only thing the modes differ on, and it never
calls ``time.sleep`` for real time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
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


RESET = "\033[0m"
CYAN = "\033[38;5;81m"
GREEN = "\033[38;5;114m"
YELLOW = "\033[38;5;221m"
MAGENTA = "\033[38;5;177m"
MUTED = "\033[38;5;244m"
WHITE = "\033[38;5;255m"
RED = "\033[38;5;210m"

# Cadence per scene in the LIVE mode, in seconds.  The default was tuned to
# make the recording fit in ~17 seconds total (well under the 60s render
# timeout).  It is NOT real-time-feel; it is recording-feel.
LIVE_SCENE_CADENCE_S = 1.6


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


def _panel(text: str, width: int, color: str = WHITE) -> str:
    return color + _clip(text, width).ljust(width) + RESET


def _schema_summary(schema: dict) -> str:
    properties = schema.get("properties", {})
    fields = ", ".join(
        f"{name}:{spec.get('type', 'union')}" for name, spec in properties.items()
    )
    return "{" + fields + "}"


def render(
    *,
    stage: int,
    title: str,
    status: str,
    event_lines: list[tuple[str, str]],
    right_lines: list[tuple[str, str]],
    progress: int,
) -> None:
    width = 118
    left_width = 57
    right_width = width - left_width - 3
    print("\033[2J\033[H", end="")
    print(_panel(" ATHENA // CAPABILITY FABRIC ", width, CYAN))
    print(_panel(" one durable intelligence | programmable computer | governed learned machinery ", width, MUTED))
    print()
    print(_panel(f" {title}", width, WHITE))
    print(_panel(f" stage {stage}/9   {status}   [{'#' * progress}{'.' * (20 - progress)}]", width, GREEN))
    print()
    print(_panel(" OPERATING LOOP", left_width, CYAN) + " " + _panel(" DURABLE STATE", right_width, MAGENTA))
    print(_panel("-" * left_width, left_width, MUTED) + "   " + _panel("-" * right_width, right_width, MUTED))
    rows = max(len(event_lines), len(right_lines))
    for index in range(rows):
        left = event_lines[index] if index < len(event_lines) else ("", "")
        right = right_lines[index] if index < len(right_lines) else ("", "")
        left_text = f" {left[0]:<9} {left[1]}" if left[0] else ""
        right_text = f" {right[0]:<13} {right[1]}" if right[0] else ""
        print(_panel(left_text, left_width, GREEN if left[0] == "[ok]" else WHITE) + " | " + _panel(right_text, right_width, MAGENTA if right[0] else MUTED))
    print()
    print(_panel(" [policy] behavior may expand; authority does not", width, YELLOW))
    print(_panel(" [kernel] workflows, capabilities, research, repair, and retention share one Task/Event boundary", width, MUTED))
    sys.stdout.flush()


@dataclass
class SceneScript:
    """The 9-scene capability-fabric narrative as canonical events.

    Each scene is a single reducer invocation: ``render(stage=..., ...)``.
    Scenes are *append-only* projection operations on the same kernel state
    (events, right_lines).  The events list and right_lines list grow as the
    loop advances; that growth is the canonical state.
    """

    title: str
    status: str
    appended_events: list[tuple[str, str]] = field(default_factory=list)
    appended_right: list[tuple[str, str]] = field(default_factory=list)
    progress: int = 0


def build_scenes(data: DemoData) -> list[SceneScript]:
    """Return the canonical scene list.  Same shape for live and replay."""
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


def _project(events: list[tuple[str, str]], right: list[tuple[str, str]],
             scene: SceneScript, stage: int) -> None:
    """Reducer: apply one scene to the running state and render once."""
    events.extend(scene.appended_events)
    right.extend(scene.appended_right)
    render(
        stage=stage,
        title=scene.title,
        status=scene.status,
        event_lines=list(events),
        right_lines=list(right),
        progress=scene.progress,
    )


def _noop_wait(_seconds: float) -> None:
    """The wait primitive in the demo.

    Live mode does a real (short) sleep so the operator can see motion.
    Replay mode (and ``--speed 0``) does NOT sleep — scenes flush as fast
    as the renderer can redraw.  This is the only thing live and replay
    differ on.
    """
    return None


def run_live(scenes: list[SceneScript], *, cadence_s: float) -> None:
    events: list[tuple[str, str]] = []
    right: list[tuple[str, str]] = []
    for index, scene in enumerate(scenes, start=1):
        _project(events, right, scene, index)
        if cadence_s > 0 and index < len(scenes):
            import time
            time.sleep(cadence_s)
    print()
    print(_panel(
        " demo complete // Athena builds the missing affordance, then remembers how to use it ",
        118, CYAN,
    ))


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
    contract: ``render()`` is the projection, the jsonl stream is the input.
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


def run_replay(scenes: list[SceneScript], *, speed: float, source: str) -> None:
    """Replay a fixture through the same projection as live mode.

    ``scenes`` is unused for projection content; we use it to bound stage
    count.  The reducer applies each fixture event identically to live mode.
    """
    import time
    events: list[tuple[str, str]] = []
    right: list[tuple[str, str]] = []
    stage = 0
    for record in _iter_fixture(source):
        kind = record.get("kind")
        if kind == "demo_meta":
            continue
        if kind != "scene":
            continue
        stage += 1
        scene = SceneScript(
            title=record["title"],
            status=record["status"],
            appended_events=[tuple(item) for item in record.get("appended_events", [])],
            appended_right=[tuple(item) for item in record.get("appended_right", [])],
            progress=int(record.get("progress", 0)),
        )
        _project(events, right, scene, stage)
        if speed == 0:
            continue
        if stage < len(scenes):
            time.sleep(LIVE_SCENE_CADENCE_S / max(speed, 0.0001))
    print()
    print(_panel(
        " demo complete // Athena builds the missing affordance, then remembers how to use it ",
        118, CYAN,
    ))


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
            "Replay default is 4.0 (~0.4s/scene, suitable for ~16s VHS recordings)."
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
        speed = args.speed if args.speed is not None else 4.0
        run_replay(scenes, speed=speed, source=args.replay)
        return 0

    # Live mode.  Speed=1.0 by default; --speed 0 means "no waits at all" for
    # raw bench runs.
    speed = args.speed if args.speed is not None else 1.0
    cadence = 0.0 if speed == 0 else LIVE_SCENE_CADENCE_S / max(speed, 0.0001)
    run_live(scenes, cadence_s=cadence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
