from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest


def _write_case(root: Path, *, incar: str = "ENCUT = 450\nIBRION = 1\nNSW = 88\n") -> None:
    (root / "POSCAR").write_text(
        "sample\n1\n1 0 0\n0 1 0\n0 0 1\nC\n1\nDirect\n0 0 0\n",
        encoding="utf-8",
    )
    (root / "INCAR").write_text(incar, encoding="utf-8")
    (root / "KPOINTS").write_text("Gamma\n0\nGamma\n1 1 1\n0 0 0\n", encoding="utf-8")
    (root / "POTCAR").write_text(
        "TITEL = PAW_PBE C 08Apr2002\nENMAX = 400.0 eV\n", encoding="utf-8"
    )
    (root / "vasp.pbs").write_text("#!/bin/sh\n", encoding="utf-8")


def _scheduler(**overrides):
    from vaspsolkit.config import SchedulerConfig

    values = {"queue": "workq", "cores": 24, "walltime": "12:00:00", "script": "vasp.pbs"}
    values.update(overrides)
    return SchedulerConfig(**values)


def _fingerprint(root: Path) -> tuple[tuple[str, str, int], ...]:
    return tuple(
        (str(path.relative_to(root)), path.read_text(encoding="utf-8"), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def test_plan_is_read_only_preserves_user_relaxation_and_has_exact_diffs(tmp_path):
    from vaspsolkit.case_setup import plan_case_initialization

    _write_case(tmp_path)
    before = _fingerprint(tmp_path)

    plan = plan_case_initialization(tmp_path, _scheduler())

    assert _fingerprint(tmp_path) == before
    assert plan.workdir == tmp_path.resolve()
    assert plan.config.profile == "vaspsol-sweep"
    assert plan.config.workflow.pbs_file == "vasp.pbs"
    assert plan.config.workflow.qsub_ppn == 24
    assert plan.config.workflow.qsub_queue == "workq"
    assert plan.config.workflow.qsub_walltime == "12:00:00"
    assert "ENCUT = 450" in plan.incar_after
    assert "IBRION = 1" in plan.incar_after
    assert "NSW = 88" in plan.incar_after
    assert "LSOL = .TRUE." in plan.incar_after
    assert [change.path.name for change in plan.file_changes] == [
        "INCAR",
        "vaspsolkit.json",
        "vaspsolkit.state.json",
    ]
    assert all(change.change_type == "create" for change in plan.file_changes[1:])


def test_plan_snapshots_are_frozen_and_defensive(tmp_path):
    from vaspsolkit.case_setup import PlannedFileChange, plan_case_initialization

    _write_case(tmp_path)
    plan = plan_case_initialization(tmp_path, _scheduler())

    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.workdir = Path("elsewhere")
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.file_changes[0].after = "changed"
    config = plan.config
    config.profile = "changed"
    config.workflow.folders.append("extra")
    assert plan.config.profile == "vaspsol-sweep"
    assert "extra" not in plan.config.workflow.folders
    assert tuple(item.label for item in plan.source_fingerprints) == (
        "POSCAR", "INCAR", "KPOINTS", "POTCAR", "script"
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.source_fingerprints[0].sha256 = "0" * 64
    outside = PlannedFileChange((tmp_path.parent / "outside").resolve(), None, "x", "create")
    with pytest.raises(ValueError, match="within the Case"):
        dataclasses.replace(plan, file_changes=(outside,))


@pytest.mark.parametrize(
    "incar, message",
    [
        ("ENCUT = 450\nENCUT = 500\n", "duplicate INCAR tags"),
        ("LSOL = .FALSE.\n", "conflicting INCAR settings"),
    ],
)
def test_plan_rejects_duplicate_and_conflicting_incar(tmp_path, incar, message):
    from vaspsolkit.case_setup import plan_case_initialization

    _write_case(tmp_path, incar=incar)
    with pytest.raises(ValueError, match=message):
        plan_case_initialization(tmp_path, _scheduler())


@pytest.mark.parametrize("name", ["POSCAR", "INCAR", "KPOINTS", "POTCAR", "vasp.pbs"])
@pytest.mark.parametrize("mode", ["missing", "empty"])
def test_plan_requires_nonempty_source_files(tmp_path, name, mode):
    from vaspsolkit.case_setup import plan_case_initialization

    _write_case(tmp_path)
    if mode == "missing":
        (tmp_path / name).unlink()
    else:
        (tmp_path / name).write_text("", encoding="utf-8")
    with pytest.raises((FileNotFoundError, ValueError), match=name):
        plan_case_initialization(tmp_path, _scheduler())


@pytest.mark.parametrize("script", ["/tmp/submit.sh", "../submit.sh"])
def test_plan_rejects_script_traversal(tmp_path, script):
    from vaspsolkit.case_setup import plan_case_initialization

    _write_case(tmp_path)
    with pytest.raises(ValueError, match="script"):
        plan_case_initialization(tmp_path, _scheduler(script=script))


def test_plan_rejects_script_symlink_outside_case_and_symlink_loop(tmp_path):
    from vaspsolkit.case_setup import plan_case_initialization

    case = tmp_path / "case"
    case.mkdir()
    _write_case(case)
    outside = tmp_path / "outside.pbs"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    (case / "outside-link.pbs").symlink_to(outside)
    with pytest.raises(ValueError, match="script"):
        plan_case_initialization(case, _scheduler(script="outside-link.pbs"))
    (case / "loop.pbs").symlink_to("loop.pbs")
    with pytest.raises(ValueError, match="script"):
        plan_case_initialization(case, _scheduler(script="loop.pbs"))


def test_existing_config_and_state_are_previewed_as_updates_with_exact_serialization(tmp_path):
    from vaspsolkit.case_setup import plan_case_initialization

    _write_case(tmp_path)
    (tmp_path / "vaspsolkit.json").write_text("old config\n", encoding="utf-8")
    (tmp_path / "vaspsolkit.state.json").write_text("old state\n", encoding="utf-8")

    plan = plan_case_initialization(tmp_path, _scheduler())
    changes = {change.path.name: change for change in plan.file_changes}

    assert changes["vaspsolkit.json"].before == "old config\n"
    assert changes["vaspsolkit.state.json"].before == "old state\n"
    assert changes["vaspsolkit.json"].change_type == "update"
    assert changes["vaspsolkit.state.json"].change_type == "update"
    assert changes["vaspsolkit.json"].after == json.dumps(
        plan.config.to_dict(), indent=2, sort_keys=True
    )
    assert json.loads(changes["vaspsolkit.state.json"].after)["stage"] == "setup"


def test_apply_requires_confirmation_and_rejects_stale_incar(tmp_path):
    from vaspsolkit.case_setup import apply_case_initialization, plan_case_initialization

    _write_case(tmp_path)
    plan = plan_case_initialization(tmp_path, _scheduler())
    with pytest.raises(PermissionError, match="confirm"):
        apply_case_initialization(plan)
    (tmp_path / "INCAR").write_text("ENCUT = 999\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="stale"):
        apply_case_initialization(plan, confirmed=True)
    assert not (tmp_path / "vaspsolkit.json").exists()


def test_apply_writes_only_declared_files(tmp_path):
    from vaspsolkit.case_setup import apply_case_initialization, plan_case_initialization

    _write_case(tmp_path)
    plan = plan_case_initialization(tmp_path, _scheduler())
    written = apply_case_initialization(plan, confirmed=True)

    assert written == tuple(change.path for change in plan.file_changes)
    assert (tmp_path / "INCAR").read_text(encoding="utf-8") == plan.incar_after
    assert json.loads((tmp_path / "vaspsolkit.json").read_text(encoding="utf-8"))["profile"] == "vaspsol-sweep"


def test_apply_preserves_concurrent_config_created_at_write_boundary(tmp_path, monkeypatch):
    from vaspsolkit import case_setup, config as config_module
    from vaspsolkit.case_setup import apply_case_initialization, plan_case_initialization
    from vaspsolkit.config import KitConfig, SchedulerConfig

    _write_case(tmp_path)
    (tmp_path / "vasp.slurm").write_text("#!/bin/sh\n", encoding="utf-8")
    scheduler = SchedulerConfig(script="vasp.slurm")
    scheduler.cores = scheduler.tasks
    scheduler.queue = scheduler.partition
    plan = plan_case_initialization(tmp_path, scheduler)
    incar_before = (tmp_path / "INCAR").read_bytes()
    concurrent = KitConfig(profile="vaspsol-neutral-relax")
    concurrent.scheduler.partition = "concurrent"
    concurrent_bytes = config_module.serialize_kit_config(concurrent)

    def interleaved_write(path, data, **kwargs):
        config_module.write_kit_config(path, concurrent)
        return config_module.write_config_bytes(path, data, **kwargs)

    monkeypatch.setattr(case_setup, "write_config_bytes", interleaved_write, raising=False)

    with pytest.raises(RuntimeError):
        apply_case_initialization(plan, confirmed=True)

    assert (tmp_path / "vaspsolkit.json").read_bytes() == concurrent_bytes
    assert (tmp_path / "INCAR").read_bytes() == incar_before


@pytest.mark.parametrize(
    "mutation",
    [
        "poscar-content",
        "kpoints-delete",
        "potcar-retarget",
        "script-content",
        "script-loop",
    ],
)
def test_apply_rejects_any_stale_source_before_writing(tmp_path, mutation):
    from vaspsolkit.case_setup import apply_case_initialization, plan_case_initialization

    _write_case(tmp_path)
    if mutation == "potcar-retarget":
        original = tmp_path / "POTCAR.original"
        (tmp_path / "POTCAR").rename(original)
        (tmp_path / "POTCAR").symlink_to(original.name)
    plan = plan_case_initialization(tmp_path, _scheduler())

    if mutation == "poscar-content":
        (tmp_path / "POSCAR").write_text("changed\n", encoding="utf-8")
    elif mutation == "kpoints-delete":
        (tmp_path / "KPOINTS").unlink()
    elif mutation == "potcar-retarget":
        replacement = tmp_path / "POTCAR.replacement"
        replacement.write_text(
            "TITEL = PAW_PBE C 08Apr2002\nENMAX = 400.0 eV\n", encoding="utf-8"
        )
        (tmp_path / "POTCAR").unlink()
        (tmp_path / "POTCAR").symlink_to(replacement.name)
    elif mutation == "script-content":
        (tmp_path / "vasp.pbs").write_text("#!/bin/sh\nchanged\n", encoding="utf-8")
    else:
        (tmp_path / "vasp.pbs").unlink()
        (tmp_path / "vasp.pbs").symlink_to("vasp.pbs")

    with pytest.raises(RuntimeError, match="stale initialization source"):
        apply_case_initialization(plan, confirmed=True)
    assert not (tmp_path / "vaspsolkit.json").exists()
    assert not (tmp_path / "vaspsolkit.state.json").exists()


def test_apply_stage_failure_leaves_all_targets_unchanged_and_cleans_temps(
    tmp_path, monkeypatch
):
    import vaspsolkit.case_setup as case_setup

    _write_case(tmp_path)
    config = tmp_path / "vaspsolkit.json"
    state = tmp_path / "vaspsolkit.state.json"
    config.write_text("old config\n", encoding="utf-8")
    state.write_text("old state\n", encoding="utf-8")
    plan = case_setup.plan_case_initialization(tmp_path, _scheduler())
    before = {path: path.read_bytes() for path in (tmp_path / "INCAR", config, state)}
    real_stage = case_setup._stage_file_change
    calls = 0

    def fail_second(change):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected staging failure")
        return real_stage(change)

    monkeypatch.setattr(case_setup, "_stage_file_change", fail_second)

    with pytest.raises(RuntimeError, match="stage") as error:
        case_setup.apply_case_initialization(plan, confirmed=True)
    assert error.value.phase == "stage"
    assert all(path.read_bytes() == content for path, content in before.items())
    assert list(tmp_path.glob(".*.tmp")) == []


def test_apply_replace_failure_is_per_file_atomic_and_cleans_remaining_temps(
    tmp_path, monkeypatch
):
    import vaspsolkit.case_setup as case_setup

    _write_case(tmp_path)
    config = tmp_path / "vaspsolkit.json"
    state = tmp_path / "vaspsolkit.state.json"
    config.write_text("old config\n", encoding="utf-8")
    state.write_text("old state\n", encoding="utf-8")
    plan = case_setup.plan_case_initialization(tmp_path, _scheduler())
    real_replace = case_setup.os.replace
    calls = 0

    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected replace failure")
        return real_replace(source, target)

    monkeypatch.setattr(case_setup.os, "replace", fail_second)

    with pytest.raises(RuntimeError, match="replace") as error:
        case_setup.apply_case_initialization(plan, confirmed=True)
    assert error.value.phase == "replace"
    assert (tmp_path / "INCAR").read_text(encoding="utf-8") == plan.incar_after
    assert config.read_text(encoding="utf-8") == "old config\n"
    assert state.read_text(encoding="utf-8") == "old state\n"
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.parametrize(
    "mutation", ["config-symlink-delete", "state-symlink-retarget", "config-directory"]
)
def test_apply_rejects_nominal_target_entry_mutation_before_staging(tmp_path, mutation):
    from vaspsolkit.case_setup import apply_case_initialization, plan_case_initialization

    _write_case(tmp_path)
    config = tmp_path / "vaspsolkit.json"
    state = tmp_path / "vaspsolkit.state.json"
    if mutation == "config-symlink-delete":
        target = tmp_path / "config.original"
        target.write_text("old config\n", encoding="utf-8")
        config.symlink_to(target.name)
    elif mutation == "state-symlink-retarget":
        first = tmp_path / "state.first"
        first.write_text("old state\n", encoding="utf-8")
        state.symlink_to(first.name)
    plan = plan_case_initialization(tmp_path, _scheduler())
    incar_before = (tmp_path / "INCAR").read_bytes()

    if mutation == "config-symlink-delete":
        config.unlink()
    elif mutation == "state-symlink-retarget":
        second = tmp_path / "state.second"
        second.write_text("old state\n", encoding="utf-8")
        state.unlink()
        state.symlink_to(second.name)
    else:
        config.mkdir()

    with pytest.raises(RuntimeError, match="stale initialization target"):
        apply_case_initialization(plan, confirmed=True)
    assert (tmp_path / "INCAR").read_bytes() == incar_before
    assert list(tmp_path.glob(".*.tmp")) == []


def test_cli_existing_incar_noninteractive_path_reuses_case_setup(tmp_path, monkeypatch):
    from vaspsolkit import cli
    from vaspsolkit.case_setup import plan_case_initialization

    _write_case(tmp_path)
    real_plan = plan_case_initialization(tmp_path, _scheduler())
    calls = []
    monkeypatch.setattr(cli, "plan_case_initialization", lambda workdir, scheduler, workflow: calls.append((workdir, scheduler, workflow)) or real_plan)
    monkeypatch.setattr(cli, "apply_case_initialization", lambda plan, confirmed=False: calls.append((plan, confirmed)) or ())

    result = cli.main([
        "init", "--workdir", str(tmp_path), "--scheduler", "pbs", "--script", "vasp.pbs",
        "--she-reference", "4.70", "--yes"
    ])

    assert result == 0
    assert calls[0][0] == tmp_path.resolve()
    assert calls[1] == (real_plan, True)


def test_controller_init_translates_core_changes_to_file_diffs(tmp_path):
    from vaspsolkit.operations.actions import ResourceRequest
    from vaspsolkit.operations.controller import WorkbenchController

    _write_case(tmp_path)
    resources = ResourceRequest(
        allocation="auto", nodes=(), cores=24, queue="workq", walltime="12:00:00", script="vasp.pbs"
    )

    plan = WorkbenchController(tmp_path).plan("init", resources)

    assert plan.effect == "file-changing"
    assert [diff.path.name for diff in plan.file_diffs] == [
        "INCAR", "vaspsolkit.json", "vaspsolkit.state.json"
    ]
    assert plan.warnings == ()
    assert plan.scheduler_request == resources
