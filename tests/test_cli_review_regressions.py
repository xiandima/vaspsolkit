"""First-round CLI UX regressions using synthetic files and mock SLURM only."""

import subprocess

import pytest

from tests.test_cli_user_experience import _contents, _write_synthetic_inputs
from vaspsolkit import cli
from vaspsolkit.config import KitConfig, WorkflowConfig, write_kit_config
from vaspsolkit.guide_model import build_snapshot, recommend_action
from vaspsolkit.interactive_menu import run_menu_action
from vaspsolkit.orchestrator import prepare_neutral_job, submit_neutral_job
from vaspsolkit.scheduler import SlurmScheduler
from vaspsolkit.state import JobRecord, WorkflowState


@pytest.fixture(autouse=True)
def no_external_processes(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail(f"unexpected external process: {args}")

    monkeypatch.setattr(subprocess, "run", blocked)


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path / "case"
    root.mkdir()
    _write_synthetic_inputs(root)
    config = KitConfig(workflow=WorkflowConfig(she_reference_confirmed=True))
    write_kit_config(root / "vaspsolkit.json", config)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "settings"))
    return root, config


@pytest.fixture
def queue(monkeypatch):
    result = {"state": "FAILED", "queries": []}

    def runner(argv, cwd):
        result["queries"].append(argv)
        if argv[0] == "squeue":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "sacct":
            return subprocess.CompletedProcess(argv, 0, f"424242|{result['state']}|0:0\n", "")
        pytest.fail(f"unexpected scheduler operation: {argv}")

    scheduler = SlurmScheduler(runner=runner)
    monkeypatch.setattr(cli, "scheduler_from_config", lambda _: scheduler)
    result["scheduler"] = scheduler
    return result


def record_neutral(root, status="FAILED"):
    state = WorkflowState(
        stage="neutral_needs_review",
        neutral=JobRecord(
            folder=".", status=status, job_id="424242",
            metadata={"stage": "neutral_relax"},
        ),
    )
    state.save(root / "vaspsolkit.state.json")


def write_outputs(root, *, converged):
    text = (
        "NSW = 88\nIBRION = 1\nNELECT = 4.0 total number of electrons\n"
        "E-fermi : -5.0\nfree  energy   TOTEN = -10.0 eV\n"
        "aborting loop because EDIFF is reached\n"
    )
    if converged:
        text += "reached required accuracy - stopping structural energy minimisation\n"
        text += "General timing and accounting informations for this job:\n"
    (root / "OUTCAR").write_text(text)
    (root / "CONTCAR").write_bytes((root / "POSCAR").read_bytes())
    for name in ("CHGCAR", "LOCPOT"):
        (root / name).write_text("synthetic output for status checks only\n")


def run_check(root, command, messages):
    if command == "02":
        return run_menu_action("02", build_snapshot(root), output=messages.append)
    return cli.main([command, "--workdir", str(root)], output=messages.append)


@pytest.mark.parametrize("command", ["02", "check", "status", "monitor"])
@pytest.mark.parametrize("terminal", ["FAILED", "CANCELLED", "TIMEOUT"])
def test_terminal_neutral_never_returns_to_submitted(case, queue, command, terminal):
    root, _ = case
    record_neutral(root)
    queue["state"] = terminal
    messages = []

    assert run_check(root, command, messages) == 0

    state = WorkflowState.load(root / "vaspsolkit.state.json")
    assert state.neutral.status == "NEEDS_REVIEW"
    assert state.neutral.job_id == "424242"
    assert state.stage == "neutral_needs_review"
    assert "outcar_missing" in "\n".join(messages)
    assert recommend_action(build_snapshot(root)).cli_command == "check-neutral"


@pytest.mark.parametrize("command", ["check", "status", "monitor"])
@pytest.mark.parametrize("converged", [False, True])
def test_completed_neutral_requires_scientific_output_checks(case, queue, command, converged):
    root, _ = case
    record_neutral(root, "RUNNING")
    write_outputs(root, converged=converged)
    queue["state"] = "COMPLETED"
    messages = []

    assert run_check(root, command, messages) == 0

    state = WorkflowState.load(root / "vaspsolkit.state.json")
    assert state.neutral.status == ("CONVERGED" if converged else "NEEDS_REVIEW")
    expected = "prepare-charge" if converged else "check-neutral"
    assert recommend_action(build_snapshot(root)).cli_command == expected


@pytest.mark.parametrize("missing", ["CONTCAR", "CHGCAR", "LOCPOT"])
def test_completed_neutral_requires_all_restart_outputs(case, queue, missing):
    root, _ = case
    record_neutral(root, "RUNNING")
    write_outputs(root, converged=True)
    (root / missing).unlink()
    queue["state"] = "COMPLETED"
    messages = []
    assert run_check(root, "status", messages) == 0
    state = WorkflowState.load(root / "vaspsolkit.state.json")
    assert state.neutral.status == "NEEDS_REVIEW"
    assert f"missing_or_empty_{missing.lower()}" in "\n".join(messages)


@pytest.mark.parametrize("converged", [False, True])
@pytest.mark.parametrize("command", ["status", "02"])
def test_unknown_scheduler_still_blocks_repreparation_and_submission(case, queue, converged, command):
    root, config = case
    record_neutral(root, "FAILED" if command == "02" else "RUNNING")
    write_outputs(root, converged=converged)
    if not converged:
        with (root / "OUTCAR").open("a") as handle:
            handle.write("BRMIX: very serious problems\n")
    queue["state"] = "UNKNOWN"
    assert run_check(root, command, []) == 0
    state = WorkflowState.load(root / "vaspsolkit.state.json")
    assert state.neutral.status == "UNKNOWN"
    assert recommend_action(build_snapshot(root)).cli_command == "monitor"
    before = _contents(root)
    with pytest.raises(RuntimeError, match="active jobs"):
        prepare_neutral_job(root, config)
    with pytest.raises(RuntimeError, match="prepare-neutral"):
        submit_neutral_job(root, config, scheduler=queue["scheduler"],
                           confirmed=True, require_prepared=True)
    assert _contents(root) == before


def test_already_converged_neutral_is_not_downgraded(case, queue):
    root, _ = case
    record_neutral(root, "CONVERGED")
    write_outputs(root, converged=True)
    queue["state"] = "FAILED"
    assert run_check(root, "status", []) == 0
    assert WorkflowState.load(root / "vaspsolkit.state.json").neutral.status == "CONVERGED"
    assert queue["queries"] == []


def test_monitor_watch_returns_after_terminal_neutral_check(case, queue, monkeypatch):
    root, _ = case
    record_neutral(root, "RUNNING")
    monkeypatch.setattr(cli.time, "sleep", lambda _: pytest.fail("terminal job kept polling"))
    assert cli.main(["monitor", "--workdir", str(root), "--watch"], output=lambda _: None) == 0


@pytest.mark.parametrize("command", ["submit-neutral", "submit-selected"])
@pytest.mark.parametrize("option", ["--resource-tasks", "--resource-node-count", "--resource-tasks-per-node"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_invalid_resource_counts_fail_before_submission(case, monkeypatch, capsys, command, option, value):
    root, _ = case
    for name in ("_submit_neutral_once", "_submit_selected_once"):
        monkeypatch.setattr(cli, name, lambda *a, **kw: pytest.fail("invalid resource reached submission"))
    argv = [command, "--workdir", str(root), "--dry-run", option, value]
    if command == "submit-selected":
        argv.append("1")
    before = _contents(root)

    assert cli.entrypoint(argv) == 2

    assert "positive" in capsys.readouterr().err
    assert _contents(root) == before


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("command", ["submit-neutral", "submit-selected"])
def test_omitted_and_positive_resources_keep_expected_request(case, monkeypatch, command, explicit):
    root, _ = case
    requests = []
    def capture(*args, **kwargs):
        requests.append(kwargs["resources"])
        return 0
    monkeypatch.setattr(cli, "_submit_neutral_once", capture)
    monkeypatch.setattr(cli, "_submit_selected_once", capture)
    argv = [command, "--workdir", str(root), "--dry-run"]
    if command == "submit-selected":
        argv.append("1")
    if explicit:
        argv.extend(["--resource-node-count", "2", "--resource-tasks", "64",
                     "--resource-tasks-per-node", "32"])
    assert cli.main(argv) == 0
    request = requests[0]
    assert (request.node_count, request.tasks, request.tasks_per_node) == (
        (2, 64, 32) if explicit else (1, 96, 96)
    )


@pytest.mark.parametrize("invalid", ["vaspsolkit.json", "vaspsolkit.state.json", "directory"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_next_reports_blocking_case_diagnostic_without_a_mutating_action(case, invalid, dry_run):
    root, _ = case
    target = root / invalid
    if invalid != "directory":
        target.write_text("{invalid json}\n")
    else:
        root = target
    before = _contents(root)
    messages = []

    argv = ["next", "--workdir", str(root)] + (["--dry-run"] if dry_run else [])
    assert cli.main(argv, input_fn=lambda p: pytest.fail(p), output=messages.append) != 0

    transcript = "\n".join(messages)
    assert str(target) in transcript
    assert "configure-reference" not in transcript
    assert "prepare-neutral" not in transcript
    assert _contents(root) == before


def test_menu_recommendation_for_bad_config_shows_diagnostic_and_recovers(case):
    root, config = case
    path = root / "vaspsolkit.json"
    good = path.read_bytes()
    path.write_text("{invalid json}\n")
    before = _contents(root)
    messages = []
    assert run_menu_action(
        "02", build_snapshot(root), input_fn=lambda p: pytest.fail(f"unexpected prompt: {p}"),
        output=messages.append,
    ) != 0
    assert str(path) in "\n".join(messages)
    assert "line 1 column 2" in "\n".join(messages)
    assert _contents(root) == before
    path.write_bytes(good)
    assert recommend_action(build_snapshot(root)).cli_command == "prepare-neutral"


@pytest.mark.parametrize("explicit", [False, True])
def test_invalid_input_settings_error_identifies_file_and_recovery(case, capsys, explicit):
    root, _ = case
    (root / "KPOINTS").unlink()
    settings = root.parent / "settings" / "vaspsolkit" / "inputs.json"
    if explicit:
        settings = root.parent / "other settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{invalid json}\n")
    argv = ["inputs", "--workdir", str(root), "--mesh", "2", "2", "1", "--yes"]
    if explicit:
        argv.extend(["--settings", str(settings)])
    before = _contents(root)

    assert cli.entrypoint(argv) == 2

    message = capsys.readouterr().err
    assert str(settings) in message
    assert "line 1 column 2" in message
    assert "--settings" in message
    assert "Traceback" not in message
    assert _contents(root) == before
    assert settings.read_text() == "{invalid json}\n"
    settings.write_text("{}\n")
    assert cli.entrypoint(argv) == 0
    assert "2 2 1" in (root / "KPOINTS").read_text()
