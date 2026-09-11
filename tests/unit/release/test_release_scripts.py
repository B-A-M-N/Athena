"""Execute the cheap release-script probes instead of only testing registration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import runpy


_ROOT = Path(__file__).resolve().parents[3]


def test_clean_install_release_script_executes_n1_preflight() -> None:
    result = subprocess.run(
        [str(_ROOT / "scripts" / "clean-install-upgrade-rollback"), "--preflight"],
        cwd=_ROOT,
        env={**os.environ, "ATHENA_UV_CACHE_DIR": "/tmp/athena-uv-cache"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PREFLIGHT PASS" in result.stdout


def test_support_matrix_generation_executes_against_bound_fixture(tmp_path: Path) -> None:
    source_sha = "a" * 40
    run_id = "fixture-run"
    matrix = {
        "schema": 2,
        "surfaces": {
            "python": {
                "claimed_versions": ["3.12"],
                "claimed_platforms": [
                    f"{platform.system().lower()}-"
                    f"{platform.machine().lower().replace('amd64', 'x86_64')}"
                ],
                "implementation_status": "implemented",
            }
        },
    }
    passport = {
        "kind": "athena_backend_passport",
        "status": "PASS",
        "release_sha": source_sha,
        "release_run_id": run_id,
        "cells": [{"passed": True}],
    }
    lanes = {
        "commit_sha": source_sha,
        "release_run_id": run_id,
        "lanes": [
            {
                "name": "python-version",
                "exit_code": 0,
                "output": "Python 3.12.13",
                "commit_sha": source_sha,
                "release_run_id": run_id,
            }
        ],
    }
    matrix_path = tmp_path / "matrix.json"
    passport_path = tmp_path / "passport.json"
    lanes_path = tmp_path / "lanes.json"
    matrix_path.write_text(json.dumps(matrix))
    passport_path.write_text(json.dumps(passport))
    lanes_path.write_text(json.dumps(lanes))
    shutil.copy2(_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    output = tmp_path / "release-support-matrix.json"
    result = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "scripts" / "generate-support-matrix"),
            "--matrix",
            str(matrix_path),
            "--passport",
            str(passport_path),
            "--release-lanes",
            str(lanes_path),
            "--output",
            str(output),
            "--source-sha",
            source_sha,
            "--release-run-id",
            run_id,
            "--require-certified",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        json.loads(output.read_text())["surfaces"]["python"]["certification_status"] == "certified"
    )


def test_migration_baseline_script_executes() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "scripts" / "verify-migration-baseline"),
            "--baseline",
            str(_ROOT / "docs" / "migration-baseline.json"),
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_toolchain_passport_executes_failure_and_success_paths(tmp_path: Path) -> None:
    script = _ROOT / "scripts" / "toolchain-passport"
    uv = shutil.which("uv")
    cargo_deny = _ROOT / ".release-toolchain" / "cargo-deny" / "bin" / "cargo-deny"
    cosign = _ROOT / ".release-toolchain" / "bin" / "cosign"
    assert uv and cargo_deny.is_file() and cosign.is_file()

    failure = subprocess.run(
        [sys.executable, str(script), "--output", str(tmp_path / "failed.json")],
        cwd=_ROOT,
        env={
            **os.environ,
            "ATHENA_RELEASE_UV": uv,
            "ATHENA_CARGO_DENY_BIN": str(tmp_path / "missing-cargo-deny"),
            "ATHENA_COSIGN": str(tmp_path / "missing-cosign"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert failure.returncode == 1
    assert "FAIL" in failure.stderr

    success = subprocess.run(
        [sys.executable, str(script), "--output", str(tmp_path / "passed.json")],
        cwd=_ROOT,
        env={
            **os.environ,
            "ATHENA_RELEASE_UV": uv,
            "ATHENA_CARGO_DENY_BIN": str(cargo_deny),
            "ATHENA_COSIGN": str(cosign),
            "ATHENA_RELEASE_SYSTEM_PYTHON": "/usr/bin/python3",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert success.returncode == 0, success.stdout + success.stderr
    assert json.loads((tmp_path / "passed.json").read_text())["status"] == "PASS"


def test_canonical_receipt_selects_one_releasable_run_around_failed_history(
    tmp_path: Path,
) -> None:
    source_sha = "b" * 40
    root = tmp_path / source_sha
    (root / "failed-run").mkdir(parents=True)
    (root / "passed-run").mkdir()
    (root / "failed-run" / "final-release-result.json").write_text(
        json.dumps({"status": "FAIL", "releasable": False})
    )
    (root / "passed-run" / "final-release-result.json").write_text(
        json.dumps({"status": "PASS", "releasable": True})
    )
    result = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "scripts" / "canonical-release-receipt"),
            "--evidence-dir",
            str(tmp_path),
            "--sha",
            source_sha,
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "canonical identity/result missing" in result.stderr
    assert "expected one selected run" not in result.stderr


def test_release_environment_ignores_path_poisoning(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    marker = tmp_path / "ambient-tool-used"
    for name in ("uv", "python3"):
        fake = fake_bin / name
        fake.write_text(f"#!/bin/sh\nprintf used > {marker}\nexit 97\n")
        fake.chmod(0o755)
    uv = shutil.which("uv")
    assert uv is not None
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "ATHENA_RELEASE_UV": uv,
        "ATHENA_RELEASE_PYTHON": str(_ROOT / ".venv" / "bin" / "python"),
        "ATHENA_RELEASE_SYSTEM_PYTHON": "/usr/bin/python3",
    }
    result = subprocess.run(
        [
            "bash",
            "-ceu",
            'source scripts/cosign-env; test "$ATHENA_RELEASE_UV" = "$EXPECTED_UV"; test "$ATHENA_RELEASE_PYTHON" = "$EXPECTED_PYTHON"; "$ATHENA_RELEASE_UV" --version >/dev/null',
        ],
        cwd=_ROOT,
        env={
            **env,
            "EXPECTED_UV": uv,
            "EXPECTED_PYTHON": str(_ROOT / ".venv" / "bin" / "python"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()


def test_architecture_lint_fails_closed_when_baseline_is_missing(tmp_path: Path) -> None:
    script = _ROOT / "scripts" / "architecture-lint"
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "athena").symlink_to(_ROOT / "src" / "athena")
    result = subprocess.run(
        [sys.executable, str(script), "--root", str(tmp_path)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "architecture-size-baseline" in result.stdout


def test_architecture_lint_rejects_unwaived_baseline_widening(tmp_path: Path) -> None:
    script = _ROOT / "scripts" / "architecture-lint"
    (tmp_path / "src" / "athena").mkdir(parents=True)
    (tmp_path / "src" / "athena" / "__init__.py").write_text("\n")
    (tmp_path / "docs").mkdir()
    baseline_path = tmp_path / "docs" / "architecture-size-baseline.json"
    baseline_path.write_text(json.dumps({"schema": 1, "python": {"legacy.py": 2000}, "rust": {}}))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Athena Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=tmp_path,
        check=True,
    )

    baseline_path.write_text(json.dumps({"schema": 1, "python": {"legacy.py": 2100}, "rust": {}}))
    result = subprocess.run(
        [sys.executable, str(script), "--root", str(tmp_path)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "budget widened 2000 -> 2100" in result.stdout

    (tmp_path / "docs" / "architecture-debt-waivers.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "waivers": [
                    {
                        "family": "python",
                        "path": "legacy.py",
                        "old_budget": 2000,
                        "new_budget": 2100,
                        "rationale": "temporary ceiling while the module is decomposed",
                    }
                ],
            }
        )
    )
    result = subprocess.run(
        [sys.executable, str(script), "--root", str(tmp_path)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "waived python/legacy.py: 2000 -> 2100" in result.stdout


def test_release_policy_matches_full_lane_registration() -> None:
    from athena.release.gates import release_commands

    policy = json.loads((_ROOT / "scripts" / "release-policy-v1.json").read_text())
    lanes = [name for name, _command in release_commands("uv", skip_e2e=False, bootstrap=False)]
    assert lanes == list(dict.fromkeys(lanes))
    assert set(lanes) == set(policy["mandatory_lanes"])


def test_native_platform_cell_requires_passing_x11_wm_receipt(tmp_path: Path, monkeypatch) -> None:
    generator = runpy.run_path(str(_ROOT / "scripts" / "generate-support-matrix"))
    native_platform_cell = generator["_native_platform_cell"]
    monkeypatch.chdir(tmp_path)

    assert native_platform_cell("linux-x86_64") == "linux-x86_64"
    (tmp_path / "native-desktop-acceptance.json").write_text(
        json.dumps({"status": "PASS", "session_type": "x11", "wm": "Openbox"})
    )
    assert native_platform_cell("linux-x86_64") == "linux-x86_64-x11-openbox"

    (tmp_path / "native-desktop-acceptance.json").write_text(
        json.dumps({"status": "PASS", "session_type": "wayland", "wm": "Mutter"})
    )
    assert native_platform_cell("linux-x86_64") == "linux-x86_64"
