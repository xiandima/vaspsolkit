from __future__ import annotations

from pathlib import Path


def _write_case(root: Path) -> None:
    root.mkdir()
    (root / "POSCAR").write_text(
        "Pt\n1\n1 0 0\n0 1 0\n0 0 1\nPt\n1\nDirect\n0 0 0\n",
        encoding="utf-8",
    )
    (root / "INCAR").write_text("IBRION = 2\nNSW = 100\n", encoding="utf-8")
    (root / "KPOINTS").write_text(
        "Gamma\n0\nGamma\n1 1 1\n0 0 0\n", encoding="utf-8"
    )
    (root / "POTCAR").write_text(
        "TITEL = PAW_PBE Pt 01Jan2000\nENMAX = 300 eV\n", encoding="utf-8"
    )
    (root / "vasp.slurm").write_text(
        "#!/bin/bash\n#SLURM -l nodes=1:ppn=48\nmpirun -np 48 vasp_std\n",
        encoding="utf-8",
    )


def test_snapshot_scans_only_bounded_outcar_tail_and_keeps_last_values(tmp_path: Path) -> None:
    from vaspsolkit.operations.snapshot import OUTCAR_SCAN_LIMIT, build_workbench_snapshot

    case = tmp_path / "case"
    _write_case(case)
    prefix_line = b"Iteration 1 electronic step without final values\n"
    with (case / "OUTCAR").open("wb") as handle:
        while handle.tell() <= OUTCAR_SCAN_LIMIT + 1024 * 1024:
            handle.write(prefix_line)
        handle.write(
            b"E-fermi : 1.234\nfree energy TOTEN = -123.456 eV\n"
            b"reached required accuracy\n"
        )

    output = build_workbench_snapshot(case).neutral_output

    assert output.outcar_status == "CONVERGED"
    assert output.toten == -123.456
    assert output.efermi == 1.234
    assert output.outcar_size > OUTCAR_SCAN_LIMIT
    assert output.scanned_bytes <= OUTCAR_SCAN_LIMIT


def test_snapshot_marks_incomplete_outcar_in_progress_with_real_values(tmp_path: Path) -> None:
    from vaspsolkit.operations.snapshot import build_workbench_snapshot

    case = tmp_path / "case"
    _write_case(case)
    (case / "OUTCAR").write_text(
        "vasp.6\nIteration 8\nE-fermi : 2.500\nTOTEN = -88.750 eV\n",
        encoding="utf-8",
    )

    output = build_workbench_snapshot(case).neutral_output

    assert output.outcar_status == "IN_PROGRESS"
    assert output.toten == -88.75
    assert output.efermi == 2.5


def test_snapshot_marks_damaged_outcar_unreadable(tmp_path: Path) -> None:
    from vaspsolkit.operations.snapshot import build_workbench_snapshot

    case = tmp_path / "case"
    _write_case(case)
    (case / "OUTCAR").write_bytes(b"\xff\xfe\x00not-an-outcar")

    output = build_workbench_snapshot(case).neutral_output

    assert output.outcar_status == "UNREADABLE"
    assert output.toten is None
    assert output.efermi is None
    assert output.diagnostic
