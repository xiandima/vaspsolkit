from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import stat
from dataclasses import asdict

import pytest


class FakeScheduler:
    def __init__(self, *, submit_result: str = "128042", submit_error: str = "") -> None:
        self.submit_result = submit_result
        self.submit_error = submit_error
        self.submit_calls = []
        self.status_calls = []

    def submit(
        self,
        workdir,
        script,
        dry_run=False,
        job_name=None,
        partition=None,
        nodes=(),
        node_count=None,
        tasks=None,
        tasks_per_node=None,
        walltime=None,
    ):
        self.submit_calls.append(
            {
                "workdir": Path(workdir),
                "script": script,
                "dry_run": dry_run,
                "job_name": job_name,
                "partition": partition,
                "nodes": tuple(nodes),
                "node_count": node_count,
                "tasks": tasks,
                "tasks_per_node": tasks_per_node,
                "walltime": walltime,
            }
        )
        if self.submit_error:
            raise RuntimeError(self.submit_error)
        return self.submit_result

    def status(self, job_id):
        self.status_calls.append(job_id)
        raise AssertionError("neutral submission must return without polling")


def _write_case(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "POSCAR").write_text(
        "PtO\n1\n1 0 0\n0 1 0\n0 0 1\nPt O\n1 1\nDirect\n0 0 0\n0 0 0\n",
        encoding="utf-8",
    )
    (root / "INCAR").write_text("ENCUT = 520\nIBRION = 2\nNSW = 80\n", encoding="utf-8")
    (root / "KPOINTS").write_text("Gamma\n0\nGamma\n1 1 1\n0 0 0\n", encoding="utf-8")
    (root / "POTCAR").write_text(
        "TITEL = PAW_PBE Pt 01Jan2000\nENMAX = 300 eV\n"
        "TITEL = PAW_PBE O 01Jan2000\nENMAX = 400 eV\n",
        encoding="utf-8",
    )
    (root / "vasp.slurm").write_text("#!/bin/sh\n", encoding="utf-8")


def _resources(*, persist=False, node="node24", tasks=48, partition="normal"):
    from vaspsolkit.operations.actions import ResourceRequest

    return ResourceRequest.create(
        allocation="specified" if node else "auto",
        nodes=(node,) if node else (),
        tasks=tasks,
        partition=partition,
        walltime="48:00:00",
        script="vasp.slurm",
        persist=persist,
    )


def _prepared_controller(root: Path, fake: FakeScheduler):
    from vaspsolkit.operations.controller import WorkbenchController

    _write_case(root)
    controller = WorkbenchController(
        root,
        scheduler_factory=lambda _: fake,
        activity_state_root=root.parent / f".{root.name}-activity",
    )
    controller.execute(controller.plan("init", _resources(node=None)), confirmed=True)
    controller.execute(controller.plan("prepare-neutral"), confirmed=True)
    return controller


def _receipt(root: Path, status: str, *, job_id: str = ""):
    from vaspsolkit.state import WorkflowState
    from vaspsolkit.operations.activity import SubmissionReceipt

    info = root.stat()
    state = WorkflowState.load(root / "vaspsolkit.state.json")
    neutral = state.neutral
    return SubmissionReceipt(
        case_path=str(root.resolve()), case_device=info.st_dev, case_inode=info.st_ino,
        case_mode=stat.S_IFMT(info.st_mode), job_id=job_id, command="sbatch",
        resources={"partition": "", "nodes": [], "tasks": 48}, timestamp="2026-07-25T00:00:00+08:00",
        state_before={
            "stage": state.stage, "jobs": {},
            "neutral": asdict(neutral) if neutral else None,
            "prepared_checked": state.prepared_checked,
        }, owner_token="test-owner", status=status,
    )


def test_submit_neutral_uses_previewed_resources_and_returns_immediately(tmp_path: Path) -> None:
    from vaspsolkit.state import WorkflowState

    fake = FakeScheduler()
    controller = _prepared_controller(tmp_path, fake)
    plan = controller.plan("submit-neutral", _resources())

    result = controller.execute(plan, confirmed=True)

    assert result.ok is True
    assert result.job_ids == {"neutral": "128042"}
    assert fake.submit_calls == [{
        "workdir": tmp_path,
        "script": "vasp.slurm",
        "dry_run": False,
        "job_name": None,
        "partition": "normal",
        "nodes": ("node24",),
        "node_count": 1,
        "tasks": 48,
        "tasks_per_node": 96,
        "walltime": "48:00:00",
    }]
    assert fake.status_calls == []
    state = WorkflowState.load(tmp_path / "vaspsolkit.state.json")
    assert state.neutral is not None
    assert state.neutral.status == "SUBMITTED"
    assert state.neutral.job_id == "128042"


@pytest.mark.parametrize("partition", ["compute", "workq"])
def test_submit_neutral_preserves_reviewed_partition_exactly(
    tmp_path: Path, partition: str
) -> None:
    fake = FakeScheduler()
    controller = _prepared_controller(tmp_path, fake)

    result = controller.execute(
        controller.plan("submit-neutral", _resources(node=None, partition=partition)),
        confirmed=True,
    )

    assert result.ok
    assert fake.submit_calls[0]["partition"] == partition


def test_controller_rejects_multiple_nodes_before_preview_or_sbatch(tmp_path: Path) -> None:
    from vaspsolkit.operations.actions import ResourceRequest

    fake = FakeScheduler()
    controller = _prepared_controller(tmp_path, fake)
    with pytest.raises(ValueError, match="node_count"):
        resources = ResourceRequest.create(
            allocation="specified", nodes=("node24", "node25"), tasks=48,
            partition="compute", walltime="48:00:00", script="vasp.slurm",
        )
        controller.plan("submit-neutral", resources)
    assert fake.submit_calls == []


@pytest.mark.parametrize(
    ("receipt_status", "job_id"),
    [("SUBMITTING", ""), ("ACCEPTED", "128777")],
)
@pytest.mark.parametrize("command", ["submit-neutral", "run"])
def test_cli_submit_neutral_fails_closed_on_existing_submission_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receipt_status: str, job_id: str,
    command: str,
) -> None:
    from vaspsolkit.cli import main
    from vaspsolkit.operations import activity
    from vaspsolkit.operations.activity import claim_submission_receipt

    fake = FakeScheduler()
    _prepared_controller(tmp_path, fake)
    state_root = tmp_path.parent / f".{tmp_path.name}-cli-state"
    monkeypatch.setattr(activity, "DEFAULT_STATE_ROOT", state_root)
    claim_submission_receipt(tmp_path, _receipt(tmp_path, receipt_status, job_id=job_id), state_root)
    monkeypatch.setattr("vaspsolkit.cli.scheduler_from_config", lambda _: fake)

    with pytest.raises(RuntimeError, match="workbench|reconcile|修复|屏障|禁止"):
        main([command, "--workdir", str(tmp_path), "--yes"])

    assert fake.submit_calls == []


@pytest.mark.parametrize("command", ["submit-neutral", "run"])
def test_cli_submit_neutral_fails_closed_on_malformed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    from vaspsolkit.cli import main
    from vaspsolkit.operations import activity
    from vaspsolkit.operations.activity import claim_submission_receipt, submission_receipt_path

    fake = FakeScheduler()
    _prepared_controller(tmp_path, fake)
    state_root = tmp_path.parent / f".{tmp_path.name}-cli-state"
    monkeypatch.setattr(activity, "DEFAULT_STATE_ROOT", state_root)
    claim_submission_receipt(tmp_path, _receipt(tmp_path, "SUBMITTING"), state_root)
    submission_receipt_path(tmp_path, state_root).write_text("{broken", encoding="utf-8")
    monkeypatch.setattr("vaspsolkit.cli.scheduler_from_config", lambda _: fake)

    with pytest.raises(RuntimeError, match="workbench|reconcile|修复|屏障|不可读|禁止"):
        main([command, "--workdir", str(tmp_path), "--yes"])

    assert fake.submit_calls == []


def test_cli_and_controller_compete_for_one_durable_submission_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vaspsolkit.cli import main
    from vaspsolkit.operations import activity
    from vaspsolkit.operations.controller import WorkbenchController

    fake = FakeScheduler(submit_result="128778")
    prepared = _prepared_controller(tmp_path, fake)
    state_root = tmp_path.parent / f".{tmp_path.name}-shared-state"
    monkeypatch.setattr(activity, "DEFAULT_STATE_ROOT", state_root)
    controller = WorkbenchController(
        tmp_path, scheduler_factory=lambda _: fake, activity_state_root=state_root,
    )
    plan = controller.plan("submit-neutral", _resources(node=None, partition="compute"))
    monkeypatch.setattr("vaspsolkit.cli.scheduler_from_config", lambda _: fake)
    start = threading.Barrier(2)

    def controller_submit():
        start.wait()
        return controller.execute(plan, confirmed=True)

    def cli_submit():
        start.wait()
        try:
            return main(["submit-neutral", "--workdir", str(tmp_path), "--yes"])
        except RuntimeError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(lambda fn: fn(), (controller_submit, cli_submit)))

    assert len(fake.submit_calls) <= 1
    assert any(getattr(outcome, "ok", False) or outcome == 0 for outcome in outcomes)


def test_run_submit_neutral_and_controller_share_one_submission_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vaspsolkit.cli import main
    from vaspsolkit.operations import activity
    from vaspsolkit.operations.controller import WorkbenchController

    fake = FakeScheduler(submit_result="128779")
    _prepared_controller(tmp_path, fake)
    state_root = tmp_path.parent / f".{tmp_path.name}-shared-state"
    monkeypatch.setattr(activity, "DEFAULT_STATE_ROOT", state_root)
    controller = WorkbenchController(
        tmp_path, scheduler_factory=lambda _: fake, activity_state_root=state_root,
    )
    plan = controller.plan("submit-neutral", _resources(node=None, partition="compute"))
    monkeypatch.setattr("vaspsolkit.cli.scheduler_from_config", lambda _: fake)
    start = threading.Barrier(3)

    def invoke(command: str):
        start.wait()
        try:
            return main([command, "--workdir", str(tmp_path), "--yes"], output=lambda _: None)
        except RuntimeError as exc:
            return exc

    def controller_submit():
        start.wait()
        try:
            return controller.execute(plan, confirmed=True)
        except RuntimeError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = (
            pool.submit(invoke, "run"),
            pool.submit(invoke, "submit-neutral"),
            pool.submit(controller_submit),
        )
        outcomes = tuple(future.result() for future in futures)

    assert len(fake.submit_calls) <= 1
    assert any(getattr(outcome, "ok", False) or outcome == 0 for outcome in outcomes)


def test_run_dry_run_keeps_prepared_state_and_creates_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vaspsolkit.cli import main
    from vaspsolkit.state import WorkflowState
    from vaspsolkit.operations import activity
    from vaspsolkit.operations.activity import read_submission_receipt

    fake = FakeScheduler(submit_result="DRY-RUN:case")
    _prepared_controller(tmp_path, FakeScheduler())
    state_root = tmp_path.parent / f".{tmp_path.name}-dry-state"
    monkeypatch.setattr(activity, "DEFAULT_STATE_ROOT", state_root)
    monkeypatch.setattr("vaspsolkit.cli.scheduler_from_config", lambda _: fake)
    output = []

    result = main(
        ["run", "--workdir", str(tmp_path), "--dry-run"], output=output.append
    )

    assert result == 0
    assert fake.submit_calls[0]["dry_run"] is True
    assert read_submission_receipt(tmp_path, state_root) is None
    state = WorkflowState.load(tmp_path / "vaspsolkit.state.json")
    assert state.neutral is not None and state.neutral.status == "PREPARED"
    assert output == ["neutral: DRY-RUN"]


def test_run_with_recorded_job_returns_without_scheduler_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vaspsolkit.cli import main
    from vaspsolkit.state import WorkflowState

    fake = FakeScheduler()
    _prepared_controller(tmp_path, FakeScheduler())
    state = WorkflowState.load(tmp_path / "vaspsolkit.state.json")
    assert state.neutral is not None
    state.neutral.status = "SUBMITTED"
    state.neutral.job_id = "128780"
    state.save(tmp_path / "vaspsolkit.state.json")
    monkeypatch.setattr("vaspsolkit.cli.scheduler_from_config", lambda _: fake)
    output = []

    result = main(["run", "--workdir", str(tmp_path), "--yes"], output=output.append)

    assert result == 0
    assert fake.submit_calls == []
    assert fake.status_calls == []
    assert "use monitor or check-neutral" in output[0]


@pytest.mark.parametrize(
    ("submit_result", "submit_error"),
    [("", ""), ("not-a-job-id", ""), ("128000", "sbatch transport failed")],
)
def test_cli_sbatch_error_or_unparseable_output_keeps_barrier_and_blocks_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    submit_result: str, submit_error: str,
) -> None:
    from vaspsolkit.cli import main
    from vaspsolkit.operations import activity
    from vaspsolkit.operations.activity import read_submission_receipt

    fake = FakeScheduler(submit_result=submit_result, submit_error=submit_error)
    _prepared_controller(tmp_path, fake)
    state_root = tmp_path.parent / f".{tmp_path.name}-cli-state"
    monkeypatch.setattr(activity, "DEFAULT_STATE_ROOT", state_root)
    monkeypatch.setattr("vaspsolkit.cli.scheduler_from_config", lambda _: fake)

    with pytest.raises(RuntimeError, match="屏障|不要再次|禁止"):
        main(["submit-neutral", "--workdir", str(tmp_path), "--yes"])
    assert len(fake.submit_calls) == 1
    assert read_submission_receipt(tmp_path, state_root) is not None

    with pytest.raises(RuntimeError, match="屏障|不要再次|禁止"):
        main(["submit-neutral", "--workdir", str(tmp_path), "--yes"])
    assert len(fake.submit_calls) == 1


def test_cli_interruption_after_sbatch_invocation_keeps_submitting_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vaspsolkit.cli import main
    from vaspsolkit.operations import activity
    from vaspsolkit.operations.activity import read_submission_receipt

    class InterruptingScheduler(FakeScheduler):
        def submit(self, *args, **kwargs):
            self.submit_calls.append({"interrupted": True})
            raise KeyboardInterrupt("simulated process interruption")

    fake = InterruptingScheduler()
    _prepared_controller(tmp_path, FakeScheduler())
    state_root = tmp_path.parent / f".{tmp_path.name}-cli-state"
    monkeypatch.setattr(activity, "DEFAULT_STATE_ROOT", state_root)
    monkeypatch.setattr("vaspsolkit.cli.scheduler_from_config", lambda _: fake)

    with pytest.raises(KeyboardInterrupt):
        main(["submit-neutral", "--workdir", str(tmp_path), "--yes"])

    receipt = read_submission_receipt(tmp_path, state_root)
    assert receipt is not None and receipt.status == "SUBMITTING"
    assert len(fake.submit_calls) == 1


@pytest.mark.parametrize(
    ("submit_result", "submit_error", "raw"),
    [
        ("128042", "sbatch failed: Unauthorized Request", "Unauthorized Request"),
        ("not-a-slurm-job", "", "not-a-slurm-job"),
        ("", "", ""),
    ],
)
def test_submit_failure_keeps_prepared_and_returns_structured_error(
    tmp_path: Path, submit_result: str, submit_error: str, raw: str
) -> None:
    from vaspsolkit.state import WorkflowState

    fake = FakeScheduler(submit_result=submit_result, submit_error=submit_error)
    controller = _prepared_controller(tmp_path, fake)
    plan = controller.plan("submit-neutral", _resources())

    result = controller.execute(plan, confirmed=True)

    assert result.ok is False
    assert result.status == "recovery-required"
    assert result.error is not None
    assert result.error.step == "reconcile-neutral-submit"
    assert result.error.command == "sbatch"
    assert raw in result.error.raw
    assert result.error.suggestion_zh
    assert result.error.suggestion_en
    state = WorkflowState.load(tmp_path / "vaspsolkit.state.json")
    assert state.neutral is not None and state.neutral.status == "PREPARED"
    assert not state.neutral.job_id
    assert controller.snapshot().neutral.status == "SUBMIT_UNKNOWN"
    if not submit_error:
        from vaspsolkit.operations.activity import read_submission_receipt
        receipt = read_submission_receipt(tmp_path, controller.activity_state_root)
        assert receipt is not None and receipt.raw_output == raw


def test_submit_revalidates_inputs_and_case_before_calling_scheduler(tmp_path: Path) -> None:
    fake = FakeScheduler()
    controller = _prepared_controller(tmp_path, fake)
    plan = controller.plan("submit-neutral", _resources())
    (tmp_path / "INCAR").write_text("changed after preview\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="已变化|重新预览"):
        controller.execute(plan, confirmed=True)

    assert fake.submit_calls == []


@pytest.mark.parametrize("status", ["SUBMITTED", "QUEUED", "RUNNING", "UNKNOWN"])
def test_submit_plan_protects_active_neutral_states(tmp_path: Path, status: str) -> None:
    from vaspsolkit.state import WorkflowState

    fake = FakeScheduler()
    controller = _prepared_controller(tmp_path, fake)
    state = WorkflowState.load(tmp_path / "vaspsolkit.state.json")
    assert state.neutral is not None
    state.neutral.status = status
    state.neutral.job_id = "128000"
    state.save(tmp_path / "vaspsolkit.state.json")

    plan = controller.plan("submit-neutral", _resources())
    assert plan.blocked_reason
    with pytest.raises(RuntimeError):
        controller.execute(plan, confirmed=True)
    assert fake.submit_calls == []


def test_submit_preview_is_single_use_even_after_failure(tmp_path: Path) -> None:
    fake = FakeScheduler(submit_error="sbatch failed")
    controller = _prepared_controller(tmp_path, fake)
    plan = controller.plan("submit-neutral", _resources())
    result = controller.execute(plan, confirmed=True)
    assert result.ok is False

    with pytest.raises(RuntimeError, match="失效"):
        controller.execute(plan, confirmed=True)
    assert len(fake.submit_calls) == 1


@pytest.mark.parametrize("persist", [False, True])
def test_submit_resource_scope_is_explicit(tmp_path: Path, persist: bool) -> None:
    fake = FakeScheduler()
    controller = _prepared_controller(tmp_path, fake)
    config_path = tmp_path / "vaspsolkit.json"
    before = config_path.read_bytes()
    request = _resources(persist=persist, node="node31", tasks=32, partition="workq")
    plan = controller.plan("submit-neutral", request)

    assert bool(plan.file_diffs) is persist
    result = controller.execute(plan, confirmed=True)
    assert result.ok
    after = config_path.read_bytes()
    if persist:
        data = json.loads(after)
        assert data["scheduler"]["nodes"] == ["node31"]
        assert data["scheduler"]["tasks"] == 32
        assert data["scheduler"]["partition"] == "workq"
    else:
        assert after == before
    assert fake.submit_calls[0]["nodes"] == ("node31",)
    assert fake.submit_calls[0]["tasks"] == 32
    assert fake.submit_calls[0]["partition"] == "workq"


def test_attempted_sbatch_failure_blocks_fresh_retry(tmp_path: Path) -> None:
    fake = FakeScheduler(submit_error="sbatch failed: transient")
    controller = _prepared_controller(tmp_path, fake)
    first = controller.execute(
        controller.plan("submit-neutral", _resources()), confirmed=True
    )
    assert not first.ok

    assert controller.plan("submit-neutral", _resources()).blocked_reason
    assert len(fake.submit_calls) == 1


def test_persisted_resources_remain_reviewed_defaults_when_sbatch_fails(tmp_path: Path) -> None:
    from vaspsolkit.state import WorkflowState

    fake = FakeScheduler(submit_error="sbatch failed: queue disabled")
    controller = _prepared_controller(tmp_path, fake)
    plan = controller.plan(
        "submit-neutral",
        _resources(persist=True, node="node31", tasks=32, partition="workq"),
    )

    result = controller.execute(plan, confirmed=True)

    assert not result.ok
    config = json.loads((tmp_path / "vaspsolkit.json").read_text(encoding="utf-8"))
    assert config["scheduler"]["nodes"] == ["node31"]
    assert config["scheduler"]["tasks"] == 32
    assert config["scheduler"]["partition"] == "workq"
    state = WorkflowState.load(tmp_path / "vaspsolkit.state.json")
    assert state.neutral is not None and state.neutral.status == "PREPARED"


def test_post_submit_state_failure_creates_durable_barrier_and_repairs_without_sbatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vaspsolkit.state import WorkflowState
    from vaspsolkit.operations.controller import WorkbenchController

    fake = FakeScheduler(submit_result="128099")
    receipt_root = tmp_path.parent / f".{tmp_path.name}-user-state"
    controller = _prepared_controller(tmp_path, fake)
    controller.activity_state_root = receipt_root
    original_save = WorkflowState.save
    fail_submitted_save = True

    def fail_once_after_submit(self, path):
        nonlocal fail_submitted_save
        if (
            fail_submitted_save
            and self.neutral is not None
            and self.neutral.status == "SUBMITTED"
        ):
            fail_submitted_save = False
            raise OSError("disk full while saving submitted state")
        return original_save(self, path)

    monkeypatch.setattr(WorkflowState, "save", fail_once_after_submit)
    result = controller.execute(
        controller.plan("submit-neutral", _resources(node="node33", tasks=24)),
        confirmed=True,
    )

    assert not result.ok
    assert result.error is not None
    assert result.error.step == "recover-neutral-state"
    assert "128099" in result.error.raw
    assert "sbatch" not in result.error.suggestion.lower()
    assert len(fake.submit_calls) == 1
    assert controller.snapshot().neutral.status == "SUBMIT_UNKNOWN"

    # Even a partially written state file is recovered from the durable receipt.
    (tmp_path / "vaspsolkit.state.json").write_text("{truncated", encoding="utf-8")

    # A fresh process/controller sees the durable receipt and cannot submit again.
    fresh = WorkbenchController(
        tmp_path,
        scheduler_factory=lambda _: fake,
        activity_state_root=receipt_root,
    )
    blocked = fresh.plan("submit-neutral", _resources())
    assert blocked.blocked_reason
    assert "128099" in blocked.blocked_reason

    repair = fresh.plan("repair-neutral-submit")
    assert repair.effect == "file-changing"
    repaired = fresh.execute(repair, confirmed=True)
    assert repaired.ok
    assert len(fake.submit_calls) == 1
    state = WorkflowState.load(tmp_path / "vaspsolkit.state.json")
    assert state.neutral is not None
    assert state.neutral.status == "SUBMITTED"
    assert state.neutral.job_id == "128099"

    # Receipt was cleared, so another fresh controller sees normal submitted protection.
    newest = WorkbenchController(
        tmp_path,
        scheduler_factory=lambda _: fake,
        activity_state_root=receipt_root,
    )
    assert newest.plan("submit-neutral", _resources()).blocked_reason


def test_write_ahead_intent_failure_never_calls_sbatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vaspsolkit.operations.controller as controller_module

    fake = FakeScheduler()
    controller = _prepared_controller(tmp_path, fake)
    monkeypatch.setattr(
        controller_module,
        "claim_submission_receipt",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("intent disk full")),
    )

    result = controller.execute(
        controller.plan("submit-neutral", _resources()), confirmed=True
    )

    assert not result.ok
    assert fake.submit_calls == []
    assert result.error is not None
    assert "intent" in result.error.raw.lower()




def test_sbatch_exception_after_attempt_keeps_intent_and_blocks_fresh_review(tmp_path: Path) -> None:
    from vaspsolkit.operations.activity import read_submission_receipt
    from vaspsolkit.operations.controller import WorkbenchController

    fake = FakeScheduler(submit_error="sbatch failed before acceptance")
    receipt_root = tmp_path.parent / f".{tmp_path.name}-user-state"
    controller = _prepared_controller(tmp_path, fake)
    controller.activity_state_root = receipt_root
    result = controller.execute(
        controller.plan("submit-neutral", _resources()), confirmed=True
    )

    assert not result.ok
    assert read_submission_receipt(tmp_path, receipt_root) is not None
    fresh = WorkbenchController(
        tmp_path, scheduler_factory=lambda _: fake, activity_state_root=receipt_root
    )
    assert fresh.plan("submit-neutral", _resources()).blocked_reason


def test_sbatch_failure_with_intent_cleanup_failure_remains_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vaspsolkit.operations.controller as controller_module
    from vaspsolkit.operations.activity import read_submission_receipt

    fake = FakeScheduler(submit_error="sbatch rejected")
    receipt_root = tmp_path.parent / f".{tmp_path.name}-user-state"
    controller = _prepared_controller(tmp_path, fake)
    controller.activity_state_root = receipt_root
    monkeypatch.setattr(
        controller_module,
        "clear_submission_receipt",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("cleanup denied")),
    )
    result = controller.execute(
        controller.plan("submit-neutral", _resources()), confirmed=True
    )

    assert not result.ok
    assert result.error is not None
    assert result.error.step == "reconcile-neutral-submit"
    assert len(fake.submit_calls) == 1
    receipt = read_submission_receipt(tmp_path, receipt_root)
    assert receipt is not None and receipt.status == "SUBMITTING"
    fresh = controller_module.WorkbenchController(
        tmp_path, scheduler_factory=lambda _: fake, activity_state_root=receipt_root
    )
    assert fresh.plan("submit-neutral", _resources()).blocked_reason


def test_two_controllers_with_prebuilt_plans_submit_exactly_once(tmp_path: Path) -> None:
    from vaspsolkit.operations.controller import WorkbenchController

    fake = FakeScheduler(submit_result="128120")
    receipt_root = tmp_path.parent / f".{tmp_path.name}-user-state"
    first = _prepared_controller(tmp_path, fake)
    first.activity_state_root = receipt_root
    second = WorkbenchController(
        tmp_path, scheduler_factory=lambda _: fake, activity_state_root=receipt_root
    )
    first_plan = first.plan("submit-neutral", _resources())
    second_plan = second.plan("submit-neutral", _resources())
    start = threading.Barrier(2)

    def execute(controller, plan):
        start.wait()
        try:
            return controller.execute(plan, confirmed=True)
        except RuntimeError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: execute(*pair), ((first, first_plan), (second, second_plan))))

    assert len(fake.submit_calls) == 1
    assert sum(getattr(result, "ok", False) for result in results) == 1


def test_stale_manual_no_job_cannot_overwrite_or_clear_accepted_receipt(tmp_path: Path) -> None:
    from dataclasses import replace
    from vaspsolkit.operations.activity import (
        SubmissionReceipt,
        claim_submission_receipt,
        clear_submission_receipt,
        new_submission_owner_token,
        read_submission_receipt,
        submission_receipt_path,
        update_submission_receipt,
    )

    case = tmp_path / "case"
    case.mkdir()
    root = tmp_path / "state"
    identity = case.stat()
    owner = new_submission_owner_token()
    submitting = SubmissionReceipt(
        case_path=str(case.resolve()), case_device=identity.st_dev,
        case_inode=identity.st_ino, job_id="", command="sbatch",
        resources={"script": "vasp.slurm"}, timestamp="2026-07-24T00:00:00+08:00",
        owner_token=owner, status="SUBMITTING", version=0,
    )
    claim_submission_receipt(case, submitting, root)
    stale_manual = read_submission_receipt(case, root)
    assert stale_manual == submitting

    accepted = replace(submitting, status="ACCEPTED", job_id="128140", version=1)
    update_submission_receipt(
        case, accepted, owner, root,
        expected_version=0, expected_status="SUBMITTING",
    )
    stale_failed = replace(stale_manual, status="FAILED", version=1)
    with pytest.raises(RuntimeError, match="stale"):
        update_submission_receipt(
            case, stale_failed, owner, root,
            expected_version=0, expected_status="SUBMITTING",
        )
    with pytest.raises(RuntimeError, match="stale"):
        clear_submission_receipt(
            case, root, owner,
            expected_version=0, expected_status="SUBMITTING",
        )

    assert read_submission_receipt(case, root) == accepted
    lock_path = submission_receipt_path(case, root).with_name(
        submission_receipt_path(case, root).name + ".lock"
    )
    assert lock_path.is_file()


def test_manual_reconcile_records_job_id_without_sbatch(tmp_path: Path) -> None:
    from vaspsolkit.state import WorkflowState
    from vaspsolkit.operations.activity import read_activities, read_submission_receipt

    fake = FakeScheduler(submit_result="malformed sbatch output")
    controller = _prepared_controller(tmp_path, fake)
    result = controller.execute(
        controller.plan("submit-neutral", _resources()), confirmed=True
    )
    assert not result.ok and len(fake.submit_calls) == 1
    with pytest.raises(ValueError, match="Job ID"):
        controller.plan_reconcile_job_id("not a job")

    plan = controller.plan_reconcile_job_id("128121")
    reconciled = controller.execute(plan, confirmed=True)

    assert reconciled.ok
    assert len(fake.submit_calls) == 1
    state = WorkflowState.load(tmp_path / "vaspsolkit.state.json")
    assert state.neutral is not None and state.neutral.job_id == "128121"
    assert read_submission_receipt(tmp_path, controller.activity_state_root) is None
    assert read_activities(tmp_path, controller.activity_state_root)[0].action == "record-job-id"


def test_confirm_no_slurm_job_requires_exact_second_confirmation(tmp_path: Path) -> None:
    from vaspsolkit.operations.activity import read_activities, read_submission_receipt

    fake = FakeScheduler(submit_result="malformed sbatch output")
    controller = _prepared_controller(tmp_path, fake)
    controller.execute(controller.plan("submit-neutral", _resources()), confirmed=True)
    with pytest.raises(ValueError, match="SLURM未创建任务"):
        controller.plan_confirm_no_job("yes")

    plan = controller.plan_confirm_no_job("SLURM未创建任务")
    assert plan.warnings
    cleared = controller.execute(plan, confirmed=True)

    assert cleared.ok
    assert len(fake.submit_calls) == 1
    assert read_submission_receipt(tmp_path, controller.activity_state_root) is None
    assert read_activities(tmp_path, controller.activity_state_root)[0].action == "confirm-no-job"






def test_task10_controller_never_writes_default_home_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vaspsolkit.operations.activity as activity_module

    fake_home_state = tmp_path / "fake-home" / "cases"
    monkeypatch.setattr(activity_module, "DEFAULT_STATE_ROOT", fake_home_state)
    case = tmp_path / "case"
    fake = FakeScheduler()
    controller = _prepared_controller(case, fake)
    result = controller.execute(
        controller.plan("submit-neutral", _resources()), confirmed=True
    )
    assert result.ok
    assert not fake_home_state.exists()


@pytest.mark.parametrize("failure_point", ["replace", "file-fsync"])
def test_atomic_state_save_failure_preserves_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    import vaspsolkit.state as state_module
    from vaspsolkit.state import JobRecord, WorkflowState

    path = tmp_path / "vaspsolkit.state.json"
    WorkflowState(neutral=JobRecord(folder=".", status="PREPARED")).save(path)
    before = path.read_bytes()
    updated = WorkflowState(neutral=JobRecord(folder=".", status="SUBMITTED", job_id="128150"))
    if failure_point == "replace":
        monkeypatch.setattr(state_module, "_replace_state", lambda *args: (_ for _ in ()).throw(OSError("replace failed")))
    else:
        original_fsync = state_module._fsync_state
        calls = 0
        def fail_first(fd):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("fsync failed")
            return original_fsync(fd)
        monkeypatch.setattr(state_module, "_fsync_state", fail_first)
    with pytest.raises(OSError):
        updated.save(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("fault", ["replace-error", "replace-noop", "fsync-error"])
def test_state_durability_fault_after_acceptance_keeps_accepted_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    import vaspsolkit.state as state_module
    from vaspsolkit.operations.activity import read_submission_receipt

    fake = FakeScheduler(submit_result="128152")
    controller = _prepared_controller(tmp_path, fake)
    if fault == "replace-error":
        monkeypatch.setattr(state_module, "_replace_state", lambda *args: (_ for _ in ()).throw(OSError("replace failed")))
    elif fault == "replace-noop":
        monkeypatch.setattr(state_module, "_replace_state", lambda *args: None)
    else:
        original = state_module._fsync_state
        calls = 0
        def fail_first(fd):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("state fsync failed")
            return original(fd)
        monkeypatch.setattr(state_module, "_fsync_state", fail_first)

    result = controller.execute(
        controller.plan("submit-neutral", _resources()), confirmed=True
    )
    assert not result.ok
    assert len(fake.submit_calls) == 1
    receipt = read_submission_receipt(tmp_path, controller.activity_state_root)
    assert receipt is not None and receipt.status == "ACCEPTED"
    assert receipt.job_id == "128152"


def test_submission_lock_symlink_prevents_sbatch(tmp_path: Path) -> None:
    from vaspsolkit.operations.activity import submission_receipt_path

    fake = FakeScheduler()
    controller = _prepared_controller(tmp_path, fake)
    receipt = submission_receipt_path(tmp_path, controller.activity_state_root)
    receipt.parent.mkdir(parents=True, mode=0o700)
    outside = tmp_path.parent / f".{tmp_path.name}-outside-lock"
    outside.write_text("do not touch", encoding="utf-8")
    receipt.with_name(receipt.name + ".lock").symlink_to(outside)
    result = controller.execute(controller.plan("submit-neutral", _resources()), confirmed=True)
    assert not result.ok
    assert fake.submit_calls == []
    assert outside.read_text(encoding="utf-8") == "do not touch"


def test_reconcile_rejects_state_symlink(tmp_path: Path) -> None:
    fake = FakeScheduler(submit_result="malformed")
    controller = _prepared_controller(tmp_path, fake)
    controller.execute(controller.plan("submit-neutral", _resources()), confirmed=True)
    state_path = tmp_path / "vaspsolkit.state.json"
    outside = tmp_path.parent / f".{tmp_path.name}-outside-state"
    outside.write_text(state_path.read_text(encoding="utf-8"), encoding="utf-8")
    state_path.unlink()
    state_path.symlink_to(outside)
    with pytest.raises(RuntimeError, match="non-symlink|普通|state target"):
        controller.plan_reconcile_job_id("128151")


@pytest.mark.parametrize("flow", ["job-id", "no-job"])
@pytest.mark.parametrize("corruption", ["path", "inode"])
def test_manual_reconcile_rejects_receipt_for_wrong_case_identity(
    tmp_path: Path, flow: str, corruption: str
) -> None:
    from vaspsolkit.operations.activity import submission_receipt_path

    fake = FakeScheduler(submit_result="malformed")
    controller = _prepared_controller(tmp_path, fake)
    controller.execute(controller.plan("submit-neutral", _resources()), confirmed=True)
    receipt_path = submission_receipt_path(tmp_path, controller.activity_state_root)
    data = json.loads(receipt_path.read_text(encoding="utf-8"))
    if corruption == "path":
        data["case_path"] = str(tmp_path.parent / "other-case")
    else:
        data["case_inode"] += 1
    receipt_path.write_text(json.dumps(data), encoding="utf-8")
    state_before = (tmp_path / "vaspsolkit.state.json").read_bytes()

    with pytest.raises(RuntimeError, match="Case"):
        if flow == "job-id":
            controller.plan_reconcile_job_id("128160")
        else:
            controller.plan_confirm_no_job("SLURM未创建任务")

    assert receipt_path.exists()
    assert (tmp_path / "vaspsolkit.state.json").read_bytes() == state_before
    assert len(fake.submit_calls) == 1


@pytest.mark.parametrize("flow", ["job-id", "no-job"])
def test_manual_reconcile_plan_rejects_case_directory_replacement_before_execute(
    tmp_path: Path, flow: str
) -> None:
    from vaspsolkit.operations.activity import submission_receipt_path

    case = tmp_path / "case"
    fake = FakeScheduler(submit_result="malformed")
    controller = _prepared_controller(case, fake)
    controller.execute(controller.plan("submit-neutral", _resources()), confirmed=True)
    plan = (
        controller.plan_reconcile_job_id("128161")
        if flow == "job-id"
        else controller.plan_confirm_no_job("SLURM未创建任务")
    )
    receipt_path = submission_receipt_path(case, controller.activity_state_root)
    old_case = tmp_path / "old-case"
    case.rename(old_case)
    case.mkdir()
    (case / "vaspsolkit.state.json").write_bytes(
        (old_case / "vaspsolkit.state.json").read_bytes()
    )
    new_state_before = (case / "vaspsolkit.state.json").read_bytes()

    with pytest.raises(RuntimeError, match="Case|身份"):
        controller.execute(plan, confirmed=True)

    assert receipt_path.exists()
    assert (case / "vaspsolkit.state.json").read_bytes() == new_state_before
    assert len(fake.submit_calls) == 1


@pytest.mark.parametrize("flow", ["job-id", "no-job"])
def test_manual_reconcile_accepts_legacy_receipt_with_unknown_case_mode(
    tmp_path: Path, flow: str
) -> None:
    from vaspsolkit.state import WorkflowState
    from vaspsolkit.operations.activity import submission_receipt_path

    fake = FakeScheduler(submit_result="malformed")
    controller = _prepared_controller(tmp_path, fake)
    controller.execute(controller.plan("submit-neutral", _resources()), confirmed=True)
    receipt_path = submission_receipt_path(tmp_path, controller.activity_state_root)
    data = json.loads(receipt_path.read_text(encoding="utf-8"))
    data["case_mode"] = 0
    receipt_path.write_text(json.dumps(data), encoding="utf-8")

    if flow == "job-id":
        plan = controller.plan_reconcile_job_id("128170")
        result = controller.execute(plan, confirmed=True)
        state = WorkflowState.load(tmp_path / "vaspsolkit.state.json")
        assert state.neutral is not None and state.neutral.job_id == "128170"
    else:
        plan = controller.plan_confirm_no_job("SLURM未创建任务")
        result = controller.execute(plan, confirmed=True)
        state = WorkflowState.load(tmp_path / "vaspsolkit.state.json")
        assert state.neutral is not None and state.neutral.status == "PREPARED"

    assert result.ok
    assert not receipt_path.exists()
    assert len(fake.submit_calls) == 1





