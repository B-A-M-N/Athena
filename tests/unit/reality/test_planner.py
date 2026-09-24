from __future__ import annotations

from athena.protocol.tasks import TaskSpec, VerificationType
from athena.verification import VerificationPlanner


def test_planner_selects_stable_independent_project_probes():
    plan = VerificationPlanner().plan(
        TaskSpec(id="task", objective="change source"),
        {
            "commands": {
                "python": ("python", "python -m pytest", "ruff check", "mypy"),
                "javascript": ("npm test", "npm run build"),
            }
        },
    )

    assert [item.verification.command for item in plan.criteria] == [
        "python -m pytest",
        "ruff check",
        "mypy",
        "npm run build",
        "npm test",
    ]
    assert all(item.verification.type is VerificationType.COMMAND for item in plan.criteria)
    assert "python" in plan.skipped_commands


def test_planner_rejects_shell_composition_from_profile_catalog():
    plan = VerificationPlanner().plan(
        TaskSpec(id="task", objective="change source"),
        {"commands": {"python": ("pytest && rm -rf /", "pytest")}},
    )

    assert [item.verification.command for item in plan.criteria] == ["pytest"]
    assert plan.skipped_commands == ("pytest && rm -rf /",)


def test_planner_keeps_impacted_tests_as_project_resources():
    plan = VerificationPlanner().plan(
        TaskSpec(id="task", objective="change source"),
        {"commands": {"python": ("pytest",)}},
        changed_resources=("src/app.py",),
        impact={
            "affected_tests": ["tests/test_app.py"],
            "index_revision": "idx-1",
        },
    )

    assert plan.impacted_tests == ("tests/test_app.py",)
    assert plan.index_revision == "idx-1"


def test_profiler_does_not_turn_installed_tools_into_acceptance_gates_without_tests(tmp_path):
    from athena.project.profile import ProjectInspector

    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    profile = ProjectInspector().inspect(str(tmp_path))
    plan = VerificationPlanner().plan(
        TaskSpec(id="task", objective="report observed state"),
        profile,
    )

    assert plan.criteria == ()
    assert profile.commands == {"python": ("python",)}
