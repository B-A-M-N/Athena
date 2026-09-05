"""Indexing-SLO parity lane (P1-12).

The canonical release gate (``scripts/bench-indexing`` through
``src/athena/release/gates.py``) enforces indexing budgets that the rest
of CI never measured: the performance lane passed while
``./scripts/release-check`` failed ``bench-indexing``. This lane pins the
same budgets as stable regression tests over a deterministic synthetic
repository, so an indexing-envelope regression surfaces in normal CI —
not only at release time.

Budgets mirror the release gate exactly:

* single-file incremental  <= 0.500s
* ten-file incremental     <= 0.500s
* source revision          <= 500ms
* impact lookup            <= 50ms

Full-build budgets are intentionally NOT asserted here: absolute
whole-repository build time depends on repo size and machine load, so a
CI assertion on them would flake across runners. The incremental
budgets are the load-sensitive ones the review called out, and they are
measured against a FIXED synthetic workspace, keeping repo size out of
the variance. Machine metadata (cpu count, load average, file count) is
printed with every result so a CI regression can be distinguished from
a loaded runner rather than silently waived.
"""

from __future__ import annotations

import os
import time

import pytest

from athena.project.index.builder import ProjectIndexBuilder

_MAX_ONE_FILE_SECONDS = 0.500
_MAX_TEN_FILE_SECONDS = 0.500
_MAX_SOURCE_REVISION_MS = 500.0
_MAX_IMPACT_MS = 50.0

_REPO_FILES = 150
_FUNCS_PER_FILE = 12


def _synthetic_repo(root: str) -> None:
    """Deterministic Python workspace (~150 files, 12 callables each).

    Deterministic content keeps token counts and reference maps stable
    across runs, so timing variance comes only from the machine.
    """
    os.makedirs(os.path.join(root, "pkg"), exist_ok=True)
    for i in range(_REPO_FILES):
        parts = ["import os\nimport sys\n\n"]
        for j in range(_FUNCS_PER_FILE):
            parts.append(
                f"def fn_{i}_{j}(a, b):\n"
                f'    """Compute thing {i}.{j}."""\n'
                "    total = 0\n"
                "    for k in range(len(a)):\n"
                f"        total += a[k] * b[{j}] + k\n"
                "    return total\n"
                "\n\n"
                f"class C_{i}_{j}:\n"
                "    def method(self, x):\n"
                f"        return x + {j}\n"
                "\n\n"
            )
        with open(os.path.join(root, "pkg", f"mod_{i}.py"), "w", encoding="utf-8") as fh:
            fh.write("".join(parts))


@pytest.fixture
def indexed_repo(tmp_path):
    root = str(tmp_path / "slo-repo")
    _synthetic_repo(root)
    builder = ProjectIndexBuilder()
    index = builder.build(root)
    candidates = [
        str(item["path"]) for item in index.files if str(item.get("path") or "").endswith(".py")
    ]
    assert len(candidates) >= 10, "synthetic repo must yield enough indexable files"
    return builder, root, index, candidates


def _metadata() -> dict:
    return {
        "logical_cpus": os.cpu_count(),
        "load_average": (
            [round(v, 3) for v in os.getloadavg()] if hasattr(os, "getloadavg") else None
        ),
    }


@pytest.mark.athena_evidence("test", "performance")
def test_single_file_incremental_within_release_budget(indexed_repo):
    builder, root, index, candidates = indexed_repo
    started = time.perf_counter()
    builder.incremental(root, index, candidates[:1])
    elapsed = time.perf_counter() - started
    print(
        f"[slo] one-file incremental {elapsed:.3f}s (limit {_MAX_ONE_FILE_SECONDS}s) "
        f"files={len(index.files)} {_metadata()}"
    )
    assert elapsed <= _MAX_ONE_FILE_SECONDS


@pytest.mark.athena_evidence("test", "performance")
def test_ten_file_incremental_within_release_budget(indexed_repo):
    """The exact budget the release gate failed at HEAD 2fa65ba (1.675s)."""
    builder, root, index, candidates = indexed_repo
    started = time.perf_counter()
    builder.incremental(root, index, candidates[:10])
    elapsed = time.perf_counter() - started
    print(
        f"[slo] ten-file incremental {elapsed:.3f}s (limit {_MAX_TEN_FILE_SECONDS}s) "
        f"files={len(index.files)} {_metadata()}"
    )
    assert elapsed <= _MAX_TEN_FILE_SECONDS


@pytest.mark.athena_evidence("test", "performance")
def test_source_revision_within_release_budget(indexed_repo):
    builder, root, index, candidates = indexed_repo
    started = time.perf_counter()
    builder.source_revision(root)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    print(
        f"[slo] source_revision {elapsed_ms:.1f}ms (limit {_MAX_SOURCE_REVISION_MS}ms) "
        f"files={len(index.files)} {_metadata()}"
    )
    assert elapsed_ms <= _MAX_SOURCE_REVISION_MS


@pytest.mark.athena_evidence("test", "performance")
def test_impact_lookup_within_release_budget(indexed_repo):
    builder, root, index, candidates = indexed_repo
    refreshed = builder.incremental(root, index, candidates[:10])
    started = time.perf_counter()
    refreshed.impact(candidates[:10])
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    print(
        f"[slo] impact {elapsed_ms:.2f}ms (limit {_MAX_IMPACT_MS}ms) "
        f"files={len(index.files)} {_metadata()}"
    )
    assert elapsed_ms <= _MAX_IMPACT_MS
