# Public-beta remediation ledger

This is the implementation ledger for the audit. A capability is considered
implemented only when its state is durable, ownership is explicit, failure is
truthful, and a product-shaped proof exercises the live path.

| Area | Implementation status | Evidence |
|---|---|---|
| Continuation authority and approvals | Complete | Durable input rows, CAS answer consumption, per-task coordination, restart-safe relaunch, and race/barrier coverage. |
| Principal identity | Complete | `AthenaConfig.cache_namespace` is the single default principal source; dispatcher, context, memory, history, workflows, skills, generated capabilities, reflection, and service APIs consume it. |
| Context continuity | Complete for bounded beta scope | Hierarchical durable digests retain objective, decisions, work, runtime state, evidence, artifacts, failures, acceptance state, transcript anchors, and recovery queries. |
| Compression | Complete for bounded beta scope | Omitted ranges are represented by structured digest state and explicit recovery anchors; compression never substitutes a tail-only summary. |
| Memory retrieval | Complete when semantic extra is installed | Durable lexical/BM25, real FastEmbed CPU semantic retrieval, hybrid reciprocal-rank fusion, content/model/version invalidation, explicit unavailable errors, and lazy backfill. |
| Evidence-linked learning | Complete | Candidate promotion requires durable provenance/proof; conclusions retain source/evidence links and conflict state. |
| Principal-scoped history | Complete | Historical transcript search is scoped to the configured principal and preserves source-session provenance. |
| Runtime continuity | Complete for certified backend scope | Backend/runtime identity is durable, workspace/network bounded, cancellation is process-tree verified, and the supervised local/container sessions expose proof-based reattachment; release qualification binds an executable `BackendPassport` to the SHA/run and reflection surfaces its per-runtime result. |
| Browser governance | Complete for supported browser path; restricted Playwright requires a pinned proxy | Browser sessions are task/session scoped and shutdown-cleaned. URL interception validates redirects/subresources, while restricted Playwright networking fails closed unless the driver advertises an Athena-controlled DNS-pinned proxy. |
| Research fabric | Complete for bounded live path | Real discover → fetch → artifact → evidence → verification → contradiction → bundle journey passes with DNS-pinned, allowlisted policy. Autonomous rounds are byte-bounded and gap-driven; evidence-required tasks remain partial until a ready bundle receipt exists. |
| Skills, workflows, delegation, MCP | Complete for beta proof scope | Bundled skills, lifecycle/promotion, durable workflow receipts, delegation ownership, real MCP stdio discovery/call/reconnect, and failure bounds are exercised. |
| Computer observation | Complete for supported backend | PNG capture is persisted as a provenance-backed artifact and exposed as model image input; unavailable backends report unavailable rather than success. |
| Release identity | Complete as a gate | Wheel, sdist, native wheel, hashes, source identity, ABI policy, signed provenance, BackendPassport, generated support matrix, and run identity are bound by the release evidence; mismatches fail closed. |

## Verification snapshot

Evidence below was produced from the current implementation checkout. The
checkout is intentionally dirty, so this is not a frozen release claim.

- Focused remediation tests pass for MCP/Anthropic compatibility, credential
  endpoint protection, durable accounting, memory history filtering, pack
  provenance, checkpoint manifests, conformance passports, reflection,
  research evidence, neutral evaluation, context fidelity, derived proofs,
  release lanes, and OI semantic runtime facts.
- Full Python unit suite: `1608 passed, 13 skipped`; contract/security suites:
  `149 passed`; performance suite: `8 passed`.
- Real E2E suite with local socket/network capability: `34 passed, 1 skipped`.
  The MCP HTTP lane includes a proxy canary; targeted Anthropic compatibility
  tests exercise the pinned SDK route with custom headers. The sandbox-only E2E
  attempt correctly failed closed where socket creation/network dependency
  resolution was denied.
- Optional MCP and Anthropic extras are lock-pinned to MCP `2.1.1` and
  Anthropic `1.0.0`; compatibility adapters are version-gated at the SDK
  boundary.
- Native Rust tests: `56 passed`; delayed EWMH fallback and native projection
  coverage are included in that lane.
- Static gates: Ruff check, Ruff format on `src/` and `tests/`, mypy across 344
  source files, architecture lint, static-critical async/security checks,
  support-matrix validation, migration digest manifest validation, compileall,
  and `git diff --check` pass. The formatter still reports unrelated drift in
  legacy Markdown and auxiliary scripts outside the source/test gate.
- Context-fidelity receipt: 50k/100k/250k-token cases pass with all anchors
  preserved and ordered through three deterministic compaction rounds. Backend
  passport: local shell/node/python cells pass, including reattach,
  cancellation/process-tree cleanup, environment handling, and the advertised
  containment cells.
- Live desktop receipt: GNOME Shell on X11 passes the EWMH/adaptive header
  moveresize, real clipboard, focus loss/return, eight-way resize, unmap/remap,
  live layout, presentation controls, CRT, and clean-close lane. The receipt is
  retained as `release-evidence/native-desktop-live.json` and is explicitly
  bound to the current dirty-checkout run identity.
- Rust supply-chain receipt: pinned `cargo-deny 0.20.2`, advisories/bans/
  licenses/sources/locked metadata pass, and the RustSec database is fresh.
- The clean-install/upgrade/rollback lane now installs the built Athena and
  native wheels into an isolated environment, reopens durable state, and
  exercises a failed migration followed by retry; it passes against the
  generated artifacts. The six-hour endurance lane was not run in this dirty
  checkout and must not be inferred from the unit results.
- The real beta endurance profile completed with `PASS`: 16 crash/restart soak
  cycles over 30 minutes, zero cycle failures, stable file descriptors,
  threads, child processes, continuations, runtime sessions, SQLite growth, and
  event-loop lag, with RSS slope below the configured threshold. The runner
  propagates the frozen interpreter/source path and uses a finite 600-second
  cycle bound that covers the soak matrix's documented recovery envelope.
- Durable inference receipts are first-writer-wins, retain broker-derived
  actual usage, reconcile budget/provider rows by attempt ID, and reconstruct a
  missing provider-usage row during replay. Legacy two-column migration ledgers
  and failed database startup/retry paths have explicit regression coverage.

## Release boundary

Do not publish the checked-in scenario manifest as release evidence from this
working tree. A clean release requires a new commit, a clean checkout, a
fresh artifact build, and a new evidence run bound to that exact commit/run
pair. The current implementation has no valid frozen SHA claim.

The release gate now also generates a canonical `athena_release_provenance`
payload over the frozen release manifest and every distribution hash, signs it
with the configured Sigstore/cosign mechanism, binds the executable backend
passport and generated support matrix to the same SHA/run, and exposes
`scripts/verify-release-provenance` as the separate cryptographic verifier.
