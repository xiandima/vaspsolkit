from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import patch


def _write_case(root: Path, *, incar: str = "ENCUT = 450\nIBRION = 2\nNSW = 100\n") -> None:
    root.mkdir(parents=True)
    (root / "POSCAR").write_text(
        "PtO\n1\n1 0 0\n0 1 0\n0 0 1\nPt O\n1 1\nDirect\n0 0 0\n0 0 0\n",
        encoding="utf-8",
    )
    (root / "INCAR").write_text(incar, encoding="utf-8")
    (root / "KPOINTS").write_text(
        "Gamma\n0\nGamma\n1 1 1\n0 0 0\n", encoding="utf-8"
    )
    (root / "POTCAR").write_text(
        "TITEL = PAW_PBE Pt 01Jan2000\nENMAX = 300 eV\n"
        "TITEL = PAW_PBE O 01Jan2000\nENMAX = 400 eV\n",
        encoding="utf-8",
    )
    (root / "vasp.slurm").write_text(
        "#!/bin/bash\n#SBATCH -p normal\n#SBATCH -N 1\n#SBATCH --ntasks=48\n"
        "#SBATCH -t 48:00:00\nmpirun -np 48 vasp_std\n",
        encoding="utf-8",
    )


def test_snapshot_validates_inputs_and_exposes_real_summaries(tmp_path: Path) -> None:
    from vaspsolkit.operations.snapshot import build_workbench_snapshot

    case = tmp_path / "case"
    _write_case(case)
    rows = {row.name: row for row in build_workbench_snapshot(case).input_rows}

    assert rows["POSCAR"].status == "READY"
    assert "elements=Pt O" in rows["POSCAR"].summary
    assert "atoms=2" in rows["POSCAR"].summary
    assert "coordinates=Direct" in rows["POSCAR"].summary
    assert rows["POTCAR"].status == "READY"
    assert "order=Pt O" in rows["POTCAR"].summary
    assert "ENMAX=400" in rows["POTCAR"].summary
    assert rows["KPOINTS"].status == "READY"
    assert "Gamma 1x1x1" in rows["KPOINTS"].summary
    assert rows["vasp.slurm"].status == "READY"
    assert "line-endings=Unix" in rows["vasp.slurm"].summary
    assert "tasks=48" in rows["vasp.slurm"].summary
    assert "partition=normal" in rows["vasp.slurm"].summary
    assert "walltime=48:00:00" in rows["vasp.slurm"].summary


def test_snapshot_marks_missing_and_semantically_invalid_inputs(tmp_path: Path) -> None:
    from vaspsolkit.operations.snapshot import build_workbench_snapshot

    corruptions = {
        "POSCAR": b"Pt\n1\n",
        "POTCAR": b"\xff\xfe\x00bad",
        "KPOINTS": b"Gamma\n0\nGamma\n0 0 nope\n",
        "vasp.slurm": b"#!/bin/bash\r\n#SBATCH -N 1\r\n",
    }
    for name, payload in corruptions.items():
        case = tmp_path / name.replace(".", "-")
        _write_case(case)
        (case / name).write_bytes(payload)
        row = next(
            item for item in build_workbench_snapshot(case).input_rows
            if item.name == name
        )
        assert row.status == "ERROR", (name, row)
        assert row.summary

    missing = tmp_path / "missing-script"
    _write_case(missing)
    (missing / "vasp.slurm").unlink()
    script = next(
        item for item in build_workbench_snapshot(missing).input_rows
        if item.name == "vasp.slurm"
    )
    assert script.status == "MISSING"


def test_snapshot_rejects_nonfinite_degenerate_poscar_and_invalid_explicit_kpoints(tmp_path: Path) -> None:
    from vaspsolkit.operations.snapshot import build_workbench_snapshot

    bad_poscars = (
        "Pt\nnan\n1 0 0\n0 1 0\n0 0 1\nPt\n1\nDirect\n0 0 0\n",
        "Pt\n1\n1 0 0\n2 0 0\n0 0 1\nPt\n1\nDirect\n0 0 0\n",
        "Pt\n1\n1 0 0\n0 1 0\n0 0 1\nPt\n1\nDirect\nnan 0 0\n",
    )
    for index, poscar in enumerate(bad_poscars):
        case = tmp_path / f"poscar-{index}"
        _write_case(case)
        (case / "POSCAR").write_text(poscar, encoding="utf-8")
        row = next(row for row in build_workbench_snapshot(case).input_rows if row.name == "POSCAR")
        assert row.status == "ERROR"


def test_snapshot_accepts_standard_line_mode_and_explicit_tetrahedra_kpoints(tmp_path: Path) -> None:
    from vaspsolkit.operations.snapshot import build_workbench_snapshot

    line_case = tmp_path / "line-mode"
    _write_case(line_case)
    (line_case / "KPOINTS").write_text(
        "band path\n# header comment\n40\n  Line-mode ! path  \nReciprocal # fractional\n"
        "0 0 0 ! Gamma\n0.5 0 0 ! X\n\n"
        "# second segment\n0.5 0 0 ! X\n0.5 0.5 0 ! M\n",
        encoding="utf-8",
    )
    row = next(row for row in build_workbench_snapshot(line_case).input_rows if row.name == "KPOINTS")
    assert row.status == "READY"
    assert "Line-mode Reciprocal" in row.summary
    assert "segments=2" in row.summary
    assert "points-per-segment=40" in row.summary

    tetra_case = tmp_path / "tetrahedra"
    _write_case(tetra_case)
    (tetra_case / "KPOINTS").write_text(
        "explicit with tetrahedra\n4\nReciprocal\n"
        "0 0 0 1\n0.5 0 0 1\n0 0.5 0 1\n0 0 0.5 1\n\n"
        "Tetrahedra\n1 0.125\n1 1 2 3 4\n",
        encoding="utf-8",
    )
    row = next(row for row in build_workbench_snapshot(tetra_case).input_rows if row.name == "KPOINTS")
    assert row.status == "READY"
    assert "explicit points=4" in row.summary
    assert "tetrahedra=1" in row.summary


def test_snapshot_rejects_malformed_line_mode_and_tetrahedra_kpoints(tmp_path: Path) -> None:
    from vaspsolkit.operations.snapshot import build_workbench_snapshot

    malformed = (
        "bad line path\n20\nLine-mode\nReciprocal\n0 0 0\n0.5 nan 0\n",
        "odd line path\n20\nLine-mode\nCartesian\n0 0 0\n0.5 0 0\n0.5 0.5 0\n",
        "bad tetra\n4\nReciprocal\n0 0 0 1\n0.5 0 0 1\n0 0.5 0 1\n0 0 0.5 1\nTetrahedra\n1 0.125\n1 1 2 3 9\n",
        "short tetra\n4\nReciprocal\n0 0 0 1\n0.5 0 0 1\n0 0.5 0 1\n0 0 0.5 1\nTetrahedra\n2 0.125\n1 1 2 3 4\n",
    )
    for index, text in enumerate(malformed):
        case = tmp_path / f"malformed-{index}"
        _write_case(case)
        (case / "KPOINTS").write_text(text, encoding="utf-8")
        row = next(row for row in build_workbench_snapshot(case).input_rows if row.name == "KPOINTS")
        assert row.status == "ERROR", (index, row.summary)

    bad_kpoints = (
        "explicit\n1\nBanana\n0 0 0 1\n",
        "explicit\n2\nReciprocal\n0 0 0 1\n",
        "explicit\n1\nCartesian\n0 0 0 nope\n",
        "explicit\n1\nReciprocal\n0 0 nan 1\n",
    )
    for index, kpoints in enumerate(bad_kpoints):
        case = tmp_path / f"kpoints-{index}"
        _write_case(case)
        (case / "KPOINTS").write_text(kpoints, encoding="utf-8")
        row = next(row for row in build_workbench_snapshot(case).input_rows if row.name == "KPOINTS")
        assert row.status == "ERROR"


def test_slurm_requires_work_and_reports_unparsed_resources_as_unknown(tmp_path: Path) -> None:
    from vaspsolkit.config import KitConfig, SchedulerConfig, write_kit_config
    from vaspsolkit.operations.snapshot import build_workbench_snapshot

    empty_case = tmp_path / "empty-slurm"
    _write_case(empty_case)
    (empty_case / "vasp.slurm").write_text("#!/bin/bash\n# comment only\n", encoding="utf-8")
    row = next(row for row in build_workbench_snapshot(empty_case).input_rows if row.name == "vasp.slurm")
    assert row.status == "ERROR"

    command_case = tmp_path / "command-slurm"
    _write_case(command_case)
    (command_case / "vasp.slurm").write_text("#!/bin/bash\nmpirun vasp_std\n", encoding="utf-8")
    write_kit_config(command_case / "vaspsolkit.json", KitConfig(scheduler=SchedulerConfig(partition="normal", tasks=48, walltime="48:00:00")))
    row = next(row for row in build_workbench_snapshot(command_case).input_rows if row.name == "vasp.slurm")
    assert row.status == "READY"
    assert "partition=unknown" in row.summary
    assert "tasks=unknown" in row.summary
    assert "walltime=unknown" in row.summary





























