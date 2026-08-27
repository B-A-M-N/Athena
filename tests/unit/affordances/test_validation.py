from __future__ import annotations

from athena.affordances.validation import GeneratedSourceValidator, ValidationTier
from athena.capabilities.synthesis import infer_input_schema


def test_task_source_validation_runs_contract_and_available_static_checks():
    result = GeneratedSourceValidator().validate(
        "def run(args):\n return {'ok': True}\n",
        tier=ValidationTier.TASK,
    )

    assert result.passed
    assert {check.name for check in result.checks} >= {
        "parse", "interface", "security", "format", "lint", "typecheck",
    }
    assert result.code.startswith("def run(args):")


def test_source_validation_rejects_host_escape_primitives_before_execution():
    result = GeneratedSourceValidator().validate(
        "import subprocess\ndef run(args):\n return subprocess.run(args)\n",
    )

    assert not result.passed
    assert any(
        check.name == "security" and check.status == "failed"
        for check in result.checks
    )


def test_candidate_validation_requires_type_and_lint_tools_when_present():
    result = GeneratedSourceValidator().validate(
        "def run(args):\n return {'ok': True}\n",
        tier=ValidationTier.CANDIDATE,
    )

    assert result.passed
    assert result.metadata["required_tools"] == ["ruff"]
    assert {check.name for check in result.checks} >= {"format", "lint", "typecheck"}


def test_input_schema_is_generated_from_validation_fixtures_when_omitted():
    schema = infer_input_schema([
        {"args": {"path": "a.txt", "limit": 10}},
        {"args": {"path": "b.txt", "limit": 20}},
    ])

    assert schema == {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["path", "limit"],
        "additionalProperties": False,
    }
