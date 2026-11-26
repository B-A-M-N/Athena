"""Mutable route state beneath :class:`RealityGate`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from athena.concurrency import ReferenceCountedKeyedLocks
from athena.reality.transaction_state import TransactionStateStore

__all__ = ["RealityRouteState", "resolve_state_root"]


class RealityRouteState:
    """Own transaction persistence and ephemeral route coordination state."""

    def __init__(self, state_root: Path | None) -> None:
        transaction_path = (
            state_root / "reality-transactions.json" if state_root is not None else None
        )
        self.transaction_store = TransactionStateStore(transaction_path)
        self.locks = ReferenceCountedKeyedLocks()

    @property
    def checkpoint_by_task(self) -> dict[str, str]:
        return self.transaction_store.checkpoint_by_task

    @property
    def checkpoint_root_by_task(self) -> dict[str, str]:
        return self.transaction_store.checkpoint_root_by_task

    @property
    def transaction_records(self) -> dict[str, dict[str, Any]]:
        return self.transaction_store.records

    def load(self) -> None:
        self.transaction_store.load()

    def persist(self) -> None:
        self.transaction_store.persist()


def resolve_state_root(shadow_engine: Any) -> Path | None:
    state_root = getattr(shadow_engine, "state_root", None)
    if state_root is None:
        state_root = getattr(shadow_engine, "_state_root", None)
    return Path(state_root) if state_root else None
