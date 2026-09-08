from athena.release.gates import candidate_commands, release_commands


def test_candidate_gate_has_no_live_hermes_dependency():
    assert not any("hermes" in command.lower() for command in candidate_commands())


def test_core_release_lanes_exclude_optional_hermes_by_default():
    names = {name for name, _command in release_commands("uv", skip_e2e=False, bootstrap=False)}

    assert "hermes-live" not in names
    assert "e2e" in names


def test_release_can_opt_into_hermes_live_evidence():
    names = {
        name
        for name, _command in release_commands(
            "uv",
            skip_e2e=False,
            bootstrap=False,
            include_hermes_live=True,
        )
    }

    assert "hermes-live" in names


def test_skip_e2e_never_claims_optional_hermes_certification():
    names = {
        name
        for name, _command in release_commands(
            "uv",
            skip_e2e=True,
            bootstrap=False,
            include_hermes_live=True,
        )
    }

    assert "hermes-live" not in names
