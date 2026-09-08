"""Tests for the exact-artifact publication verifier."""

from __future__ import annotations

import json
from pathlib import Path
import runpy


_REPO = Path(__file__).resolve().parents[3]
_VERIFY = runpy.run_path(str(_REPO / "scripts" / "verify-release-evidence"))


def _write_supporting_evidence(evidence: Path, sha: str, run_id: str) -> None:
    record = {"commit_sha": sha, "release_run_id": run_id, "exit_code": 0}
    (evidence / "optional-integrations.json").write_text(
        json.dumps(
            {
                "commit_sha": sha,
                "release_run_id": run_id,
                "hermes_referee": {
                    "source_sha": sha,
                    "release_run_id": run_id,
                    "status": "not_certified_live",
                },
            }
        )
    )
    (evidence / "static-checks.json").write_text(
        json.dumps({"commit_sha": sha, "release_run_id": run_id, "lanes": [record]})
    )
    (evidence / "pytest-summary.json").write_text(
        json.dumps({"commit_sha": sha, "release_run_id": run_id, "lane": record})
    )
    (evidence / "release-scenarios.json").write_text(
        json.dumps(
            {
                "commit_sha": sha,
                "release_run_id": run_id,
                "source_identity": {"head_sha": sha, "release_run_id": run_id},
                "scenarios": [
                    {
                        "commit_sha": sha,
                        "release_run_id": run_id,
                        "source_identity": {"head_sha": sha, "release_run_id": run_id},
                        "evidence": [record],
                    }
                ],
                "summary": {"required_not_passed": []},
            }
        )
    )


def test_verify_release_evidence_accepts_frozen_manifest(tmp_path: Path) -> None:
    sha = "a" * 40
    run_id = "run-1"
    evidence = tmp_path / "evidence"
    artifact_dir = evidence / "distributions"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "athena_agent-0.1.0-py3-none-any.whl"
    artifact.write_bytes(b"certified wheel")

    manifest = {
        "source_sha": sha,
        "source_status": "frozen-clean",
        "python_version": "0.1.0",
        "artifacts": [
            {
                "path": f"distributions/{artifact.name}",
                "size": artifact.stat().st_size,
                "sha256": _VERIFY["sha256"](artifact),
                "commit_sha": sha,
                "release_run_id": run_id,
            }
        ],
        "release_run_id": run_id,
    }
    (evidence / "release-manifest.json").write_text(json.dumps(manifest))
    (evidence / "release-identity.json").write_text(
        json.dumps({"commit_sha": sha, "release_run_id": run_id, "skip_e2e": False})
    )
    (evidence / "final-release-result.json").write_text(
        json.dumps(
            {
                "commit_sha": sha,
                "release_run_id": run_id,
                "status": "PASS",
                "releasable": True,
                "lanes": [{"commit_sha": sha, "release_run_id": run_id, "exit_code": 0}],
            }
        )
    )
    _write_supporting_evidence(evidence, sha, run_id)

    assert (
        _VERIFY["main"](
            [
                "--evidence-dir",
                str(evidence),
                "--sha",
                sha,
                "--tag",
                "v0.1.0",
            ]
        )
        == 0
    )

    static = json.loads((evidence / "static-checks.json").read_text())
    static["lanes"][0]["release_run_id"] = "different-run"
    (evidence / "static-checks.json").write_text(json.dumps(static))
    assert _VERIFY["main"](["--evidence-dir", str(evidence), "--sha", sha, "--tag", "v0.1.0"]) == 1


def test_verify_release_evidence_rejects_non_frozen_source(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    artifact_dir = evidence / "distributions"
    artifact_dir.mkdir(parents=True)
    (evidence / "release-manifest.json").write_text(
        json.dumps(
            {
                "source_sha": "a" * 40,
                "source_status": "dirty",
                "python_version": "0.1.0",
                "artifacts": [],
            }
        )
    )
    (evidence / "release-identity.json").write_text(
        json.dumps({"commit_sha": "a" * 40, "skip_e2e": False})
    )
    (evidence / "final-release-result.json").write_text(
        json.dumps({"commit_sha": "a" * 40, "status": "PASS", "releasable": True})
    )

    assert (
        _VERIFY["main"](
            [
                "--evidence-dir",
                str(evidence),
                "--sha",
                "a" * 40,
                "--tag",
                "v0.1.0",
            ]
        )
        == 1
    )


def test_verify_release_evidence_rejects_unexpected_distribution(tmp_path: Path) -> None:
    sha = "a" * 40
    run_id = "run-3"
    evidence = tmp_path / "evidence"
    artifact_dir = evidence / "distributions"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "athena_agent-0.1.0-py3-none-any.whl"
    artifact.write_bytes(b"certified wheel")
    (artifact_dir / "release-manifest.json").write_text("not a distribution")
    manifest = {
        "source_sha": sha,
        "source_status": "frozen-clean",
        "python_version": "0.1.0",
        "artifacts": [
            {
                "path": f"distributions/{artifact.name}",
                "size": artifact.stat().st_size,
                "sha256": _VERIFY["sha256"](artifact),
                "commit_sha": sha,
                "release_run_id": run_id,
            }
        ],
        "release_run_id": run_id,
    }
    (evidence / "release-manifest.json").write_text(json.dumps(manifest))
    (evidence / "release-identity.json").write_text(
        json.dumps({"commit_sha": sha, "release_run_id": run_id, "skip_e2e": False})
    )
    (evidence / "final-release-result.json").write_text(
        json.dumps(
            {
                "commit_sha": sha,
                "release_run_id": run_id,
                "status": "PASS",
                "releasable": True,
                "lanes": [],
            }
        )
    )
    _write_supporting_evidence(evidence, sha, run_id)
    assert _VERIFY["main"](["--evidence-dir", str(evidence), "--sha", sha, "--tag", "v0.1.0"])


def test_verify_release_evidence_selects_commit_run_transaction(tmp_path: Path) -> None:
    sha = "b" * 40
    run_id = "run-42"
    evidence = tmp_path / "evidence" / sha / run_id
    artifact_dir = evidence / "distributions"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "athena_agent-0.1.0-py3-none-any.whl"
    artifact.write_bytes(b"certified wheel")
    manifest = {
        "source_sha": sha,
        "source_status": "frozen-clean",
        "python_version": "0.1.0",
        "artifacts": [
            {
                "path": f"distributions/{artifact.name}",
                "size": artifact.stat().st_size,
                "sha256": _VERIFY["sha256"](artifact),
                "commit_sha": sha,
                "release_run_id": run_id,
            }
        ],
        "release_run_id": run_id,
    }
    (evidence / "release-manifest.json").write_text(json.dumps(manifest))
    (evidence / "release-identity.json").write_text(
        json.dumps({"commit_sha": sha, "release_run_id": run_id, "skip_e2e": False})
    )
    (evidence / "final-release-result.json").write_text(
        json.dumps(
            {
                "commit_sha": sha,
                "release_run_id": run_id,
                "status": "PASS",
                "releasable": True,
                "lanes": [{"commit_sha": sha, "release_run_id": run_id, "exit_code": 0}],
            }
        )
    )
    _write_supporting_evidence(evidence, sha, run_id)

    assert (
        _VERIFY["main"](
            [
                "--evidence-dir",
                str(tmp_path / "evidence"),
                "--sha",
                sha,
                "--run-id",
                run_id,
                "--tag",
                "v0.1.0",
            ]
        )
        == 0
    )
