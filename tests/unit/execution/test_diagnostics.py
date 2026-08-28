"""Structured diagnostic normalization tests."""

from athena.execution.diagnostics import normalize_diagnostics


def test_normalize_compiler_linter_and_cargo_shapes():
    output = """
src/main.py:4:2: error: incompatible type [ARG001]
src/main.py:8:1: F401 imported but unused
error[E0308]: mismatched types
 --> src/lib.rs:12:3
src/app.ts(5,7): error TS2322: type mismatch
"""

    diagnostics = normalize_diagnostics(output, tool="test")

    assert [(item.file, item.line, item.column, item.code) for item in diagnostics] == [
        ("src/main.py", 4, 2, None),
        ("src/main.py", 8, 1, "F401"),
        ("src/lib.rs", 12, 3, "E0308"),
        ("src/app.ts", 5, 7, "TS2322"),
    ]
    assert all(item.fingerprint for item in diagnostics)


def test_normalize_tracebacks_and_deduplicates_records():
    output = """
Traceback (most recent call last):
  File "src/main.py", line 9, in <module>
ValueError: invalid configuration
ValueError: invalid configuration
"""

    diagnostics = normalize_diagnostics(output, tool="python")

    assert len(diagnostics) == 1
    assert diagnostics[0].file == "src/main.py"
    assert diagnostics[0].line == 9
    assert diagnostics[0].message == "ValueError: invalid configuration"
