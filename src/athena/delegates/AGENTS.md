# Delegates

## Purpose

This package owns trusted external specialist definitions and their durable session receipts.

## Authority And Evidence Boundaries

- Host configuration owns delegate admission; models may request configured delegation but cannot register or widen delegates.
- `DelegateRegistry` is the authoritative spec/connector admission boundary and must fail closed on missing transport.
- External sessions record truthful status and evidence; pending work is never reported as successful completion.

## Verification

- Run delegate registry/session tests plus `./scripts/architecture-lint --quiet`.
