# Contributing

Athena is developed as a small, local-first runtime. Keep changes inside the
existing service, task, capability, execution, and terminal authority
boundaries; do not add a second agent loop or mutation path.

Before submitting a change, run the focused tests for the affected area and
the non-visual release checks:

```bash
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src/athena
uv run pytest -q
scripts/architecture-lint
```

Do not include credentials, generated local databases, or host-specific
artifacts. Visual VHS capture is an optional manual demo concern, not part of
the stable-beta release gate.
