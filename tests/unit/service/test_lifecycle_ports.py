"""Lifecycle startup/teardown port contract evidence."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from athena.service.lifecycle_ports import LifecyclePorts


def test_lifecycle_ports_use_public_names_and_preserve_owner_state() -> None:
    owner = SimpleNamespace(_started=False, _startup_health={"status": "starting"})
    ports = LifecyclePorts(owner)

    assert ports.started is False
    ports.started = True
    ports.startup_health = {"status": "ok"}

    assert owner._started is True
    assert owner._startup_health == {"status": "ok"}


def test_startup_ports_reject_unlisted_resources() -> None:
    owner = SimpleNamespace(_shutdown_status={"state": "running"})
    startup = LifecyclePorts(owner).startup

    startup.shutdown_status = {"state": "stopped"}
    assert owner._shutdown_status == {"state": "stopped"}
    with pytest.raises(AttributeError, match="startup port is not allowed"):
        _ = startup.unrelated_resource
