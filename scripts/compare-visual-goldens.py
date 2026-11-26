#!/usr/bin/env python3
"""Compare a captured visual-golden candidate set against the committed baselines.

Prints the complete per-fixture RMSE table (candidate captures in GOLDEN_DIR
versus the checked-in approved goldens) and writes
``<GOLDEN_DIR>/rmse-table.csv``.  Read-only with respect to the repository:
this is evidence for golden review, never a regeneration path.

Exit status is 0 even when fixtures are over tolerance; the RMSE-tolerance
gate is scripts/native-visual-smoke.  This report exists so over-tolerance
fixtures can be classified (intended design change vs regression vs
staleness) instead of being merged blindly.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED_DIR = REPO_ROOT / "native/assets/oi/visual-goldens"
TOLERANCE = 0.08


def rmse(baseline: Path, captured: Path) -> str:
    proc = subprocess.run(
        ["compare", "-metric", "RMSE", str(baseline), str(captured), "null:"],
        capture_output=True,
        text=True,
        check=False,
    )
    raw = proc.stderr.strip()
    if raw == "":
        return "0" if proc.returncode == 0 else "n/a"
    return raw.split("(")[-1].rstrip(")") if "(" in raw else raw


def main() -> int:
    golden_dir = Path(os.environ.get("GOLDEN_DIR") or "").resolve()
    if not golden_dir.is_dir():
        print(f"compare-visual-goldens: candidate dir {golden_dir} does not exist", file=sys.stderr)
        return 2

    committed = json.loads((COMMITTED_DIR / "manifest.json").read_text(encoding="utf-8"))
    required = committed["required_pngs"]

    rows: list[tuple[str, str, str]] = []
    for name in sorted(required):
        captured = golden_dir / name
        baseline = COMMITTED_DIR / name
        if not captured.is_file():
            rows.append((name, "MISSING", "OVER"))
            continue
        if not baseline.is_file():
            rows.append((name, "NO-COMMITTED-BASELINE", "OVER"))
            continue
        value = rmse(baseline, captured)
        if value == "n/a":
            rows.append((name, value, "OVER"))
            continue
        over = float(value) > TOLERANCE
        rows.append((name, value, "OVER" if over else "ok"))

    print("=== per-fixture RMSE (committed approved golden vs candidate capture) ===")
    table_path = golden_dir / "rmse-table.csv"
    with table_path.open("w", encoding="utf-8") as handle:
        handle.write("fixture,rmse,status\n")
        for name, value, status in rows:
            print(f"{status:>4}  {value:>8}  {name}")
            handle.write(f"{name},{value},{status}\n")
    over_count = sum(1 for _, _, status in rows if status == "OVER")
    print(f"=== fixtures over/missing: {over_count} of {len(rows)} ===")

    cabinet_captured = Path(
        os.environ.get("CABINET_CANDIDATE", "")
        or (golden_dir.parent / "cabinet-golden.png")
    )
    cabinet_baseline = REPO_ROOT / "native/assets/athenabox/cabinet-golden.png"
    if cabinet_captured.is_file() and cabinet_baseline.is_file():
        print(f"cabinet RMSE (committed vs candidate): {rmse(cabinet_baseline, cabinet_captured)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
