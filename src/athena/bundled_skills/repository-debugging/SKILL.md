---
name: repository-debugging
description: Bounded repository debugging and targeted test verification.
version: 1
triggers:
  - debug
  - failing test
  - traceback
  - regression
  - repository
metadata:
  athena:
    purpose: repository-debugging
    trust: authority
    scope: bundled
---

For a repository failure, inspect the smallest relevant files and existing
project instructions before changing code. Reproduce the failure, make the
minimal scoped change through the governed filesystem path, and run targeted
verification plus the affected gate. Keep unrelated files untouched. If the
environment prevents a check, report that limitation explicitly instead of
calling the change verified.
