"""Immutable identity for verification evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from athena.protocol.ids import new_id


def certificate_digest(record: Mapping[str, Any]) -> str:
    """Hash canonical certificate content, excluding its self-hash."""
    payload = {key: value for key, value in record.items() if key != "certificate_hash"}
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class VerificationCertificate(Mapping[str, Any]):
    """Immutable, canonical proof that a candidate passed verification."""

    _payload: str = "{}"

    @classmethod
    def issue(cls, record: Mapping[str, Any]) -> "VerificationCertificate":
        values = dict(record)
        values.setdefault("certificate_id", new_id("certificate"))
        values.setdefault("certificate_schema_version", 1)
        values["certificate_hash"] = certificate_digest(values)
        return cls._from_values(values)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "VerificationCertificate":
        return cls._from_values(dict(record))

    @classmethod
    def empty(cls) -> "VerificationCertificate":
        return cls()

    @classmethod
    def _from_values(cls, values: Mapping[str, Any]) -> "VerificationCertificate":
        payload = json.dumps(
            dict(values),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return cls(payload)

    def _record(self) -> dict[str, Any]:
        value = json.loads(self._payload)
        return value if isinstance(value, dict) else {}

    def to_record(self) -> dict[str, Any]:
        return self._record()

    def valid(self) -> bool:
        record = self._record()
        stored = record.get("certificate_hash")
        return bool(stored) and stored == certificate_digest(record)

    def __getitem__(self, key: str) -> Any:
        return self._record()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._record())

    def __len__(self) -> int:
        return len(self._record())


__all__ = ["VerificationCertificate", "certificate_digest"]
