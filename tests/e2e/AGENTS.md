# End-to-End Tests

## Purpose

End-to-end tests prove real subsystem seams and their serialized contracts.

## Ownership

This directory owns cross-boundary evidence, not unit-level implementation
coverage.

## Local Contracts

- Exercise producer and consumer together for bridge/schema changes.
- Include wrapper and lifecycle seams, not only isolated reducers/renderers.
- Avoid mocks that remove the boundary under test.

## Work Guidance

Keep fixtures deterministic and record the schema/version or authority seam
being proven.

## Verification

Run the focused e2e test and the relevant native/Python checks.

## Child DOX Index

No nested contracts are currently required.
