"""Structured diagnostic normalization tests."""

from athena.execution.diagnostics import normalize_diagnostics, normalize_diagnostics_payload


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


def test_json_diagnostics_have_occurrence_and_semantic_signatures():
    first = normalize_diagnostics_payload(
        {
            "diagnostics": [
                {
                    "severity": "error",
                    "code": "E001",
                    "path": "/tmp/a.py",
                    "line": 3,
                    "message": "cannot open '/tmp/a.py'",
                    "related": [{"message": "context"}],
                }
            ],
        },
        tool="compiler",
        source_tool_version="1.2",
    )
    second = normalize_diagnostics(
        "error[E001]: cannot open '/tmp/b.py'",
        tool="compiler",
        source_tool_version="1.2",
    )

    assert first[0].occurrence_fingerprint
    assert first[0].signature_fingerprint
    assert first[0].signature_fingerprint == second[0].signature_fingerprint
    assert first[0].to_dict()["source_tool_version"] == "1.2"
