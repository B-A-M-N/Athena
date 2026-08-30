"""Independent, candidate-free eligibility review for self-host promotion."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


class SelfHostIndependentReviewer:
    """Check promotion evidence using only the retained certificate record."""

    @staticmethod
    def review(
        *,
        status: str,
        certificate: Mapping[str, Any],
        verification: Sequence[Mapping[str, Any] | Any],
    ) -> dict[str, Any]:
        authority = certificate.get("proof_authority")
        authority_ok = isinstance(authority, Mapping) and all(
            str(authority.get(key) or "") for key in ("source_revision", "gate_bundle_hash")
        )
        checks_ok = bool(verification) and all(
            bool(item.get("passed")) if isinstance(item, Mapping) else False
            for item in verification
        )
        identity_ok = bool(
            certificate.get("certificate_hash")
            and certificate.get("base_fingerprint")
            and certificate.get("candidate_fingerprint")
            and certificate.get("candidate_fingerprint") != certificate.get("base_fingerprint")
        )
        eligible = status == "VERIFIED" and authority_ok and checks_ok and identity_ok
        evidence = {
            "reviewer": "frozen-certificate-integrity",
            "independent": True,
            "eligible": eligible,
            "checks": {
                "branch_verified": status == "VERIFIED",
                "proof_authority_bound": authority_ok,
                "all_verification_checks_passed": checks_ok,
                "candidate_identity_bound": identity_ok,
            },
        }
        evidence["evidence_hash"] = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return evidence


__all__ = ["SelfHostIndependentReviewer"]
