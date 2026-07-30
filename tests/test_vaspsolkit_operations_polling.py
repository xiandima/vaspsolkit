from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import threading
from unittest.mock import patch

import pytest

from vaspsolkit.config import KitConfig
from vaspsolkit.scheduler import JobState
from vaspsolkit.state import JobRecord, WorkflowState


class FakeScheduler:
    def __init__(self, statuses=None, error: Exception | None = None) -> None:
        self.statuses = statuses or {}
        self.error = error
        self.status_calls: list[str] = []
        self.inspect_calls = 0

    def status(self, job_id: str) -> JobState:
        self.status_calls.append(job_id)
        if self.error is not None:
            raise self.error
        return self.statuses[job_id]

    def inspect(self):
        self.inspect_calls += 1
        raise AssertionError("global queue inspection is forbidden")


def _write_state(root: Path, state: WorkflowState) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "vaspsolkit.state.json"
    state.save(path)
    return path


def _write_initialized_case(root: Path, state: WorkflowState) -> None:
    _write_state(root, state)
    (root / "POSCAR").write_text(
        "Pt\n1\n1 0 0\n0 1 0\n0 0 1\nPt\n1\nDirect\n0 0 0\n",
        encoding="utf-8",
    )
    (root / "INCAR").write_text("IBRION = 2\nNSW = 100\n", encoding="utf-8")
    (root / "KPOINTS").write_text("Gamma\n0\nGamma\n1 1 1\n0 0 0\n", encoding="utf-8")
    (root / "POTCAR").write_text("potcar\n", encoding="utf-8")
    (root / "vaspsolkit.json").write_text(
        json.dumps({"scheduler": {"kind": "pbs", "queue": "normal", "cores": 24}}),
        encoding="utf-8",
    )


def test_refresh_queries_each_unique_recorded_job_once_without_global_inspection(tmp_path: Path) -> None:
    from vaspsolkit.orchestrator import refresh_recorded_jobs

    root = tmp_path / "case"
    state = WorkflowState(
        stage="monitor",
        neutral=JobRecord(".", "SUBMITTED", "128042.server"),
        jobs={
            "q1": JobRecord("charge/q1", "SUBMITTED", "128043.server"),
            "q2": JobRecord("charge/q2", "QUEUED", "128043.server"),
            "done": JobRecord("charge/done", "CONVERGED", "128044.server"),
        },
    )
    _write_state(root, state)
    fake = FakeScheduler(
        {
            "128042.server": JobState("128042.server", True, "R"),
            "128043.server": JobState("128043.server", True, "Q"),
        }
    )

    result = refresh_recorded_jobs(root, KitConfig(), state, scheduler=fake)

    assert fake.status_calls == ["128042.server", "128043.server"]
    assert fake.inspect_calls == 0
    assert result.neutral is not None and result.neutral.status == "RUNNING"
    assert result.jobs["q1"].status == result.jobs["q2"].status == "QUEUED"
    assert result.jobs["done"].status == "CONVERGED"


def test_refresh_with_no_recorded_ids_does_not_construct_or_call_scheduler(tmp_path: Path) -> None:
    from vaspsolkit.orchestrator import refresh_recorded_jobs

    root = tmp_path / "case"
    state = WorkflowState(neutral=JobRecord(".", "PREPARED"))
    path = _write_state(root, state)
    before = path.read_bytes()
    fake = FakeScheduler()

    result = refresh_recorded_jobs(root, KitConfig(), state, scheduler=fake)

    assert result.neutral is not None and result.neutral.status == "PREPARED"
    assert fake.status_calls == []
    assert path.read_bytes() == before


def test_collect_phase_only_queries_and_never_changes_case_files(tmp_path: Path) -> None:
    from vaspsolkit.orchestrator import (
        capture_recorded_jobs,
        collect_recorded_job_statuses,
    )

    root = tmp_path / "case"
    state = WorkflowState(neutral=JobRecord(".", "SUBMITTED", "pure.server"))
    path = _write_state(root, state)
    captured = capture_recorded_jobs(root)
    before = path.read_bytes()
    before_stat = path.stat()
    fake = FakeScheduler(
        {"pure.server": JobState("pure.server", True, "R")}
    )

    collected = collect_recorded_job_statuses(captured, fake)

    assert tuple(status.job_id for status in collected.statuses) == ("pure.server",)
    assert path.read_bytes() == before
    after_stat = path.stat()
    assert (after_stat.st_ino, after_stat.st_mtime_ns, after_stat.st_ctime_ns) == (
        before_stat.st_ino,
        before_stat.st_mtime_ns,
        before_stat.st_ctime_ns,
    )


def test_collect_rejects_scheduler_result_for_another_job_id(tmp_path: Path) -> None:
    from vaspsolkit.orchestrator import (
        capture_recorded_jobs,
        collect_recorded_job_statuses,
    )

    root = tmp_path / "case"
    _write_state(
        root, WorkflowState(neutral=JobRecord(".", "SUBMITTED", "expected.server"))
    )
    captured = capture_recorded_jobs(root)
    fake = FakeScheduler(
        {"expected.server": JobState("other.server", True, "R")}
    )

    with pytest.raises(RuntimeError, match="different Job ID"):
        collect_recorded_job_statuses(captured, fake)


def test_unknown_preserves_recorded_status_and_terminal_records(tmp_path: Path) -> None:
    from vaspsolkit.orchestrator import refresh_recorded_jobs

    root = tmp_path / "case"
    state = WorkflowState(
        stage="monitor",
        neutral=JobRecord(".", "RUNNING", "128100.server"),
        jobs={"failed": JobRecord("charge/failed", "FAILED", "128101.server")},
    )
    _write_state(root, state)
    fake = FakeScheduler(
        {"128100.server": JobState("128100.server", True, "UNKNOWN", "pbs timeout")}
    )

    before = (root / "vaspsolkit.state.json").read_bytes()
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        refresh_recorded_jobs(root, KitConfig(), state, scheduler=fake)

    assert fake.status_calls == ["128100.server"]
    assert state.neutral is not None and state.neutral.status == "RUNNING"
    assert state.jobs["failed"].status == "FAILED"
    assert (root / "vaspsolkit.state.json").read_bytes() == before


def test_default_scheduler_runner_has_a_bounded_subprocess_timeout() -> None:
    from vaspsolkit.scheduler import PBSScheduler

    completed = subprocess.CompletedProcess(["qstat", "1.server"], 0, "", "")
    with patch("subprocess.run", return_value=completed) as run:
        PBSScheduler().status("1.server")
    assert run.call_args.kwargs["timeout"] == 30


def test_query_failure_leaves_state_file_and_passed_state_unchanged(tmp_path: Path) -> None:
    from vaspsolkit.orchestrator import refresh_recorded_jobs

    root = tmp_path / "case"
    state = WorkflowState(
        stage="neutral_submitted",
        neutral=JobRecord(".", "SUBMITTED", "128200.server"),
    )
    path = _write_state(root, state)
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match="qstat unavailable"):
        refresh_recorded_jobs(
            root, KitConfig(), state, scheduler=FakeScheduler(error=RuntimeError("qstat unavailable"))
        )

    assert state.neutral is not None and state.neutral.status == "SUBMITTED"
    assert path.read_bytes() == before


def test_job_missing_from_queue_uses_local_outputs_for_convergence(tmp_path: Path) -> None:
    from vaspsolkit.orchestrator import refresh_recorded_jobs

    root = tmp_path / "case"
    state = WorkflowState(
        stage="neutral_submitted",
        neutral=JobRecord(".", "RUNNING", "128250.server"),
    )
    _write_state(root, state)
    (root / "OUTCAR").write_text("reached required accuracy\n", encoding="utf-8")
    for name in ("CONTCAR", "CHGCAR", "LOCPOT"):
        (root / name).write_text(f"{name}\n", encoding="utf-8")
    fake = FakeScheduler(
        {"128250.server": JobState("128250.server", False, "MISSING")}
    )

    result = refresh_recorded_jobs(root, KitConfig(), state, scheduler=fake)

    assert result.neutral is not None and result.neutral.status == "CONVERGED"
    assert result.stage == "neutral_converged"


def test_missing_unstarted_charge_returns_to_prepared_and_charge_ready(tmp_path: Path) -> None:
    from vaspsolkit.orchestrator import refresh_recorded_jobs

    root = tmp_path / "case"
    (root / "charge/q1").mkdir(parents=True)
    state = WorkflowState(
        stage="monitor",
        neutral=JobRecord(".", "CONVERGED", "neutral.done"),
        jobs={"q1": JobRecord("charge/q1", "RUNNING", "charge.server")},
    )
    _write_state(root, state)
    fake = FakeScheduler({"charge.server": JobState("charge.server", False, "MISSING")})

    result = refresh_recorded_jobs(root, KitConfig(), state, scheduler=fake)

    assert result.jobs["q1"].status == "PREPARED"
    assert result.jobs["q1"].job_id == ""
    assert result.stage == "charge_ready"


def test_case_or_state_change_during_query_blocks_stale_save(tmp_path: Path) -> None:
    from vaspsolkit.orchestrator import refresh_recorded_jobs

    root = tmp_path / "case"
    state = WorkflowState(neutral=JobRecord(".", "SUBMITTED", "128300.server"))
    path = _write_state(root, state)

    class MutatingScheduler(FakeScheduler):
        def status(self, job_id: str) -> JobState:
            changed = WorkflowState(neutral=JobRecord(".", "BLOCKED", job_id))
            changed.save(path)
            return JobState(job_id, True, "R")

    with pytest.raises(RuntimeError, match="changed during scheduler refresh"):
        refresh_recorded_jobs(root, KitConfig(), state, scheduler=MutatingScheduler())

    assert WorkflowState.load(path).neutral.status == "BLOCKED"  # type: ignore[union-attr]
    lock_path = path.with_name(f".{path.name}.lock")
    assert lock_path.stat().st_mode & 0o777 == 0o600






















def test_workflow_state_saves_are_serialized_by_one_stable_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vaspsolkit.state as state_module

    path = tmp_path / "vaspsolkit.state.json"
    WorkflowState(neutral=JobRecord(".", "PREPARED")).save(path)
    original_replace = state_module._replace_state
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def controlled_replace(source, destination):
        nonlocal calls
        with calls_lock:
            calls += 1
            call = calls
        if call == 1:
            first_entered.set()
            assert release_first.wait(1.0)
        else:
            second_entered.set()
        return original_replace(source, destination)

    monkeypatch.setattr(state_module, "_replace_state", controlled_replace)
    first = threading.Thread(
        target=lambda: WorkflowState(
            neutral=JobRecord(".", "RUNNING", "one.server")
        ).save(path)
    )
    second = threading.Thread(
        target=lambda: WorkflowState(
            neutral=JobRecord(".", "BLOCKED", "two.server")
        ).save(path)
    )
    first.start()
    assert first_entered.wait(1.0)
    second.start()
    assert not second_entered.wait(0.05)
    release_first.set()
    first.join(1.0)
    second.join(1.0)
    assert not first.is_alive() and not second.is_alive()
    assert second_entered.is_set()
    assert WorkflowState.load(path).neutral.status == "BLOCKED"  # type: ignore[union-attr]


def test_state_lock_alias_is_reentrant_and_hardlink_is_rejected(tmp_path: Path) -> None:
    from vaspsolkit.state import workflow_state_lock

    path = tmp_path / "vaspsolkit.state.json"
    alias = tmp_path / ".." / tmp_path.name / path.name
    with workflow_state_lock(path):
        with workflow_state_lock(alias):
            pass

    victim = tmp_path / "victim"
    victim.write_text("do not chmod", encoding="utf-8")
    victim.chmod(0o644)
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.unlink()
    os.link(victim, lock_path)
    with pytest.raises(ValueError, match="stable regular file"):
        WorkflowState().save(path)
    assert victim.stat().st_mode & 0o777 == 0o644


def test_state_lock_rejects_symlink_parent_before_write(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe state parent"):
        WorkflowState().save(alias / "vaspsolkit.state.json")
    assert not (real / "vaspsolkit.state.json").exists()




