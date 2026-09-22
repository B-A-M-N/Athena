.PHONY: format format-check static-critical lint typecheck compile test full-test check critical-contracts perf performance scenarios arch-lint native-build native-check native-test native-fmt native-clippy native-smoke native-package release-check

UV ?= uv
UV_RUN_DEV := $(UV) run --extra dev
RUFF := $(UV_RUN_DEV) ruff
MYPY := $(UV_RUN_DEV) mypy
PYTHON := $(UV_RUN_DEV) python
PYTEST := $(UV) run --extra dev --extra anthropic pytest

format:
	$(RUFF) format src tests

format-check:
	$(RUFF) format --check src tests

static-critical:
	scripts/static-critical

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

native-build:
	cargo build --manifest-path native/Cargo.toml --locked --offline

native-check:
	cargo check --manifest-path native/Cargo.toml --locked --offline

native-fmt:
	cargo fmt --manifest-path native/Cargo.toml -- --check

native-clippy:
	cargo clippy --manifest-path native/Cargo.toml --all-targets --locked --offline -- -D warnings

native-test:
	cargo test --manifest-path native/Cargo.toml --locked --offline

native-package:
	scripts/build-native-package

native-smoke:
	scripts/native-smoke

# Resolve RELEASE_SHA once, build a clean detached worktree at that commit,
# and run the complete release gate there.  A dirty developer checkout is
# intentionally not treated as release evidence.
release-check:
	$(if $(RELEASE_SHA),scripts/release-check --sha "$(RELEASE_SHA)",scripts/release-check) \
		$(if $(RELEASE_EVIDENCE_DIR),--evidence-dir "$(RELEASE_EVIDENCE_DIR)",)

# The default merge gate is deterministic and does not mutate the checkout.
# Use `make format` to apply the formatter and `make test` for the full suite.
# Focused generated-machinery/model-boundary tests stay in the default gate
# because they protect the highest-risk contracts.
#
# Appended (P1.29/P1.30/P1.32): the scenario manifest and the architecture
# lint are part of the gate.  The kernel router-fallback defect that once
# made arch-lint run red is fixed; the lint is part of the GREEN gate.
# Critical correctness contracts (P1.29): invariants protecting state
# transactions, event idempotency, dispatcher controls/provenance, task
# failure/finalization, and projection conformance.  Run on every PR.
critical-contracts:
	$(PYTEST) -q \
		tests/unit/state/test_database.py \
		tests/unit/state/test_task_metadata.py \
		tests/unit/state/test_task_lease.py \
		tests/unit/state/test_event_sequencing.py \
		tests/unit/capabilities/test_dispatch_provenance_scoped.py \
		tests/unit/capabilities/test_dispatcher_operation_contracts.py \
		tests/unit/capabilities/test_dispatch_many_preflight.py \
		tests/unit/tasks/test_failure_propagation.py \
		tests/unit/cli/test_projection_conformance.py \
		tests/unit/synthesis/test_generated_discipline.py

# Performance lane: deterministic SLO counters (model-call counts, query
# counts, data-structure cardinality).  No wall-clock assertions.
performance:
	$(PYTEST) -q tests/performance/

full-test: format-check static-critical lint typecheck compile native-fmt native-clippy
	$(PYTEST) -q

check: lint typecheck compile
	$(PYTEST) -q \
		tests/unit/affordances/test_validation.py \
		tests/unit/capabilities/test_synthesis_capability.py \
		tests/unit/synthesis/test_synthesis.py \
		tests/unit/models/test_compat_kernel.py \
		tests/unit/models/test_openai_compat.py \
		tests/unit/models/test_anthropic.py \
		tests/unit/capabilities/test_dispatch_many_preflight.py
	$(PYTHON) scripts/scenarios --output /tmp/athena-check-scenarios.json
	$(PYTHON) scripts/architecture-lint
