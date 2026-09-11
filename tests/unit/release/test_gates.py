from athena.release.gates import candidate_commands, release_commands


def test_candidate_gate_has_no_live_hermes_dependency():
    assert not any("hermes" in command.lower() for command in candidate_commands())


def test_core_release_lanes_exclude_optional_hermes_by_default():
    names = {name for name, _command in release_commands("uv", skip_e2e=False, bootstrap=False)}

    assert "hermes-live" not in names
    assert "e2e" in names
    assert "functional-proof" in names
    assert "backend-passport" in names
    assert "clean-install-upgrade-rollback" in names


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


def test_release_can_opt_into_real_beta_endurance():
    names = {
        name
        for name, _command in release_commands(
            "uv",
            skip_e2e=False,
            bootstrap=False,
            include_endurance=True,
        )
    }

    assert "endurance" in names


def test_python_release_lanes_use_frozen_uv_interpreter():
    lanes = dict(release_commands("uv", skip_e2e=False, bootstrap=False))
    for name in ("backend-passport", "endurance", "toolchain-passport"):
        assert lanes[name][:4] == ["uv", "run", "--frozen", "--extra"]
        assert "python" in lanes[name]


def test_uv_lock_check_uses_the_resolved_release_uv():
    lanes = dict(release_commands("/opt/athena/uv", skip_e2e=False, bootstrap=False))
    assert lanes["uv-lock-check"] == ["/opt/athena/uv", "lock", "--check", "--offline"]
