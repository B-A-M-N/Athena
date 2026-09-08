from __future__ import annotations

from pathlib import Path

from athena.release import native_abi


def _readelf_output(_binary: Path, *args: str) -> str:
    if args == ("-h",):
        return "Class: ELF64\nMachine: Advanced Micro Devices X86-64\n"
    if args == ("-l",):
        return "Requesting program interpreter: /lib64/ld-linux-x86-64.so.2"
    if args == ("-d",):
        return "\n".join(
            f"Shared library: [{name}]"
            for name in native_abi.NATIVE_ABI_POLICY["required_shared_libraries"]
        )
    if args == ("--version-info",):
        return "Name: GLIBC_2.34\nName: GLIBC_2.2.5\n"
    raise AssertionError(args)


def test_native_abi_requires_and_allows_declared_libraries(monkeypatch, tmp_path):
    monkeypatch.setattr(native_abi, "_readelf", _readelf_output)

    result = native_abi.inspect_native_abi(tmp_path / "athena-terminal")

    assert result["status"] == "PASS"
    assert result["observed"]["max_glibc_symbol"] == "2.34"
    assert set(native_abi.NATIVE_ABI_POLICY["required_shared_libraries"]) <= set(
        result["observed"]["needed"]
    )


def test_native_abi_rejects_missing_required_library(monkeypatch, tmp_path):
    def missing_library(binary, *args):
        output = _readelf_output(binary, *args)
        if args == ("-d",):
            return output.replace("Shared library: [libGL.so.1]\n", "")
        return output

    monkeypatch.setattr(native_abi, "_readelf", missing_library)

    result = native_abi.inspect_native_abi(tmp_path / "athena-terminal")

    assert result["status"] == "FAIL"
    assert any("missing required shared libraries" in item for item in result["violations"])


def test_native_abi_rejects_unallowed_library(monkeypatch, tmp_path):
    def extra_library(binary, *args):
        output = _readelf_output(binary, *args)
        if args == ("-d",):
            return output + "Shared library: [libunexpected.so.1]\n"
        return output

    monkeypatch.setattr(native_abi, "_readelf", extra_library)

    result = native_abi.inspect_native_abi(tmp_path / "athena-terminal")

    assert result["status"] == "FAIL"
    assert any("unexpected shared libraries" in item for item in result["violations"])
