from __future__ import annotations

import pytest
from athena.affordances import validation as validation_module
from athena.affordances.validation import GeneratedSourceValidator, ValidationTier
from athena.capabilities.synthesis import infer_input_schema


def test_task_source_validation_runs_contract_and_available_static_checks():
    result = GeneratedSourceValidator().validate(
        "def run(args):\n return {'ok': True}\n",
        tier=ValidationTier.TASK,
    )

    assert result.passed
    assert {check.name for check in result.checks} >= {
        "parse",
        "interface",
        "security",
        "format",
        "lint",
        "typecheck",
    }
    assert result.code.startswith("def run(args):")


@pytest.mark.athena_scenario("AUTH-003")
def test_source_validation_rejects_host_escape_primitives_before_execution():
    result = GeneratedSourceValidator().validate(
        "import subprocess\ndef run(args):\n return subprocess.run(args)\n",
    )

    assert not result.passed
    assert any(check.name == "security" and check.status == "failed" for check in result.checks)


def test_candidate_validation_requires_type_and_lint_tools_when_present():
    result = GeneratedSourceValidator().validate(
        "def run(args):\n return {'ok': True}\n",
        tier=ValidationTier.CANDIDATE,
    )

    assert result.passed
    assert result.metadata["required_tools"] == ["ruff"]
    assert {check.name for check in result.checks} >= {"format", "lint", "typecheck"}


def test_validation_classifies_mypy_timeout_without_calling_source_invalid(monkeypatch):
    calls: list[tuple[str, float]] = []

    def fake_tool(command, *, cwd, timeout):
        del cwd
        tool = command[0].rsplit("/", 1)[-1]
        calls.append((tool, timeout))
        if tool == "mypy":
            return validation_module._ToolResult(
                list(command), 124, stderr="timed out", status="timed_out"
            )
        return validation_module._ToolResult(list(command), 0, status="passed")

    monkeypatch.setattr(validation_module.shutil, "which", lambda tool: f"/{tool}")
    monkeypatch.setattr(validation_module, "_run_tool", fake_tool)
    result = GeneratedSourceValidator(timeout=2.0, tool_timeouts={"mypy": 45.0}).validate(
        "def run(args):\n return {'ok': True}\n",
        tier=ValidationTier.PROJECT,
    )

    assert not result.passed
    assert result.outcome == "timed_out"
    assert result.to_dict()["metadata"]["outcome"] == "timed_out"
    assert ("mypy", 45.0) in calls


def test_validation_distinguishes_invalid_source_from_environment_unavailable(monkeypatch):
    invalid = GeneratedSourceValidator().validate("def run(args):\n return\n  broken\n")
    assert invalid.outcome == "invalid_source"

    monkeypatch.setattr(
        validation_module.shutil,
        "which",
        lambda tool: None if tool == "mypy" else f"/{tool}",
    )
    unavailable = GeneratedSourceValidator().validate(
        "def run(args):\n return {'ok': True}\n",
        tier=ValidationTier.PROJECT,
    )

    assert unavailable.outcome == "environment_unavailable"
    assert any(check.status == "unavailable" for check in unavailable.checks)


def test_validation_distinguishes_tool_failure_from_source_diagnostics(monkeypatch):
    monkeypatch.setattr(
        validation_module.shutil,
        "which",
        lambda tool: f"/{tool}",
    )

    def failed_tool(command, *, cwd, timeout):
        del cwd, timeout
        return validation_module._ToolResult(
            list(command), 2, stderr="tool configuration failed", status="tool_error"
        )

    monkeypatch.setattr(validation_module, "_run_tool", failed_tool)
    result = GeneratedSourceValidator().validate(
        "def run(args):\n return {'ok': True}\n",
        tier=ValidationTier.PROJECT,
    )

    assert result.outcome == "tool_failed"
    assert any(check.status == "tool_error" for check in result.checks)


def test_input_schema_is_generated_from_validation_fixtures_when_omitted():
    schema = infer_input_schema(
        [
            {"args": {"path": "a.txt", "limit": 10}},
            {"args": {"path": "b.txt", "limit": 20}},
        ]
    )

    assert schema == {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["path", "limit"],
        "additionalProperties": False,
    }
