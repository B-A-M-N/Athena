"""Bounded capability-contributed instrument views."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


INSTRUMENT_KINDS = frozenset(
    {
        "text",
        "markdown",
        "code",
        "diff",
        "table",
        "tree",
        "graph",
        "progress",
        "key_value",
        "image",
        "artifact",
        "form",
        "select",
        "confirm",
    }
)
_MAX_STRING = 16_384
_MAX_ITEMS = 256


@dataclass(frozen=True)
class InstrumentView:
    id: str
    kind: str
    title: str = ""
    payload: Any = None
    operation_id: str | None = None
    replace_key: str | None = None

    @classmethod
    def from_record(cls, value: Mapping[str, Any]) -> "InstrumentView":
        kind = str(value.get("kind") or "text")
        if kind not in INSTRUMENT_KINDS:
            raise ValueError(f"unsupported instrument kind: {kind}")
        view_id = str(value.get("id") or value.get("replace_key") or "instrument")
        if not view_id or len(view_id) > 256:
            raise ValueError("instrument id is invalid")
        return cls(
            id=view_id[:256],
            kind=kind,
            title=str(value.get("title") or "")[:512],
            payload=_bound(value.get("payload")),
            operation_id=(str(value["operation_id"]) if value.get("operation_id") else None),
            replace_key=(str(value["replace_key"]) if value.get("replace_key") else None),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "payload": self.payload,
            "operation_id": self.operation_id,
            "replace_key": self.replace_key,
        }


def _bound(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[depth-limited]"
    if isinstance(value, str):
        return value[:_MAX_STRING]
    if isinstance(value, Mapping):
        return {
            str(key)[:256]: _bound(item, depth + 1)
            for key, item in list(value.items())[:_MAX_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        return [_bound(item, depth + 1) for item in value[:_MAX_ITEMS]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_STRING]


__all__ = ["INSTRUMENT_KINDS", "InstrumentView"]
