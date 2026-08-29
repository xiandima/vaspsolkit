from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest


def _write_case(root: Path, *, incar: str = "ENCUT = 520\nIBRION = 2\nNSW = 80\n") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "POSCAR").write_text(
        "PtO\n1\n1 0 0\n0 1 0\n0 0 1\nPt O\n1 1\nDirect\n0 0 0\n0 0 0\n",
        encoding="utf-8",
    )
    (root / "INCAR").write_text(incar, encoding="utf-8")
    (root / "KPOINTS").write_text("Gamma\n0\nGamma\n1 1 1\n0 0 0\n", encoding="utf-8")
    (root / "POTCAR").write_text(
        "TITEL = PAW_PBE Pt 01Jan2000\nENMAX = 300 eV\n"
        "TITEL = PAW_PBE O 01Jan2000\nENMAX = 400 eV\n",
        encoding="utf-8",
    )
    (root / "vasp.pbs").write_text("#!/bin/sh\n", encoding="utf-8")


def _resources(*, persist: bool = False, allocation: str = "auto", nodes=(), queue="normal"):
    from vaspsolkit.operations.actions import ResourceRequest

    return ResourceRequest.create(
        allocation=allocation,
        nodes=tuple(nodes),
        cores=48,
        queue=queue,
        walltime="48:00:00",
        script="vasp.pbs",
        persist=persist,
    )


def test_initialize_and_prepare_neutral_follow_reviewed_plans(tmp_path: Path) -> None:
    from vaspsolkit.case_setup import STATE_FILENAME
    from vaspsolkit.state import WorkflowState
    from vaspsolkit.operations.controller import WorkbenchController

    _write_case(tmp_path)
    controller = WorkbenchController(tmp_path)
    init_plan = controller.plan("init", resources=_resources())

    assert init_plan.effect == "file-changing"
    assert {diff.path.name for diff in init_plan.file_diffs} == {
        "INCAR", "vaspsolkit.json", "vaspsolkit.state.json"
    }
    controller.execute(init_plan, confirmed=True)
    assert "ENCUT = 520" in (tmp_path / "INCAR").read_text(encoding="utf-8")

    prepare_plan = controller.plan("prepare-neutral")
    assert prepare_plan.effect == "file-changing"
    assert prepare_plan.warnings == ("旧计算输出如存在将归档",)
    assert {item.path.name for item in prepare_plan.file_diffs} >= {
        "POSCAR.initial", STATE_FILENAME
    }
    result = controller.execute(prepare_plan, confirmed=True)

    state = WorkflowState.load(tmp_path / STATE_FILENAME)
    assert state.neutral is not None and state.neutral.status == "PREPARED"
    assert result.snapshot is not None
    assert result.snapshot.neutral.status == "PREPARED"


@pytest.mark.parametrize("changed", ("source", "target"))
def test_initialize_rejects_stale_preview_without_partial_write(
    tmp_path: Path, changed: str
) -> None:
    from vaspsolkit.operations.controller import WorkbenchController

    _write_case(tmp_path)
    controller = WorkbenchController(tmp_path)
    plan = controller.plan("init", _resources())
    if changed == "source":
        (tmp_path / "KPOINTS").write_text("changed\n", encoding="utf-8")
    else:
        (tmp_path / "vaspsolkit.json").write_text("surprise\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="stale|已变化|重新预览"):
        controller.execute(plan, confirmed=True)
    assert not (tmp_path / "vaspsolkit.state.json").exists()
    assert "LSOL" not in (tmp_path / "INCAR").read_text(encoding="utf-8")


def test_prepare_neutral_rejects_stale_source_before_archive(tmp_path: Path) -> None:
    from vaspsolkit.operations.controller import WorkbenchController

    _write_case(tmp_path)
    controller = WorkbenchController(tmp_path)
    controller.execute(controller.plan("init", _resources()), confirmed=True)
    (tmp_path / "OUTCAR").write_text("old output\n", encoding="utf-8")
    plan = controller.plan("prepare-neutral")
    (tmp_path / "POSCAR").write_text("changed after preview\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="已变化|重新预览"):
        controller.execute(plan, confirmed=True)
    assert (tmp_path / "OUTCAR").read_text(encoding="utf-8") == "old output\n"
    assert not (tmp_path / ".vaspsolkit").exists()


def test_resource_request_rejects_unsafe_node_names() -> None:
    for node in ("node01/../../x", "node 01", "node01;rm"):
        with pytest.raises(ValueError, match="node"):
            _resources(allocation="specified", nodes=(node,))


def test_persist_false_resources_do_not_change_initialized_case(tmp_path: Path) -> None:
    from vaspsolkit.operations.controller import WorkbenchController

    _write_case(tmp_path)
    controller = WorkbenchController(tmp_path)
    controller.execute(controller.plan("init", _resources()), confirmed=True)
    before = (tmp_path / "vaspsolkit.json").read_bytes()

    controller.preview_resources(_resources(persist=False, allocation="specified", nodes=("node24",)))

    assert (tmp_path / "vaspsolkit.json").read_bytes() == before


def test_persisted_resource_defaults_use_reviewed_atomic_config_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vaspsolkit.config import load_kit_config
    from vaspsolkit.operations.controller import WorkbenchController

    _write_case(tmp_path)
    controller = WorkbenchController(tmp_path)
    controller.execute(controller.plan("init", _resources()), confirmed=True)
    resources = _resources(persist=True, allocation="specified", nodes=("node24",))
    plan = controller.plan_resource_defaults(resources)
    assert plan.effect == "file-changing"
    assert [diff.path.name for diff in plan.file_diffs] == ["vaspsolkit.json"]
    controller.execute(plan, confirmed=True)

    config = load_kit_config(tmp_path / "vaspsolkit.json")
    assert config.scheduler.nodes == ["node24"]
    assert config.scheduler.cores == 48
    assert config.workflow.qsub_ppn == 48


def test_resource_defaults_preserve_concurrent_valid_config_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vaspsolkit import config as config_module
    from vaspsolkit.operations import controller as controller_module
    from vaspsolkit.operations.actions import ResourceRequest
    from vaspsolkit.operations.controller import WorkbenchController

    (tmp_path / "vasp.slurm").write_text("#!/bin/sh\n", encoding="utf-8")
    config_path = tmp_path / "vaspsolkit.json"
    config_module.write_kit_config(config_path, config_module.KitConfig())
    controller = WorkbenchController(tmp_path)
    plan = controller.plan_resource_defaults(
        ResourceRequest.create(
            allocation="auto",
            nodes=(),
            cores=96,
            queue="compute",
            walltime="72:00:00",
            script="vasp.slurm",
            persist=True,
        )
    )
    concurrent = config_module.KitConfig()
    concurrent.scheduler.partition = "concurrent"
    concurrent_bytes = config_module.serialize_kit_config(concurrent)
    real_write = controller_module.write_config_bytes

    def interleaved_write(path, data, **kwargs):
        config_module.write_kit_config(path, concurrent)
        return real_write(path, data, **kwargs)

    monkeypatch.setattr(controller_module, "write_config_bytes", interleaved_write)

    with pytest.raises(RuntimeError, match="changed"):
        controller.execute(plan, confirmed=True)

    assert config_path.read_bytes() == concurrent_bytes
















def _tree_bytes(root: Path) -> dict[str, tuple[str, bytes]]:
    values = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            values[relative] = ("link", str(path.readlink()).encode())
        elif path.is_file():
            values[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            values[relative] = ("dir", b"")
    return values


def _initialized_case(root: Path):
    from vaspsolkit.operations.controller import WorkbenchController

    _write_case(root)
    controller = WorkbenchController(root)
    controller.execute(controller.plan("init", _resources()), confirmed=True)
    return controller


def test_prepare_neutral_rolls_back_when_second_archive_move_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vaspsolkit.orchestrator as orchestrator
    from vaspsolkit.config import load_kit_config

    _initialized_case(tmp_path)
    (tmp_path / "OUTCAR").write_text("old outcar\n", encoding="utf-8")
    (tmp_path / "CHGCAR").write_bytes(b"charge-density")
    before = _tree_bytes(tmp_path)
    original = orchestrator._move_archive_entry
    calls = 0

    def fail_second(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second move failure")
        return original(source, destination)

    monkeypatch.setattr(orchestrator, "_move_archive_entry", fail_second)
    with pytest.raises(OSError, match="second move"):
        orchestrator.prepare_neutral_job(
            tmp_path, load_kit_config(tmp_path / "vaspsolkit.json")
        )

    assert _tree_bytes(tmp_path) == before


def test_prepare_neutral_rolls_back_when_state_install_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vaspsolkit.orchestrator as orchestrator
    from vaspsolkit.config import load_kit_config

    _initialized_case(tmp_path)
    (tmp_path / "OUTCAR").write_text("old outcar\n", encoding="utf-8")
    (tmp_path / "charge_sweep").mkdir()
    (tmp_path / "charge_sweep" / "keep.dat").write_bytes(b"keep")
    before = _tree_bytes(tmp_path)
    original = orchestrator._install_prepared_entry

    def fail_state(source, destination):
        if destination.name == "vaspsolkit.state.json":
            raise OSError("injected state install failure")
        return original(source, destination)

    monkeypatch.setattr(orchestrator, "_install_prepared_entry", fail_state)
    with pytest.raises(OSError, match="state install"):
        orchestrator.prepare_neutral_job(
            tmp_path, load_kit_config(tmp_path / "vaspsolkit.json")
        )

    assert _tree_bytes(tmp_path) == before


def test_prepare_neutral_cleans_transaction_when_staged_state_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vaspsolkit.orchestrator as orchestrator
    from vaspsolkit.config import load_kit_config

    _initialized_case(tmp_path)
    (tmp_path / "OUTCAR").write_text("old outcar\n", encoding="utf-8")
    before = _tree_bytes(tmp_path)

    def fail_state_write(path, state, old_path):
        path.write_text("partial", encoding="utf-8")
        raise OSError("injected staged state write failure")

    monkeypatch.setattr(orchestrator, "_write_staged_state", fail_state_write)
    with pytest.raises(OSError, match="state write"):
        orchestrator.prepare_neutral_job(
            tmp_path, load_kit_config(tmp_path / "vaspsolkit.json")
        )

    assert _tree_bytes(tmp_path) == before


def test_init_rejects_replaced_case_directory_even_with_identical_files(
    tmp_path: Path,
) -> None:
    from vaspsolkit.operations.controller import WorkbenchController

    case = tmp_path / "case"
    _write_case(case)
    controller = WorkbenchController(case)
    plan = controller.plan("init", _resources())
    old = tmp_path / "old-case"
    case.rename(old)
    shutil.copytree(old, case)

    with pytest.raises(RuntimeError, match="Case.*变化|stale"):
        controller.execute(plan, confirmed=True)
    assert not (case / "vaspsolkit.json").exists()


def test_prepare_preview_uses_structured_archive_changes_for_large_files_and_directory(
    tmp_path: Path,
) -> None:
    controller = _initialized_case(tmp_path)
    (tmp_path / "CHGCAR").write_bytes(b"x" * 4096)
    (tmp_path / "WAVECAR").write_bytes(b"wave")
    (tmp_path / "charge_sweep").mkdir()
    (tmp_path / "charge_sweep" / "1").mkdir()
    (tmp_path / "charge_sweep" / "1" / "CHGCAR").write_bytes(b"nested")

    plan = controller.plan("prepare-neutral")
    changes = {change.source.name: change for change in plan.archive_changes}
    assert {"INCAR", "CHGCAR", "WAVECAR", "charge_sweep", "vaspsolkit.state.json"} <= set(changes)
    assert changes["charge_sweep"].entry_type == "directory"
    assert changes["charge_sweep"].destination.parent.name.startswith("restart-")
    assert changes["CHGCAR"].size == 4096
    assert len(changes["CHGCAR"].sha256) == 64
    assert all(diff.path.name not in {"CHGCAR", "WAVECAR"} for diff in plan.file_diffs)














def test_prepare_rejects_poscar_symlink_target_content_change(
    tmp_path: Path,
) -> None:
    case = tmp_path / "case"
    _write_case(case)
    original = case / "POSCAR.original"
    (case / "POSCAR").rename(original)
    (case / "POSCAR").symlink_to(original.name)
    from vaspsolkit.operations.controller import WorkbenchController

    controller = WorkbenchController(case)
    controller.execute(controller.plan("init", _resources()), confirmed=True)
    plan = controller.plan("prepare-neutral")
    original.write_text(original.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="POSCAR.*变化|重新预览"):
        controller.execute(plan, confirmed=True)
    assert not (case / ".vaspsolkit").exists()


def test_prepare_rolls_back_if_poscar_changes_during_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vaspsolkit.orchestrator as orchestrator
    from vaspsolkit.config import load_kit_config

    _initialized_case(tmp_path)
    (tmp_path / "OUTCAR").write_text("old output\n", encoding="utf-8")
    old_state = (tmp_path / "vaspsolkit.state.json").read_bytes()
    original = orchestrator._install_prepared_entry
    changed = False

    def mutate_after_first_install(source, destination):
        nonlocal changed
        result = original(source, destination)
        if not changed:
            changed = True
            (tmp_path / "POSCAR").write_text("concurrent change\n", encoding="utf-8")
        return result

    monkeypatch.setattr(orchestrator, "_install_prepared_entry", mutate_after_first_install)
    with pytest.raises(RuntimeError, match="POSCAR.*changed"):
        orchestrator.prepare_neutral_job(
            tmp_path, load_kit_config(tmp_path / "vaspsolkit.json")
        )

    assert (tmp_path / "POSCAR").read_text(encoding="utf-8") == "concurrent change\n"
    assert (tmp_path / "OUTCAR").read_text(encoding="utf-8") == "old output\n"
    assert (tmp_path / "vaspsolkit.state.json").read_bytes() == old_state
    assert not (tmp_path / ".vaspsolkit").exists()


def test_prepare_cleanup_failure_is_success_with_warning_and_single_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vaspsolkit.orchestrator as orchestrator

    controller = _initialized_case(tmp_path)
    plan = controller.plan("prepare-neutral")
    original = orchestrator.shutil.rmtree

    def fail_transaction_cleanup(path, *args, **kwargs):
        if Path(path).name.startswith("prepare-neutral-"):
            raise OSError("injected transaction cleanup failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(orchestrator.shutil, "rmtree", fail_transaction_cleanup)
    result = controller.execute(plan, confirmed=True)

    assert result.status == "completed"
    assert result.warnings
    assert "cleanup" in result.message.lower()
    cleanup_path = Path(result.warnings[0].split("path=", 1)[1])
    assert cleanup_path.exists()
    with pytest.raises(RuntimeError, match="失效"):
        controller.execute(plan, confirmed=True)






def test_prepare_plan_hashes_each_archive_payload_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vaspsolkit.operations.controller as controller_module

    controller = _initialized_case(tmp_path)
    (tmp_path / "CHGCAR").write_bytes(b"charge")
    (tmp_path / "WAVECAR").write_bytes(b"wave")
    (tmp_path / "charge_sweep").mkdir()
    (tmp_path / "charge_sweep" / "nested.dat").write_bytes(b"nested")
    original = controller_module._sha256_file
    calls = []

    def count_hash(path):
        calls.append(Path(path).resolve())
        return original(path)

    monkeypatch.setattr(controller_module, "_sha256_file", count_hash)
    controller.plan("prepare-neutral")

    for relative in ("CHGCAR", "WAVECAR", "charge_sweep/nested.dat"):
        assert calls.count((tmp_path / relative).resolve()) == 1


@pytest.mark.parametrize("layout", ("self-loop", "mutual-links"))
def test_charge_sweep_directory_symlink_cycles_are_fingerprinted_without_recursion(
    tmp_path: Path, layout: str
) -> None:
    controller = _initialized_case(tmp_path)
    charge = tmp_path / "charge_sweep"
    charge.mkdir()
    if layout == "self-loop":
        (charge / "loop").symlink_to(".")
    else:
        (charge / "a").mkdir()
        (charge / "b").mkdir()
        (charge / "a" / "to-b").symlink_to("../b")
        (charge / "b" / "to-a").symlink_to("../a")

    plan = controller.plan("prepare-neutral")

    assert not plan.blocked_reason
    change = next(item for item in plan.archive_changes if item.source.name == "charge_sweep")
    assert change.entry_type == "directory"
    assert len(change.sha256) == 64


@pytest.mark.parametrize("target", ("missing-target", "../../outside"))
def test_charge_sweep_broken_or_external_symlink_blocks_plan_safely(
    tmp_path: Path, target: str
) -> None:
    controller = _initialized_case(tmp_path)
    charge = tmp_path / "charge_sweep"
    charge.mkdir()
    if target == "../../outside":
        (tmp_path.parent / "outside").write_text("outside", encoding="utf-8")
    (charge / "unsafe-link").symlink_to(target)

    plan = controller.plan("prepare-neutral")

    assert plan.blocked_reason
    assert "symlink" in plan.blocked_reason.lower()
