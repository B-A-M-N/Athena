.PHONY: format format-check lint typecheck compile test check scenarios arch-lint demo native-check native-test native-smoke native-package release-check static-critical support-matrix-check migration-baseline

UV ?= uv
UV_RUN_DEV := $(UV) run --frozen --extra dev
RUFF := $(UV_RUN_DEV) ruff
MYPY := $(UV_RUN_DEV) mypy
PYTHON := $(UV_RUN_DEV) python
PYTEST := $(UV) run --frozen --extra dev --extra anthropic pytest
CHECK_ARTIFACT_DIR ?= .artifacts/check

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

# Regenerate the published terminal demo intentionally.  This is not part of
# the read-only merge gate because it updates the canonical GIF artifact.
demo:
	scripts/render-demo

arch-lint:
	$(PYTHON) scripts/architecture-lint

static-critical:
	bash scripts/static-critical

support-matrix-check:
	$(PYTHON) scripts/support-matrix-check

migration-baseline:
	$(PYTHON) scripts/verify-migration-baseline

native-check:
	cargo check --manifest-path native/Cargo.toml --offline

native-test:
	cargo test --manifest-path native/Cargo.toml --offline

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
check: format-check lint typecheck compile static-critical support-matrix-check migration-baseline
	@set -eu; \
	check_before="$$(git status --porcelain --untracked-files=no)"; \
	check_status() { \
		check_after="$$(git status --porcelain --untracked-files=no)"; \
		if [ "$$check_before" != "$$check_after" ]; then \
			echo "make check modified tracked files" >&2; \
			echo "before:" >&2; echo "$$check_before" >&2; \
			echo "after:" >&2; echo "$$check_after" >&2; \
			return 1; \
		fi; \
	}; \
	trap 'check_status_code=$$?; if ! check_status; then check_status_code=1; fi; exit "$$check_status_code"' EXIT; \
	mkdir -p "$(CHECK_ARTIFACT_DIR)"; \
	$(PYTEST) -q \
		tests/unit/affordances/test_validation.py \
		tests/unit/capabilities/test_synthesis_capability.py \
		tests/unit/synthesis/test_synthesis.py \
		tests/unit/models/test_compat_kernel.py \
		tests/unit/models/test_openai_compat.py \
		tests/unit/models/test_anthropic.py \
		tests/unit/capabilities/test_dispatch_many_preflight.py; \
	$(PYTHON) scripts/scenarios --exclude-family VHS --output "$(CHECK_ARTIFACT_DIR)/scenarios-manifest.json"; \
	$(PYTHON) scripts/architecture-lint
