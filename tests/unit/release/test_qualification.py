from __future__ import annotations

from pathlib import Path

from athena.release.qualification import benchmark_environment, qualify_benchmark_environment


def _proc_entry(root: Path, pid: str, command: str, run_id: str | None = None) -> None:
    entry = root / pid
    entry.mkdir()
    (entry / "cmdline").write_bytes(command.encode() + b"\0")
    environ = b"PATH=/usr/bin\0"
    if run_id is not None:
        environ += f"ATHENA_RELEASE_RUN_ID={run_id}\0".encode()
    (entry / "environ").write_bytes(environ)


def test_qualification_counts_runs_not_launcher_processes(tmp_path, monkeypatch):
    _proc_entry(tmp_path, "10", "scripts/release-check", "run-a")
    _proc_entry(tmp_path, "11", "python scripts/bench-alacrity", "run-a")
    _proc_entry(tmp_path, "12", "scripts/release-check", "run-b")
    _proc_entry(tmp_path, "13", "python -m pytest", None)
    monkeypatch.setenv("ATHENA_RELEASE_RUN_ID", "run-a")

    record = benchmark_environment(proc_root=tmp_path)

    assert record["other_release_instances"] == 1
    assert record["other_release_run_ids"] == ["run-b"]


def test_standalone_benchmark_without_run_id_does_not_count_itself(tmp_path, monkeypatch):
    _proc_entry(tmp_path, "20", "python scripts/bench-indexing", None)
    monkeypatch.delenv("ATHENA_RELEASE_RUN_ID", raising=False)

    record = benchmark_environment(proc_root=tmp_path)

    assert record["other_release_instances"] == 0


def test_unreadable_process_entry_is_ignored(tmp_path, monkeypatch):
    entry = tmp_path / "30"
    entry.mkdir()
    (entry / "cmdline").write_bytes(b"scripts/release-check\0")
    monkeypatch.setenv("ATHENA_RELEASE_RUN_ID", "run-a")

    assert benchmark_environment(proc_root=tmp_path)["other_release_instances"] == 0


def test_qualification_reports_load_memory_and_steal_reasons(tmp_path, monkeypatch):
    monkeypatch.setattr("athena.release.qualification.os.getloadavg", lambda: (2.0, 0.0, 0.0))
    monkeypatch.setattr("athena.release.qualification.os.cpu_count", lambda: 1)
    monkeypatch.setattr(
        "athena.release.qualification.Path.read_text",
        lambda self, encoding="utf-8": (
            "cpu  1 0 0 0 0 0 0 100 0 0\n"
            if str(self) == "/proc/stat"
            else "MemAvailable: 128 kB\n"
        ),
    )

    record = qualify_benchmark_environment()

    assert record["status"] == "BENCH_INVALID_ENVIRONMENT"
    assert "load_per_cpu>0.90" in record["reasons"]
    assert "memory_available<512MB" in record["reasons"]
