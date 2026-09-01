"""Native companion ABI policy and inspection."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any


NATIVE_ABI_POLICY: dict[str, Any] = {
    "contract": "linux-x86_64-gnu-glibc-2.34-plus",
    "architecture": "Advanced Micro Devices X86-64",
    "elf_class": "ELF64",
    "interpreter": "/lib64/ld-linux-x86-64.so.2",
    # The runtime floor is a deployment promise. The imported-symbol ceiling
    # is a build/linkage constraint; they happen to match for this release but
    # must remain independently named if either policy changes later.
    "certified_runtime_floor": "2.34",
    "max_allowed_imported_glibc_symbol": "2.34",
    "required_shared_libraries": [
        "libX11.so.6",
        "libXft.so.2",
        "libGL.so.1",
        "libgcc_s.so.1",
        "libm.so.6",
        "libc.so.6",
    ],
    "optional_shared_libraries": [],
    "allowed_shared_libraries": [
        "libX11.so.6",
        "libXft.so.2",
        "libGL.so.1",
        "libgcc_s.so.1",
        "libm.so.6",
        "libc.so.6",
        "ld-linux-x86-64.so.2",
    ],
    "require_no_rpath_or_runpath": True,
    "external_runtime_requirements": ["X11", "Xft", "OpenGL", "glibc>=2.34"],
}


def _readelf(binary: Path, *args: str) -> str:
    result = subprocess.run(  # architecture-lint: allow subprocess-outside-approved-backends reason=read-only native ABI inspection
        ["readelf", *args, str(binary)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"readelf {' '.join(args)} failed")
    return result.stdout


def inspect_native_abi(binary: str | Path) -> dict[str, Any]:
    """Inspect one ELF and return a serializable policy result."""
    path = Path(binary)
    violations: list[str] = []
    try:
        header = _readelf(path, "-h")
        program_headers = _readelf(path, "-l")
        dynamic = _readelf(path, "-d")
        versions = _readelf(path, "--version-info")
    except (OSError, RuntimeError) as exc:
        return {
            "policy": dict(NATIVE_ABI_POLICY),
            "status": "FAIL",
            "violations": [str(exc)],
        }

    elf_class = _field(header, "Class")
    machine = _field(header, "Machine")
    interpreter_match = re.search(r"Requesting program interpreter: ([^]]+)", program_headers)
    interpreter = interpreter_match.group(1).strip() if interpreter_match else None
    needed = sorted(re.findall(r"Shared library: \[([^]]+)\]", dynamic))
    runpaths = sorted(
        value
        for tag in ("RPATH", "RUNPATH")
        for value in re.findall(rf"\({tag}\).*?: \[([^]]*)\]", dynamic)
    )
    glibc_versions = sorted(
        {match for match in re.findall(r"GLIBC_(\d+\.\d+)", versions)},
        key=lambda value: tuple(int(part) for part in value.split(".")),
    )
    max_glibc = NATIVE_ABI_POLICY["max_allowed_imported_glibc_symbol"]
    max_glibc_tuple = tuple(int(part) for part in max_glibc.split("."))
    too_new = [
        version
        for version in glibc_versions
        if tuple(int(part) for part in version.split(".")) > max_glibc_tuple
    ]

    if elf_class != NATIVE_ABI_POLICY["elf_class"]:
        violations.append(f"ELF class {elf_class!r} != {NATIVE_ABI_POLICY['elf_class']!r}")
    if machine != NATIVE_ABI_POLICY["architecture"]:
        violations.append(f"architecture {machine!r} != {NATIVE_ABI_POLICY['architecture']!r}")
    if interpreter != NATIVE_ABI_POLICY["interpreter"]:
        violations.append(f"interpreter {interpreter!r} != {NATIVE_ABI_POLICY['interpreter']!r}")
    required = set(NATIVE_ABI_POLICY["required_shared_libraries"])
    allowed = set(NATIVE_ABI_POLICY["allowed_shared_libraries"])
    missing = sorted(required - set(needed))
    if missing:
        violations.append(f"missing required shared libraries: {missing}")
    unexpected = sorted(set(needed) - allowed)
    if unexpected:
        violations.append(f"unexpected shared libraries: {unexpected}")
    if NATIVE_ABI_POLICY["require_no_rpath_or_runpath"] and runpaths:
        violations.append(f"RPATH/RUNPATH is forbidden: {runpaths}")
    if too_new:
        violations.append(f"GLIBC symbols exceed {max_glibc}: {too_new}")

    return {
        "policy": dict(NATIVE_ABI_POLICY),
        "status": "PASS" if not violations else "FAIL",
        "violations": violations,
        "observed": {
            "architecture": machine,
            "elf_class": elf_class,
            "interpreter": interpreter,
            "needed": needed,
            "rpath_runpath": runpaths,
            "glibc_symbols": glibc_versions,
            "max_glibc_symbol": glibc_versions[-1] if glibc_versions else None,
        },
    }


def _field(text: str, name: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(name)}:\s*(.+?)\s*$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


__all__ = ["NATIVE_ABI_POLICY", "inspect_native_abi"]
