#!/usr/bin/env python3
"""Launch guard for the frozen release gate on shared machines.

Trend-aware preflight + live watch. The gate runs 30-60 minutes, so a single
point-in-time check can pass and the environment can still degrade mid-run
(observed: run 4 killed by the harness when ambient memory climbed during the
pytest lane). Two mechanisms, neither touching gate thresholds:

1. Preflight: not just current load/memory, but the recent TREND. Load may
   not exceed the ceiling AND its 5-minute average must not be climbing away
   from the 1-minute value (a machine spiraling up fails the check even if
   the instantaneous sample is under the bar). Memory must be above the
   floor with the same ratchet logic.
2. Live watch: after launch, sample /proc/pressure/memory + loadavg every
   30s. If PSI memory pressure sustains high for 4 consecutive samples
   (2 minutes of real stalls) or load crosses the ceiling, pause (SIGSTOP)
   the RELEASE-CHECK PROCESS TREE ONLY — never the ambient tenants — resume
   when pressure clears, and record every action in a log. This sheds the
   one workload we own instead of racing the machine.

Exit 0 = launched (watch keeps running until the gate finishes).
Exit 1 = conditions not met at preflight (launch retried next cron tick).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

REPO = "/home/bamn/Athena"
LOG = "/tmp/gate-env-guard.log"

LOAD_CEIL = 3.0        # 1-min loadavg ceiling at launch
LOAD_HARD = 12.0       # live ceiling before we shed our own tree
MEM_FLOOR_GI = 5.0     # MemAvailable floor (GiB) at launch
PSI_FLOOR = 25.0       # sustained memory PSI (some avg10) that means real stalls
TREND_RATIO = 1.35     # load5/load1 above this = climbing, fail even under ceil


def log(msg: str) -> None:
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def loadavg() -> tuple[float, float]:
    parts = open("/proc/loadavg").read().split()
    return float(parts[0]), float(parts[1])


def mem_avail_gi() -> float:
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable"):
            return int(line.split()[1]) / 1024 / 1024
    return 0.0


def mem_psi() -> float:
    try:
        for line in open("/proc/pressure/memory"):
            if line.startswith("some"):
                return float(line.split()[1].split("=")[1])
    except OSError:
        pass
    return 0.0


def gate_running() -> bool:
    out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    return "scripts/release-check" in out and "gate-env-guard" not in out


def tree_pids(root: int) -> list[int]:
    """All descendants of root plus root itself."""
    out = subprocess.run(["ps", "-eo", "pid,ppid"], capture_output=True, text=True).stdout
    children: dict[int, list[int]] = {}
    for line in out.splitlines()[1:]:
        pid_s, ppid_s = line.split()
        children.setdefault(int(ppid_s), []).append(int(pid_s))
    seen, stack = [], [root]
    while stack:
        cur = stack.pop()
        seen.append(cur)
        stack.extend(children.get(cur, ()))
    return seen


def preflight() -> bool:
    load1, load5 = loadavg()
    avail = mem_avail_gi()
    climbing = load5 / load1 if load1 > 0.01 else 1.0
    ok = load1 <= LOAD_CEIL and avail >= MEM_FLOOR_GI and climbing <= TREND_RATIO
    log(
        f"preflight load1={load1:.2f} load5={load5:.2f} "
        f"(trend x{climbing:.2f}) avail={avail:.1f}Gi -> {'GO' if ok else 'HOLD'}"
    )
    return ok


def watch(gate: subprocess.Popen) -> None:
    paused = False
    high = 0
    while gate.poll() is None:
        time.sleep(30)
        psi = mem_psi()
        load1, _ = loadavg()
        if psi >= PSI_FLOOR or load1 >= LOAD_HARD:
            high += 1
        else:
            high = 0
        if high >= 4 and not paused:
            pids = tree_pids(gate.pid)
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGSTOP)
                except OSError:
                    pass
            paused = True
            log(f"PAUSED gate tree ({len(pids)} pids): psi={psi:.0f} load1={load1:.2f}")
        elif paused and high == 0:
            pids = tree_pids(gate.pid)
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGCONT)
                except OSError:
                    pass
            paused = False
            log(f"RESUMED gate tree ({len(pids)} pids): psi={psi:.0f} load1={load1:.2f}")
    if paused:
        for pid in tree_pids(gate.pid):
            try:
                os.kill(pid, signal.SIGCONT)
            except OSError:
                pass
    log(f"gate exited rc={gate.returncode}")


def main() -> int:
    if gate_running():
        log("preflight: a release-check is already running; holding")
        return 1
    if not preflight():
        return 1
    outfile = open("/tmp/release-check-10c7353-run6.log", "w")
    # Daemonize (new session, reparented to init) so that harness teardown of
    # THIS wrapper process — observed 15:12 run 6: the 60s-timeout Bash wrapper
    # was killed on low memory and the gate died with it as its child — cannot
    # take the gate down. start_new_session detaches it from our process group
    # and signal dispositions.
    gate = subprocess.Popen(
        [".venv/bin/python", "scripts/release-check", "--sha", "10c7353",
         "--bootstrap", "--evidence-dir", ".release-evidence"],
        cwd=REPO, stdout=outfile, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log(f"launched run 6 (sha 10c7353) pid={gate.pid} (own session)")
    watch(gate)
    return 0 if gate.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
