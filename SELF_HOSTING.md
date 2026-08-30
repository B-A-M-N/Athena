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

