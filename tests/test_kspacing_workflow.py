from pathlib import Path

import pytest

from vaspsolkit.config import KitConfig, WorkflowConfig, write_kit_config
from vaspsolkit.orchestrator import (
    STATE_FILENAME,
    check_prepared_jobs,
    prepare_charge_jobs,
    prepare_neutral_job,
    submit_neutral_job,
    submit_ready_jobs,
    submit_selected_jobs,
)
from vaspsolkit.state import JobRecord, WorkflowState


MESH = "Gamma\n0\nGamma\n2 2 1\n0 0 0\n"
SAMPLING = "kspacing = 2.5D-1; kgamma = .FALSE. ! preserve native policy\n"


def _case(root: Path, sampling: str = SAMPLING) -> KitConfig:
    (root / "POSCAR").write_text(
        "synthetic C\n1\n3 0 0\n0 3 0\n0 0 12\nC\n1\nDirect\n0 0 0\n"
    )
    (root / "INCAR").write_text("ENCUT=450; IBRION=2; NSW=80\n" + sampling)
    (root / "POTCAR").write_text("TITEL = PAW_PBE C 08Apr2002\nENMAX = 400.0 eV\n")
    (root / "vasp.slurm").write_text("#!/bin/sh\n# synthetic submission script\n")
    config = KitConfig(workflow=WorkflowConfig(
        nelect_ref=4.0, folders=["minus", "neutral", "plus"],
        nelect_offsets=[-0.5, 0.0, 0.5], she_reference_confirmed=True,
    ))
    write_kit_config(root / "vaspsolkit.json", config)
    return config


def _neutral_outputs(root: Path) -> None:
    # Synthetic provenance only: no VASP execution or convergence claim.
    (root / "CONTCAR").write_bytes((root / "POSCAR").read_bytes())
    for name in ("OUTCAR", "CHGCAR", "LOCPOT"):
        (root / name).write_text(f"synthetic {name}\n")
    WorkflowState(
        stage="neutral_converged",
        neutral=JobRecord(folder=".", status="CONVERGED", metadata={"stage": "neutral_relax"}),
    ).save(root / STATE_FILENAME)


class FakeScheduler:
    def __init__(self):
        self.calls = []

    def inspect(self):
        return []

    def submit(self, folder, script, **kwargs):
        self.calls.append((Path(folder), script, kwargs))
        return f"synthetic-{len(self.calls)}"


@pytest.mark.parametrize("spacing", ["0.25", "2.5D-1", "2.5d-1", ".25", "+2.5E-1"])
@pytest.mark.parametrize("gamma", [None, ".TRUE.", ".false.", "T", "F", "true", "FALSE"])
def test_native_sampling_accepts_vasp_numbers_and_logicals(tmp_path, spacing, gamma):
    from vaspsolkit.sampling import describe_sampling, kpoints_required

    text = f"# KSPACING=bad\nkspacing={spacing}"
    if gamma is not None:
        text += f"; kgamma={gamma}"
    (tmp_path / "INCAR").write_text(text + " ! KSPACING=0\n")
    assert kpoints_required(tmp_path) is False
    description = describe_sampling(tmp_path)
    assert "KSPACING" in description and "KGAMMA" in description
    assert "0.25" in description
    assert not (tmp_path / "KPOINTS").exists()


@pytest.mark.parametrize("sampling, tag", [
    ("KSPACING=0", "KSPACING"), ("KSPACING=-0.2", "KSPACING"),
    ("KSPACING=nan", "KSPACING"), ("KSPACING=inf", "KSPACING"),
    ("KSPACING=1D999", "KSPACING"), ("KSPACING=1D-999", "KSPACING"),
    ("KSPACING=", "KSPACING"), ("KSPACING=0.2 0.3", "KSPACING"),
    ("KSPACING=0_2", "KSPACING"), ("KSPACING=bad", "KSPACING"),
    ("KSPACING=0.2; KGAMMA=yes", "KGAMMA"),
    ("KSPACING=0.2; KGAMMA=", "KGAMMA"),
    ("KSPACING=0.2; KGAMMA=1", "KGAMMA"),
    ("KSPACING=0.2; KGAMMA=T F", "KGAMMA"),
    ("KSPACING=0.2; kspacing=0.3", "KSPACING"),
    ("KSPACING=0.2; KGAMMA=T; kgamma=F", "KGAMMA"),
])
def test_invalid_native_sampling_raises_informative_error(tmp_path, sampling, tag):
    from vaspsolkit.sampling import describe_sampling, kpoints_required

    (tmp_path / "INCAR").write_text(sampling + "\n")
    for inspect in (kpoints_required, describe_sampling):
        with pytest.raises(ValueError, match=tag):
            inspect(tmp_path)


def test_missing_or_commented_sampling_still_requires_kpoints(tmp_path):
    from vaspsolkit.sampling import kpoints_required

    assert kpoints_required(tmp_path) is True
    (tmp_path / "INCAR").write_text('# KSPACING=0.2\nSYSTEM="KSPACING=0.2"\nKGAMMA=T\n')
    assert kpoints_required(tmp_path) is True


@pytest.mark.parametrize("contents", [MESH, "", "invalid mesh\n"])
def test_existing_kpoints_always_takes_precedence(tmp_path, contents):
    from vaspsolkit.sampling import describe_sampling, kpoints_required

    (tmp_path / "INCAR").write_text("KSPACING=bad; KGAMMA=bad\n")
    (tmp_path / "KPOINTS").write_text(contents)
    assert kpoints_required(tmp_path) is True
    assert "ignored" in describe_sampling(tmp_path).lower()
    assert (tmp_path / "KPOINTS").read_text() == contents


def test_broken_kpoints_symlink_never_falls_back_to_native(tmp_path):
    from vaspsolkit.sampling import kpoints_required

    (tmp_path / "INCAR").write_text(SAMPLING)
    (tmp_path / "KPOINTS").symlink_to("missing-mesh")
    assert kpoints_required(tmp_path) is True


def test_native_case_initialization_preserves_sampling(tmp_path):
    from vaspsolkit.case_setup import apply_case_initialization, plan_case_initialization

    config = _case(tmp_path)
    plan = plan_case_initialization(tmp_path, config.scheduler, config.workflow)
    apply_case_initialization(plan, confirmed=True)
    assert SAMPLING in (tmp_path / "INCAR").read_text()
    assert not (tmp_path / "KPOINTS").exists()


@pytest.mark.parametrize("during_staging", [False, True])
def test_initialization_rejects_kpoints_added_after_preview(tmp_path, monkeypatch, during_staging):
    from vaspsolkit import case_setup

    config = _case(tmp_path)
    plan = case_setup.plan_case_initialization(tmp_path, config.scheduler, config.workflow)
    before = (tmp_path / "INCAR").read_bytes()
    if during_staging:
        original = case_setup._stage_file_change

        def stage(change):
            (tmp_path / "KPOINTS").write_text(MESH)
            return original(change)

        monkeypatch.setattr(case_setup, "_stage_file_change", stage)
    else:
        (tmp_path / "KPOINTS").write_text(MESH)
    with pytest.raises(RuntimeError, match="KPOINTS|sampling"):
        case_setup.apply_case_initialization(plan, confirmed=True)
    assert (tmp_path / "INCAR").read_bytes() == before
    assert not (tmp_path / STATE_FILENAME).exists()


def test_native_snapshots_are_ready_and_menu_can_prepare_neutral(tmp_path):
    from vaspsolkit.operations.controller import WorkbenchController
    from vaspsolkit.operations.snapshot import build_workbench_snapshot
    from vaspsolkit.tui_model import inspect_case

    _case(tmp_path)
    case = inspect_case(tmp_path)
    assert case.initialized and "KPOINTS" not in case.missing_files
    row = next(row for row in build_workbench_snapshot(tmp_path).input_rows if row.name == "KPOINTS")
    assert row.status == "READY" and not row.exists and not row.required
    assert "KSPACING" in row.summary
    controller = WorkbenchController(tmp_path)
    plan = controller.plan("prepare-neutral")
    assert plan.blocked_reason == ""
    assert controller.execute(plan, confirmed=True).ok


def test_invalid_native_snapshot_is_an_error_not_ready(tmp_path):
    from vaspsolkit.operations.snapshot import build_workbench_snapshot
    from vaspsolkit.tui_model import inspect_case

    _case(tmp_path, "KSPACING=0.2; KGAMMA=bad\n")
    case = inspect_case(tmp_path)
    assert not case.initialized
    assert any("KGAMMA" in diagnostic.detail for diagnostic in case.diagnostics)
    row = next(row for row in build_workbench_snapshot(tmp_path).input_rows if row.name == "KPOINTS")
    assert row.status == "ERROR" and "KGAMMA" in row.summary


def test_invalid_existing_mesh_does_not_use_valid_native_policy(tmp_path):
    from vaspsolkit.operations.snapshot import build_workbench_snapshot

    _case(tmp_path)
    (tmp_path / "KPOINTS").write_text("invalid mesh\n")
    row = next(row for row in build_workbench_snapshot(tmp_path).input_rows if row.name == "KPOINTS")
    assert row.status == "ERROR" and row.required


def test_neutral_and_charge_workflow_preserves_native_sampling(tmp_path):
    config = _case(tmp_path)
    scheduler = FakeScheduler()
    prepared = prepare_neutral_job(tmp_path, config)
    assert prepared.neutral.status == "PREPARED"
    assert SAMPLING in (tmp_path / "INCAR").read_text()
    submitted = submit_neutral_job(tmp_path, config, scheduler=scheduler, confirmed=True, require_prepared=True)
    assert submitted.neutral.status == "SUBMITTED"
    _neutral_outputs(tmp_path)
    state = prepare_charge_jobs(tmp_path, config)
    checked = check_prepared_jobs(tmp_path, config, state)
    assert checked.prepared_checked
    for record in state.jobs.values():
        folder = tmp_path / record.folder
        assert SAMPLING in (folder / "INCAR").read_text()
        assert not (folder / "KPOINTS").exists()
        assert (folder / "POSCAR").read_bytes() == (tmp_path / "CONTCAR").read_bytes()
    jobs = submit_ready_jobs(tmp_path, config, checked, scheduler=scheduler, confirmed=True, require_prepared_check=True)
    assert set(jobs) == set(state.jobs)
    assert len(scheduler.calls) == len(state.jobs) + 1


def test_submission_rechecks_native_policy_after_prepared_check(tmp_path):
    config = _case(tmp_path)
    _neutral_outputs(tmp_path)
    state = check_prepared_jobs(tmp_path, config, prepare_charge_jobs(tmp_path, config))
    name = next(iter(state.jobs))
    folder = tmp_path / state.jobs[name].folder
    (folder / "INCAR").write_text((folder / "INCAR").read_text().replace("2.5D-1", "nan"))
    scheduler = FakeScheduler()
    with pytest.raises(ValueError, match="KSPACING"):
        submit_selected_jobs(tmp_path, config, state, [name], scheduler=scheduler, confirmed=True)
    assert scheduler.calls == []


@pytest.mark.parametrize("name", ["POSCAR", "INCAR", "POTCAR", "vasp.slurm"])
def test_native_policy_does_not_make_other_neutral_inputs_optional(tmp_path, name):
    config = _case(tmp_path)
    (tmp_path / name).unlink()
    with pytest.raises(FileNotFoundError, match=name):
        prepare_neutral_job(tmp_path, config)


def test_menu_prepare_plan_tracks_absent_kpoints(tmp_path):
    from vaspsolkit.operations.controller import WorkbenchController

    _case(tmp_path)
    controller = WorkbenchController(tmp_path)
    plan = controller.plan("prepare-neutral")
    assert not plan.blocked_reason
    (tmp_path / "KPOINTS").write_text(MESH)
    with pytest.raises(RuntimeError, match="KPOINTS"):
        controller.execute(plan, confirmed=True)
    assert not (tmp_path / STATE_FILENAME).exists()


def test_legacy_prepare_and_submission_support_native_sampling(tmp_path):
    from vaspsolkit.workflow import prepare_jobs, submit_jobs

    config = _case(tmp_path)
    _neutral_outputs(tmp_path)
    jobs = prepare_jobs(tmp_path, config.workflow)
    scheduler = FakeScheduler()
    submitted = submit_jobs(tmp_path, config.workflow, scheduler)
    assert len(submitted) == len(jobs)
    for job in jobs:
        assert SAMPLING in (job.folder / "INCAR").read_text()
        assert not (job.folder / "KPOINTS").exists()


def test_legacy_prepare_rejects_invalid_native_sampling_before_writes(tmp_path):
    from vaspsolkit.workflow import prepare_jobs

    config = _case(tmp_path, "KSPACING=-1\n")
    _neutral_outputs(tmp_path)
    with pytest.raises(ValueError, match="KSPACING"):
        prepare_jobs(tmp_path, config.workflow)
    assert not (tmp_path / config.workflow.job_root).exists()


def test_explicit_mesh_still_copied_unchanged(tmp_path):
    config = _case(tmp_path, "KSPACING=-1; KGAMMA=bad\n")
    (tmp_path / "KPOINTS").write_text(MESH)
    _neutral_outputs(tmp_path)
    state = prepare_charge_jobs(tmp_path, config)
    assert check_prepared_jobs(tmp_path, config, state).prepared_checked
    for record in state.jobs.values():
        assert (tmp_path / record.folder / "KPOINTS").read_text() == MESH


@pytest.mark.parametrize("legacy", [False, True])
def test_native_reprepare_does_not_silently_keep_stale_child_kpoints(tmp_path, legacy):
    from vaspsolkit.workflow import prepare_jobs

    config = _case(tmp_path)
    _neutral_outputs(tmp_path)
    child = tmp_path / config.workflow.job_root / "minus"
    child.mkdir(parents=True)
    (child / "KPOINTS").write_text(MESH)
    with pytest.raises(ValueError, match="KPOINTS"):
        if legacy:
            prepare_jobs(tmp_path, config.workflow)
        else:
            prepare_charge_jobs(tmp_path, config, strict=False)
    assert (child / "KPOINTS").read_text() == MESH
    assert not (child / "INCAR").exists()


def test_menu_charge_submit_tracks_absent_kpoints(tmp_path):
    from vaspsolkit.operations.actions import ResourceRequest
    from vaspsolkit.operations.controller import WorkbenchController

    config = _case(tmp_path)
    _neutral_outputs(tmp_path)
    state = check_prepared_jobs(tmp_path, config, prepare_charge_jobs(tmp_path, config))
    scheduler = FakeScheduler()
    controller = WorkbenchController(tmp_path, scheduler_factory=lambda _config: scheduler)
    resources = ResourceRequest.create(
        allocation="auto", nodes=(), tasks=96, partition="compute", walltime="72:00:00", script="vasp.slurm",
    )
    plan = controller.plan("submit-selected", resources, selected=("minus",))
    assert not plan.blocked_reason
    child = tmp_path / state.jobs["minus"].folder
    (child / "KPOINTS").write_text(MESH)
    with pytest.raises(RuntimeError, match="KPOINTS"):
        controller.execute(plan, confirmed=True)
    assert scheduler.calls == []


@pytest.mark.parametrize("entrypoint", ["neutral-prepare", "neutral-submit", "charge-prepare", "legacy-submit"])
def test_all_backend_preflights_reject_invalid_active_sampling(tmp_path, entrypoint):
    from vaspsolkit.workflow import submit_jobs

    config = _case(tmp_path, "KSPACING=0.25; KGAMMA=invalid\n")
    _neutral_outputs(tmp_path)
    scheduler = FakeScheduler()
    with pytest.raises(ValueError, match="KGAMMA"):
        if entrypoint == "neutral-prepare":
            prepare_neutral_job(tmp_path, config)
        elif entrypoint == "neutral-submit":
            submit_neutral_job(tmp_path, config, scheduler=scheduler, confirmed=True)
        elif entrypoint == "charge-prepare":
            prepare_charge_jobs(tmp_path, config)
        else:
            config.workflow.job_root = "."
            config.workflow.folders = ["."]
            submit_jobs(tmp_path, config.workflow, scheduler)
    assert scheduler.calls == []


@pytest.mark.parametrize("name", ["INCAR", "POTCAR", "vasp.slurm", "CONTCAR", "OUTCAR", "CHGCAR", "LOCPOT"])
def test_native_policy_does_not_make_charge_sources_optional(tmp_path, name):
    config = _case(tmp_path)
    _neutral_outputs(tmp_path)
    (tmp_path / name).unlink()
    with pytest.raises(FileNotFoundError, match=name):
        prepare_charge_jobs(tmp_path, config)
    assert not (tmp_path / config.workflow.job_root).exists()
