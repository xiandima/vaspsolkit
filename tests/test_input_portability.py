import json
import os
from pathlib import Path
import subprocess

import pytest

from tests.test_cli_user_experience import _write_synthetic_inputs
from vaspsolkit import cli
from vaspsolkit.operations.snapshot import build_workbench_snapshot


def _no_prompt(prompt):
    pytest.fail(f"unexpected interactive prompt: {prompt}")


def test_custom_init_accepts_quoted_commands_without_prompting(tmp_path):
    _write_synthetic_inputs(tmp_path)
    original_script = (tmp_path / "vasp.slurm").read_bytes()
    assert cli.main([
        "init", "--workdir", str(tmp_path), "--yes", "--scheduler", "custom",
        "--script", "vasp.slurm", "--she-reference", "4.7",
        "--submit-command", '"/site tools/submit" --label "my case" {script}',
        "--status-command", '"/site tools/status" {job_id}',
        "--cancel-command", '"/site tools/cancel" {job_id}',
        "--inspect-command", '"/site tools/queue" --user "a user"',
        "--job-id-regex", r"accepted (?P<job_id>job-\d+)",
    ], input_fn=_no_prompt, output=lambda _: None) == 0
    scheduler = json.loads((tmp_path / "vaspsolkit.json").read_text())["scheduler"]
    assert scheduler["submit_command"] == ["/site tools/submit", "--label", "my case", "{script}"]
    assert scheduler["status_command"] == ["/site tools/status", "{job_id}"]
    assert scheduler["cancel_command"] == ["/site tools/cancel", "{job_id}"]
    assert scheduler["inspect_command"] == ["/site tools/queue", "--user", "a user"]
    assert scheduler["job_id_pattern"] == r"accepted (?P<job_id>job-\d+)"
    assert (tmp_path / "vasp.slurm").read_bytes() == original_script


@pytest.mark.parametrize("extra, match", [
    ([], "--submit-command"),
    (["--submit-command", '"unterminated'], "quotation|quote"),
    (["--submit-command", "launch {unknown}"], "placeholder"),
    (["--submit-command", "launch {script}", "--job-id-regex", "["], "job_id_pattern"),
])
def test_invalid_custom_init_fails_before_creating_case(tmp_path, extra, match):
    case = tmp_path / "not created"
    with pytest.raises(ValueError, match=match):
        cli.main([
            "init", "--workdir", str(case), "--profile", "static", "--yes",
            "--scheduler", "custom", "--script", "submit.sh", "--she-reference", "4.7",
            *extra,
        ], input_fn=_no_prompt, output=lambda _: None)
    assert not case.exists()


def _vaspkit_case(tmp_path, monkeypatch):
    _write_synthetic_inputs(tmp_path)
    (tmp_path / "KPOINTS").unlink()
    monkeypatch.setattr(cli, "vaspkit_executable", lambda: "/site tools/vaspkit")
    return {name: (tmp_path / name).read_bytes() for name in ("POSCAR", "INCAR", "POTCAR")}


def test_vaspkit_only_imports_missing_inputs_from_isolated_directory(tmp_path, monkeypatch):
    before = _vaspkit_case(tmp_path, monkeypatch)
    stages = []

    def generate(args, cwd, **kwargs):
        stage = Path(cwd)
        stages.append(stage)
        assert stage != tmp_path
        assert (stage / "INCAR").read_bytes() == before["INCAR"]
        assert not (stage / "vasp.slurm").exists()
        (stage / "INCAR").write_text("ENCUT=1\n")
        (stage / "POTCAR").write_text("unrelated helper side effect\n")
        (stage / "KPOINTS").write_text("Gamma\n0\nGamma\n2 2 1\n0 0 0\n")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(cli.subprocess, "run", generate)
    cli._ensure_standard_inputs(tmp_path, False, lambda _: "y", lambda _: None)
    assert {name: (tmp_path / name).read_bytes() for name in before} == before
    assert "2 2 1" in (tmp_path / "KPOINTS").read_text()
    assert all(not path.exists() for path in stages)


@pytest.mark.parametrize("failure", ["exit", "empty", "symlink", "malformed", "wrong-element"])
def test_vaspkit_failure_does_not_import_partial_outputs(tmp_path, monkeypatch, failure):
    _vaspkit_case(tmp_path, monkeypatch)
    (tmp_path / "POTCAR").unlink()

    def generate(args, cwd, **kwargs):
        stage = Path(cwd)
        (stage / "POTCAR").write_text("TITEL = PAW_PBE C FAKE\nENMAX = 400.0\n")
        (stage / "KPOINTS").write_text("Gamma\n0\nGamma\n1 1 1\n0 0 0\n")
        if failure == "empty":
            (stage / "KPOINTS").write_text("")
        elif failure == "symlink":
            (stage / "KPOINTS").unlink()
            (stage / "KPOINTS").symlink_to(tmp_path / "POSCAR")
        elif failure == "malformed":
            (stage / "KPOINTS").write_text("not a k mesh\n")
        elif failure == "wrong-element":
            (stage / "POTCAR").write_text("TITEL = PAW_PBE Si FAKE\nENMAX = 400.0\n")
        return subprocess.CompletedProcess(args, 1 if failure == "exit" else 0)

    monkeypatch.setattr(cli.subprocess, "run", generate)
    with pytest.raises((ValueError, RuntimeError)):
        cli._ensure_standard_inputs(tmp_path, False, lambda _: "y", lambda _: None)
    assert not (tmp_path / "POTCAR").exists()
    assert not (tmp_path / "KPOINTS").exists()


def test_vaspkit_refuses_a_changed_source_after_interaction(tmp_path, monkeypatch):
    _vaspkit_case(tmp_path, monkeypatch)

    def generate(args, cwd, **kwargs):
        (Path(cwd) / "KPOINTS").write_text("Gamma\n0\nGamma\n1 1 1\n0 0 0\n")
        (tmp_path / "POSCAR").write_text("user changed structure\n")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(cli.subprocess, "run", generate)
    with pytest.raises(RuntimeError, match="changed|stale"):
        cli._ensure_standard_inputs(tmp_path, False, lambda _: "y", lambda _: None)
    assert not (tmp_path / "KPOINTS").exists()
    assert (tmp_path / "POSCAR").read_text() == "user changed structure\n"


def test_vaspkit_refuses_to_overwrite_a_concurrently_created_input(tmp_path, monkeypatch):
    _vaspkit_case(tmp_path, monkeypatch)

    def generate(args, cwd, **kwargs):
        (Path(cwd) / "KPOINTS").write_text("Gamma\n0\nGamma\n1 1 1\n0 0 0\n")
        (tmp_path / "KPOINTS").write_text("user mesh\n")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(cli.subprocess, "run", generate)
    with pytest.raises((RuntimeError, FileExistsError), match="changed|exists|appeared"):
        cli._ensure_standard_inputs(tmp_path, False, lambda _: "y", lambda _: None)
    assert (tmp_path / "KPOINTS").read_text() == "user mesh\n"


@pytest.mark.parametrize("mode", ["Gamma", "Monkhorst-Pack"])
@pytest.mark.parametrize("shift", ["", "# default zero shift\n", "0.5 0 0\n"])
def test_regular_kpoints_accept_optional_shift(tmp_path, mode, shift):
    _write_synthetic_inputs(tmp_path)
    (tmp_path / "KPOINTS").write_text(f"regular mesh\n0\n{mode}\n2 2 1\n{shift}")
    row = next(row for row in build_workbench_snapshot(tmp_path).input_rows if row.name == "KPOINTS")
    assert row.status == "READY", row.summary
    expected = "shift=0.5 0 0" if shift.startswith("0.5") else "shift=0 0 0"
    assert expected in row.summary


@pytest.mark.parametrize("shift", ["0 0\n", "0 nan 0\n", "bad shift\n"])
def test_regular_kpoints_reject_invalid_explicit_shift(tmp_path, shift):
    _write_synthetic_inputs(tmp_path)
    (tmp_path / "KPOINTS").write_text(f"regular mesh\n0\nGamma\n2 2 1\n{shift}")
    row = next(row for row in build_workbench_snapshot(tmp_path).input_rows if row.name == "KPOINTS")
    assert row.status == "ERROR"


@pytest.mark.parametrize("failed_name", ["INCAR", "vaspsolkit.state.json"])
def test_generic_initialization_write_failure_can_be_retried(tmp_path, monkeypatch, failed_name):
    _write_synthetic_inputs(tmp_path)
    before = (tmp_path / "INCAR").read_bytes()
    mode = (tmp_path / "INCAR").stat().st_mode
    real_replace = os.replace

    def fail_once(source, target):
        if Path(target).name == failed_name:
            monkeypatch.setattr(os, "replace", real_replace)
            raise PermissionError("injected initialization failure")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_once)
    # A profile uses the generic initialization path, not case_setup.
    args = ["init", "--workdir", str(tmp_path), "--yes", "--scheduler", "slurm",
            "--script", "vasp.slurm", "--she-reference", "4.7", "--profile", "static"]
    with pytest.raises(PermissionError, match="injected"):
        cli.main(args, input_fn=_no_prompt, output=lambda _: None)
    assert (tmp_path / "INCAR").read_bytes() == before
    assert (tmp_path / "INCAR").stat().st_mode == mode
    assert not (tmp_path / "vaspsolkit.json").exists()
    assert not (tmp_path / "vaspsolkit.state.json").exists()
    assert cli.main(args, input_fn=_no_prompt, output=lambda _: None) == 0


@pytest.mark.parametrize("external_edit", [False, "replace", "in-place"])
def test_vaspkit_publication_failure_rolls_back_only_own_files(tmp_path, monkeypatch, external_edit):
    _vaspkit_case(tmp_path, monkeypatch)
    (tmp_path / "POTCAR").unlink()

    def generate(args, cwd, **kwargs):
        stage = Path(cwd)
        (stage / "POTCAR").write_text("TITEL = PAW_PBE C FAKE\nENMAX = 400.0\n")
        (stage / "KPOINTS").write_text("Gamma\n0\nGamma\n1 1 1\n")
        return subprocess.CompletedProcess(args, 0)

    real_link = os.link

    def fail_second(source, target, **kwargs):
        if Path(target) == tmp_path / "KPOINTS":
            if external_edit:
                if external_edit == "replace":
                    (tmp_path / "POTCAR").unlink()
                (tmp_path / "POTCAR").write_text("user replacement\n")
            raise OSError("injected disk failure")
        return real_link(source, target, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", generate)
    monkeypatch.setattr(os, "link", fail_second)
    with pytest.raises((OSError, RuntimeError), match="injected|rollback|changed"):
        cli._ensure_standard_inputs(tmp_path, False, lambda _: "y", lambda _: None)
    assert not (tmp_path / "KPOINTS").exists()
    if external_edit:
        assert (tmp_path / "POTCAR").read_text() == "user replacement\n"
    else:
        assert not (tmp_path / "POTCAR").exists()


def test_initialization_rollback_keeps_external_template_edit(tmp_path, monkeypatch):
    _write_synthetic_inputs(tmp_path)

    def fail_save(self, path):
        (tmp_path / "INCAR").write_text("user replacement\n")
        raise OSError("state write failed")

    monkeypatch.setattr(cli.WorkflowState, "save", fail_save)
    with pytest.raises(RuntimeError, match="rollback needs manual review.*INCAR"):
        cli.main([
            "init", "--workdir", str(tmp_path), "--yes", "--scheduler", "slurm",
            "--script", "vasp.slurm", "--she-reference", "4.7", "--profile", "static",
        ], input_fn=_no_prompt, output=lambda _: None)
    assert (tmp_path / "INCAR").read_text() == "user replacement\n"
    assert not (tmp_path / "vaspsolkit.json").exists()


def test_vaspkit_staging_failure_does_not_publish_any_inputs(tmp_path, monkeypatch):
    _vaspkit_case(tmp_path, monkeypatch)
    (tmp_path / "POTCAR").unlink()

    def generate(args, cwd, **kwargs):
        (Path(cwd) / "POTCAR").write_text("TITEL = PAW_PBE C FAKE\nENMAX = 400.0\n")
        (Path(cwd) / "KPOINTS").write_text("Gamma\n0\nGamma\n1 1 1\n")
        return subprocess.CompletedProcess(args, 0)

    count = 0
    real_fsync = os.fsync

    def fail_second(fd):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("injected staging failure")
        return real_fsync(fd)

    monkeypatch.setattr(cli.subprocess, "run", generate)
    monkeypatch.setattr(os, "fsync", fail_second)
    with pytest.raises(OSError, match="staging"):
        cli._ensure_standard_inputs(tmp_path, False, lambda _: "y", lambda _: None)
    assert not (tmp_path / "POTCAR").exists()
    assert not (tmp_path / "KPOINTS").exists()
