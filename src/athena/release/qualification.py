"""Host qualification for timing-sensitive release benchmarks."""

from __future__ import annotations

import os
from pathlib import Path
import re
import time
from typing import Any


def benchmark_environment() -> dict[str, Any]:
    """Capture the host facts that determine whether timing is meaningful."""
    cpus = max(1, int(os.cpu_count() or 1))
    load1 = 0.0
    try:
        load1 = float(os.getloadavg()[0])
    except (AttributeError, OSError):
        pass
    steal = 0.0
    try:
        fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()
        cpu = next(line for line in fields if line.startswith("cpu ")).split()
        total = sum(float(value) for value in cpu[1:])
        steal = float(cpu[8]) / total * 100.0 if total else 0.0
    except (OSError, StopIteration, IndexError, ValueError):
        pass
    memory_available_mb = None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                memory_available_mb = round(float(line.split()[1]) / 1024.0, 1)
                break
    except (OSError, IndexError, ValueError):
        pass
    release_instances = 0
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                command = (
                    (entry / "cmdline")
                    .read_bytes()
                    .replace(b"\x00", b" ")
                    .decode("utf-8", "replace")
                )
            except OSError:
                continue
            if re.search(r"release-check|bench-(?:alacrity|indexing|rendering)", command):
                release_instances += 1
    except OSError:
        pass
    return {
        "captured_at": time.time(),
        "cpus": cpus,
        "load1": round(load1, 3),
        "load_per_cpu": round(load1 / cpus, 3),
        "steal_percent": round(steal, 3),
        "memory_available_mb": memory_available_mb,
        "other_release_instances": max(0, release_instances - 1),
    }


def qualify_benchmark_environment() -> dict[str, Any]:
    """Return a qualification record; invalid hosts must not emit fake PASS."""
    record = benchmark_environment()
    reasons: list[str] = []
    if float(record["load_per_cpu"]) > 0.90:
        reasons.append("load_per_cpu>0.90")
    if float(record["steal_percent"]) > 5.0:
        reasons.append("cpu_steal>5%")
    available = record.get("memory_available_mb")
    if available is not None and float(available) < 512.0:
        reasons.append("memory_available<512MB")
    if int(record["other_release_instances"]) > 0:
        reasons.append("another_release_instance_active")
    record["status"] = "BENCH_INVALID_ENVIRONMENT" if reasons else "QUALIFIED"
    record["reasons"] = reasons
    return record


__all__ = ["benchmark_environment", "qualify_benchmark_environment"]
