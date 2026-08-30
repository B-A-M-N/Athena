# Athena self-hosting

Athena improves its own source through one bounded, reviewable loop:

**PLAN → PATCH → PROVE → PROMOTE**

## PLAN

Athena selects one bounded work item grounded in the current source,
source-verified project index, frozen design contracts, and current proof.

## PATCH

The ordinary coding task writes only to an isolated speculative candidate.
The live Athena checkout is unchanged.

## PROVE

The candidate runs the service-owned verification gates plus frozen-base safety
proof. Candidate source is imported explicitly, the candidate is disposable
during verification, and the proof is retained as a certificate.

## PROMOTE

Athena shows the diff, proof, risk, and review evidence. Only the human may
apply or discard the exact certified candidate through the canonical shadow
commit path. After promotion, start a new `athena self continue` process so
the newly changed Athena code is actually loaded.

The operator surface remains intentionally small:

```text
athena self "one bounded improvement"
athena self continue
athena self status
```

To provision the local Hermes Agent referee, use the Hermes checkout explicitly:

```bash
athena referee setup --hermes-root /path/to/hermes-agent
athena self status
```

The named `athena-referee` profile should be the profile clone you prepared in
Hermes. Provisioning pins the service to the supplied Hermes runtime, binds
the API to `127.0.0.1:8643`, disables messaging/MCP/background paths, installs
the user service, and proves `/models`, `/capabilities`, and one structured
review before Athena is enabled. The capability proof requires
`runtime.mode: referee`, `runtime.tool_execution: disabled`,
`referee.effective_tools: []`, and `build.referee_contract: 1`; late MCP
refreshes cannot repopulate the effective tool surface.

The generated bearer key is kept in the owner-only user secret store at
`~/.config/athena/secrets/HERMES_REFEREE_API_KEY` and is never placed in
`config.toml`. Use `athena referee status`, `athena referee repair`, or
`athena referee disable` for lifecycle operations.

`athena self status` reports Athena proof readiness, Hermes connectivity,
separate read-only safety verification, profile, and the invariant that human
promotion remains required. Hermes is called only at semantic
candidate/mission checkpoints. It receives a bounded read-only `ReviewPacket`;
a missing, malformed, or failed response holds the review closed.

## Completion and performance gates

Candidate verification is diff-targeted, but mission completion is stricter:
the service-owned completion verifier runs all three performance proofs against
the promoted source before it can create a complete mission record:

- event-stream alacrity;
- project indexing, measured with three samples (median target ≤5 seconds,
  hard maximum 8 seconds) and incremental updates (median target ≤500 ms,
  hard maximum 1 second); and
- native/TUI rendering, including idle redraw and CPU bounds.

The benchmark output records process CPU, logical CPU count, and load average.
Performance proof identities are semantic, so equivalent command wrappers remain
the same proof and release-check is the authoritative final gate.
