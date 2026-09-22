"""Architecture ratchets must expose existing breadth debt explicitly."""

from __future__ import annotations

import json
import ast
from pathlib import Path


def test_baselines_declare_generic_and_exact_breadth_ratchets():
    payload = json.loads(Path("docs/architecture-size-baseline.json").read_text(encoding="utf-8"))
    assert payload["default-callable-lines"] == 200
    assert payload["default-class-lines"] == 600
    assert payload["python-class-budgets"]["service/service.py"]["AthenaService"] == 2194
    assert payload["python-function-budgets"]["kernel/inference_broker.py"]["_invoke"] == 16


def test_high_breadth_objects_are_in_committed_ratchet():
    payload = json.loads(Path("docs/architecture-size-baseline.json").read_text(encoding="utf-8"))
    classes = payload["python-class-budgets"]
    for rel, name in [
        ("affordances/fabric.py", "CapabilityFabric"),
        ("capabilities/dispatcher.py", "CapabilityDispatcher"),
        ("execution/manager.py", "ExecutionManager"),
        ("kernel/inference_broker.py", "InferenceBroker"),
        ("service/service.py", "AthenaService"),
        ("workflows/runs.py", "WorkflowRunStore"),
    ]:
        assert name in classes[rel]


def test_completed_extractions_have_no_committed_headroom():
    """Ratchets for completed seams must move with the extracted boundary."""
    payload = json.loads(Path("docs/architecture-size-baseline.json").read_text(encoding="utf-8"))
    targets = (
        ("reality/gate.py", "RealityGate", "python-class-budgets"),
        ("execution/manager.py", "ExecutionManager", "python-class-budgets"),
        ("service/lifecycle.py", "ServiceLifecycle", "python-class-budgets"),
        ("kernel/inference_broker.py", "_invoke", "python-function-budgets"),
        ("capabilities/synthesis.py", "SynthesisCapability", "python-class-budgets"),
        ("workflows/runs.py", "WorkflowRunStore", "python-class-budgets"),
        ("cli/framebuffer.py", "OIFrameBuffer", "python-class-budgets"),
    )
    for rel, name, family in targets:
        tree = ast.parse((Path("src/athena") / rel).read_text(encoding="utf-8"))
        nodes = (
            node
            for node in ast.walk(tree)
            if (
                isinstance(node, ast.ClassDef)
                if family == "python-class-budgets"
                else isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
            and node.name == name
        )
        node = next(nodes)
        assert payload[family][rel][name] == node.end_lineno - node.lineno
