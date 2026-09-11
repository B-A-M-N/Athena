"""The architecture exception inventory must fail closed and stay narrow."""

from __future__ import annotations

import json
from pathlib import Path


def test_model_reachable_subprocess_exceptions_are_explicit_and_bounded() -> None:
    root = Path(__file__).resolve().parents[3]
    policy = json.loads(
        (root / "docs" / "architecture-exception-policy.json").read_text(encoding="utf-8")
    )
    entries = policy["subprocess"]
    model_reachable = [entry for entry in entries if entry["model_reachable"]]

    assert policy["default_action"] == "fail_on_new_or_moved_exception_site"
    assert {entry["path"] for entry in model_reachable} == {
        "src/athena/synthesis/child_runtime.py",
        "src/athena/synthesis/runtime.py",
    }
    assert len(model_reachable) <= 2
    assert all(entry.get("authority_boundary") for entry in model_reachable)
    assert all(entry["category"] == "model_reachable_data_plane" for entry in model_reachable)
    assert all(
        not entry["model_reachable"]
        for entry in entries
        if entry["category"] == "trusted_control_plane"
    )
