"""Computer + browser interaction packs (P1-20, P1-28).

The contracts under test:

* computer.observe is a pure read (no COMPUTER_INPUT effect); every
  mutating operation carries COMPUTER_INPUT and therefore rides the
  ask-by-default, call-scoped, dangerous/persistent governance the
  dispatcher/policy/reality-gate already apply to that effect class.
* Both capabilities degrade honestly when their driver is absent:
  results come back as FAILED outcomes, never fabricated success.
* browser navigation enforces http/https; fill/click carry
  COMPUTER_INPUT while navigate/snapshot/query/text carry NETWORK
  effects — the operation_effects map, not the capability-level union,
  is what policy evaluates.
* The BrowserDriver seam keeps the capability testable headlessly and
  lets operators wire remote drivers.
"""

from __future__ import annotations

import json

import pytest

from athena.artifacts.store import ArtifactStore
from athena.capabilities.browser import (
    BrowserCapability,
    ElementSnapshot,
)
from athena.capabilities.computer import ComputerCapability
from athena.capabilities.registry import validate_schema
from athena.protocol.capabilities import (
    CapabilityRequest,
    EffectClass,
    ResourceClass,
)


def _request(operation: str, *, task_id: str = "task-1", **extra) -> CapabilityRequest:
    return CapabilityRequest(
        capability_id="computer"
        if operation in {"observe", "click", "type", "key", "scroll", "move", "wait"}
        else "browser",
        arguments={"operation": operation, **extra},
        task_id=task_id,
        call_id="call-1",
    )


# --------------------------------------------------------------------------- #
# P1-20: computer capability
# --------------------------------------------------------------------------- #
@pytest.mark.athena_evidence("test", "unit")
def test_computer_observe_is_pure_read_but_mutations_are_computer_input():
    d = ComputerCapability.descriptor
    assert d.operation_effects["observe"] == frozenset()
    for op in ("click", "type", "key", "scroll", "move"):
        assert d.operation_effects[op] == frozenset({EffectClass.COMPUTER_INPUT})
    assert EffectClass.COMPUTER_INPUT in d.effects
    assert d.resolve_resources() == frozenset({ResourceClass.PROCESS})


@pytest.mark.athena_evidence("test", "unit")
def test_computer_schema_binds_operations():
    schema = ComputerCapability.descriptor.input_schema
    assert validate_schema(schema, {"operation": "wait", "seconds": 1.0}) == []
    assert validate_schema(schema, {"operation": "reboot"})
    assert validate_schema(schema, {"operation": "type", "text": 5})


class _FakeScreen:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def screenshot(self):
        self.calls.append(("screenshot",))
        return _FakeImage()

    def click(self, x=None, y=None, button="left", **kw):
        self.calls.append(("click", x, y, button))

    def typewrite(self, text, interval=0.0):
        self.calls.append(("type", text))

    def hotkey(self, *keys):
        self.calls.append(("key", keys))

    def scroll(self, amount, x=None, y=None):
        self.calls.append(("scroll", amount))

    def moveTo(self, x, y, duration=0.0):
        self.calls.append(("move", x, y))

    def position(self):
        return type("P", (), {"x": 1, "y": 2})()

    def size(self):
        return type("S", (), {"width": 800, "height": 600})()


class _FakeImage:
    def save(self, target, format="PNG"):
        assert format == "PNG"
        target.write(b"\x89PNG\r\n\x1a\nathena-test-frame")


@pytest.mark.athena_evidence("test", "unit")
async def test_computer_drives_backend_and_reports_shape():
    screen = _FakeScreen()
    cap = ComputerCapability(backend=screen)

    moved = await cap.invoke(_request("move", x=10, y=20))
    assert json.loads(moved.output)["moved_to"] == [10, 20]
    typed = await cap.invoke(_request("type", text="hello"))
    assert json.loads(typed.output)["typed_chars"] == 5
    pressed = await cap.invoke(_request("key", keys=["ctrl", "s"]))
    assert json.loads(pressed.output)["pressed"] == ["ctrl", "s"]
    waited = await cap.invoke(_request("wait", seconds=0.01))
    assert json.loads(waited.output)["waited_s"] > 0

    assert ("move", 10, 20) in screen.calls
    assert ("type", "hello") in screen.calls
    assert ("key", ("ctrl", "s")) in screen.calls


@pytest.mark.athena_evidence("test", "unit")
async def test_computer_observe_persists_visual_artifact_and_reports_model_input(tmp_path):
    artifacts = ArtifactStore(root=tmp_path / "artifacts")
    cap = ComputerCapability(backend=_FakeScreen(), artifact_store=artifacts)
    assert (await cap.probe_health())["state"] == "ready"
    outcome = await cap.invoke(_request("observe"))
    assert outcome.status.name == "OK"
    note = json.loads(outcome.output)
    assert note["observed"] is True
    assert note["screen_size"] == [800, 600]
    assert note["model_input"] == "image"
    assert outcome.ref_uri == note["artifact_uri"]
    assert await artifacts.load(outcome.ref_uri) == b"\x89PNG\r\n\x1a\nathena-test-frame"
    assert len(outcome.output) < 1024  # bounded; no raw image in context


@pytest.mark.athena_evidence("test", "unit")
async def test_computer_observe_rejects_opaque_capture_without_fabricating_success(tmp_path):
    class OpaqueScreen(_FakeScreen):
        def screenshot(self):
            return object()

    cap = ComputerCapability(
        backend=OpaqueScreen(),
        artifact_store=ArtifactStore(root=tmp_path / "artifacts"),
    )
    outcome = await cap.invoke(_request("observe"))
    assert outcome.status.name == "FAILED"
    assert "serializable image" in outcome.error


@pytest.mark.athena_evidence("test", "unit")
async def test_computer_observe_rejects_unbounded_capture_without_retaining_it(tmp_path):
    class OversizedImage:
        def save(self, target, format="PNG"):
            target.write(b"x" * (4 * 1024 * 1024 + 1))

    class OversizedScreen(_FakeScreen):
        def screenshot(self):
            return OversizedImage()

    cap = ComputerCapability(
        backend=OversizedScreen(),
        artifact_store=ArtifactStore(root=tmp_path / "artifacts"),
    )
    outcome = await cap.invoke(_request("observe"))

    assert outcome.status.name == "FAILED"
    assert "too large" in outcome.error


@pytest.mark.athena_evidence("test", "unit")
async def test_visual_result_transcript_keeps_reference_without_pixels(tmp_path):
    from athena.kernel.run_finalizer import _results_message
    from athena.protocol.messages import ArtifactRefBlock, CapabilityResultBlock

    artifacts = ArtifactStore(root=tmp_path / "artifacts")
    outcome = await ComputerCapability(backend=_FakeScreen(), artifact_store=artifacts).invoke(
        _request("observe")
    )
    block = CapabilityResultBlock(
        call_id=outcome.call_id,
        capability_id=outcome.capability_id,
        output=outcome.output,
        metadata=outcome.metadata,
        ref_uri=outcome.ref_uri,
    )
    message = _results_message(
        type("Task", (), {"id": "task-visual", "session_id": None})(), (block,)
    )

    assert any(isinstance(item, ArtifactRefBlock) for item in message.blocks)
    assert all(not isinstance(item, bytes) for item in message.blocks)


@pytest.mark.athena_evidence("test", "unit")
async def test_computer_honest_failure_when_backend_missing():
    cap = ComputerCapability(backend=None)
    # Simulate the uninstalled-extra path: backend resolution fails.
    import athena.capabilities.computer as mod

    saved = mod._screen_backend
    mod._screen_backend = None
    try:
        outcome = await cap.invoke(_request("click", x=1, y=1))
        assert outcome.status.name == "FAILED"
        assert "unavailable" in outcome.error
    finally:
        mod._screen_backend = saved


# --------------------------------------------------------------------------- #
# P1-28: browser capability
# --------------------------------------------------------------------------- #
@pytest.mark.athena_evidence("test", "unit")
def test_browser_operation_effects_split_network_from_computer_input():
    d = BrowserCapability.descriptor
    assert d.operation_effects["navigate"] >= {EffectClass.NETWORK_WRITE}
    assert d.operation_effects["snapshot"] == frozenset({EffectClass.NETWORK_READ})
    assert d.operation_effects["fill"] == frozenset({EffectClass.COMPUTER_INPUT})
    assert d.operation_effects["click"] == frozenset(
        {EffectClass.COMPUTER_INPUT, EffectClass.NETWORK_WRITE}
    )
    assert d.resolve_resources() == frozenset({ResourceClass.NETWORK})


@pytest.mark.athena_evidence("test", "unit")
class _FakeDriver:
    def __init__(self) -> None:
        self.closed = False
        self.calls: list[tuple] = []

    async def navigate(self, url):
        self.calls.append(("navigate", url))
        return {"url": url, "title": "Example", "status": 200}

    async def snapshot(self):
        self.calls.append(("snapshot",))
        return [
            ElementSnapshot(role="textbox", name="Search", selector="#q", value=""),
            ElementSnapshot(role="link", name="Docs", selector="a.docs"),
        ]

    async def query(self, selector):
        if selector == "#q":
            return {"role": "textbox", "name": "Search", "value": ""}
        return None

    async def fill(self, selector, value):
        self.calls.append(("fill", selector, value))
        return {"selector": selector, "filled": True}

    async def click(self, selector):
        self.calls.append(("click", selector))
        return {"selector": selector, "clicked": True}

    async def text(self):
        self.calls.append(("text",))
        return "Example Domain"

    async def close(self):
        self.closed = True


def _browser_cap() -> tuple[BrowserCapability, _FakeDriver]:
    driver = _FakeDriver()
    return BrowserCapability(driver_factory=lambda: driver), driver


@pytest.mark.athena_evidence("test", "unit")
async def test_browser_navigates_snapshots_queries_fills_clicks():
    cap, driver = _browser_cap()

    nav = await cap.invoke(_request("navigate", url="https://example.com"))
    assert json.loads(nav.output)["status"] == 200

    snap = await cap.invoke(_request("snapshot"))
    assert "textbox\tSearch\t#q" in snap.output
    assert snap.metadata["elements"] == 2

    hit = await cap.invoke(_request("query", selector="#q"))
    assert json.loads(hit.output)["name"] == "Search"
    miss = await cap.invoke(_request("query", selector="#nope"))
    assert miss.status.name == "FAILED"

    filled = await cap.invoke(_request("fill", selector="#q", value="athena"))
    assert json.loads(filled.output)["filled"] is True
    clicked = await cap.invoke(_request("click", selector="a.docs"))
    assert json.loads(clicked.output)["clicked"] is True
    text = await cap.invoke(_request("text"))
    assert text.output == "Example Domain"

    await cap.close()
    assert driver.closed  # the capability closes what it opened


@pytest.mark.athena_evidence("test", "unit")
async def test_browser_preserves_state_per_task_until_shutdown():
    created: list[_FakeDriver] = []

    def factory():
        driver = _FakeDriver()
        created.append(driver)
        return driver

    cap = BrowserCapability(driver_factory=factory)
    await cap.invoke(_request("navigate", url="https://example.com"))
    await cap.invoke(_request("snapshot"))
    await cap.invoke(_request("snapshot", task_id="task-2"))

    assert len(created) == 2
    assert created[0].calls[0] == ("navigate", "https://example.com")
    assert created[0].calls[1] == ("snapshot",)
    assert not created[0].closed
    await cap.close()
    assert all(driver.closed for driver in created)


@pytest.mark.athena_evidence("test", "unit")
async def test_browser_rejects_non_http_navigation():
    cap, _ = _browser_cap()
    for url in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x"):
        outcome = await cap.invoke(_request("navigate", url=url))
        assert outcome.status.name == "FAILED", url
    blank = await cap.invoke(_request("navigate", url=""))
    assert blank.status.name == "FAILED"


@pytest.mark.athena_evidence("test", "unit")
async def test_browser_honest_failure_without_driver_factory():
    cap = BrowserCapability(driver_factory=None)
    outcome = await cap.invoke(_request("navigate", url="https://example.com"))
    assert outcome.status.name == "FAILED"
    assert "unavailable" in outcome.error


@pytest.mark.athena_evidence("test", "unit")
async def test_playwright_preflight_reports_missing_optional_dependency(monkeypatch):
    import athena.capabilities.browser as browser_module

    monkeypatch.setattr(browser_module, "_playwright", None)
    result = await browser_module.PlaywrightBrowserDriver.preflight()

    assert result == {
        "state": "unavailable",
        "installed": False,
        "reason": "Playwright is not installed",
    }


@pytest.mark.athena_evidence("test", "unit")
async def test_playwright_preflight_rejects_invalid_cdp_configuration(monkeypatch):
    import athena.capabilities.browser as browser_module

    monkeypatch.setattr(browser_module, "_playwright", object())
    result = await browser_module.PlaywrightBrowserDriver.preflight(cdp_endpoint="not-a-url")

    assert result["state"] == "unavailable"
    assert "http(s) URL" in result["reason"]


# --------------------------------------------------------------------------- #
# Service wiring
# --------------------------------------------------------------------------- #
@pytest.mark.athena_evidence("test", "unit")
async def test_service_registers_computer_and_browser_by_config():
    from athena.service.config import AthenaConfig
    from athena.service.service import AthenaService

    config = AthenaConfig(
        db_path=":memory:",
        browser_driver_factory=lambda: _FakeDriver(),
    )
    svc = AthenaService(config=config)
    try:
        await svc.start()
        # The service wires the browser capability iff the operator supplied
        # a driver factory; computer wires iff pyautogui imports (absent on
        # headless CI). Registration state mirrors the config contract.
        assert (svc._browser is not None) == (config.browser_driver_factory is not None)
        if svc._browser is not None:
            registered = svc._dispatcher.registry._by_id.get("browser")
            assert registered is svc._browser
    finally:
        await svc.stop()
