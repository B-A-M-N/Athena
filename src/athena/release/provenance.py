"""Cryptographic release-provenance verification shared by release gates."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any


class ProvenanceVerificationError(RuntimeError):
    """The release signature could not be verified under the configured policy."""


def _file_fingerprint(path: str) -> str | None:
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        return None
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _cosign_path() -> str:
    configured = os.environ.get("ATHENA_COSIGN") or os.environ.get("COSIGN_BIN")
    executable = configured or shutil.which("cosign")
    if not executable:
        raise ProvenanceVerificationError("cosign is required for release provenance verification")
    return executable


def _version(executable: str) -> str:
    try:
        result = subprocess.run(
            [executable, "version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ProvenanceVerificationError(f"cannot determine cosign version: {exc}") from exc
    output = (result.stdout or result.stderr).strip().splitlines()
    return output[0] if output else "unknown"


def verify_provenance(
    provenance: Path,
    signature: Path,
    *,
    certificate: Path | None = None,
) -> dict[str, Any]:
    """Verify a detached cosign signature and return auditable verifier metadata."""

    if not provenance.is_file() or not signature.is_file() or not signature.stat().st_size:
        raise ProvenanceVerificationError("provenance payload/signature is missing or empty")
    executable = _cosign_path()
    cosign_version = _version(executable)
    public_key = os.environ.get("COSIGN_PUBLIC_KEY")
    identity = os.environ.get("COSIGN_CERTIFICATE_IDENTITY")
    issuer = os.environ.get("COSIGN_CERTIFICATE_OIDC_ISSUER")
    if public_key:
        command = [executable, "verify-blob", "--key", public_key]
        mechanism = "cosign-key"
        signer_identity = public_key
        key_fingerprint = _file_fingerprint(public_key)
        issuer_value = None
    else:
        if not identity or not issuer:
            raise ProvenanceVerificationError(
                "keyless verification requires COSIGN_CERTIFICATE_IDENTITY and "
                "COSIGN_CERTIFICATE_OIDC_ISSUER"
            )
        command = [
            executable,
            "verify-blob",
            "--certificate-identity",
            identity,
            "--certificate-oidc-issuer",
            issuer,
        ]
        if certificate is not None:
            if not certificate.is_file() or not certificate.stat().st_size:
                raise ProvenanceVerificationError("configured keyless certificate is missing")
            command.extend(["--certificate", str(certificate)])
        mechanism = "cosign-keyless"
        signer_identity = identity
        key_fingerprint = None
        issuer_value = issuer
    command.extend(["--signature", str(signature), str(provenance)])
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stdout", "") or getattr(exc, "stderr", "") or str(exc)
        raise ProvenanceVerificationError(
            f"cosign cryptographic verification failed: {detail.strip()}"
        ) from exc
    return {
        "status": "PASS",
        "result": "verified",
        "mechanism": mechanism,
        "cosign_executable": executable,
        "cosign_version": cosign_version,
        "signer_identity": signer_identity,
        "oidc_issuer": issuer_value,
        "key_fingerprint": key_fingerprint,
        "certificate": str(certificate) if certificate is not None else None,
        "cosign_output": (result.stdout or result.stderr).strip()[-2000:],
    }


__all__ = ["ProvenanceVerificationError", "verify_provenance"]
