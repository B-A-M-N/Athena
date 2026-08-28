from athena.cli.app import Options, _arg_parse
from athena.cli.native import worker_command
from athena.cli.native_session import parse_args


def test_native_command_is_available_in_argparse_fallback():
    options = _arg_parse(["native", "--workspace", "/tmp/project"])

    assert options.command == "native"
    assert options.workspace == "/tmp/project"


def test_native_worker_command_forwards_scope_without_credentials():
    options = Options(
        command="native",
        config_path="/tmp/athena.toml",
        db_path="/tmp/athena.db",
        workspace="/tmp/project",
        autonomy="coding",
        model="openrouter/free",
        criteria="command:pytest -q;report exists",
        verbose=True,
    )

    command = worker_command(options)

    assert command[:3] == [command[0], "-m", "athena.cli.native_session"]
    assert "--workspace" in command
    assert "--model" in command
    assert "OPENROUTER_API_KEY" not in command


def test_native_session_parser_matches_worker_contract():
    options = parse_args(
        [
            "--db",
            "/tmp/athena.db",
            "--workspace",
            "/tmp/project",
            "--autonomy",
            "coding",
            "--criteria",
            "tests pass",
        ]
    )

    assert options.command == "native"
    assert options.db_path == "/tmp/athena.db"
    assert options.workspace == "/tmp/project"
    assert options.autonomy == "coding"
    assert options.criteria == "tests pass"
