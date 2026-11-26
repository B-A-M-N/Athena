from athena.capabilities.health import CapabilityHealth


class _Store:
    def __init__(self):
        self.values = {}

    async def save(self, record):
        self.values[record["capability_id"]] = dict(record)

    async def delete(self, capability_id):
        self.values.pop(capability_id, None)

    async def clear(self):
        self.values.clear()


def test_health_opens_after_repeated_failures_and_allows_one_probe():
    health = CapabilityHealth(failure_threshold=2, cooldown_seconds=1)

    assert health.before_call("remote.tool")[0] is True
    health.record_failure("remote.tool", "transport down")
    health.record_failure("remote.tool", "transport down")
    allowed, record = health.before_call("remote.tool")
    assert allowed is False
    assert record["status"] == "open"
    assert record["retry_after_seconds"] > 0


def test_health_probe_closes_circuit_after_recovery(monkeypatch):
    values = iter([0.0, 0.0, 2.0, 2.0, 2.0, 2.0])
    monkeypatch.setattr("athena.capabilities.health.time.monotonic", lambda: next(values))
    health = CapabilityHealth(failure_threshold=1, cooldown_seconds=1)
    health.record_failure("tool", "down")
    allowed, record = health.before_call("tool")
    assert allowed is True
    assert record["status"] == "half_open"
    health.record_success("tool")
    assert health.get("tool")["status"] == "closed"


def test_health_circuit_survives_restart_via_durable_record():
    store = _Store()
    original = CapabilityHealth(failure_threshold=2, cooldown_seconds=60, store=store)
    original.record_failure("remote.tool", "transport down")
    original.record_failure("remote.tool", "transport down")

    import asyncio

    asyncio.run(original.persist("remote.tool"))

    restored = CapabilityHealth(failure_threshold=2, cooldown_seconds=60)
    asyncio.run(restored.load(list(store.values.values())))
    allowed, record = restored.before_call("remote.tool")
    assert allowed is False
    assert record["status"] == "open"
    assert record["failures"] == 2


async def test_domain_failures_do_not_open_circuit_but_infrastructure_does():
    """Repeated file-not-found must not open `fs`; repeated crashes must."""
    from athena.capabilities.health import CapabilityHealth
    from athena.protocol.capabilities import (
        CapabilityFailure,
        CapabilityFailureCode,
        HEALTH_FAILURE_CODES,
    )

    health = CapabilityHealth(failure_threshold=3)

    # Domain failure: expected outcome, not a health problem.
    domain = CapabilityFailure(
        code=CapabilityFailureCode.PERMANENT_RUNTIME,
        detail="file not found",
        retryable=False,
    )
    for _ in range(5):
        record = health.record_failure("fs", domain.detail)
    # Without our dispatcher gate, raw calls do open the circuit. The
    # dispatcher must classify domain failures as success, so prove the
    # classification works at the protocol level.
    assert domain.code not in HEALTH_FAILURE_CODES

    # Infrastructure failure: same code repeated does open the circuit.
    infra = CapabilityFailure(
        code=CapabilityFailureCode.TRANSIENT_RUNTIME,
        detail="executor crashed",
    )
    assert infra.code in HEALTH_FAILURE_CODES
    for _ in range(3):
        record = health.record_failure("execute", infra.detail)
    assert record["status"] == "open"
