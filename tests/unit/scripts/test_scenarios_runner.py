from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest


def _runner_module():
    root = Path(__file__).resolve().parents[3]
    path = root / "scripts" / "scenarios"
    spec = importlib.util.spec_from_file_location(
        "athena_scenarios_runner",
        path,
        loader=importlib.machinery.SourceFileLoader("athena_scenarios_runner", str(path)),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load scenario runner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_filtered_run_requires_explicit_manifest_output(tmp_path, monkeypatch):
    runner = _runner_module()
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        runner.main(["--only", "SYNTH-007"])

    assert exc.value.code == 2
    assert not (tmp_path / "scenarios-manifest.json").exists()


def test_excluded_family_is_not_executed(tmp_path, monkeypatch):
    runner = _runner_module()
    runner.SCENARIOS = (
        runner.Scenario(
            id="VHS-001",
            family="VHS",
            title="visual",
            status="MISSING",
            required=False,
        ),
        runner.Scenario(
            id="SAFE-001",
            family="SAFE",
            title="safe",
            status="MISSING",
            required=False,
        ),
    )
    runner.FAMILY_ORDER = ("VHS", "SAFE")
    runner.FAMILY_DESCRIPTIONS = {"VHS": "visual", "SAFE": "safe"}
    output = tmp_path / "scenarios.json"
    monkeypatch.chdir(tmp_path)

    assert runner.main(["--exclude-family", "VHS", "--output", str(output)]) == 0
    manifest = output.read_text(encoding="utf-8")
    assert "VHS-001" not in manifest
    assert "SAFE-001" in manifest


def test_list_honors_excluded_family(tmp_path, monkeypatch, capsys):
    runner = _runner_module()
    runner.SCENARIOS = (
        runner.Scenario(
            id="VHS-001",
            family="VHS",
            title="visual",
            status="MISSING",
            required=False,
        ),
        runner.Scenario(
            id="SAFE-001",
            family="SAFE",
            title="safe",
            status="MISSING",
            required=False,
        ),
    )
    runner.FAMILY_ORDER = ("VHS", "SAFE")
    runner.FAMILY_DESCRIPTIONS = {"VHS": "visual", "SAFE": "safe"}
    monkeypatch.chdir(tmp_path)

    assert runner.main(["--list", "--exclude-family", "VHS"]) == 0
    assert "VHS-001" not in capsys.readouterr().out


def test_artifact_identity_binds_manifest_and_artifact_hashes(tmp_path, monkeypatch):
    runner = _runner_module()
    artifact_root = tmp_path / "release-artifacts"
    artifact_root.mkdir()
    payload = b"exact-release-artifact"
    artifact = artifact_root / "distributions" / "athena.whl"
    artifact.parent.mkdir()
    artifact.write_bytes(payload)
    manifest = {
        "source_sha": "frozen-sha",
        "artifacts": [
            {"path": "distributions/athena.whl", "sha256": hashlib.sha256(payload).hexdigest()}
        ],
    }
    manifest_bytes = json.dumps(manifest).encode("utf-8")
    (artifact_root / "release-manifest.json").write_bytes(manifest_bytes)
    monkeypatch.setenv("ATHENA_RELEASE_ARTIFACT_DIR", str(artifact_root))

    identity = runner._artifact_identity()

    assert identity["status"] == "available"
    assert identity["source_sha"] == "frozen-sha"
    assert identity["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert identity["artifacts"] == [
        {"path": "distributions/athena.whl", "sha256": hashlib.sha256(payload).hexdigest()}
    ]


def test_required_capability_unavailable_fails_the_gate(tmp_path, monkeypatch):
    """Required host evidence cannot silently become a green release gate."""
    runner = _runner_module()
    runner.SCENARIOS = (
        runner.Scenario(
            id="HOST-001",
            family="HOST",
            title="host-bound evidence",
            nodeids=("tests/does-not-exist.py::never_run",),
            required=True,
            capabilities=("TCP_LOOPBACK", "NETWORK_EGRESS"),
        ),
    )
    runner.FAMILY_ORDER = ("HOST",)
    runner.FAMILY_DESCRIPTIONS = {"HOST": "host"}
    monkeypatch.setattr(
        runner,
        "_capability_available",
        lambda capability: {"TCP_LOOPBACK": False, "NETWORK_EGRESS": True}[capability],
    )
    output = tmp_path / "scenarios.json"
    monkeypatch.chdir(tmp_path)

    assert runner.main(["--output", str(output)]) == 1

    manifest = json.loads(output.read_text(encoding="utf-8"))
    entry = manifest["scenarios"][0]
    assert entry["status"] == "environment_unavailable"
    assert entry["capabilities"] == ["TCP_LOOPBACK", "NETWORK_EGRESS"]
    assert entry["unavailable_capabilities"] == ["TCP_LOOPBACK"]
    assert entry["evidence"] == []
    assert manifest["summary"]["required_environment_unavailable"] == ["HOST-001"]
    assert manifest["summary"]["required_not_passed"] == ["HOST-001"]


def test_capability_unavailable_does_not_execute_evidence(tmp_path, monkeypatch, capsys):
    runner = _runner_module()
    executed = []

    def forbidden_pytest(_nodeids):
        executed.append(_nodeids)
        raise AssertionError("evidence must not run without its host capability")

    runner.SCENARIOS = (
        runner.Scenario(
            id="HOST-002",
            family="HOST",
            title="host-bound evidence",
            nodeids=("tests/does-not-exist.py::never_run",),
            required=True,
            capabilities=("DISPLAY",),
        ),
    )
    runner.FAMILY_ORDER = ("HOST",)
    runner.FAMILY_DESCRIPTIONS = {"HOST": "host"}
    runner._run_pytest = forbidden_pytest
    monkeypatch.setattr(runner, "_capability_available", lambda _capability: False)
    output = tmp_path / "scenarios.json"
    monkeypatch.chdir(tmp_path)

    assert runner.main(["--output", str(output), "--quiet"]) == 1
    assert executed == []
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["scenarios"][0]["status"] == "environment_unavailable"


def test_scenario_capability_list_matches_test_markers_for_high_risk_paths():
    """Registry and pytest declarations stay aligned for environment lanes."""
    from tests.scenarios.registry import SCENARIOS

    expected = {
        "FUSE-007": ("NETWORK_EGRESS",),
        "RESEARCH-001": ("TCP_LOOPBACK",),
        "PROOF-002": ("NETWORK_EGRESS",),
    }
    by_id = {scenario.id: scenario for scenario in SCENARIOS}
    assert all(scenario_id in by_id for scenario_id in expected)
    for scenario_id, capabilities in expected.items():
        assert by_id[scenario_id].capabilities == capabilities
