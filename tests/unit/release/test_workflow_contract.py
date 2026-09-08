from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"


def test_workflow_keeps_core_release_and_hermes_certification_separate():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "  core-release-certification:" in text
    assert "  hermes-referee-transport-certification:" in text
    assert "  protected-release:\n" not in text
    assert "vars.ATHENA_HERMES_E2E_ENABLED == 'true'" in text
    assert 'ATHENA_HERMES_INTEGRATION_GATE: "1"' in text
    assert "needs.core-release-certification.result == 'success'" in text
    assert "environment: core-release" in text
    assert "environment: hermes-integration" in text
    assert "name: athena-release-core-${{ github.sha }}" in text


def test_workflow_verification_and_publish_depend_on_core_evidence():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "needs: [core-release-certification]" in text
    assert "needs: [verify-release]" in text
    assert "name: athena-release-verified-${{ github.sha }}" in text


def test_workflow_uploads_structured_optional_hermes_evidence():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "ATHENA_HERMES_E2E_EVIDENCE_PATH" in text
    assert "athena-hermes-referee-certification.json" in text


def test_workflow_pins_runner_actions_and_uv():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "runs-on: ubuntu-latest" not in text
    assert "runs-on: ubuntu-24.04" in text
    assert "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683" in text
    assert "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405" in text
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in text
    assert "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" in text
    assert "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33" in text
    assert "dtolnay/rust-toolchain@bc540ba06a4ccee415bb241490e0b25ee8e7d315" in text
    assert "dtolnay/rust-toolchain@1.85.0" not in text
    assert "python -m pip install uv==0.11.21" in text
    assert "permissions:\n  contents: read" in text


def test_workflow_does_not_call_optional_hermes_evidence_certified():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "name: athena-hermes-evidence-${{ github.sha }}" in text
    assert "name: athena-hermes-certified-${{ github.sha }}" not in text
