"""Neutral, outcome-oriented evaluation primitives."""

from athena.evaluation.benchmark import benchmark_environment, run_fixed_corpus_benchmark
from athena.evaluation.neutral import (
    EvaluationCase,
    EvaluationOutcome,
    NeutralEvaluationHarness,
    compare_outcomes,
    compare_delegation_outcomes,
)
from athena.evaluation.provider_portability import (
    ProviderParityObservation,
    compare_provider_parity,
)

__all__ = [
    "EvaluationCase",
    "EvaluationOutcome",
    "NeutralEvaluationHarness",
    "compare_outcomes",
    "compare_delegation_outcomes",
    "benchmark_environment",
    "run_fixed_corpus_benchmark",
    "ProviderParityObservation",
    "compare_provider_parity",
]
