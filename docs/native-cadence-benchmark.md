# Native presentation cadence benchmark (review item 24)

## Question

`ACTIVE_FRAME_INTERVAL` is 100 ms (~10 Hz animation). Should OI presentation
move to 50 ms, and should the per-frame `XSync` be unconditional?

## Changes landed (measurement-first, behavior-preserving by default)

1. `ATHENA_NATIVE_FRAME_INTERVAL_MS` env override (clamped 16–1000 ms;
   default stays 100 ms). All pacing paths (`active_frame_interval()`) use it.
2. Per-frame `XSync` is now **capture-gated**: it runs only when a capture
   consumer is configured (`ATHENA_NATIVE_PRESENTATION_SYNC`,
   `ATHENA_NATIVE_LAYOUT_DUMP`, or `ATHENA_NATIVE_OI_DUMP`). The comment says
   the sync exists for the external capture contract; production presentation
   no longer pays the round-trip when no consumer exists.
3. `--benchmark-frames N`: binary exits after N presented frames so a fixed
   work unit can be measured.
4. `scripts/bench-native-cadence` runs 30 frames at 100 ms vs 50 ms under the
   ambient `DISPLAY`. It requires an X display (normally
   `xvfb-run -s '-screen 0 1024x768x24' scripts/bench-native-cadence`) and
   prints the `ATHENA_NATIVE_RENDER_STATS` JSON for both intervals.
5. Benchmark mode now requests an idle redraw at the configured cadence until
   the fixed frame count is reached. Without this, an idle projection reached
   its terminal state after startup and could not produce a 30-frame unit.

## Qualified local run (2026-09-19, Linux Xvfb 1.21.1.4)

Command: `DISPLAY=<xvfb> scripts/bench-native-cadence`

| Interval | Total CPU s | Elapsed s | Redraws | Initial | Idle | Steady CPU s |
|---:|---:|---:|---:|---:|---:|---:|
| 100 ms | 3.007597 | 3.079148 | 30 | 1 | 29 | 0.001040 |
| 50 ms | 3.545120 | 1.701825 | 30 | 1 | 29 | 0.000913 |

Interpretation:

- The 50 ms cadence completed the fixed 30-frame unit in 1.702 s versus
  3.079 s at 100 ms, as expected.
- Whole-process CPU rose from 3.008 s to 3.545 s (~17.9% more CPU over this
  workload), mostly outside the measured steady interval.
- Steady-state CPU for the idle 29-frame window was effectively equal:
  1.040 ms at 100 ms and 0.913 ms at 50 ms (the latter is lower for this
  sample).

Decision: **do not adopt 50 ms as the global default.** The doubling of frame
rate increases total CPU on this host without a demonstrated presentation
need. Keep the 100 ms default; operators can opt into the existing
`ATHENA_NATIVE_FRAME_INTERVAL_MS=50` override. Keep capture-gated `XSync`.
