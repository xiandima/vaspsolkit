"""Input completion uses metadata-only fixtures, never licensed potentials."""
import json
import subprocess
from pathlib import Path

import pytest

from tests.test_cli_user_experience import _write_synthetic_inputs
from vaspsolkit import cli


def no_prompt(prompt):
    pytest.fail(f"unexpected prompt: {prompt}")


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "settings"))
    root = tmp_path / "case with spaces"
    root.mkdir()
    _write_synthetic_inputs(root)
    return root


def library(case):
    root = case.parent / "licensed library"
    (root / "C").mkdir(parents=True)
    (root / "C" / "POTCAR").write_bytes((case / "POTCAR").read_bytes())
    return root


def run(case, *args, input_fn=no_prompt):
    return cli.main(["inputs", "--workdir", str(case), *args], input_fn=input_fn, output=lambda _: None)


def test_complete_case_check_writes_nothing(case):
    before = {p.name: p.read_bytes() for p in case.iterdir()}
    assert run(case, "--dry-run") == 0
    assert before == {p.name: p.read_bytes() for p in case.iterdir()}


def test_explicit_mesh_fills_only_missing_file(case):
    (case / "KPOINTS").unlink()
    before = (case / "INCAR").read_bytes()
    assert run(case, "--mesh", "3", "3", "1", "--yes") == 0
    assert "3 3 1" in (case / "KPOINTS").read_text()
    assert (case / "INCAR").read_bytes() == before
    reports = list(case.glob("vaspsolkit.inputs-*.json"))
    report = json.loads(reports[0].read_text())
    assert report["generated"] == ["KPOINTS"]
    assert report["sampling"]["mesh"] == [3, 3, 1]


def test_dry_run_does_not_generate_or_save_settings(case):
    (case / "KPOINTS").unlink()
    assert run(case, "--mesh", "2", "2", "1", "--save-settings", "--dry-run") == 0
    assert not (case / "KPOINTS").exists()
    assert not (case.parent / "settings").exists()


def test_potential_library_requires_explicit_mapping_and_functional(case):
    root = library(case)
    (case / "POTCAR").unlink()
    with pytest.raises(ValueError, match="functional|potential"):
        run(case, "--potcar-root", str(root), "--yes")
    assert not (case / "POTCAR").exists()


def test_native_potcar_settings_reused_for_new_case(case):
    root = library(case)
    expected = (case / "POTCAR").read_bytes()
    (case / "POTCAR").unlink()
    assert run(case, "--potcar-root", str(root), "--functional", "PBE", "--potential", "C=C", "--save-settings", "--yes") == 0
    assert (case / "POTCAR").read_bytes() == expected
    other = case.parent / "other"
    other.mkdir()
    _write_synthetic_inputs(other)
    (other / "POTCAR").unlink()
    assert run(other, "--yes") == 0
    assert (other / "POTCAR").read_bytes() == expected


@pytest.mark.parametrize("variant, text", [
    ("../C", None),
    ("C", "TITEL = PAW C TEST\nENMAX = 400\n"),
    ("C", "TITEL = PAW_PBE Si TEST\nENMAX = 400\n"),
])
def test_potential_mapping_or_family_mismatch_is_rejected(case, variant, text):
    root = library(case)
    if text is not None:
        (root / "C" / "POTCAR").write_text(text)
    (case / "POTCAR").unlink()
    with pytest.raises(ValueError):
        run(case, "--potcar-root", str(root), "--functional", "PBE", "--potential", f"C={variant}", "--yes")
    assert not (case / "POTCAR").exists()


def test_explicit_template_is_copied_without_changing_source(case):
    source = case.parent / "template"
    original = (case / "INCAR").read_bytes()
    source.write_bytes(original)
    (case / "INCAR").unlink()
    assert run(case, "--incar-template", str(source), "--yes") == 0
    assert (case / "INCAR").read_bytes() == source.read_bytes() == original


def test_declining_completion_preserves_case(case):
    (case / "KPOINTS").unlink()
    assert run(case, "--mesh", "2", "2", "1", input_fn=lambda _: "n") == 1
    assert not (case / "KPOINTS").exists()
    assert not list(case.glob("vaspsolkit.inputs-*.json"))


def test_vaspkit_automatic_mesh_is_isolated_and_bounded(case, monkeypatch):
    from vaspsolkit import input_completion
    (case / "KPOINTS").unlink()
    original = (case / "INCAR").read_bytes()
    monkeypatch.setattr(input_completion.shutil, "which", lambda _: "/site/vaspkit")
    calls = []

    def helper(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] > 0
        assert kwargs["input"] == ""
        stage = Path(kwargs["cwd"])
        assert stage != case and not (stage / "vasp.slurm").exists()
        if "-help" in argv:
            return subprocess.CompletedProcess(argv, 0, "VASPKIT Standard Edition 1.5.0", "")
        assert argv[1:3] == ["-task", "102"]
        assert "-kpr" in argv and "-kps" in argv
        assert Path(kwargs["env"]["HOME"]) != Path.home()
        (stage / "KPOINTS").write_text("test\n0\nGamma\n4 4 1\n")
        (stage / "INCAR").write_text("ENCUT=1\n")
        return subprocess.CompletedProcess(argv, 0, "done", "")

    monkeypatch.setattr(input_completion.subprocess, "run", helper)
    assert run(case, "--backend", "vaspkit", "--kpr", "0.04", "--yes") == 0
    assert len(calls) == 2
    assert (case / "INCAR").read_bytes() == original
    assert "4 4 1" in (case / "KPOINTS").read_text()


def test_stale_completion_does_not_publish(case):
    (case / "KPOINTS").unlink()

    def answer(_):
        (case / "INCAR").write_text("ENCUT=700\n")
        return "y"

    with pytest.raises(RuntimeError, match="changed|stale"):
        run(case, "--mesh", "2", "2", "1", input_fn=answer)
    assert not (case / "KPOINTS").exists()


def test_missing_batch_parameters_fail_without_prompt(case):
    (case / "KPOINTS").unlink()
    with pytest.raises(ValueError, match="mesh|kpr"):
        run(case, "--yes")


def test_invalid_existing_kpoints_not_hidden_by_kspacing(case):
    (case / "KPOINTS").write_text("broken mesh\n")
    with (case / "INCAR").open("a") as handle:
        handle.write("KSPACING=0.3\n")
    with pytest.raises(ValueError, match="KPOINTS|mesh"):
        run(case, "--dry-run")


def test_active_job_blocks_input_generation(case):
    from vaspsolkit.state import WorkflowState, JobRecord
    (case / "KPOINTS").unlink()
    WorkflowState(neutral=JobRecord(folder=".", status="UNKNOWN", job_id="42")).save(case / "vaspsolkit.state.json")
    with pytest.raises(ValueError, match="active|UNKNOWN"):
        run(case, "--mesh", "2", "2", "1", "--yes")
    assert not (case / "KPOINTS").exists()


@pytest.mark.parametrize("failure", ["timeout", "version", "empty", "symlink", "exit", "badmesh"])
def test_automatic_helper_failure_does_not_publish(case, monkeypatch, failure):
    from vaspsolkit import input_completion
    (case / "KPOINTS").unlink()
    monkeypatch.setattr(input_completion.shutil, "which", lambda _: "/site/vaspkit")

    def helper(argv, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 120)
        if "-help" in argv:
            version = "2.0.0" if failure == "version" else "1.5.0"
            return subprocess.CompletedProcess(argv, 0, f"VASPKIT {version}", "")
        target = Path(kwargs["cwd"]) / "KPOINTS"
        if failure == "symlink":
            target.symlink_to(case / "POSCAR")
        elif failure == "badmesh":
            target.write_text("invalid\n")
        else:
            target.write_text("")
        return subprocess.CompletedProcess(argv, 1 if failure == "exit" else 0, "", "")

    monkeypatch.setattr(input_completion.subprocess, "run", helper)
    with pytest.raises((ValueError, RuntimeError)):
        run(case, "--backend", "vaspkit", "--kpr", "0.03", "--yes")
    assert not (case / "KPOINTS").exists()
    assert not list(case.glob("vaspsolkit.inputs-*.json"))


def test_vaspkit_potcar_uses_confirmed_mapping_in_private_config(case, monkeypatch):
    from vaspsolkit import input_completion
    root = library(case)
    expected = (case / "POTCAR").read_bytes()
    (case / "POTCAR").unlink()
    monkeypatch.setattr(input_completion.shutil, "which", lambda _: "/site/vaspkit")

    def helper(argv, **kwargs):
        if "-help" in argv:
            return subprocess.CompletedProcess(argv, 0, "VASPKIT 1.5.0", "")
        assert argv[-2:] == ["-task", "103"]
        home = Path(kwargs["env"]["HOME"])
        config = (home / ".vaspkit").read_text()
        assert "RECOMMENDED_POTCAR = .FALSE." in config
        assert "AUTO_SUBMIT_JOB = .FALSE." in config
        assert (home / "potentials" / "C" / "POTCAR").read_bytes() == expected
        (Path(kwargs["cwd"]) / "POTCAR").write_bytes(expected)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(input_completion.subprocess, "run", helper)
    assert run(case, "--backend", "vaspkit", "--potcar-root", str(root), "--functional", "PBE", "--potential", "C=C", "--yes") == 0
    assert (case / "POTCAR").read_bytes() == expected
