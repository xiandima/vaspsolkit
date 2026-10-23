from pathlib import Path
import subprocess

import pytest

from vaspsolkit.config import SchedulerConfig
from vaspsolkit.scheduler import CustomScheduler, SlurmScheduler, scheduler_from_config


@pytest.fixture(autouse=True)
def forbid_external_scheduler_commands(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("tests must use an injected scheduler runner")

    monkeypatch.setattr(subprocess, "run", forbidden)


def custom_config(**overrides):
    values = {
        "kind": "custom",
        "submit_command": ["site-submit", "{script}"],
        "status_command": ["site-status", "{job_id}"],
        "cancel_command": ["site-cancel", "{job_id}"],
        "job_id_pattern": r"JobID=(?P<job_id>\d+)",
    }
    values.update(overrides)
    return SchedulerConfig(**values)


@pytest.mark.parametrize(
    "field", ["submit_command", "status_command", "inspect_command", "cancel_command"]
)
@pytest.mark.parametrize("template", ["{jobid}", "{script", "{job_id.__class__}"])
def test_custom_templates_are_validated_before_any_command(field, template):
    config = custom_config(**{field: ["site-command", template]})

    with pytest.raises(ValueError, match=field):
        scheduler_from_config(config)


@pytest.mark.parametrize("template", ["{}", "{0}", "{script:>20}", "{job_id!r}"])
def test_custom_templates_reject_unsupported_field_formatting(template):
    with pytest.raises(ValueError, match="submit_command"):
        scheduler_from_config(custom_config(submit_command=["site-submit", template]))


@pytest.mark.parametrize("pattern", ["[", "(?P<job_id>", r"(?P<job_id>\d+)\k"])
def test_custom_job_id_regex_is_validated_before_any_command(pattern):
    with pytest.raises(ValueError, match="job_id_pattern"):
        scheduler_from_config(custom_config(job_id_pattern=pattern))


def test_custom_templates_preserve_escaped_braces_and_argument_boundaries(tmp_path: Path):
    calls = []

    def runner(args, cwd):
        calls.append((list(args), cwd))
        out = "JobID=4321\n" if args[0] == "site-submit" else "RUNNING\n"
        return subprocess.CompletedProcess(args, 0, out, "")

    config = custom_config(
        submit_command=["site-submit", "{script}", "{{escaped}}", "${{HOME}}"],
        status_command=["site-status", "{job_id}", '{{"job": "{job_id}"}}'],
        inspect_command=["site-inspect", "{{ print $1 }}"],
        cancel_command=["site-cancel", "{job_id}"],
    )
    scheduler = CustomScheduler(config, runner)
    assert scheduler.submit(tmp_path, "job {job_id} with spaces.sh") == "4321"
    assert scheduler.status("4321").state == "RUNNING"
    scheduler.inspect()
    scheduler.cancel("4321")

    assert calls == [
        (["site-submit", "job {job_id} with spaces.sh", "{escaped}", "${HOME}"], tmp_path),
        (["site-status", "4321", '{"job": "4321"}'], None),
        (["site-inspect", "{ print $1 }"], None),
        (["site-cancel", "4321"], None),
    ]


def test_custom_regex_without_named_capture_remains_supported(tmp_path: Path):
    def runner(args, cwd):
        return subprocess.CompletedProcess(args, 0, "site accepted 4321\n", "")

    scheduler = CustomScheduler(custom_config(job_id_pattern=r"\d+"), runner)
    assert scheduler.submit(tmp_path, "job.sh") == "4321"


@pytest.mark.parametrize(
    "output",
    [
        "4321\n",
        "4321;cluster-a\n",
        "Submitted batch job 4321\n",
        "site wrapper: using account chemistry\n4321;cluster-a\n",
        "site wrapper: using account chemistry\nSubmitted batch job 4321\n",
    ],
)
def test_slurm_submit_extracts_job_id_from_site_output(tmp_path: Path, output):
    calls = []

    def runner(args, cwd):
        calls.append((list(args), cwd))
        return subprocess.CompletedProcess(args, 0, output, "site warning\n")

    assert SlurmScheduler(runner).submit(tmp_path, "site job.slurm") == "4321"
    assert calls == [(["sbatch", "--parsable", "site job.slurm"], tmp_path)]


@pytest.mark.parametrize(
    "output",
    [
        "",
        "submitted successfully\n",
        "account 4321 has exhausted its quota\n",
        "4321;cluster-a;unexpected\n",
        "4321\n5678\n",
        "Submitted batch job 4321\nSubmitted batch job 5678\n",
    ],
)
def test_slurm_submit_rejects_unrecognized_or_ambiguous_ids(tmp_path: Path, output):
    calls = []

    def runner(args, cwd):
        calls.append((list(args), cwd))
        return subprocess.CompletedProcess(args, 0, output, "")

    with pytest.raises(RuntimeError, match="job id"):
        SlurmScheduler(runner).submit(tmp_path, "site.slurm")
    assert len(calls) == 1


@pytest.mark.parametrize("query", ["squeue", "sacct", "custom"])
@pytest.mark.parametrize("failure_type", [FileNotFoundError, subprocess.TimeoutExpired])
def test_query_invocation_failures_are_unknown_not_missing(query, failure_type):
    failure = (
        subprocess.TimeoutExpired("query", 30)
        if failure_type is subprocess.TimeoutExpired
        else FileNotFoundError("query command not installed")
    )
    calls = []

    def runner(args, cwd):
        calls.append(list(args))
        if query == "sacct" and args[0] == "squeue":
            return subprocess.CompletedProcess(args, 0, "", "")
        raise failure

    scheduler = (
        CustomScheduler(custom_config(), runner)
        if query == "custom" else SlurmScheduler(runner)
    )
    state = scheduler.status("4321")

    assert state.job_id == "4321"
    assert state.exists is True
    assert state.state == "UNKNOWN"
    assert state.raw == str(failure)
    assert len(calls) == (2 if query == "sacct" else 1)


@pytest.mark.parametrize("kind", ["slurm", "custom"])
def test_default_status_query_has_a_timeout(kind, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((list(args), kwargs))
        return subprocess.CompletedProcess(args, 0, "RUNNING\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    scheduler = CustomScheduler(custom_config()) if kind == "custom" else SlurmScheduler()
    assert scheduler.status("4321").state == "RUNNING"
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 30


@pytest.mark.parametrize("output", ["", "\n", " \n\t\n"])
def test_custom_empty_status_is_unknown_not_missing(output):
    def runner(args, cwd):
        return subprocess.CompletedProcess(args, 0, output, "")

    state = CustomScheduler(custom_config(), runner).status("4321")
    assert state.exists is True
    assert state.state == "UNKNOWN"
    assert state.raw == output
