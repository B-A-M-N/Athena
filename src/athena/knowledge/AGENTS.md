# Knowledge

## Purpose

Knowledge ingestion, provenance tracking, embedding preparation, and durable
knowledge retrieval.

## Ownership

Knowledge processing is a durable evidence boundary. It is NOT another
reasoning authority: it never decides what work happens next, never selects
models, and never plans work. AgentKernel remains the one reasoning authority.

## Local Contracts

- Provenance and trust are immutable once admitted. Downstream consumers
  cannot upgrade the trust class of a knowledge record.
- Ingestion paths must classify source trust before content is indexed, not
  after. Untrusted sources must be usable as data, never as instructions.
- Embedding preparation is deterministic for a given input and model version.
- Retrieval results are bounded by size and relevance; unbounded payloads may
  not enter context through this boundary.
- Knowledge retrieval must never synthesize new facts: it returns admitted
  evidence only.

## Verification

Run knowledge unit tests plus the repository's architecture lint.
