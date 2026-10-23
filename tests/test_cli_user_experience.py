"""Black-box CLI regressions using synthetic inputs and no cluster executables."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_case(tmp_path):
    workdir = tmp_path / "case"
    home = tmp_path / "home"
    empty_path = tmp_path / "empty-bin"
    for directory in (workdir, home, empty_path):
        directory.mkdir()
    env = {
        key: os.environ[key]
        for key in ("LANG", "LC_ALL", "LD_LIBRARY_PATH", "SYSTEMROOT", "WINDIR")
        if key in os.environ
    }
    env.update(
        HOME=str(home),
        PATH=str(empty_path),
        PYTHONPATH=str(ROOT),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUNBUFFERED="1",
        PYTHONIOENCODING="utf-8",
        VASPSOLKIT_STATE_ROOT=str(tmp_path / "activity"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        MPLCONFIGDIR=str(tmp_path / "matplotlib"),
    )
    return workdir, env


def _run(isolated_case, *args, input_text=""):
    workdir, env = isolated_case
    return subprocess.run(
        [sys.executable, "-m", "vaspsolkit", *args],
        cwd=workdir,
        env=env,
        input=input_text,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=10,
    )


def _write_synthetic_inputs(workdir):
    # The POTCAR is parser-only metadata, not a pseudopotential or VASP input.
    inputs = {
        "POSCAR": "synthetic\n1\n1 0 0\n0 1 0\n0 0 1\nC\n1\nDirect\n0 0 0\n",
        "INCAR": "ENCUT = 450\nIBRION = 1\nNSW = 88\n",
        "KPOINTS": "Gamma\n0\nGamma\n1 1 1\n0 0 0\n",
        "POTCAR": "TITEL = PAW_PBE C UX_SYNTHETIC\nENMAX = 400.0 eV\n",
        "vasp.slurm": "#!/bin/sh\nexit 97\n",
    }
    for name, contents in inputs.items():
        (workdir / name).write_text(contents, encoding="utf-8")


def _contents(workdir):
    return {
        str(path.relative_to(workdir)): path.read_bytes()
        for path in workdir.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("answers", ["11\n", "11\ny\n"])
def test_menu_eof_during_action_exits_without_traceback(isolated_case, answers):
    workdir, _ = isolated_case
    before = _contents(workdir)
    result = _run(isolated_case, "menu", input_text=answers)
    transcript = result.stdout + result.stderr

    assert "Traceback" not in transcript, transcript
    assert "EOFError" not in transcript, transcript
    assert result.returncode in (0, 1, 130), transcript
    assert _contents(workdir) == before


def test_negative_scheduler_selection_cannot_initialize_case(isolated_case):
    workdir, _ = isolated_case
    _write_synthetic_inputs(workdir)
    before = _contents(workdir)
    result = _run(
        isolated_case,
        "init",
        "--script", "vasp.slurm",
        "--she-reference", "4.7",
        input_text="-1\n\ny\n",
    )
    transcript = result.stdout + result.stderr

    assert result.returncode != 0, transcript
    assert _contents(workdir) == before, transcript
    assert not (workdir / "vaspsolkit.json").exists()


def test_init_yes_without_reference_does_not_wait_for_non_tty_input(isolated_case):
    workdir, env = isolated_case
    _write_synthetic_inputs(workdir)
    before = _contents(workdir)
    process = subprocess.Popen(
        [
            sys.executable, "-m", "vaspsolkit", "init", "--yes",
            "--scheduler", "slurm", "--script", "vasp.slurm",
        ],
        cwd=workdir,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    blocked = False
    try:
        # Keep stdin open: communicate() would close it and hide a prompt hang.
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        blocked = True
    finally:
        if process.poll() is None:
            process.kill()
        stdout, stderr = process.communicate(timeout=5)
    transcript = stdout + stderr

    assert not blocked, "Non-TTY init --yes waited for stdin:\n" + transcript
    assert process.returncode != 0, transcript
    assert "--she-reference" in transcript, transcript
    assert "Traceback" not in transcript, transcript
    assert _contents(workdir) == before


@pytest.mark.parametrize("command", ["submit-neutral", "run"])
def test_declining_neutral_submission_is_not_an_execution_error(isolated_case, command):
    workdir, _ = isolated_case
    _write_synthetic_inputs(workdir)
    initialized = _run(
        isolated_case,
        "init", "--yes", "--scheduler", "slurm",
        "--script", "vasp.slurm", "--she-reference", "4.7",
    )
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    prepared = _run(isolated_case, "prepare-neutral", "--yes")
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    before = _contents(workdir)

    result = _run(isolated_case, command, input_text="n\n")
    transcript = result.stdout + result.stderr

    assert "Traceback" not in transcript, transcript
    assert "PermissionError" not in transcript, transcript
    assert "cancel" in transcript.lower() or "\u53d6\u6d88" in transcript, transcript
    assert result.returncode in (0, 1, 130), transcript
    assert _contents(workdir) == before
