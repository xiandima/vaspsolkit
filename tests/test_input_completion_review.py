import json
import subprocess
from pathlib import Path

import pytest

from tests.test_input_completion import case, library, run
from vaspsolkit import cli, input_completion


def mock_helper(monkeypatch, generate):
    monkeypatch.setattr(input_completion.shutil, "which", lambda _: "/mock/vaspkit")

    def helper(argv, **kwargs):
        if "-help" in argv:
            return subprocess.CompletedProcess(argv, 0, "VASPKIT 1.5.0", "")
        generate(Path(kwargs["cwd"]))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(input_completion.subprocess, "run", helper)


def test_saved_library_is_absolute_even_with_existing_potcar(case, monkeypatch):
    root = library(case)
    monkeypatch.chdir(root.parent)
    assert run(case, "--potcar-root", root.name, "--functional", "PBE", "--potential", "C=C", "--save-settings", "--yes") == 0
    settings = json.loads((case.parent / "settings" / "vaspsolkit" / "inputs.json").read_text())
    assert settings["potcar_root"] == str(root.resolve())


@pytest.mark.parametrize("status", ["SUBMITTING", "ACCEPTED"])
def test_input_completion_respects_submission_receipts(case, status):
    from vaspsolkit.operations.activity import SubmissionReceipt, claim_submission_receipt
    root = library(case)
    (case / "POTCAR").unlink()
    info = case.stat()
    receipt = SubmissionReceipt(case_path=str(case), case_device=info.st_dev, case_inode=info.st_ino,
                                job_id="42" if status == "ACCEPTED" else "", command="sbatch vasp.slurm", resources={},
                                timestamp="2026-10-22T00:00:00+00:00", owner_token="test-only", status=status)
    path = claim_submission_receipt(case, receipt)
    with pytest.raises(ValueError, match="receipt"):
        run(case, "--potcar-root", str(root), "--functional", "PBE", "--potential", "C=C", "--yes")
    assert not (case / "POTCAR").exists()
    assert path.exists()


def test_helper_cannot_change_geometry_used_for_mesh(case, monkeypatch):
    (case / "KPOINTS").unlink()

    def generate(stage):
        (stage / "POSCAR").write_text((stage / "POSCAR").read_text().replace("1 0 0", "100 0 0"))
        (stage / "KPOINTS").write_text("mesh\n0\nGamma\n2 2 1\n")

    mock_helper(monkeypatch, generate)
    with pytest.raises(ValueError, match="POSCAR|geometry"):
        run(case, "--backend", "vaspkit", "--kpr", "0.04", "--yes")
    assert not (case / "KPOINTS").exists()


def test_helper_cannot_substitute_same_named_potential(case, monkeypatch):
    root = library(case)
    other = (case / "POTCAR").read_bytes() + b"different dataset content\n"
    (case / "POTCAR").unlink()
    mock_helper(monkeypatch, lambda stage: (stage / "POTCAR").write_bytes(other))
    with pytest.raises(ValueError, match="content|library"):
        run(case, "--backend", "vaspkit", "--potcar-root", str(root), "--functional", "PBE", "--potential", "C=C", "--yes")
    assert not (case / "POTCAR").exists()


@pytest.mark.parametrize("value, success", [("0.25", True), ("-1", False)])
def test_init_uses_effective_sampling_overrides(case, value, success):
    (case / "KPOINTS").unlink()
    argv = ["init", "--workdir", str(case), "--yes", "--scheduler", "slurm", "--script", "vasp.slurm", "--she-reference", "4.7", "--set", f"KSPACING={value}"]
    if success:
        assert cli.main(argv, output=lambda _: None) == 0
        assert not (case / "KPOINTS").exists()
    else:
        with pytest.raises(ValueError, match="KSPACING"):
            cli.main(argv, output=lambda _: None)
        assert not (case / "vaspsolkit.json").exists()


def test_invalid_saved_density_is_an_actionable_error(case):
    (case / "KPOINTS").unlink()
    settings = case.parent / "bad-settings.json"
    settings.write_text(json.dumps({"backend": "vaspkit", "kpr": {"value": 0.04}}))
    with pytest.raises(ValueError, match="kpr"):
        run(case, "--settings", str(settings), "--yes")
