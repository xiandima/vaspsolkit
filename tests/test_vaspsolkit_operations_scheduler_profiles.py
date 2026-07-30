from __future__ import annotations


def test_detects_nodes_ppn_profile() -> None:
    from vaspsolkit.operations.scheduler_profiles import detect_scheduler_profile

    script = (
        "#!/bin/bash\n#PBS -q normal\n#PBS -l nodes=node24:ppn=48\n"
        "#PBS -l walltime=12:34:56\nmpirun -np 48 vasp_std\n"
    )
    profile = detect_scheduler_profile(script, script_name="vasp.pbs")
    assert profile.resource_syntax == "nodes-ppn"
    assert profile.nodes == ("node24",)
    assert profile.cores == 48
    assert profile.queue == "normal"
    assert profile.walltime == "12:34:56"


def test_detects_select_ncpus_profile() -> None:
    from vaspsolkit.operations.scheduler_profiles import detect_scheduler_profile

    script = (
        "#!/bin/bash\n#PBS -q workq\n"
        "#PBS -l select=1:ncpus=32:mem=64gb\n"
        "#PBS -l walltime=24:00:00\nmpiexec vasp_std\n"
    )
    profile = detect_scheduler_profile(script, script_name="job.pbs")
    assert profile.resource_syntax == "select-ncpus"
    assert profile.cores == 32
    assert profile.queue == "workq"
    assert profile.script == "job.pbs"


def test_unmanaged_profile_preserves_unknown_script() -> None:
    from vaspsolkit.operations.scheduler_profiles import detect_scheduler_profile

    script = "#!/bin/bash\n# site-specific resources\nrun_vasp\n"
    profile = detect_scheduler_profile(script)
    assert profile.resource_syntax == "unmanaged"
    assert profile.cores == 0
    assert profile.launch_command == "run_vasp"


def test_case_override_does_not_mutate_user_profile() -> None:
    from vaspsolkit.operations.scheduler_profiles import (
        CaseResourceOverride,
        SchedulerProfile,
        apply_case_override,
    )

    profile = SchedulerProfile(
        name="cluster-a",
        kind="pbs",
        resource_syntax="nodes-ppn",
        queue="normal",
        cores=48,
        walltime="48:00:00",
        script="vasp.pbs",
    )
    resolved = apply_case_override(
        profile,
        CaseResourceOverride(nodes=("node31",), cores=32, queue="batch"),
    )
    assert resolved.nodes == ("node31",)
    assert resolved.cores == 32
    assert resolved.queue == "batch"
    assert profile.nodes == ()
    assert profile.cores == 48


def test_rewrite_resources_preserves_vasp_launch_command() -> None:
    from vaspsolkit.operations.scheduler_profiles import (
        CaseResourceOverride,
        apply_case_override,
        detect_scheduler_profile,
        rewrite_pbs_resources,
    )

    before = (
        "#!/bin/bash\n#PBS -q normal\n#PBS -l nodes=1:ppn=48\n"
        "#PBS -l walltime=48:00:00\nmodule load vasp\nmpirun -np 48 vasp_std\n"
    )
    profile = detect_scheduler_profile(before)
    resolved = apply_case_override(
        profile,
        CaseResourceOverride(nodes=("node24",), cores=32, queue="batch", walltime="12:00:00"),
    )
    after = rewrite_pbs_resources(before, resolved)
    assert "#PBS -q batch" in after
    assert "#PBS -l nodes=node24:ppn=32" in after
    assert "#PBS -l walltime=12:00:00" in after
    assert "module load vasp" in after
    assert "mpirun -np 48 vasp_std" in after


def test_workbench_snapshot_exposes_detected_resource_syntax(tmp_path) -> None:
    from vaspsolkit.config import KitConfig, write_kit_config
    from vaspsolkit.operations.snapshot import build_workbench_snapshot

    case = tmp_path / "case"
    case.mkdir()
    for name in ("POSCAR", "INCAR", "KPOINTS", "POTCAR"):
        (case / name).write_text("input\n", encoding="utf-8")
    (case / "vasp.pbs").write_text(
        "#!/bin/bash\n#PBS -l select=1:ncpus=48\nmpirun vasp_std\n",
        encoding="utf-8",
    )
    write_kit_config(case / "vaspsolkit.json", KitConfig())
    snapshot = build_workbench_snapshot(case)
    assert snapshot.scheduler.resource_syntax == "select-ncpus"
