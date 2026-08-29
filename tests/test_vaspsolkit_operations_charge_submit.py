from __future__ import annotations

from pathlib import Path


def _prepared_case(root: Path) -> None:
    from vaspsolkit.config import KitConfig, write_kit_config
    from vaspsolkit.state import JobRecord, WorkflowState

    root.mkdir()
    for name in ("POSCAR", "INCAR", "KPOINTS", "POTCAR", "CHGCAR"):
        (root / name).write_text("neutral\n", encoding="utf-8")
    (root / "vasp.slurm").write_text(
        "#!/bin/bash\n#SLURM -l nodes=1:ppn=48\nmpirun vasp_std\n",
        encoding="utf-8",
    )
    write_kit_config(root / "vaspsolkit.json", KitConfig())
    for point in ("2", "3"):
        folder = root / "charge_sweep" / point
        folder.mkdir(parents=True)
        for name in ("POSCAR", "INCAR", "KPOINTS", "POTCAR", "CHGCAR", "vasp.slurm"):
            (folder / name).write_text(f"{point}-{name}\n", encoding="utf-8")
    WorkflowState(
        stage="charge_ready",
        neutral=JobRecord(folder=".", status="CONVERGED", job_id="neutral.1"),
        prepared_checked=True,
        jobs={
            "2": JobRecord(folder="charge_sweep/2", status="PREPARED"),
            "3": JobRecord(folder="charge_sweep/3", status="QUEUED", job_id="old.3"),
        },
    ).save(root / "vaspsolkit.state.json")


def _resources():
    from vaspsolkit.operations.actions import ResourceRequest

    return ResourceRequest.create(
        allocation="specified",
        nodes=("node24",),
        tasks=48,
        partition="normal",
        walltime="48:00:00",
        script="vasp.slurm",
    )


def test_submit_selected_plan_targets_only_prepared_user_selection(tmp_path: Path) -> None:
    from vaspsolkit.operations.controller import WorkbenchController

    case = tmp_path / "case"
    _prepared_case(case)
    controller = WorkbenchController(case)
    plan = controller.plan("submit-selected", _resources(), selected=("2",))
    assert plan.target_jobs == ("2",)
    assert plan.commands_summary == ("sbatch × 1",)
    assert plan.blocked_reason == ""
    assert plan.scheduler_request.nodes == ("node24",)


def test_submit_selected_plan_blocks_non_prepared_jobs(tmp_path: Path) -> None:
    from vaspsolkit.operations.controller import WorkbenchController

    case = tmp_path / "case"
    _prepared_case(case)
    controller = WorkbenchController(case)
    plan = controller.plan("submit-selected", _resources(), selected=("3",))
    assert "PREPARED" in plan.blocked_reason
    assert plan.target_jobs == ("3",)






def test_confirmed_selected_submission_calls_sbatch_once_and_updates_only_that_job(
    tmp_path: Path,
) -> None:
    from vaspsolkit.state import WorkflowState
    from vaspsolkit.operations.controller import WorkbenchController

    class FakeScheduler:
        def __init__(self):
            self.calls = []

        def submit(self, folder, script, **kwargs):
            self.calls.append((Path(folder).name, script, kwargs))
            return "127500.node01"

    case = tmp_path / "case"
    _prepared_case(case)
    scheduler = FakeScheduler()
    controller = WorkbenchController(
        case,
        scheduler_factory=lambda _config: scheduler,
        activity_state_root=tmp_path / "activities",
    )
    plan = controller.plan("submit-selected", _resources(), selected=("2",))
    result = controller.execute(plan, confirmed=True)
    state = WorkflowState.load(case / "vaspsolkit.state.json")
    assert result.ok
    assert result.job_ids == {"2": "127500.node01"}
    assert len(scheduler.calls) == 1
    assert state.jobs["2"].status == "SUBMITTED"
    assert state.jobs["2"].job_id == "127500.node01"
    assert state.jobs["3"].status == "QUEUED"


def test_unknown_charge_sbatch_marks_charge_recovery_without_corrupting_neutral(
    tmp_path: Path,
) -> None:
    from vaspsolkit.state import WorkflowState
    from vaspsolkit.operations.controller import WorkbenchController

    class FailingScheduler:
        def submit(self, folder, script, **kwargs):
            raise RuntimeError("sbatch connection dropped")

    case = tmp_path / "case"
    _prepared_case(case)
    controller = WorkbenchController(
        case,
        scheduler_factory=lambda _config: FailingScheduler(),
        activity_state_root=tmp_path / "activities",
    )
    plan = controller.plan("submit-selected", _resources(), selected=("2",))
    result = controller.execute(plan, confirmed=True)
    state = WorkflowState.load(case / "vaspsolkit.state.json")
    assert not result.ok
    assert result.snapshot.neutral.status == "CONVERGED"
    charge = next(job for job in result.snapshot.charge_jobs if job.name == "2")
    assert charge.status == "SUBMIT_UNKNOWN"
    assert state.jobs["2"].status == "PREPARED"


def test_charge_sbatch_job_id_survives_local_state_save_failure(tmp_path: Path) -> None:
    from unittest.mock import patch

    from vaspsolkit.operations.controller import WorkbenchController

    class AcceptedScheduler:
        def submit(self, folder, script, **kwargs):
            return "127501.node01"

    case = tmp_path / "case"
    _prepared_case(case)
    controller = WorkbenchController(
        case,
        scheduler_factory=lambda _config: AcceptedScheduler(),
        activity_state_root=tmp_path / "activities",
    )
    plan = controller.plan("submit-selected", _resources(), selected=("2",))
    with patch("vaspsolkit.orchestrator.WorkflowState.save", side_effect=OSError("disk full")):
        result = controller.execute(plan, confirmed=True)
    assert not result.ok
    assert result.job_ids == {"2": "127501.node01"}
    charge = next(job for job in result.snapshot.charge_jobs if job.name == "2")
    assert charge.status == "SUBMIT_UNKNOWN"
    assert charge.job_id == "127501.node01"


def test_reset_queued_plan_rechecks_pbs_then_marks_job_prepared(tmp_path: Path) -> None:
    from vaspsolkit.scheduler import JobState
    from vaspsolkit.state import WorkflowState
    from vaspsolkit.operations.controller import WorkbenchController

    class QueueScheduler:
        def __init__(self):
            self.status_calls = []
            self.cancel_calls = []

        def status(self, job_id):
            self.status_calls.append(job_id)
            return JobState(job_id, True, "Q")

        def cancel(self, job_id):
            self.cancel_calls.append(job_id)

    case = tmp_path / "case"
    _prepared_case(case)
    scheduler = QueueScheduler()
    controller = WorkbenchController(
        case,
        scheduler_factory=lambda _config: scheduler,
    )
    plan = controller.plan_reset_queued(("3",), _resources())
    assert plan.target_jobs == ("3",)
    assert plan.commands_summary == ("squeue × 1", "scancel ≤ 1")
    assert plan.blocked_reason == ""
    result = controller.execute(plan, confirmed=True)
    state = WorkflowState.load(case / "vaspsolkit.state.json")
    assert result.ok
    assert scheduler.cancel_calls == ["old.3"]
    assert state.jobs["3"].status == "PREPARED"
    assert state.jobs["3"].job_id == ""


def test_reset_queued_plan_blocks_running_job_before_scancel(tmp_path: Path) -> None:
    from vaspsolkit.scheduler import JobState
    from vaspsolkit.operations.controller import WorkbenchController

    class RunningScheduler:
        def status(self, job_id):
            return JobState(job_id, True, "R")

        def cancel(self, job_id):
            raise AssertionError("scancel must not be called for RUNNING jobs")

    case = tmp_path / "case"
    _prepared_case(case)
    controller = WorkbenchController(
        case,
        scheduler_factory=lambda _config: RunningScheduler(),
    )
    plan = controller.plan_reset_queued(("3",), _resources())
    assert "RUNNING" in plan.blocked_reason
