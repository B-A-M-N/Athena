"""Fixed-corpus benchmark reports with explicit environment and outcomes."""

from __future__ import annotations

import os
import platform
import time
from collections.abc import Mapping, Sequence
from typing import Any

from athena.evaluation.neutral import EvaluationCase, NeutralEvaluationHarness

__all__ = ["benchmark_environment", "run_fixed_corpus_benchmark"]


def benchmark_environment() -> dict[str, Any]:
    """Return non-secret host facts needed to interpret benchmark timings."""
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "system": platform.system(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "pid": os.getpid(),
    }


async def run_fixed_corpus_benchmark(
    cases: Sequence[EvaluationCase],
    adapters: Mapping[str, Any],
    *,
    repetitions: int = 1,
) -> dict[str, Any]:
    """Run a fixed corpus and preserve measured dimensions without a score.

    The harness measures latency and the supplied outcome records contain
    model/tool/token/cost dimensions. A repetition is a loop over the same
    immutable corpus; the report keeps every observation so a regression can
    be compared rather than hidden behind an average.
    """
    if repetitions < 1 or repetitions > 16:
        raise ValueError("repetitions must be between 1 and 16")
    started = time.perf_counter()
    observations: list[dict[str, Any]] = []
    for index in range(repetitions):
        report = await NeutralEvaluationHarness().run(cases, adapters)
        for case_report in report["cases"]:
            observations.append(
                {
                    "repetition": index + 1,
                    "case": case_report["case"],
                    "systems": case_report["systems"],
                }
            )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    case_ids = [case.id for case in cases]
    passed = sum(
        1
        for observation in observations
        for system in observation["systems"].values()
        if system["eligible_as_complete"]
    )
    return {
        "schema": "athena.evaluation.benchmark.v1",
        "environment": benchmark_environment(),
        "case_count": len(case_ids),
        "repetitions": repetitions,
        "observation_count": len(observations),
        "elapsed_ms": round(elapsed_ms, 3),
        "observations": observations,
        "systems": sorted(adapters),
        "completion_count": passed,
        "completion_rate": passed / max(1, len(observations) * len(adapters)),
        "timing_qualified": "observed",
        "limitations": (
            "This is a fixed-corpus local benchmark, not a matched Hermes or live-provider "
            "comparison."
        ),
    }
