import pytest

from vaspsolkit import cli


@pytest.mark.parametrize("command", ["status", "plan", "prepare-neutral"])
def test_missing_config_is_actionable_without_traceback(tmp_path, capsys, command):
    assert cli.entrypoint([command, "--workdir", str(tmp_path)]) == 2
    message = capsys.readouterr().err
    assert "vaspsolkit.json" in message
    assert "init" in message
    assert "Traceback" not in message


def test_debug_mode_preserves_original_exception(tmp_path):
    with pytest.raises(FileNotFoundError):
        cli.entrypoint(["--debug", "status", "--workdir", str(tmp_path)])


@pytest.mark.parametrize("missing", ["--scheduler", "--script", "--profile", "--she-reference"])
def test_yes_init_validates_all_required_arguments_before_prompting(tmp_path, missing):
    args = [
        "init", "--workdir", str(tmp_path), "--yes", "--scheduler", "slurm",
        "--script", "vasp.slurm", "--profile", "vaspsol-neutral-relax",
        "--she-reference", "4.7",
    ]
    index = args.index(missing)
    del args[index:index + 2]
    with pytest.raises(ValueError, match=missing):
        cli.main(args, input_fn=lambda prompt: pytest.fail(f"unexpected prompt: {prompt}"))
    assert not list(tmp_path.iterdir())


def test_yes_init_rejects_empty_script_without_prompting(tmp_path):
    with pytest.raises(ValueError, match="--script"):
        cli.main([
            "init", "--workdir", str(tmp_path), "--yes", "--scheduler", "slurm",
            "--script", "", "--profile", "static", "--she-reference", "4.7",
        ], input_fn=lambda prompt: pytest.fail(f"unexpected prompt: {prompt}"))


def test_yes_repair_requires_explicit_job_without_prompting(tmp_path):
    from argparse import Namespace
    from vaspsolkit.state import WorkflowState

    WorkflowState(stage="setup").save(tmp_path / "vaspsolkit.state.json")
    with pytest.raises(ValueError, match="--job"):
        cli._cmd_repair(
            Namespace(yes=True, job=None), tmp_path, None,
            lambda prompt: pytest.fail(f"unexpected prompt: {prompt}"), lambda _: None,
        )


@pytest.mark.parametrize("answer", ["0", "-1", "-2", "3", "wrong"])
def test_choice_rejects_out_of_range_indices(answer):
    with pytest.raises(ValueError, match="selection"):
        cli._choose("scheduler", ["slurm", "custom"], lambda _: answer, lambda _: None)


@pytest.mark.parametrize("error, code", [(EOFError(), 1), (KeyboardInterrupt(), 130)])
def test_entrypoint_handles_interruption_without_claiming_no_job_was_submitted(monkeypatch, capsys, error, code):
    def interrupted(*args, **kwargs):
        raise error

    monkeypatch.setattr(cli, "main", interrupted)
    assert cli.entrypoint(["submit-neutral"]) == code
    assert "receipt" in capsys.readouterr().err.lower()


def test_python_errors_still_surface_for_debugging(monkeypatch):
    def broken(*args, **kwargs):
        raise TypeError("unexpected programming error")

    monkeypatch.setattr(cli, "main", broken)
    with pytest.raises(TypeError, match="programming error"):
        cli.entrypoint(["status"])


@pytest.mark.parametrize("filename", ["vaspsolkit.json", "vaspsolkit.state.json"])
def test_reinitialization_cannot_overwrite_existing_case_files(tmp_path, filename):
    from tests.test_cli_user_experience import _write_synthetic_inputs

    _write_synthetic_inputs(tmp_path)
    path = tmp_path / filename
    path.write_text('{"stage": "neutral_submitted", "job_id": "123"}')
    before = path.read_bytes()
    with pytest.raises(FileExistsError, match="already|existing|已"):
        cli.main([
            "init", "--workdir", str(tmp_path), "--yes", "--profile", "static",
            "--scheduler", "slurm", "--script", "vasp.slurm", "--she-reference", "4.7",
        ])
    assert path.read_bytes() == before
