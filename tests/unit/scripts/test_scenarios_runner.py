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
