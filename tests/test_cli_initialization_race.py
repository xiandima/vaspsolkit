import pytest

from tests.test_cli_user_experience import _write_synthetic_inputs
from vaspsolkit import cli
from vaspsolkit.state import JobRecord, WorkflowState


def _arguments(root, *extra):
    return [
        "init", "--workdir", str(root), "--scheduler", "slurm",
        "--script", "vasp.slurm", "--she-reference", "4.7", *extra,
    ]


def test_old_confirmation_cannot_erase_another_sessions_job_id(tmp_path):
    _write_synthetic_inputs(tmp_path)
    committed = {}

    def other_session(prompt):
        if prompt.startswith("Additional"):
            return ""
        assert prompt.startswith("Write INCAR")
        assert cli.main(_arguments(tmp_path, "--yes"), output=lambda _: None) == 0
        WorkflowState(
            stage="neutral_submitted",
            neutral=JobRecord(folder=".", status="SUBMITTED", job_id="12345"),
        ).save(tmp_path / "vaspsolkit.state.json")
        for name in ("INCAR", "vaspsolkit.json", "vaspsolkit.state.json"):
            committed[name] = (tmp_path / name).read_bytes()
        return "y"

    with pytest.raises((FileExistsError, RuntimeError), match="existing|stale|changed"):
        cli.main(_arguments(tmp_path, "--profile", "static"), input_fn=other_session, output=lambda _: None)
    assert committed
    assert {name: (tmp_path / name).read_bytes() for name in committed} == committed
    assert WorkflowState.load(tmp_path / "vaspsolkit.state.json").neutral.job_id == "12345"


def test_edited_template_invalidates_pending_confirmation(tmp_path):
    _write_synthetic_inputs(tmp_path)
    new_incar = "ENCUT=700; ALGO=Normal; IBRION=2; NSW=100\n"

    def edit_template(prompt):
        if prompt.startswith("Additional"):
            return ""
        assert prompt.startswith("Write INCAR")
        (tmp_path / "INCAR").write_text(new_incar)
        return "y"

    with pytest.raises(RuntimeError, match="stale|changed"):
        cli.main(_arguments(tmp_path), input_fn=edit_template, output=lambda _: None)
    assert (tmp_path / "INCAR").read_text() == new_incar
    assert not (tmp_path / "vaspsolkit.json").exists()
    assert not (tmp_path / "vaspsolkit.state.json").exists()


def test_noninteractive_init_rechecks_state_after_input_preflight(tmp_path, monkeypatch):
    _write_synthetic_inputs(tmp_path)
    real_ensure = cli.complete_inputs

    def concurrent_state(*args, **kwargs):
        result = real_ensure(*args, **kwargs)
        WorkflowState(
            stage="neutral_submitted",
            neutral=JobRecord(folder=".", status="SUBMITTED", job_id="12345"),
        ).save(tmp_path / "vaspsolkit.state.json")
        return result

    monkeypatch.setattr(cli, "complete_inputs", concurrent_state)
    with pytest.raises(FileExistsError, match="existing"):
        cli.main(_arguments(tmp_path, "--yes"), output=lambda _: None)
    assert WorkflowState.load(tmp_path / "vaspsolkit.state.json").neutral.job_id == "12345"


def test_legacy_init_writes_configuration_while_holding_the_state_lock(tmp_path, monkeypatch):
    from vaspsolkit import state

    _write_synthetic_inputs(tmp_path)
    real_write = cli.write_kit_config
    observed = []

    def checked_write(*args, **kwargs):
        key = str(tmp_path / "vaspsolkit.state.json")
        assert key in getattr(state._HELD_STATE_LOCKS, "paths", {})
        observed.append(True)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(cli, "write_kit_config", checked_write)
    assert cli.main(_arguments(tmp_path, "--profile", "static", "--yes"), output=lambda _: None) == 0
    assert observed
