---
name: release-verification
description: Evidence-driven release checks for changes, artifacts, and runtime claims.
version: 1
triggers:
  - release
  - artifact
  - evidence
  - verify
metadata:
  athena:
    purpose: release-audit
    trust: authority
    scope: bundled
---

Use this narrow procedure when the user asks for release validation or when a
result depends on a claim such as “the artifact contains the change.” Identify
the affected surface first, run the smallest relevant verification through the
normal execution capability, and preserve the resulting receipt or artifact.
Report expected versus observed behavior and name any unavailable optional
runtime. Never treat a model statement, an old artifact, or a test rerun as
proof for a different source revision.
