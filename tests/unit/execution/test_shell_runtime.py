from athena.execution.runtimes.shell import (
    detect_end_of_execution,
    preprocess_shell,
)


def test_shell_completion_marker_is_invocation_specific_and_exact():
    first = preprocess_shell("printf 'hello'", marker="##athena_end_111##")
    second = preprocess_shell("printf 'hello'", marker="##athena_end_222##")

    assert "##athena_end_111##%s" in first
    assert "##athena_end_222##%s" in second
    assert "##end_of_execution##" not in first
    assert detect_end_of_execution("noise ##athena_end_111##0\n", "##athena_end_111##")
    assert not detect_end_of_execution("noise ##athena_end_111##0\n", "##athena_end_222##")
