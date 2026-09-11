"""Tests for the exact-artifact publication verifier."""

from __future__ import annotations

import json
from pathlib import Path
import runpy


_REPO = Path(__file__).resolve().parents[3]
_VERIFY = runpy.run_path(str(_REPO / "scripts" / "verify-release-evidence"))


def _write_supporting_evidence(evidence: Path, sha: str, run_id: str) -> None:
    record = {"commit_sha": sha, "release_run_id": run_id, "exit_code": 0}
    manifest = json.loads((evidence / "release-manifest.json").read_text())
    (evidence / "backend-passport.json").write_text(
        json.dumps(
            {
                "kind": "athena_backend_passport",
                "schema_version": 1,
                "backend": "local-supervised",
                "release_sha": sha,
                "release_run_id": run_id,
                "status": "PASS",
                "cells": [
                    {
                        "backend": "local-supervised",
                        "runtime": "python",
                        "passed": True,
                        "unverified_claims": [],
                    }
                ],
            }
        )
    )
    (evidence / "support-matrix.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "generated_by": "scripts/generate-support-matrix",
                "release_binding": {"source_sha": sha, "release_run_id": run_id},
                "generated_from": {
                    "release_sha": sha,
                    "release_run_id": run_id,
                    "behavioral_evidence": {"backend_passport": {"status": "PASS"}},
                },
                "surfaces": {
                    name: {
                        "implementation_status": "implemented",
                        "certification_status": "certified",
                        "certified_versions": ["fixture"],
                        "certified_platforms": ["linux-x86_64"],
                        "evidence_lane": ["fixture-lane"],
                        "evidence_receipt": {
                            "source_sha": sha,
                            "release_run_id": run_id,
                            "status": "PASS",
                        },
                        "proof": {"status": "certified", "evidence": ["release-check:fixture"]},
                    }
                    for name in (
                        "python",
                        "mcp",
                        "anthropic_sdk",
                        "native",
                        "remote_packs",
                        "rollback",
                        "execution_backend_passport",
                        "endurance",
                    )
                },
            }
        )
    )
    (evidence / "toolchain-passport.json").write_text(
        json.dumps(
            {
                "kind": "athena_toolchain_passport",
                "status": "PASS",
                "release_sha": sha,
                "release_run_id": run_id,
                "policy_errors": [],
                "policy": {"path": "docs/toolchain-policy.json"},
                "lock_hashes": {
                    "cargo_lock_sha256": manifest["cargo_lock_sha256"],
                    "uv_lock_sha256": manifest["uv_lock_sha256"],
                },
            }
        )
    )
    (evidence / "rust-supply-chain.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "source_sha": sha,
                "release_run_id": run_id,
                "cargo_deny_version": "0.20.2",
            }
        )
    )
    (evidence / "cargo-deny-bootstrap.json").write_text(
        json.dumps({"status": "PASS", "version": "0.20.2"})
    )
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


def _attach_sbom(evidence: Path, manifest: dict, sha: str) -> None:
    lock_sha = "c" * 64
    uv_lock_sha = "d" * 64
    native_artifact = (
        evidence / "distributions" / "athena_agent_native-0.1.0-py3-none-linux_x86_64.whl"
    )
    native_artifact.write_bytes(b"certified native wheel")
    manifest["artifacts"].append(
        {
            "path": f"distributions/{native_artifact.name}",
            "size": native_artifact.stat().st_size,
            "sha256": _VERIFY["sha256"](native_artifact),
            "commit_sha": manifest["source_sha"],
            "release_run_id": manifest["release_run_id"],
        }
    )
    manifest["native_abi_policy"] = {"status": "PASS", "violations": []}
    sbom = {
        "spdxVersion": "SPDX-2.3",
        "athenaBinding": {
            "source_sha": sha,
            "cargo_lock_sha256": lock_sha,
            "uv_lock_sha256": uv_lock_sha,
        },
        "packages": [
            {
                "SPDXID": "SPDXRef-Python-fixture",
                "name": "fixture",
                "versionInfo": "1",
                "downloadLocation": "https://pypi.org/simple",
                "licenseDeclared": "MIT",
                "licenseConcluded": "NOASSERTION",
                "athena_license_metadata": {
                    "source": "fixture",
                    "field": "license",
                    "value": "MIT",
                },
                "athena_dependency_scope": "python-transitive",
                "checksums": [{"algorithm": "SHA256", "checksumValue": "e" * 64}],
            },
            {
                "SPDXID": "SPDXRef-AthenaNativeDistribution",
                "name": "athena-agent-native",
                "versionInfo": "0.1.0",
                "downloadLocation": "https://athena.invalid/release/fixture/native",
                "licenseDeclared": "MIT",
                "licenseConcluded": "MIT",
                "athena_license_metadata": {
                    "source": "fixture",
                    "field": "license",
                    "value": "MIT",
                },
                "athena_dependency_scope": "distribution",
                "athena_artifact_path": f"distributions/{native_artifact.name}",
                "checksums": [
                    {
                        "algorithm": "SHA256",
                        "checksumValue": _VERIFY["sha256"](native_artifact),
                    }
                ],
            },
        ],
    }
    sbom_path = evidence / "release-sbom.spdx.json"
    sbom_path.write_text(json.dumps(sbom))
    manifest["cargo_lock_sha256"] = lock_sha
    manifest["uv_lock_sha256"] = uv_lock_sha
    manifest["sbom"] = {
        "path": sbom_path.name,
        "sha256": _VERIFY["sha256"](sbom_path),
        "component_count": 2,
        "python_package_count": 1,
        "native_distribution_count": 1,
        "bound_to": {"source_sha": sha, "cargo_lock_sha256": lock_sha},
    }


def _attach_provenance(evidence: Path, sha: str, run_id: str) -> None:
    manifest_path = evidence / "release-manifest.json"
    artifacts = []
    for artifact in sorted((evidence / "distributions").iterdir()):
        if artifact.is_file():
            artifacts.append(
                {
                    "path": f"distributions/{artifact.name}",
                    "sha256": _VERIFY["sha256"](artifact),
                    "size": artifact.stat().st_size,
                }
            )
    (evidence / "release-provenance.json").write_text(
        json.dumps(
            {
                "kind": "athena_release_provenance",
                "schema_version": 1,
                "source_sha": sha,
                "release_run_id": run_id,
                "manifest_sha256": _VERIFY["sha256"](manifest_path),
                "artifacts": artifacts,
            }
        )
    )
    (evidence / "release-provenance.sig").write_bytes(b"fixture-signature")
    (evidence / "release-provenance-verification.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "mechanism": "fixture",
                "cosign_version": "fixture",
                "signer_identity": "fixture",
                "oidc_issuer": None,
                "key_fingerprint": None,
            }
        )
    )


def _install_fixture_verifier() -> None:
    def fake_verify(*_args, **_kwargs):
        return {
            "status": "PASS",
            "mechanism": "fixture",
            "cosign_version": "fixture",
            "signer_identity": "fixture",
            "oidc_issuer": None,
            "key_fingerprint": None,
        }

    _VERIFY["verify_provenance"] = fake_verify
    _VERIFY["main"].__globals__["verify_provenance"] = fake_verify


def _attach_endurance(evidence: Path, sha: str, run_id: str) -> None:
    (evidence / "endurance-receipt.json").write_text(
        json.dumps(
            {
                "kind": "athena_endurance_receipt",
                "status": "PASS",
                "source_sha": sha,
                "release_run_id": run_id,
                "duration_hours": 0.5,
            }
        )
    )


def _attach_certification_root(evidence: Path, sha: str, run_id: str) -> None:
    policy = json.loads((_REPO / "scripts" / "release-policy-v1.json").read_text())
    identity_path = evidence / "release-identity.json"
    identity = json.loads(identity_path.read_text())
    identity["release_policy"] = policy["policy_version"]
    identity_path.write_text(json.dumps(identity))
    result_path = evidence / "final-release-result.json"
    result = json.loads(result_path.read_text())
    result["release_policy"] = policy["policy_version"]
    result["lanes"] = [
        {"name": name, "commit_sha": sha, "release_run_id": run_id, "exit_code": 0}
        for name in policy["mandatory_lanes"]
    ]
    result_path.write_text(json.dumps(result))
    (evidence / "native-desktop-acceptance.json").write_text(
        json.dumps(
            {
                "kind": "athena_native_desktop_receipt",
                "status": "PASS",
                "source_sha": sha,
                "release_run_id": run_id,
                "display": ":99",
                "session_type": "x11",
                "wm": "Openbox",
                "xserver": "vendor=The X.Org Foundation;version=1.21",
                "window_strategy": "ewmh",
                "active_strategy": "ewmh_confirmed",
                "fallback_reason": None,
            }
        )
    )
    files = []
    for path in sorted(item for item in evidence.rglob("*") if item.is_file()):
        if path.name in {
            "release-certification-manifest.json",
            "release-certification.sig",
            "release-certification.crt",
            "release-certification-verification.json",
            "release-provenance-verification.json",
        }:
            continue
        files.append(
            {
                "path": path.relative_to(evidence).as_posix(),
                "sha256": _VERIFY["sha256"](path),
                "size": path.stat().st_size,
            }
        )
    (evidence / "release-certification-manifest.json").write_text(
        json.dumps(
            {
                "kind": "athena_release_certification_manifest",
                "schema_version": 1,
                "source_sha": sha,
                "release_run_id": run_id,
                "files": files,
            }
        )
    )
    (evidence / "release-certification.sig").write_bytes(b"fixture-signature")
    (evidence / "release-certification-verification.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "mechanism": "fixture",
                "cosign_version": "fixture",
                "signer_identity": "fixture",
                "oidc_issuer": None,
                "key_fingerprint": None,
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
    _attach_sbom(evidence, manifest, sha)
    (evidence / "release-manifest.json").write_text(json.dumps(manifest))
    (evidence / "release-identity.json").write_text(
        json.dumps(
            {
                "commit_sha": sha,
                "release_run_id": run_id,
                "skip_e2e": False,
                "include_endurance": True,
            }
        )
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
    _attach_provenance(evidence, sha, run_id)
    _attach_endurance(evidence, sha, run_id)
    _attach_certification_root(evidence, sha, run_id)
    _install_fixture_verifier()

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
    _attach_sbom(evidence, manifest, sha)
    (evidence / "release-manifest.json").write_text(json.dumps(manifest))
    (evidence / "release-identity.json").write_text(
        json.dumps(
            {
                "commit_sha": sha,
                "release_run_id": run_id,
                "skip_e2e": False,
                "include_endurance": True,
            }
        )
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
    _attach_provenance(evidence, sha, run_id)
    _attach_endurance(evidence, sha, run_id)
    _attach_certification_root(evidence, sha, run_id)
    _install_fixture_verifier()
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
    _attach_sbom(evidence, manifest, sha)
    (evidence / "release-manifest.json").write_text(json.dumps(manifest))
    (evidence / "release-identity.json").write_text(
        json.dumps(
            {
                "commit_sha": sha,
                "release_run_id": run_id,
                "skip_e2e": False,
                "include_endurance": True,
            }
        )
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
    _attach_provenance(evidence, sha, run_id)
    _attach_endurance(evidence, sha, run_id)
    _attach_certification_root(evidence, sha, run_id)
    _install_fixture_verifier()

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
