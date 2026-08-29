from __future__ import annotations

import importlib.machinery
import importlib.util
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
