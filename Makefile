.PHONY: format format-check lint typecheck compile test check scenarios arch-lint

UV ?= uv
RUFF := $(UV) run ruff
MYPY := $(UV) run mypy
PYTHON := $(UV) run python
PYTEST := $(UV) run pytest

format:
	$(RUFF) format src tests

format-check:
	$(RUFF) format --check src tests

lint:
	$(RUFF) check src tests

typecheck:
	$(MYPY) src/athena

compile:
	$(PYTHON) -m compileall -q src tests

test:
	$(PYTEST) -q

# Release-verification infrastructure (audit P1.29/P1.30/P1.32).
# scenarios: run the named scenario families and emit the JSON evidence
#            manifest (scenarios-manifest.json).  Exit 0 iff every REQUIRED
#            scenario passed; declared gaps appear as "missing".
# arch-lint: architecture boundary scan of src/athena.  The historical
#            "EXPECTED RED" note applied to the kernel's `router or
#            ModelRouter(registry)` fallback (P1-23 residual) — that defect
#            is FIXED (router is required and raises on None,
#            src/athena/kernel/kernel.py); the lint is expected GREEN.
scenarios:
	$(PYTHON) scripts/scenarios --output scenarios-manifest.json

arch-lint:
	$(PYTHON) scripts/architecture-lint

# The default merge gate is deterministic and does not mutate the checkout.
# Use `make format` to apply the formatter and `make test` for the full suite.
# Focused generated-machinery/model-boundary tests stay in the default gate
# because they protect the highest-risk contracts.
#
# Appended (P1.29/P1.30/P1.32): the scenario manifest and the architecture
# lint are part of the gate.  The kernel router-fallback defect that once
# made arch-lint run red is fixed; the lint is part of the GREEN gate.
check: lint typecheck compile
	$(PYTEST) -q \
		tests/unit/affordances/test_validation.py \
		tests/unit/capabilities/test_synthesis_capability.py \
		tests/unit/synthesis/test_synthesis.py \
		tests/unit/models/test_compat_kernel.py \
		tests/unit/models/test_openai_compat.py \
		tests/unit/models/test_anthropic.py \
		tests/unit/capabilities/test_dispatch_many_preflight.py
	$(PYTHON) scripts/scenarios --output scenarios-manifest.json
	$(PYTHON) scripts/architecture-lint
