# Interpreter

## Purpose

This package translates bounded execution observations into kernel-owned interpretive subturns and proposals.

## Authority And Evidence Boundaries

- The extension has no autonomous authority, store, or provider. It returns proposals to the one `AgentKernel` dispatch path.
- Observation payloads are bounded and must be artifactized upstream when they exceed the subturn ceiling.
- Subturn evidence is visible and metered; process continuity after restart is explicitly not claimed.

## Verification

- Run interpreter extension/protocol tests plus `./scripts/architecture-lint --quiet`.
