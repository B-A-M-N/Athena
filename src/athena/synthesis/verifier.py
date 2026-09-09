"""Independent deterministic review of generated-capability evidence."""

from __future__ import annotations

import json
from typing import Any


class GeneratedCapabilityVerifier:
    """Review validation receipts without owning promotion or execution."""

    @staticmethod
    def review(capability: Any) -> dict[str, Any]:
        failures: list[str] = []
        validation = capability.validation if isinstance(capability.validation, dict) else {}
        if validation.get("all_passed") is not True:
            failures.append("behavioral validation is not fully passing")
        details = validation.get("details") or ()
        if any(not isinstance(item, dict) or item.get("passed") is not True for item in details):
            failures.append("a positive validation case is not proven")
        negative = validation.get("negative_cases") or ()
        if not negative or any(
            not isinstance(item, dict) or item.get("passed") is not True for item in negative
        ):
            failures.append("negative input boundary is not proven")
        effective = {str(value) for value in capability.effective_effects}
        if not effective.issubset({"READ_LOCAL", "EXECUTE"}):
            failures.append("effective effects widen the generated sandbox ceiling")
        try:
            to_record = getattr(capability, "to_record", None)
            record = to_record() if callable(to_record) else vars(capability)
            json.dumps(record, sort_keys=True, default=str)
        except (TypeError, ValueError) as exc:
            failures.append(f"validation evidence is not serializable: {exc}")
        return {
            "role": "generated_verifier",
            "passed": not failures,
            "failures": failures,
            "promotion_authority": False,
        }


__all__ = ["GeneratedCapabilityVerifier"]
