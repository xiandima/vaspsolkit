"""Scheduler/CLI review regressions; all scheduler calls are injected fakes."""

import hashlib
import json
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from tests.test_cli_user_experience import _write_synthetic_inputs
from vaspsolkit import cli
from vaspsolkit.config import KitConfig, write_kit_config
from vaspsolkit.guide_model import build_snapshot, recommend_action
from vaspsolkit.operations.activity import read_submission_receipt
from vaspsolkit.operations.controller import WorkbenchController
from vaspsolkit.orchestrator import STATE_FILENAME, submission_preview, submit_ready_jobs, submit_selected_jobs
from vaspsolkit.scheduler import CustomScheduler, SlurmScheduler
from vaspsolkit.state import JobRecord, WorkflowState
from vaspsolkit.submission_resources import resources_from_config


@pytest.fixture(autouse=True)
def forbid_real_scheduler(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("real scheduler commands are forbidden")

    monkeypatch.setattr("vaspsolkit.scheduler._default_runner", forbidden)


def prepared_case(root, *, limit=None, kind="slurm", count=3):
    root.mkdir()
    _write_synthetic_inputs(root)
    config = KitConfig()
    config.workflow.she_reference_confirmed = True
    config.scheduler.max_inflight = limit
    config.scheduler.kind = kind
    if kind == "custom":
        config.scheduler.submit_command = ["fake-submit", "{script}"]
    write_kit_config(root / "vaspsolkit.json", config)
    jobs = {}
    for index in range(1, count + 1):
        name = str(index)
        folder = root / "charge_sweep" / name
        folder.mkdir(parents=True)
        for filename in ("POSCAR", "INCAR", "KPOINTS", "POTCAR", "vasp.slurm"):
            (folder / filename).write_bytes((root / filename).read_bytes())
        (folder / "CHGCAR").write_text("synthetic charge\n")
        jobs[name] = JobRecord(folder=str(folder.relative_to(root)))
    state = WorkflowState(
        stage="charge_ready", prepared_checked=True, jobs=jobs,
        neutral=JobRecord(folder=".", status="CONVERGED", job_id="8000"),
    )
    state.save(root / STATE_FILENAME)
    return config, state


class FakeSlurm(SlurmScheduler):
    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at
        super().__init__(self.run)

    def run(self, argv, cwd):
        if argv[0] == "squeue":
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert argv[0] == "sbatch"
        self.calls.append(Path(cwd).name)
        if len(self.calls) == self.fail_at:
            raise RuntimeError("injected submission connection loss")
        return subprocess.CompletedProcess(argv, 0, str(9000 + len(self.calls)), "")


def test_cli_batch_failure_keeps_accepted_id_and_blocks_retry(tmp_path, monkeypatch):
    case = tmp_path / "case"
    prepared_case(case, count=2)
    scheduler = FakeSlurm(fail_at=2)
    monkeypatch.setattr(cli, "scheduler_from_config", lambda _: scheduler)
    args = ["submit", "--workdir", str(case), "--yes"]

    with pytest.raises(RuntimeError):
        cli.main(args, output=lambda _: None)
    saved = WorkflowState.load(case / STATE_FILENAME)
    assert saved.jobs["1"].job_id == "9001"
    assert saved.jobs["1"].status == "SUBMITTED"
    receipt = read_submission_receipt(case)
    assert receipt is not None and receipt.resources["target_job"] == "2"
    with pytest.raises(RuntimeError):
        cli.main(args, output=lambda _: None)
    assert scheduler.calls == ["1", "2"]


@pytest.mark.parametrize("submitter", [submit_ready_jobs, submit_selected_jobs])
def test_low_level_api_checkpoints_returned_ids_but_has_no_receipt(tmp_path, submitter):
    case = tmp_path / "case"
    config, state = prepared_case(case, count=2)
    scheduler = FakeSlurm(fail_at=2)
    selected = {"selected": ["1", "2"]} if submitter is submit_selected_jobs else {}
    with pytest.raises(RuntimeError, match="connection loss"):
        submitter(case, config, state, scheduler=scheduler, confirmed=True, **selected)
    saved = WorkflowState.load(case / STATE_FILENAME)
    assert saved.jobs["1"].job_id == "9001"
    assert saved.jobs["1"].status == "SUBMITTED"
    assert saved.jobs["2"].job_id == ""
    # Only the controller can reconcile an ambiguous scheduler call safely.
    assert read_submission_receipt(case) is None


def test_repair_auto_submit_also_uses_durable_receipts(tmp_path, monkeypatch):
    from types import SimpleNamespace

    case = tmp_path / "case"
    _, state = prepared_case(case, count=2)
    state.jobs["1"].status = "NEEDS_REVIEW"
    state.save(case / STATE_FILENAME)
    scheduler = FakeSlurm(fail_at=2)
    monkeypatch.setattr(cli, "scheduler_from_config", lambda _: scheduler)
    monkeypatch.setattr(cli, "propose_repair", lambda *a: SimpleNamespace(reason="test", seed_mode="neutral", incar_changes={}))
    monkeypatch.setattr(cli, "apply_repair", lambda *a, **kw: case / "archive")

    with pytest.raises(RuntimeError):
        cli.main(["repair", "--workdir", str(case), "--job", "1", "--yes"], output=lambda _: None)
    assert WorkflowState.load(case / STATE_FILENAME).jobs["1"].job_id == "9001"
    assert read_submission_receipt(case).resources["target_job"] == "2"
    assert scheduler.calls == ["1", "2"]


@pytest.mark.parametrize("active_status", ["QUEUED", "RUNNING", "SUBMITTED", "UNKNOWN"])
def test_selected_capacity_includes_recorded_active_jobs(tmp_path, active_status):
    case = tmp_path / "case"
    config, state = prepared_case(case, limit=2)
    state.jobs["1"].status = active_status
    state.jobs["1"].job_id = "8001"
    state.save(case / STATE_FILENAME)
    scheduler = FakeSlurm()
    controller = WorkbenchController(case, scheduler_factory=lambda _: scheduler)
    plan = controller.plan("submit-selected", resources_from_config(config), selected=("2", "3"))

    assert plan.target_jobs == ("2",)
    assert plan.warnings
    result = controller.execute(plan, confirmed=True)
    assert result.ok and scheduler.calls == ["2"]
    assert WorkflowState.load(case / STATE_FILENAME).jobs["3"].status == "PREPARED"


def test_full_capacity_blocks_without_receipt_or_submission(tmp_path):
    case = tmp_path / "case"
    config, state = prepared_case(case, limit=1)
    state.jobs["1"].status, state.jobs["1"].job_id = "UNKNOWN", "8001"
    state.save(case / STATE_FILENAME)
    scheduler = FakeSlurm()
    controller = WorkbenchController(case, scheduler_factory=lambda _: scheduler)
    plan = controller.plan("submit-selected", resources_from_config(config), selected=("2",))

    assert "max_inflight" in plan.blocked_reason
    with pytest.raises(RuntimeError, match="max_inflight"):
        controller.execute(plan, confirmed=True)
    assert scheduler.calls == []
    assert read_submission_receipt(case) is None


@pytest.mark.parametrize("dry_run", [False, True])
def test_low_level_selected_batch_uses_same_capacity(tmp_path, dry_run):
    case = tmp_path / "case"
    config, state = prepared_case(case, limit=1)
    scheduler = FakeSlurm()
    before = (case / STATE_FILENAME).read_bytes()
    submitted = submit_selected_jobs(
        case, config, state, ["1", "2", "3"], scheduler=scheduler,
        confirmed=not dry_run, dry_run=dry_run,
    )
    assert list(submitted) == ["1"]
    if dry_run:
        assert (case / STATE_FILENAME).read_bytes() == before
        assert scheduler.calls == []


def test_custom_scheduler_without_inspect_counts_case_jobs(tmp_path):
    case = tmp_path / "case"
    config, state = prepared_case(case, limit=1, kind="custom")
    state.jobs["1"].status, state.jobs["1"].job_id = "UNKNOWN", "site.1"
    scheduler = CustomScheduler(config.scheduler, runner=lambda *a: pytest.fail("no command expected"))
    preview = submission_preview(config, state, scheduler=scheduler)
    assert preview["active"] == 1
    assert preview["batches"][0] == []


def test_competing_selected_plans_cannot_exceed_capacity(tmp_path):
    case = tmp_path / "case"
    config, _ = prepared_case(case, limit=1)
    scheduler = FakeSlurm()
    controllers = [WorkbenchController(case, scheduler_factory=lambda _: scheduler) for _ in range(2)]
    plans = [controller.plan("submit-selected", resources_from_config(config), selected=(str(i + 1),))
             for i, controller in enumerate(controllers)]
    ready = Barrier(2)

    def execute(index):
        ready.wait(timeout=5)
        try:
            return controllers[index].execute(plans[index], confirmed=True).ok
        except RuntimeError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(execute, range(2)))
    assert sum(outcomes) == 1
    assert len(scheduler.calls) == 1


def fake_collect(base, workflow, output=None):
    target = output or base / workflow.results_root / workflow.summary_file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("she_reference_eV,she_reference_source\n4.7,\n")
    return [{"she_reference_eV": 4.7, "she_reference_source": ""}]


def write_fit(case):
    summary = case / "results" / "summary.csv"
    fit = summary.parent / "eu"
    fit.mkdir(exist_ok=True)
    (fit / "analysis.json").write_text(json.dumps({
        "provenance": {"summary_sha256": hashlib.sha256(summary.read_bytes()).hexdigest()},
    }))
    for name in ("eu_curve.csv", "eu_curve.png", "report.md", "quality_report.csv", "points_to_rerun.csv"):
        (fit / name).write_text("synthetic existing fit\n")


@pytest.mark.parametrize("change", ["job_id", "restart_count", "input", "output", "summary", "workflow"])
@pytest.mark.parametrize("collection_entry", ["cli", "controller"])
def test_completed_rerun_requires_recollection(tmp_path, monkeypatch, change, collection_entry):
    case = tmp_path / "case"
    config, state = prepared_case(case)
    for record in state.jobs.values():
        record.status = "CONVERGED"
    state.stage = "converged"
    state.save(case / STATE_FILENAME)
    monkeypatch.setattr(cli, "collect_results", fake_collect)
    monkeypatch.setattr("vaspsolkit.operations.controller.collect_results", fake_collect)

    def collect():
        if collection_entry == "cli":
            assert cli.main(["collect", "--workdir", str(case)], output=lambda _: None) == 0
        else:
            controller = WorkbenchController(case)
            assert controller.execute(controller.plan("collect"), confirmed=True).ok

    collect()
    write_fit(case)
    assert recommend_action(build_snapshot(case)).name == "results-ready"
    state = WorkflowState.load(case / STATE_FILENAME)
    if change == "job_id":
        state.jobs["2"].job_id = "new-job"
        state.save(case / STATE_FILENAME)
    elif change == "restart_count":
        state.jobs["2"].restart_count += 1
        state.save(case / STATE_FILENAME)
    elif change == "output":
        (case / "charge_sweep" / "2" / "OUTCAR").write_text("new output\n")
    elif change == "input":
        (case / "charge_sweep" / "2" / "KPOINTS").write_text("new input\n")
    elif change == "summary":
        (case / "results" / "summary.csv").write_text("she_reference_eV,she_reference_source\n4.7,\n4.7,\n")
    else:
        config.workflow.method_fingerprint = "new-method"
        write_kit_config(case / "vaspsolkit.json", config)
    assert recommend_action(build_snapshot(case)).name == "collect"
    collect()
    assert recommend_action(build_snapshot(case)).name == "results-ready"


def test_untracked_legacy_summary_requires_one_collection(tmp_path):
    case = tmp_path / "case"
    config, state = prepared_case(case)
    for record in state.jobs.values():
        record.status = "CONVERGED"
    state.save(case / STATE_FILENAME)
    fake_collect(case, config.workflow)
    write_fit(case)
    assert recommend_action(build_snapshot(case)).name == "collect"


def test_source_change_during_collection_cannot_be_marked_current(tmp_path, monkeypatch):
    case = tmp_path / "case"
    _, state = prepared_case(case)
    for record in state.jobs.values():
        record.status = "CONVERGED"
    state.save(case / STATE_FILENAME)

    def changing_collect(base, workflow):
        rows = fake_collect(base, workflow)
        (case / "charge_sweep" / "1" / "OUTCAR").write_text("changed during collection\n")
        return rows

    monkeypatch.setattr(cli, "collect_results", changing_collect)
    with pytest.raises(RuntimeError, match="sources changed"):
        cli.main(["collect", "--workdir", str(case)], output=lambda _: None)
    assert recommend_action(build_snapshot(case)).name == "collect"


def test_next_prints_parseable_selected_command_without_authorizing_it(tmp_path):
    case = tmp_path / "case with spaces"
    prepared_case(case)
    messages = []
    assert cli.main(["next", "--workdir", str(case), "--dry-run"], output=messages.append) == 0
    line = next(line for line in messages if "vaspsolkit submit-selected" in line)
    argv = shlex.split(line[line.index("vaspsolkit"):])[1:]
    args = cli._build_parser().parse_args(argv)
    assert args.jobs == ["1", "2", "3"]
    assert args.workdir == str(case)
    assert not args.yes


@pytest.mark.parametrize("collection_entry", ["cli", "controller"])
@pytest.mark.parametrize("changed_file", ["KPOINTS", "OUTCAR"])
def test_external_job_root_collection_tracks_source_changes(tmp_path, monkeypatch, collection_entry, changed_file):
    case = tmp_path / "case"
    config, state = prepared_case(case)
    external = tmp_path / "external jobs"
    (case / "charge_sweep").rename(external)
    config.workflow.job_root = str(external)
    config.validate()
    write_kit_config(case / "vaspsolkit.json", config)
    for name, record in state.jobs.items():
        record.folder = str(external / name)
        record.status = "CONVERGED"
    state.save(case / STATE_FILENAME)
    monkeypatch.setattr(cli, "collect_results", fake_collect)
    monkeypatch.setattr("vaspsolkit.operations.controller.collect_results", fake_collect)

    def collect():
        if collection_entry == "cli":
            assert cli.main(["collect", "--workdir", str(case)], output=lambda _: None) == 0
        else:
            controller = WorkbenchController(case)
            assert controller.execute(controller.plan("collect"), confirmed=True).ok

    fake_collect(case, config.workflow)
    write_fit(case)
    assert recommend_action(build_snapshot(case)).name == "collect"
    collect()
    assert recommend_action(build_snapshot(case)).name == "results-ready"
    (external / "2" / changed_file).write_text("changed external source\n")
    assert recommend_action(build_snapshot(case)).name == "collect"
    collect()
    assert recommend_action(build_snapshot(case)).name == "results-ready"


def test_external_collection_change_after_preview_preserves_summary(tmp_path, monkeypatch):
    case = tmp_path / "case"
    config, state = prepared_case(case)
    external = tmp_path / "external jobs"
    (case / "charge_sweep").rename(external)
    config.workflow.job_root = str(external)
    write_kit_config(case / "vaspsolkit.json", config)
    for name, record in state.jobs.items():
        record.folder, record.status = str(external / name), "CONVERGED"
    state.save(case / STATE_FILENAME)
    fake_collect(case, config.workflow)
    summary = case / "results" / "summary.csv"
    summary.write_text("legacy summary must survive\n")
    monkeypatch.setattr("vaspsolkit.operations.controller.collect_results", fake_collect)
    controller = WorkbenchController(case)
    plan = controller.plan("collect")
    (external / "2" / "OUTCAR").write_text("changed after preview\n")
    with pytest.raises(RuntimeError, match="sources changed"):
        controller.execute(plan, confirmed=True)
    assert summary.read_text() == "legacy summary must survive\n"


@pytest.mark.parametrize("command", ["submit", "submit-selected"])
def test_cli_external_config_is_read_only_and_receipt_guarded(tmp_path, monkeypatch, command):
    case = tmp_path / "case"
    prepared_case(case, count=2)
    external = tmp_path / "external config.json"
    (case / "vaspsolkit.json").rename(external)
    before = external.read_bytes()
    scheduler = FakeSlurm(fail_at=2)
    original_run = scheduler.run

    def guarded_run(argv, cwd):
        if argv[0] == "sbatch":
            receipt = read_submission_receipt(case)
            assert receipt.status == "SUBMITTING"
            assert receipt.resources["target_job"] == Path(cwd).name
        return original_run(argv, cwd)

    scheduler.runner = guarded_run
    monkeypatch.setattr(cli, "scheduler_from_config", lambda _: scheduler)
    args = [command, "--workdir", str(case), "--config", str(external), "--yes"]
    if command == "submit-selected":
        args.extend(["1", "2"])
    with pytest.raises(RuntimeError):
        cli.main(args, output=lambda _: None)
    assert scheduler.calls == ["1", "2"]
    assert WorkflowState.load(case / STATE_FILENAME).jobs["1"].job_id == "9001"
    assert read_submission_receipt(case).resources["target_job"] == "2"
    with pytest.raises(RuntimeError):
        cli.main(args, output=lambda _: None)
    assert scheduler.calls == ["1", "2"]
    assert external.read_bytes() == before
    assert not (case / "vaspsolkit.json").exists()


@pytest.mark.parametrize("command", ["submit", "submit-selected"])
def test_external_config_edit_during_cli_confirmation_blocks_submit(tmp_path, monkeypatch, command):
    case = tmp_path / "case"
    config, _ = prepared_case(case, count=1)
    external = tmp_path / "external.json"
    (case / "vaspsolkit.json").rename(external)
    scheduler = FakeSlurm()
    monkeypatch.setattr(cli, "scheduler_from_config", lambda _: scheduler)

    def confirm(_):
        config.scheduler.partition = "edited-during-confirmation"
        write_kit_config(external, config)
        return "yes"

    args = [command, "--workdir", str(case), "--config", str(external)]
    if command == "submit-selected":
        args.append("1")
    with pytest.raises(RuntimeError):
        cli.main(args, input_fn=confirm, output=lambda _: None)
    assert scheduler.calls == []
    assert read_submission_receipt(case) is None


def test_external_config_cannot_persist_resource_overrides(tmp_path):
    case = tmp_path / "case"
    config, _ = prepared_case(case, count=1)
    external = tmp_path / "external.json"
    (case / "vaspsolkit.json").rename(external)
    before = external.read_bytes()
    controller = WorkbenchController(case, config_path=external)
    with pytest.raises(ValueError, match="within the Case"):
        controller.plan("submit-selected", replace(resources_from_config(config), persist=True), selected=("1",))
    assert external.read_bytes() == before
    assert read_submission_receipt(case) is None


@pytest.mark.parametrize("command", ["submit", "submit-selected"])
def test_cli_prepared_children_do_not_require_root_script(tmp_path, monkeypatch, command):
    case = tmp_path / "case"
    prepared_case(case, count=2)
    (case / "vasp.slurm").unlink()
    scheduler = FakeSlurm()
    monkeypatch.setattr(cli, "scheduler_from_config", lambda _: scheduler)
    args = [command, "--workdir", str(case), "--yes"]
    if command == "submit-selected":
        args.extend(["1", "2"])
    assert cli.main(args, output=lambda _: None) == 0
    assert scheduler.calls == ["1", "2"]
    assert all(record.status == "SUBMITTED" for record in WorkflowState.load(case / STATE_FILENAME).jobs.values())
    assert read_submission_receipt(case) is None


@pytest.mark.parametrize("unsafe", ["missing", "symlink"])
def test_selected_child_script_must_be_safe_before_any_submission(tmp_path, monkeypatch, unsafe):
    case = tmp_path / "case"
    prepared_case(case, count=2)
    script = case / "charge_sweep" / "2" / "vasp.slurm"
    script.unlink()
    if unsafe == "symlink":
        external = tmp_path / "outside.slurm"
        external.write_text("external script\n")
        script.symlink_to(external)
    scheduler = FakeSlurm()
    monkeypatch.setattr(cli, "scheduler_from_config", lambda _: scheduler)
    with pytest.raises(ValueError):
        cli.main(["submit", "--workdir", str(case), "--yes"], output=lambda _: None)
    assert scheduler.calls == []
    assert read_submission_receipt(case) is None


def test_neutral_submission_still_requires_root_script(tmp_path):
    case = tmp_path / "case"
    config, state = prepared_case(case, count=1)
    (case / "vasp.slurm").unlink()
    state.neutral.status, state.neutral.job_id = "PREPARED", ""
    state.save(case / STATE_FILENAME)
    controller = WorkbenchController(case)
    with pytest.raises(ValueError, match="script"):
        controller.plan("submit-neutral", resources_from_config(config))
