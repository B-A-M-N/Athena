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
| Runtime continuity | Complete for container backend | Backend/runtime identity is durable, workspace/network bounded, and container sessions have proof-based reattachment; local in-process runtimes remain service-lifetime only and report `RuntimeStateLost`. |
| Browser governance | Complete for supported browser path | Browser sessions are task/session scoped, shutdown-cleaned, and pass destination/subresource policy before navigation. |
| Research fabric | Complete for bounded acquisition path | Real discover → fetch → artifact → evidence → verification → contradiction → bundle journey passes with DNS-pinned, allowlisted policy. Open-ended autonomous acquisition/critique remains intentionally bounded. |
| Skills, workflows, delegation, MCP | Complete for beta proof scope | Bundled skills, lifecycle/promotion, durable workflow receipts, delegation ownership, real MCP stdio discovery/call/reconnect, and failure bounds are exercised. |
| Computer observation | Complete for supported backend | PNG capture is persisted as a provenance-backed artifact and exposed as model image input; unavailable backends report unavailable rather than success. |
| Release identity | Complete as a gate | Wheel, sdist, native wheel, hashes, source identity, ABI policy, and run identity are bound by the release manifest; mismatches fail closed. |

## Verification snapshot

Evidence below was produced from the current implementation checkout. The
checkout is intentionally dirty, so this is not a frozen release claim.

- Full suite: `1646 passed, 4 skipped`.
- Integration/end-to-end suite: `56 passed, 1 skipped`.
- Functional product proof: `6 passed`.
- Static gates: Ruff check, Ruff format check, mypy (`305` source files), and
  compileall all pass.
- Installed-artifact acceptance: passed against the exact bundle in
  `/tmp/athena-release-artifacts`.
- FastEmbed smoke: real `BAAI/bge-small-en-v1.5` CPU embedding and paraphrase
  retrieval passed; normal writes remain non-blocking and semantic queries
  backfill the derived index explicitly.
- Research boundary proof: `RESEARCH-001` passed.
- MCP boundary proof: `MCP-001` passed.
- The required non-VHS scenario failures seen in the restricted sandbox were
  rerun in the supported process/network environment and passed. VHS is an
  optional visual renderer, excluded from product acceptance; its unavailable
  PTY path is recorded as skipped rather than allowed to block the product.

## Release boundary

Do not publish the checked-in scenario manifest as release evidence from this
working tree. A clean release requires a new commit, a clean checkout, a
fresh artifact build, and a new evidence run bound to that exact commit/run
pair. The current implementation has no valid frozen SHA claim.
