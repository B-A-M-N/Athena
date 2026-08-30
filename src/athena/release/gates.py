"""Canonical command lanes shared by candidate proof and release checks."""

from __future__ import annotations


def candidate_commands() -> tuple[str, ...]:
    """Commands required before a self-host candidate can be reviewed."""
    return (
        "uv run --frozen --no-sync ruff format --check --no-cache src tests",
        "uv run --frozen --no-sync ruff check --no-cache src tests",
        "uv run --frozen --no-sync mypy --cache-dir /tmp/athena-mypy-cache src",
        "uv run --frozen --no-sync python --version",
        "uv lock --check --offline",
        "uv run --frozen --no-sync pytest -p no:cacheprovider -q",
        "uv run --frozen --no-sync python scripts/architecture-lint",
        "uv run --frozen --no-sync python scripts/scenarios --exclude-family VHS --output /tmp/athena-self-scenarios.json",
        "cargo check --manifest-path native/Cargo.toml --locked --offline",
        "cargo test --manifest-path native/Cargo.toml --locked --offline",
        "cargo --version",
        "rustc --version",
        "scripts/native-smoke",
        "uv run --frozen --no-sync pytest -p no:cacheprovider -q tests/e2e/test_release_black_box.py",
    )


def release_commands(
    uv: str,
    *,
    skip_e2e: bool,
    bootstrap: bool,
) -> tuple[tuple[str, list[str]], ...]:
    """Return the release lanes without duplicating them in shell glue."""
    prefix = [uv, "run", "--frozen", "--extra", "dev"]
    commands: list[tuple[str, list[str]]] = [
        ("ruff-format", [*prefix, "ruff", "format", "--check", "src", "tests"]),
        ("ruff-check", [*prefix, "ruff", "check", "src", "tests"]),
        ("uv-lock-check", ["uv", "lock", "--check", "--offline"]),
        ("mypy", [*prefix, "mypy", "src/athena"]),
        ("compileall", [*prefix, "python", "-m", "compileall", "-q", "src", "tests"]),
        ("pytest", [*prefix, "pytest", "-q", "-p", "no:cacheprovider", "--ignore=tests/e2e"]),
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
                "--max-full-seconds",
                "5",
                "--max-incremental-seconds",
                "0.5",
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
                    "tests/e2e/test_release_black_box.py",
                    "tests/e2e/test_self_host_continuation.py",
                ],
            )
        )
    return tuple(commands)


__all__ = ["candidate_commands", "release_commands"]
