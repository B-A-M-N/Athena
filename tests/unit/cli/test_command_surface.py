"""Discoverability and parser-parity tests for the public CLI surface."""

from __future__ import annotations

import pytest

import click
from click.testing import CliRunner

import athena.cli.app as app


GROUP_ACTIONS = {
    "sessions": ("list", "show", "close"),
    "tasks": ("list", "show", "result", "cancel", "interrupt", "resume", "steer", "input"),
    "jobs": ("list", "show", "enable", "disable", "run-now", "grant", "revoke"),
    "workflows": ("list", "show"),
    "packs": ("list", "search", "inspect", "install", "enable", "disable", "remove"),
    "memory": ("candidates", "inspect", "promote", "discard"),
    "mcp": ("list", "tools", "resources", "prompts", "doctor", "reconnect"),
    "skills": ("list", "search", "inspect", "enable", "disable"),
    "inference-recoveries": ("list", "show", "resolve", "close-liability"),
    "artifacts": ("list",),
    "candidates": ("list", "inspect", "promote", "deprecate"),
    "mutations": ("list", "undo"),
    "context": ("show",),
    "generated-capabilities": ("list", "show", "promote", "deprecate"),
}


def _recording_cli(monkeypatch):
    seen = []

    def dispatch(options):
        seen.append(options)
        return 0

    monkeypatch.setattr(app, "dispatch", dispatch)
    return app._click_cli(click), seen


@pytest.mark.parametrize("group", GROUP_ACTIONS)
def test_group_help_lists_nested_commands(monkeypatch, group):
    cli, _seen = _recording_cli(monkeypatch)
    result = CliRunner().invoke(cli, [group, "--help"], prog_name="athena")

    assert result.exit_code == 0, result.output
    assert "[COMMAND]" in result.output
    assert "Commands:" in result.output
    for action in GROUP_ACTIONS[group]:
        assert action in result.output


def test_typed_click_commands_forward_structured_options(monkeypatch):
    cli, seen = _recording_cli(monkeypatch)

    result = CliRunner().invoke(
        cli,
        ["jobs", "grant", "job-1", "task-1", "--operation", "run", "--expires-at", "2026-01-01"],
        prog_name="athena",
    )

    assert result.exit_code == 0, result.output
    options = seen[-1]
    assert options.command == "jobs"
    assert options.args == ["grant", "job-1", "task-1"]
    assert options.job_operations == ("run",)
    assert options.job_expires_at == "2026-01-01"


def test_provider_recovery_command_forwards_operator_evidence(monkeypatch):
    cli, seen = _recording_cli(monkeypatch)

    result = CliRunner().invoke(
        cli,
        [
            "inference-recoveries",
            "resolve",
            "attempt-1",
            "--resolution",
            "succeeded",
            "--note",
            "provider receipt verified",
            "--provider-response-id",
            "provider-1",
            "--actual-cost",
            "0.40",
        ],
        prog_name="athena",
    )

    assert result.exit_code == 0, result.output
    options = seen[-1]
    assert options.args == ["resolve", "attempt-1"]
    assert options.recovery_resolution == "succeeded"
    assert options.recovery_note == "provider receipt verified"
    assert options.recovery_provider_response_id == "provider-1"
    assert options.recovery_actual_cost == "0.40"


def test_click_validation_uses_usage_exit_code(monkeypatch):
    cli, _seen = _recording_cli(monkeypatch)

    result = CliRunner().invoke(
        cli, ["tasks", "list", "--status", "not-a-status"], prog_name="athena"
    )

    assert result.exit_code == 2
    assert "Invalid value for '--status'" in result.output


def test_click_keeps_legacy_workflow_scope_position(monkeypatch):
    cli, seen = _recording_cli(monkeypatch)

    result = CliRunner().invoke(
        cli, ["workflows", "describe", "workflow-1", "task-1"], prog_name="athena"
    )

    assert result.exit_code == 0, result.output
    assert seen[-1].args == ["show", "workflow-1", "task-1"]


def test_completion_command_emits_click_script(monkeypatch):
    cli, _seen = _recording_cli(monkeypatch)

    result = CliRunner().invoke(cli, ["completion", "bash"], prog_name="athena")

    assert result.exit_code == 0
    assert "_ATHENA_COMPLETE" in result.output
    assert "_athena_completion" in result.output


@pytest.mark.parametrize(
    ("argv", "expected_command", "expected_args"),
    [
        (["jobs", "show", "job-1"], "jobs", ["show", "job-1"]),
        (["tasks", "list", "--status", "CREATED"], "tasks", ["list"]),
        (["sessions", "close", "session-1"], "sessions", ["close", "session-1"]),
        (
            [
                "inference-recoveries",
                "resolve",
                "attempt-1",
                "--resolution",
                "retry",
                "--note",
                "checked",
            ],
            "inference-recoveries",
            ["resolve", "attempt-1"],
        ),
        (
            ["candidates", "promote", "candidate-1", "--scope", "project"],
            "candidates",
            ["promote", "candidate-1"],
        ),
    ],
)
def test_argparse_fallback_has_the_same_nested_shape(argv, expected_command, expected_args):
    options = app._arg_parse(argv)

    assert options.command == expected_command
    assert options.args == expected_args
