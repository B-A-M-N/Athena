"""Canonical command lanes shared by candidate proof and release checks."""

from __future__ import annotations


# Lane -> stage mapping. Stages exist so a wall-clock benchmark failure does
# not cost a full integration run and vice versa: each stage is independently
# runnable and re-runnable, and the default full gate still covers every lane.
LANE_STAGES: dict[str, str] = {
    # environment + budgets: timing-sensitive, want a quiet machine
    "uv-version": "bench",
    "python-version": "bench",
    "cargo-version": "bench",
    "rustc-version": "bench",
    "alacrity-benchmark": "bench",
    "indexing-benchmark": "bench",
    "rendering-benchmark": "bench",
    # deterministic source checks: cheapest, run first everywhere
    "ruff-format": "static",
    "ruff-check": "static",
    "uv-lock-check": "static",
    "mypy": "static",
    "dependency-audit": "static",
    "compileall": "static",
    "architecture-lint": "static",
    # test evidence
    "pytest": "tests",
    "pytest-performance": "tests",
    "functional-proof": "tests",
    "release-scenarios": "tests",
    # heavy integration: artifacts, native, E2E, sandboxes
    "native-fetch": "integration",
    "native-check": "integration",
    "native-test": "integration",
    "release-artifacts": "integration",
    "native-smoke": "integration",
    "e2e": "integration",
    "sandbox-matrix": "integration",
    "workflow-strategy": "integration",
    "mcp-stdio": "integration",
    "native-input-smoke": "integration",
    "native-visual-smoke": "integration",
    "native-desktop-acceptance": "integration",
    "hermes-live": "integration",
}

VALID_STAGES = ("static", "tests", "bench", "integration")

FUNCTIONAL_PROOF_NODEIDS = (
    "tests/integration/test_end_to_end.py::test_full_loop_returns_complete_with_answer",
    "tests/e2e/test_real_execution.py::test_persistent_python_session_keeps_state",
    "tests/e2e/test_session_resume.py::test_resume_session_sees_prior_transcript",
    "tests/integration/test_memory_recall.py::test_explicit_remember_request_is_retrievable_on_next_turn",
    "tests/integration/test_scheduler_execution.py::test_scheduled_job_fires_and_runs_to_complete",
    "tests/unit/capabilities/test_reality_coordinator.py::test_simple_patch_reaches_real_workspace_only_after_verification",
)


def lane_stage(name: str) -> str:
    return LANE_STAGES.get(name, "integration")


def xdist_workers(default: str = "6") -> str:
    """Worker count for the parallel pytest lane.

    ``auto`` is deliberately NOT the default: at full 20-way parallelism on a
    loaded machine, timing-sensitive subprocess tests (kill outcomes, repair
    receipts, soak restarts) flake under scheduler contention — observed three
    different tests failing across -n 12 runs while each passes solo and at
    -n 6. The release gate values truthful failures over maximum throughput.
    Operators on quiet machines can opt into more via ATHENA_XDIST_N=auto.
    """
    import os

    return os.environ.get("ATHENA_XDIST_N", default)


def candidate_commands() -> tuple[str, ...]:
    """Commands required before a self-host candidate can be reviewed."""
    return (
        "uv run --frozen --no-sync ruff format --check --no-cache src tests",
        "uv run --frozen --no-sync ruff check --no-cache src tests",
        "uv run --frozen --no-sync mypy --cache-dir /tmp/athena-mypy-cache src",
        "uv --version",
        "uv run --frozen --no-sync python --version",
        "uv lock --check --offline",
        "uv run --frozen --no-sync python scripts/architecture-lint",
        "uv run --frozen --no-sync python scripts/scenarios --exclude-family VHS --output /tmp/athena-self-scenarios.json",
        "cargo check --manifest-path native/Cargo.toml --locked --offline",
        "cargo test --manifest-path native/Cargo.toml --locked --offline",
        "scripts/build-native-package",
        "cargo --version",
        "rustc -vV",
        "scripts/native-smoke",
        "uv run --frozen --no-sync python scripts/bench-alacrity --events 5000 --min-producer-events-per-second 10000",
        "uv run --frozen --no-sync python scripts/bench-indexing --samples 3 --max-full-seconds 5 --hard-max-full-seconds 8 --max-cold-start-seconds 8 --max-incremental-seconds 0.5 --hard-max-incremental-seconds 1",
        "uv run --frozen --no-sync python scripts/bench-rendering --max-scene-p95-ms 2 --max-native-projection-p95-ms 5 --max-idle-redraws-per-second 0.1 --max-idle-cpu-percent 2 --max-active-fps 25 --max-cache-bytes 16777216 --require-native",
        "uv run --frozen --no-sync pytest -p no:cacheprovider -q",
        "uv run --frozen --no-sync --extra dev python scripts/dependency-audit",
        "uv run --frozen --no-sync pytest -p no:cacheprovider -q tests/e2e/test_release_black_box.py",
        "scripts/sandbox-release-matrix",
        "uv run --frozen --no-sync pytest -p no:cacheprovider -q tests/e2e/test_workflow_strategy.py",
        "scripts/native-input-smoke",
        "scripts/native-visual-smoke",
        "scripts/native-desktop-acceptance",
    )


def release_commands(
    uv: str,
    *,
    skip_e2e: bool,
    bootstrap: bool,
    include_hermes_live: bool = False,
    stage: str | None = None,
) -> tuple[tuple[str, list[str]], ...]:
    """Return core lanes, with live Hermes evidence opt-in.

    ``stage`` optionally filters to one of ``static | tests | bench |
    integration``. ``None`` (the default) returns every lane — the full gate.
    """
    prefix = [uv, "run", "--frozen", "--extra", "dev"]
    commands: list[tuple[str, list[str]]] = [
        ("uv-version", [uv, "--version"]),
        ("python-version", [*prefix, "python", "--version"]),
        ("cargo-version", ["cargo", "--version"]),
        ("rustc-version", ["rustc", "-vV"]),
        (
            "alacrity-benchmark",
            [
                *prefix,
                "python",
                "scripts/bench-alacrity",
                "--events",
                "5000",
                "--min-producer-events-per-second",
                "10000",
            ],
        ),
        (
            "indexing-benchmark",
            [
                *prefix,
                "python",
                "scripts/bench-indexing",
                "--samples",
                "3",
                "--max-full-seconds",
                "5",
                "--hard-max-full-seconds",
                "8",
                "--max-cold-start-seconds",
                "8",
                "--max-incremental-seconds",
                "0.5",
                "--hard-max-incremental-seconds",
                "1",
                "--max-ten-file-seconds",
                "0.5",
                "--max-source-revision-ms",
                "500",
                "--max-impact-ms",
                "50",
            ],
        ),
        (
            "rendering-benchmark",
            [
                *prefix,
                "python",
                "scripts/bench-rendering",
                "--max-scene-p95-ms",
                "2",
                "--max-native-projection-p95-ms",
                "5",
                "--max-idle-redraws-per-second",
                "0.1",
                "--max-idle-cpu-percent",
                "2",
                "--max-active-fps",
                "25",
                "--max-cache-bytes",
                str(16 * 1024 * 1024),
                "--require-native",
            ],
        ),
        ("ruff-format", [*prefix, "ruff", "format", "--check", "--no-cache", "src", "tests"]),
        ("ruff-check", [*prefix, "ruff", "check", "--no-cache", "src", "tests"]),
        ("uv-lock-check", ["uv", "lock", "--check", "--offline"]),
        ("mypy", [*prefix, "mypy", "src/athena"]),
        (
            "dependency-audit",
            [*prefix, "python", "scripts/dependency-audit"],
        ),
        ("compileall", [*prefix, "python", "-m", "compileall", "-q", "src", "tests"]),
        (
            "pytest",
            [
                *prefix,
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--ignore=tests/e2e",
                "--ignore=tests/performance",
                "-n",
                xdist_workers(),
            ],
        ),
        (
            "functional-proof",
            [*prefix, "python", "-m", "pytest", "-q", *FUNCTIONAL_PROOF_NODEIDS],
        ),
        # Wall-clock budget tests measure real latency; they run serially so
        # their measurement is not fighting other workers for CPU.
        (
            "pytest-performance",
            [*prefix, "pytest", "-q", "-p", "no:cacheprovider", "tests/performance"],
        ),
        (
            "release-scenarios",
            [
                *prefix,
                "python",
                "scripts/scenarios",
                "--require-clean",
                "--exclude-family",
                "VHS",
                "--output",
                "release-scenarios.json",
            ],
        ),
        ("architecture-lint", [*prefix, "python", "scripts/architecture-lint"]),
    ]
    if bootstrap:
        commands.append(
            (
                "native-fetch",
                ["cargo", "fetch", "--manifest-path", "native/Cargo.toml", "--locked"],
            )
        )
    commands.extend(
        [
            (
                "native-check",
                [
                    "cargo",
                    "check",
                    "--manifest-path",
                    "native/Cargo.toml",
                    "--locked",
                    "--offline",
                ],
            ),
            (
                "native-test",
                [
                    "cargo",
                    "test",
                    "--manifest-path",
                    "native/Cargo.toml",
                    "--locked",
                    "--offline",
                ],
            ),
            (
                "release-artifacts",
                ["scripts/build-release-artifacts", "--output-dir", "release-artifacts"],
            ),
            ("native-smoke", ["scripts/native-smoke"]),
        ]
    )
    if not skip_e2e:
        commands.append(
            (
                "e2e",
                [
                    *prefix,
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "tests/e2e/test_artifact_store_wiring.py",
                    "tests/e2e/test_failure_semantics.py",
                    "tests/e2e/test_release_black_box.py",
                    "tests/e2e/test_real_execution.py",
                    "tests/e2e/test_session_resume.py",
                    "tests/e2e/test_self_host_continuation.py",
                ],
            )
        )
        commands.extend(
            [
                (
                    "sandbox-matrix",
                    [
                        "scripts/sandbox-release-matrix",
                    ],
                ),
                (
                    "workflow-strategy",
                    [
                        *prefix,
                        "pytest",
                        "-q",
                        "-p",
                        "no:cacheprovider",
                        "tests/e2e/test_workflow_strategy.py",
                    ],
                ),
                (
                    "mcp-stdio",
                    [
                        uv,
                        "run",
                        "--frozen",
                        "--extra",
                        "dev",
                        "--extra",
                        "mcp",
                        "pytest",
                        "-q",
                        "-p",
                        "no:cacheprovider",
                        "tests/e2e/test_mcp_transport.py",
                    ],
                ),
                ("native-input-smoke", ["scripts/native-input-smoke"]),
                ("native-visual-smoke", ["scripts/native-visual-smoke"]),
                ("native-desktop-acceptance", ["scripts/native-desktop-acceptance"]),
            ]
        )
        if include_hermes_live:
            commands.append(
                (
                    "hermes-live",
                    [
                        *prefix,
                        "pytest",
                        "-q",
                        "-p",
                        "no:cacheprovider",
                        "tests/e2e/test_hermes_agent.py",
                    ],
                )
            )
    if stage is not None:
        if stage not in VALID_STAGES:
            raise ValueError(f"unknown release stage: {stage!r}")
        commands = [item for item in commands if lane_stage(item[0]) == stage]
    return tuple(commands)


__all__ = ["candidate_commands", "release_commands"]
