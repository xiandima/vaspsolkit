from __future__ import annotations

from pathlib import Path


def _neutral_case(root: Path) -> None:
    from vaspsolkit.config import KitConfig, SchedulerConfig, WorkflowConfig, write_kit_config
    from vaspsolkit.state import JobRecord, WorkflowState

    root.mkdir()
    for name, text in {
        "INCAR": "ENCUT = 520\nIBRION = 2\nNSW = 100\nISTART = 1\nICHARG = 2\n",
        "POTCAR": "potcar\n",
        "KPOINTS": "kpoints\n",
        "CONTCAR": "neutral optimized structure\n",
        "CHGCAR": "neutral optimized charge\n",
        "LOCPOT": "neutral locpot\n",
        "OUTCAR": "neutral outcar\n",
        "WAVECAR": "must not be copied\n",
        "vasp.pbs": "#!/bin/bash\n#PBS -l nodes=1:ppn=48\nmpirun vasp_std\n",
    }.items():
        (root / name).write_text(text, encoding="utf-8")
    config = KitConfig(
        workflow=WorkflowConfig(
            folders=["neutral", "plus"],
            nelect_offsets=[0.0, 0.5],
            nelect_ref=10.0,
            charge_points_include_neutral=False,
        ),
        scheduler=SchedulerConfig(kind="pbs", script="vasp.pbs"),
    )
    write_kit_config(root / "vaspsolkit.json", config)
    WorkflowState(
        stage="neutral_converged",
        neutral=JobRecord(
            folder=".",
            status="CONVERGED",
            metadata={"stage": "neutral_relax"},
        ),
    ).save(root / "vaspsolkit.state.json")


def test_workbench_prepares_and_checks_charge_inputs_through_reviewed_actions(tmp_path: Path) -> None:
    from vaspsolkit.state import WorkflowState
    from vaspsolkit.operations.controller import WorkbenchController

    case = tmp_path / "case"
    _neutral_case(case)
    controller = WorkbenchController(case)

    prepare = controller.plan("prepare-charge")
    assert prepare.effect == "file-changing"
    assert prepare.target_jobs == ("plus",)
    assert prepare.blocked_reason == ""
    prepared = controller.execute(prepare, confirmed=True)
    assert prepared.ok
    folder = case / "charge_sweep" / "plus"
    assert (folder / "POSCAR").read_text(encoding="utf-8") == (case / "CONTCAR").read_text(encoding="utf-8")
    assert (folder / "CHGCAR").read_text(encoding="utf-8") == (case / "CHGCAR").read_text(encoding="utf-8")
    assert not (folder / "WAVECAR").exists()
    incar = (folder / "INCAR").read_text(encoding="utf-8")
    for expected in ("ENCUT = 520", "IBRION = 2", "NSW = 100", "ISTART = 0", "ICHARG = 1", "NELECT = 10.5000"):
        assert expected in incar

    check = controller.plan("check-prepared")
    assert check.effect == "file-changing"
    assert "不会提交任务" in " ".join(check.warnings)
    checked = controller.execute(check, confirmed=True)
    assert checked.ok
    assert WorkflowState.load(case / "vaspsolkit.state.json").prepared_checked


def test_charge_prepare_preview_rejects_changed_neutral_chgcar(tmp_path: Path) -> None:
    from vaspsolkit.operations.controller import WorkbenchController

    case = tmp_path / "case"
    _neutral_case(case)
    controller = WorkbenchController(case)
    plan = controller.plan("prepare-charge")
    (case / "CHGCAR").write_text("changed\n", encoding="utf-8")
    try:
        controller.execute(plan, confirmed=True)
    except RuntimeError as exc:
        assert "CHGCAR 已变化" in str(exc)
    else:
        raise AssertionError("stale charge preparation preview was executed")


def test_collect_writes_exact_previewed_summary_and_rejects_stale_outputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from vaspsolkit.state import WorkflowState
    from vaspsolkit.operations.controller import WorkbenchController

    case = tmp_path / "case"
    _neutral_case(case)
    controller = WorkbenchController(case)
    controller.execute(controller.plan("prepare-charge"), confirmed=True)
    state = WorkflowState.load(case / "vaspsolkit.state.json")
    state.jobs["plus"].status = "CONVERGED"
    state.save(case / "vaspsolkit.state.json")

    expected = "folder,converged\nplus,1\n"

    def fake_collect(_base, _workflow, output):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(expected, encoding="utf-8")
        return [{"folder": "plus", "converged": 1}]

    monkeypatch.setattr("vaspsolkit.operations.controller.collect_results", fake_collect)
    plan = controller.plan("collect")
    assert plan.blocked_reason == ""
    assert plan.file_diffs[0].after == expected
    (case / "charge_sweep" / "plus" / "OUTCAR").write_text("changed\n", encoding="utf-8")
    try:
        controller.execute(plan, confirmed=True)
    except RuntimeError as exc:
        assert "OUTCAR 已变化" in str(exc)
    else:
        raise AssertionError("stale collect preview was executed")

    fresh = controller.plan("collect")
    result = controller.execute(fresh, confirmed=True)
    assert result.ok
    assert (case / "results" / "summary.csv").read_text(encoding="utf-8") == expected
