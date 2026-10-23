"""Executable newcomer examples with synthetic inputs and isolated command paths."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import sysconfig
import venv
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = """#!/bin/sh
#SBATCH --account=example-account
#SBATCH --qos=example-qos
export USER_SETUP_MARKER='keep this exact line'
mpirun -np "${SLURM_NTASKS}" vasp_std > vasp.log 2>&1
"""
INIT = (
    "init", "--yes", "--scheduler", "slurm", "--script", "vasp.slurm",
    "--she-reference", "4.7", "--she-reference-source", "synthetic test only",
)


@pytest.fixture
def newcomer(tmp_path):
    case = tmp_path / "new user" / "case with spaces"
    home = tmp_path / "isolated home"
    commands = tmp_path / "empty commands"
    for path in (case, home, commands):
        path.mkdir(parents=True)
    env = {
        key: os.environ[key]
        for key in ("LANG", "LC_ALL", "LD_LIBRARY_PATH", "SYSTEMROOT", "WINDIR")
        if key in os.environ
    }
    env.update(
        HOME=str(home), PATH=str(commands), PYTHONPATH=str(ROOT),
        PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
        VASPSOLKIT_STATE_ROOT=str(tmp_path / "isolated activity"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        MPLCONFIGDIR=str(tmp_path / "matplotlib"),
        FAKE_SCHEDULER_LOG=str(tmp_path / "scheduler.jsonl"),
    )
    # Parser-only metadata: this is deliberately not a usable pseudopotential.
    inputs = {
        "POSCAR": "synthetic\n1\n1 0 0\n0 1 0\n0 0 1\nC\n1\nDirect\n0 0 0\n",
        "INCAR": "ENCUT = 450\nIBRION = 1\nNSW = 88\n",
        "KPOINTS": "Gamma\n0\nGamma\n1 1 1\n0 0 0\n",
        "POTCAR": "TITEL = PAW_PBE C SYNTHETIC_ONLY\nENMAX = 400 eV\n",
        "vasp.slurm": SCRIPT,
    }
    for name, contents in inputs.items():
        (case / name).write_text(contents, encoding="utf-8")
    (case / "vasp.slurm").chmod(0o750)
    return case, env


def _run(newcomer, *args, input_text="", expect=0):
    case, env = newcomer
    result = subprocess.run(
        [sys.executable, "-m", "vaspsolkit", *args, "--workdir", str(case)],
        cwd=case.parent, env=env, input=input_text, text=True,
        encoding="utf-8", capture_output=True, timeout=15,
    )
    assert result.returncode == expect, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    return result


def _files(case):
    return {str(path.relative_to(case)): path.read_bytes()
            for path in case.rglob("*") if path.is_file()}


def _fake_slurm(newcomer):
    _, env = newcomer
    program = f"#!{sys.executable}\n" + """import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
with open(os.environ['FAKE_SCHEDULER_LOG'], 'a', encoding='utf-8') as log:
    log.write(json.dumps({'name': name, 'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')
if name == 'sbatch':
    print('910001;synthetic-cluster')
elif name == 'squeue':
    if '-j' in sys.argv:
        print('RUNNING')
elif name == 'sinfo':
    print('compute' if '-N' not in sys.argv else 'node1|compute|idle|96|0/96/0/96')
else:
    raise SystemExit('unexpected fake scheduler command: ' + name)
"""
    for name in ("sbatch", "squeue", "sinfo"):
        path = Path(env["PATH"]) / name
        path.write_text(program, encoding="utf-8")
        path.chmod(0o700)


def _calls(newcomer):
    path = Path(newcomer[1]["FAKE_SCHEDULER_LOG"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_offline_quick_start_in_path_with_spaces_preserves_user_script(newcomer):
    case, env = newcomer
    script_before = (case / "vasp.slurm").read_bytes()
    mode_before = (case / "vasp.slurm").stat().st_mode
    _run(newcomer, *INIT)
    assert "IBRION = 1" in (case / "INCAR").read_text()
    assert "NSW = 88" in (case / "INCAR").read_text()
    before = _files(case)
    _run(newcomer, "plan", "--offline")
    assert _files(case) == before
    _run(newcomer, "prepare-neutral", "--yes")
    before = _files(case)
    _run(newcomer, "submit-neutral", "--dry-run")
    assert _files(case) == before
    assert (case / "vasp.slurm").read_bytes() == script_before
    assert (case / "vasp.slurm").stat().st_mode == mode_before
    assert not Path(env["VASPSOLKIT_STATE_ROOT"]).exists()
    assert not list(Path(env["PATH"]).iterdir())


def test_missing_incar_can_be_generated_from_explicit_profile(newcomer):
    case, _ = newcomer
    (case / "INCAR").unlink()
    _run(newcomer, *INIT, "--profile", "vaspsol-neutral-relax")
    assert "ENCUT = 520" in (case / "INCAR").read_text()
    assert (case / "vasp.slurm").read_text() == SCRIPT


@pytest.mark.parametrize("missing", ["POTCAR", "KPOINTS"])
def test_yes_requires_complete_inputs_without_vaspkit(newcomer, missing):
    case, _ = newcomer
    (case / missing).unlink()
    before = _files(case)
    result = _run(newcomer, *INIT, expect=2)
    assert missing in result.stderr
    assert _files(case) == before


def test_kspacing_only_initializes_and_prepares_without_generating_kpoints(newcomer):
    case, _ = newcomer
    (case / "KPOINTS").unlink()
    incar = case / "INCAR"
    sampling = "KSPACING = 2.5D-1; KGAMMA = .FALSE.\n"
    incar.write_text(incar.read_text() + sampling, encoding="utf-8")
    _run(newcomer, *INIT)
    _run(newcomer, "prepare-neutral", "--yes")
    before = _files(case)
    _run(newcomer, "submit-neutral", "--dry-run")
    assert _files(case) == before
    assert sampling in incar.read_text()
    assert not (case / "KPOINTS").exists()
    assert _calls(newcomer) == []


def test_native_sampling_does_not_bypass_relaxation_validation(newcomer):
    case, _ = newcomer
    (case / "KPOINTS").unlink()
    (case / "INCAR").write_text("ENCUT = 450\nKSPACING = 0.25\n", encoding="utf-8")
    before = _files(case)
    result = _run(newcomer, *INIT, expect=2)
    assert "RELAXATION" in result.stderr
    assert all((case / name).read_bytes() == contents for name, contents in before.items())
    assert not (case / "vaspsolkit.json").exists()
    assert not (case / "vaspsolkit.state.json").exists()


def test_piped_init_reports_missing_library_options_without_writing(newcomer):
    case, _ = newcomer
    (case / "POTCAR").unlink()
    before = _files(case)
    result = _run(newcomer, *(arg for arg in INIT if arg != "--yes"), expect=2)
    assert "--potcar-root" in result.stderr
    assert _files(case) == before


def test_existing_case_resume_keeps_job_id_and_cannot_reinitialize(newcomer):
    case, _ = newcomer
    _fake_slurm(newcomer)
    _run(newcomer, *INIT)
    _run(newcomer, "prepare-neutral", "--yes")
    script_before = (case / "vasp.slurm").read_bytes()
    _run(newcomer, "submit-neutral", "--yes")
    before = _files(case)
    result = _run(newcomer, *INIT, expect=2)
    assert "existing" in result.stderr
    assert _files(case) == before
    _run(newcomer, "status")
    _run(newcomer, "run", "--yes")
    _run(newcomer, "configure-reference", "--yes", "--she-reference", "4.7",
         "--she-reference-source", "updated synthetic provenance")
    state = json.loads((case / "vaspsolkit.state.json").read_text())
    assert state["neutral"]["job_id"] == "910001"
    assert state["neutral"]["status"] == "RUNNING"
    assert (case / "vasp.slurm").read_bytes() == script_before
    submissions = [item for item in _calls(newcomer) if item["name"] == "sbatch"]
    assert len(submissions) == 1
    assert submissions[0]["cwd"] == str(case)
    assert submissions[0]["argv"][-1] == "vasp.slurm"
    assert not (case / "vasp.log").exists()


def test_scheduler_configuration_failure_keeps_existing_config(newcomer):
    case, _ = newcomer
    _run(newcomer, *INIT)
    before = _files(case)
    result = _run(newcomer, "configure-scheduler", expect=2)
    assert "sinfo" in result.stderr
    assert _files(case) == before


def test_custom_quoted_commands_submit_and_resume_without_shell(newcomer):
    case, env = newcomer
    wrapper = Path(env["PATH"]) / "site wrapper"
    wrapper.write_text(f"#!{sys.executable}\n" + """import json
import os
import sys
with open(os.environ['FAKE_SCHEDULER_LOG'], 'a', encoding='utf-8') as log:
    log.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')
print('JobID=site.42' if sys.argv[1] == 'submit' else 'RUNNING')
""", encoding="utf-8")
    wrapper.chmod(0o700)
    submit = [str(wrapper), "submit", "{script}", "literal ; $HOME"]
    _run(newcomer, "init", "--yes", "--scheduler", "custom", "--script", "vasp.slurm",
         "--she-reference", "4.7", "--submit-command", shlex.join(submit),
         "--status-command", shlex.join([str(wrapper), "status", "{job_id}"]),
         "--cancel-command", shlex.join([str(wrapper), "cancel", "{job_id}"]),
         "--inspect-command", shlex.join([str(wrapper), "inspect"]),
         "--job-id-regex", r"JobID=(?P<job_id>site\.\d+)")
    config = json.loads((case / "vaspsolkit.json").read_text())
    assert config["scheduler"]["submit_command"] == submit
    _run(newcomer, "plan", "--offline")
    _run(newcomer, "prepare-neutral", "--yes")
    _run(newcomer, "submit-neutral", "--yes")
    _run(newcomer, "status")
    _run(newcomer, "run", "--yes")
    assert _calls(newcomer) == [
        {"argv": ["submit", "vasp.slurm", "literal ; $HOME"], "cwd": str(case)},
        {"argv": ["status", "site.42"], "cwd": str(case.parent)},
    ]
    assert (case / "vasp.slurm").read_text() == SCRIPT
    state = json.loads((case / "vaspsolkit.state.json").read_text())
    assert state["neutral"]["job_id"] == "site.42"
    assert state["neutral"]["status"] == "RUNNING"


def test_custom_without_status_command_warns_unknown_without_prompting(newcomer):
    result = _run(
        newcomer, "init", "--yes", "--scheduler", "custom", "--script", "vasp.slurm",
        "--she-reference", "4.7", "--submit-command", "site-submit {script}",
    )
    assert "UNKNOWN" in result.stdout
    assert _calls(newcomer) == []


@pytest.mark.parametrize("missing", ["POTCAR", "KPOINTS"])
def test_automatic_vaspkit_imports_only_missing_input(newcomer, missing):
    case, env = newcomer
    library = case.parent / "library" / "C"
    library.mkdir(parents=True)
    (library / "POTCAR").write_bytes((case / "POTCAR").read_bytes())
    (case / missing).unlink()
    original = _files(case)
    helper = Path(env["PATH"]) / "vaspkit"
    helper.write_text(f"#!{sys.executable}\n" + """import json
import os
import sys
from pathlib import Path
assert not Path('vasp.slurm').exists()
assert Path('POSCAR').is_file() and Path('INCAR').is_file()
if '-help' in sys.argv:
    print('VASPKIT Standard Edition 1.5.0')
    sys.exit(0)
with open(os.environ['FAKE_SCHEDULER_LOG'], 'a', encoding='utf-8') as log:
    log.write(json.dumps({'cwd': os.getcwd()}) + '\\n')
Path('INCAR').write_text('ENCUT = 1\\nIBRION = -1\\n')
if '103' in sys.argv:
    Path('POTCAR').write_bytes((Path(os.environ['HOME']) / 'potentials/C/POTCAR').read_bytes())
else:
    Path('POTCAR').write_text('TITEL = PAW_PBE C GENERATED_FAKE\\nENMAX = 400 eV\\n')
Path('KPOINTS').write_text('Generated mesh\\n0\\nGamma\\n2 2 1\\n')
""", encoding="utf-8")
    helper.chmod(0o700)
    _run(newcomer, *INIT, "--backend", "vaspkit", "--kpr", "0.04",
         "--potcar-root", str(library.parent), "--functional", "PBE", "--potential", "C=C")
    for name, contents in original.items():
        if name != "INCAR":
            assert (case / name).read_bytes() == contents
    assert "ENCUT = 450" in (case / "INCAR").read_text()
    assert "IBRION = 1" in (case / "INCAR").read_text()
    assert (case / missing).is_file()
    call, = _calls(newcomer)
    assert Path(call["cwd"]) != case
    assert not Path(call["cwd"]).exists()


def test_yes_init_does_not_wait_with_open_stdin(newcomer):
    case, env = newcomer
    process = subprocess.Popen(
        [sys.executable, "-m", "vaspsolkit", *INIT, "--workdir", str(case)],
        cwd=case.parent, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    try:
        # communicate() closes stdin and would conceal an accidental prompt.
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail("init --yes waited for input:\n" + stdout + stderr)
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stdout + stderr


def test_wheel_install_exposes_console_and_module_entrypoints(newcomer, tmp_path):
    case, isolated_env = newcomer
    source = tmp_path / "source tree with spaces"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(ROOT / "vaspsolkit", source / "vaspsolkit",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    environment = tmp_path / "installed environment"
    # Reuse the tested build toolchain, not ensurepip's older bundled setuptools.
    venv.EnvBuilder(with_pip=False, system_site_packages=True).create(environment)
    python = environment / "bin" / "python"
    # A nested venv inherits the base interpreter's packages, not the runner's
    # venv. Reuse only its dependency directory, never its source-tree sys.path.
    site_packages = subprocess.check_output(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        text=True, timeout=15,
    ).strip()
    (Path(site_packages) / "test-dependencies.pth").write_text(
        sysconfig.get_path("purelib") + "\n", encoding="utf-8",
    )
    env = dict(isolated_env)
    env.pop("PYTHONPATH")
    env.update(PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_CONFIG_FILE=os.devnull)
    wheels = tmp_path / "wheels"
    result = subprocess.run(
        [str(python), "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
         "--wheel-dir", str(wheels), str(source)],
        cwd=case, env=env, text=True, capture_output=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    artifacts = list(wheels.glob("*.whl"))
    assert len(artifacts) == 1 and artifacts[0].name.startswith("vaspsolkit-"), (
        artifacts, result.stdout, result.stderr
    )
    wheel = artifacts[0]
    with zipfile.ZipFile(wheel) as archive:
        metadata_path, = (name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        assert "Name: vaspsolkit\n" in archive.read(metadata_path).decode("utf-8")
    result = subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", "--ignore-installed", str(wheel)],
        cwd=case, env=env, text=True, capture_output=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run(
        [str(python), "-c",
         "import importlib.metadata as m, json, vaspsolkit; "
         "print(json.dumps({'module': vaspsolkit.__file__, "
         "'origin': json.loads(m.distribution('vaspsolkit').read_text('direct_url.json'))}))"],
        cwd=case, env=env, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    installed = json.loads(result.stdout)
    assert Path(installed["module"]).resolve().is_relative_to(environment.resolve())
    assert installed["origin"]["url"] == wheel.as_uri()
    for entrypoint in ([str(environment / "bin" / "vaspsolkit")],
                       [str(python), "-m", "vaspsolkit"]):
        result = subprocess.run([*entrypoint, "--help"], cwd=case, env=env,
                                text=True, capture_output=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "prepare-neutral" in result.stdout
        result = subprocess.run([*entrypoint, "status"], cwd=case, env=env,
                                text=True, capture_output=True, timeout=15)
        assert result.returncode == 2, result.stdout + result.stderr
        assert "vaspsolkit.json" in result.stderr
        assert "Traceback" not in result.stderr
    # Execute the manual's offline journey from the wheel, outside the source tree.
    console = str(environment / "bin" / "vaspsolkit")
    commands = [
        ("inputs", "--dry-run"), ("next", "--dry-run"), INIT,
        ("plan", "--offline"), ("prepare-neutral", "--yes"),
        ("submit-neutral", "--dry-run"),
        ("postprocess", "--summary", str(ROOT / "docs/_static/example-summary.csv"),
         "--output", str(case / "synthetic analysis")),
    ]
    for command in commands:
        result = subprocess.run([console, *command], cwd=case, env=env,
                                text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, (command, result.stdout, result.stderr)
        assert "Traceback" not in result.stderr
    analysis = json.loads((case / "synthetic analysis/analysis.json").read_text())
    assert len(analysis["points"]) == 5
    assert sum(point["delta_electrons"] == 0 for point in analysis["points"]) == 1
    assert analysis["energy_fit"]["u_vertex"] == pytest.approx(0.2)
    assert (case / "synthetic analysis/eu_curve.png").stat().st_size > 1000
    assert not _calls(newcomer)
    assert (case / "vasp.slurm").read_text() == SCRIPT
