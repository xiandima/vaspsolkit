"""Batch-user regressions with isolated HOME/PATH/state and synthetic inputs."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_documented_workflows import (
    INIT,
    _calls,
    _fake_slurm,
    _files,
    _run,
    newcomer,
)


def _run_with_open_stdin(newcomer, *args):
    case, env = newcomer
    process = subprocess.Popen(
        [sys.executable, "-m", "vaspsolkit", *args, "--workdir", str(case)],
        cwd=case.parent, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    blocked = False
    try:
        # communicate() would close stdin and conceal an accidental input prompt.
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        blocked = True
    finally:
        if process.poll() is None:
            process.kill()
        stdout, stderr = process.communicate(timeout=5)
    assert not blocked, "Batch CLI waited for stdin:\n" + stdout + stderr
    assert "Traceback" not in stderr, stderr
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


@pytest.mark.parametrize("yes", [False, True], ids=["without-yes", "with-yes"])
@pytest.mark.parametrize("missing", ["--she-reference", "--scheduler", "--script", "--profile"])
def test_batch_init_reports_missing_options_without_waiting(newcomer, missing, yes):
    case, env = newcomer
    args = [*INIT, "--profile", "vaspsol-neutral-relax"]
    if not yes:
        args.remove("--yes")
    if missing == "--profile":
        (case / "INCAR").unlink()
    index = args.index(missing)
    del args[index:index + 2]
    before = _files(case)

    result = _run_with_open_stdin(newcomer, *args)

    assert result.returncode == 2, result.stdout + result.stderr
    assert missing in result.stderr
    assert _files(case) == before
    assert _calls(newcomer) == []
    assert not Path(env["VASPSOLKIT_STATE_ROOT"]).exists()


@pytest.mark.parametrize("yes", [False, True], ids=["without-yes", "with-yes"])
def test_batch_reference_requires_explicit_value_without_waiting(newcomer, yes):
    case, _ = newcomer
    _run(newcomer, *INIT)
    before = _files(case)

    result = _run_with_open_stdin(newcomer, "configure-reference", *(["--yes"] if yes else []))

    assert result.returncode == 2, result.stdout + result.stderr
    assert "--she-reference" in result.stderr
    assert _files(case) == before
    assert _calls(newcomer) == []


@pytest.mark.parametrize("sampling", ["KPOINTS", "KSPACING"])
def test_batch_custom_script_with_spaces_keeps_inputs_and_submits_once(newcomer, sampling):
    case, env = newcomer
    script = case / "site submit.slurm"
    (case / "vasp.slurm").rename(script)
    script_before = script.read_bytes()
    mode_before = script.stat().st_mode
    if sampling == "KSPACING":
        (case / "KPOINTS").unlink()
        incar = case / "INCAR"
        incar.write_text(incar.read_text() + "KSPACING = 2.5D-1; KGAMMA = .FALSE.\n")
    inputs_before = {name: (case / name).read_bytes() for name in ("POSCAR", "POTCAR")}
    _fake_slurm(newcomer)
    assert not (Path(env["PATH"]) / "vaspkit").exists()
    args = list(INIT)
    args[args.index("--script") + 1] = script.name

    result = _run_with_open_stdin(newcomer, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    _run(newcomer, "prepare-neutral", "--yes")
    before = _files(case)
    _run(newcomer, "submit-neutral", "--dry-run")
    assert _files(case) == before
    assert _calls(newcomer) == []
    _run(newcomer, "submit-neutral", "--yes")
    _run(newcomer, "status")
    _run(newcomer, "run", "--yes")

    submissions = [call for call in _calls(newcomer) if call["name"] == "sbatch"]
    assert len(submissions) == 1
    assert submissions[0]["argv"][-1] == script.name
    assert submissions[0]["cwd"] == str(case)
    assert script.read_bytes() == script_before
    assert script.stat().st_mode == mode_before
    assert all((case / name).read_bytes() == contents for name, contents in inputs_before.items())
    assert (case / "KPOINTS").exists() == (sampling == "KPOINTS")
    if sampling == "KSPACING":
        assert "KSPACING = 2.5D-1; KGAMMA = .FALSE." in (case / "INCAR").read_text()
    state = json.loads((case / "vaspsolkit.state.json").read_text())
    assert state["neutral"]["job_id"] == "910001"
    assert state["neutral"]["status"] == "RUNNING"
    assert not (case / "vasp.log").exists()


def test_piped_init_cancellation_preserves_case(newcomer):
    case, _ = newcomer
    before = _files(case)
    result = _run(newcomer, *(arg for arg in INIT if arg != "--yes"), input_text="\nn\n", expect=1)
    assert "cancelled" in result.stdout
    assert _files(case) == before
    assert _calls(newcomer) == []
