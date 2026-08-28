"""Negative-path check for the scenario runner gate logic (run once manually).

Loads scripts/scenarios as a module, injects a REQUIRED scenario that will
fail (nonexistent pytest node id), and asserts the runner exits 1 and lists
it in required_not_passed.  Not part of the test suite.
"""

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "scenarios_runner",
    str(ROOT / "scripts" / "scenarios"),
    loader=importlib.machinery.SourceFileLoader(
        "scenarios_runner", str(ROOT / "scripts" / "scenarios")
    ),
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

import dataclasses  # noqa: E402 — must load the runner module first

import tests.scenarios.registry as reg  # noqa: E402

fake = dataclasses.replace(
    reg.SCENARIOS[0],
    id="ZZZ-999",
    family="FUSE",
    title="gate negative check (must fail)",
    nodeids=("tests/unit/kernel/test_kernel_loop.py::test_does_not_exist_xyz",),
    probe=(),
    required=True,
    notes="",
)

reg.SCENARIOS = (fake,) + reg.SCENARIOS
mod.SCENARIOS = reg.SCENARIOS

rc = mod.main(["--only", "ZZZ-999", "--output", "/tmp/scm-neg.json"])
print("runner rc with failed required scenario:", rc)
assert rc == 1, "gate must exit 1 when a required scenario fails"
m = json.load(open("/tmp/scm-neg.json"))
assert m["summary"]["required_not_passed"] == ["ZZZ-999"], m["summary"]
print("summary:", m["summary"])
print("negative-path OK")
