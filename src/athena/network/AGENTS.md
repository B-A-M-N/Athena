# Network

## Purpose

Provide pinned, policy-gated network primitives (DNS resolution, transport)
for capabilities that need governed egress.

## Local Contracts

- All acquisition goes through source policy checks before any connection.
- DNS results are pinned per request; redirects are not followed implicitly.
- Response bodies are bounded before persistence or model exposure.
- Never fetch arbitrary URLs outside the named acquisition primitives.

## Verification

Run research/network policy tests and `./scripts/architecture-lint --quiet`.
