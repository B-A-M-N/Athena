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
# arch-lint: architecture boundary scan of src/athena.  EXPECTED RED while
#            the known kernel ModelRouter fallback defect (P1-23 residual)
#            is open — do not "fix" by editing this target.
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
# lint are part of the gate.  NOTE: arch-lint is EXPECTED RED while the
# kernel's `router or ModelRouter(registry)` fallback defect (P1-23 residual,
# src/athena/kernel/kernel.py) is open — that is the lint doing its job.
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
