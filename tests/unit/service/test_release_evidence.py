"""Tests for the exact-artifact publication verifier."""

from __future__ import annotations

import json
from pathlib import Path
import runpy


_REPO = Path(__file__).resolve().parents[3]
_VERIFY = runpy.run_path(str(_REPO / "scripts" / "verify-release-evidence"))


def test_verify_release_evidence_accepts_frozen_manifest(tmp_path: Path) -> None:
    sha = "a" * 40
    evidence = tmp_path / "evidence"
    artifact_dir = evidence / "distributions"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "athena_agent-0.1.0b1-py3-none-any.whl"
    artifact.write_bytes(b"certified wheel")

    manifest = {
        "source_sha": sha,
        "source_status": "frozen-clean",
        "python_version": "0.1.0b1",
        "artifacts": [
            {
                "path": f"distributions/{artifact.name}",
                "size": artifact.stat().st_size,
                "sha256": _VERIFY["sha256"](artifact),
            }
        ],
    }
    (evidence / "release-manifest.json").write_text(json.dumps(manifest))
    (evidence / "release-identity.json").write_text(
        json.dumps({"commit_sha": sha, "skip_e2e": False})
    )
    (evidence / "final-release-result.json").write_text(
        json.dumps({"commit_sha": sha, "status": "PASS", "releasable": True})
    )

    assert (
        _VERIFY["main"](
            [
                "--evidence-dir",
                str(evidence),
                "--sha",
                sha,
                "--tag",
                "v0.1.0b1",
            ]
        )
        == 0
    )


def test_verify_release_evidence_rejects_non_frozen_source(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    artifact_dir = evidence / "distributions"
    artifact_dir.mkdir(parents=True)
    (evidence / "release-manifest.json").write_text(
        json.dumps(
            {
                "source_sha": "a" * 40,
                "source_status": "dirty",
                "python_version": "0.1.0b1",
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
                "v0.1.0b1",
            ]
        )
        == 1
    )


def test_verify_release_evidence_rejects_unexpected_distribution(tmp_path: Path) -> None:
    sha = "a" * 40
    evidence = tmp_path / "evidence"
    artifact_dir = evidence / "distributions"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "athena_agent-0.1.0b1-py3-none-any.whl"
    artifact.write_bytes(b"certified wheel")
    (artifact_dir / "release-manifest.json").write_text("not a distribution")
    manifest = {
        "source_sha": sha,
        "source_status": "frozen-clean",
        "python_version": "0.1.0b1",
        "artifacts": [
            {
                "path": f"distributions/{artifact.name}",
                "size": artifact.stat().st_size,
                "sha256": _VERIFY["sha256"](artifact),
            }
        ],
    }
    (evidence / "release-manifest.json").write_text(json.dumps(manifest))
    (evidence / "release-identity.json").write_text(
        json.dumps({"commit_sha": sha, "skip_e2e": False})
    )
    (evidence / "final-release-result.json").write_text(
        json.dumps({"commit_sha": sha, "status": "PASS", "releasable": True})
    )
    assert (
        _VERIFY["main"](["--evidence-dir", str(evidence), "--sha", sha, "--tag", "v0.1.0b1"]) == 1
    )
