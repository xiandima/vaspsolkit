"""Side-effect guarded planning controller for the Textual workbench."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import tempfile
from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple

from ..case_setup import (
    CONFIG_FILENAME,
    STATE_FILENAME,
    CaseInitializationPlan,
    apply_case_initialization,
    plan_case_initialization,
)
from ..config import (
    NO_EXPECTATION,
    KitConfig,
    SchedulerConfig,
    load_kit_config,
    serialize_kit_config,
    write_config_bytes,
)
from ..orchestrator import (
    PostSubmitPersistenceError,
    RecordedJobStatuses,
    RecordedJobsSnapshot,
    apply_recorded_job_statuses as apply_case_recorded_job_statuses,
    capture_recorded_jobs as capture_case_recorded_jobs,
    collect_recorded_job_statuses as collect_case_recorded_job_statuses,
    prepare_neutral_job,
    prepare_charge_jobs,
    check_prepared_jobs,
    reset_queued_jobs,
    submit_selected_jobs,
    submit_neutral_job,
)
from ..scheduler import PBSScheduler, scheduler_from_config
from ..postprocess import postprocess_versioned
from ..reference_settings import inspect_reference_freshness
from ..state import JobRecord, WorkflowState
from ..workflow import collect_results, result_file_path, results_root_path
from .actions import (
    ACTION_EFFECTS,
    ActionError,
    ActionPlan,
    ActionResult,
    ArchiveChange,
    FileDiff,
    ResourceRequest,
)
from .activity import (
    ActivityRecord,
    SubmissionReceipt,
    append_activity,
    claim_submission_receipt,
    clear_submission_receipt,
    new_submission_owner_token,
    read_submission_receipt,
    submission_receipt_path,
    update_submission_receipt,
)
from .models import WorkbenchSnapshot
from .snapshot import build_workbench_snapshot


_ACTION_METADATA = {
    "fix-inputs": ("补齐基础输入文件", "当前 Case 的基础输入不完整。"),
    "init": ("初始化 vaspsolkit 配置", "为当前 Case 创建 workflow 配置。"),
    "save-resources": ("保存 Case 默认资源", "将已预览的资源设置写入当前 Case 配置。"),
    "prepare-neutral": ("准备中性结构优化", "生成中性结构优化所需输入。"),
    "repair-neutral-submit": ("修复中性任务记录", "qsub 已返回 Job ID，仅修复本地状态，不会再次提交。"),
    "monitor": ("刷新任务状态", "只检查已记录任务的当前状态。"),
    "prepare-charge": ("准备带电点计算", "从中性结果生成带电点目录。"),
    "check-prepared": ("检查带电点输入", "检查带电点输入是否可以提交。"),
    "submit-selected": ("提交选中的带电点", "只提交用户明确选择的带电任务。"),
    "collect": ("收集计算结果", "汇总已完成带电点的计算结果。"),
    "postprocess": ("创建恒电势分析记录", "使用已收集的五点数据创建不可覆盖的分析历史记录。"),
    "check": ("检查计算输出", "检查当前记录任务的计算输出。"),
}
_ARCHIVE_NAMES = (
    "INCAR", "CONTCAR", "OUTCAR", "CHGCAR", "LOCPOT", "CHG", "WAVECAR",
    "vasprun.xml", "OSZICAR", "DOSCAR", "REPORT", "charge_sweep",
)


@dataclass(frozen=True)
class _EntryFingerprint:
    relative: str
    kind: str
    mode: int = 0
    size: int = 0
    modified_ns: int = 0
    changed_ns: int = 0
    digest: str = ""
    link_target: str = ""
    children: Tuple["_EntryFingerprint", ...] = ()
    resolved_target: Optional["_EntryFingerprint"] = None


@dataclass(frozen=True)
class _NeutralPayload:
    config: KitConfig
    case_identity: Tuple[int, int]
    fingerprints: Tuple[_EntryFingerprint, ...]
    archive_path: Path


@dataclass(frozen=True)
class _ConfigPayload:
    case_identity: Tuple[int, int]
    fingerprint: _EntryFingerprint
    before: bytes
    after: str


@dataclass(frozen=True)
class _SubmitPayload:
    config: KitConfig
    case_identity: Tuple[int, int]
    fingerprints: Tuple[_EntryFingerprint, ...]
    state_before: dict
    config_before: bytes
    config_after: str = ""


@dataclass(frozen=True)
class _ChargeSubmitPayload:
    config: KitConfig
    selected: Tuple[str, ...]
    case_identity: Tuple[int, int]
    fingerprints: Tuple[_EntryFingerprint, ...]
    state_before: dict
    config_before: bytes
    config_after: str = ""


@dataclass(frozen=True)
class _ResetQueuedPayload:
    config: KitConfig
    selected: Tuple[str, ...]
    case_identity: Tuple[int, int]
    state_fingerprint: _EntryFingerprint
    state_before: dict


@dataclass(frozen=True)
class _PostprocessPayload:
    summary_path: Path
    history_root: Path
    run_id: str
    case_identity: Tuple[int, int]
    summary_fingerprint: _EntryFingerprint


@dataclass(frozen=True)
class _WorkflowPayload:
    action: str
    config: KitConfig
    case_identity: Tuple[int, int]
    fingerprints: Tuple[_EntryFingerprint, ...]


@dataclass(frozen=True)
class _CollectPayload:
    config: KitConfig
    output_path: Path
    after: str
    case_identity: Tuple[int, int]
    fingerprints: Tuple[_EntryFingerprint, ...]


@dataclass(frozen=True)
class _RecoveryPayload:
    receipt: SubmissionReceipt
    case_identity: Tuple[int, int]
    state_fingerprint: _EntryFingerprint


@dataclass(frozen=True)
class _ReconcilePayload:
    current: SubmissionReceipt
    accepted: Optional[SubmissionReceipt]
    case_identity: Tuple[int, int]
    state_fingerprint: _EntryFingerprint


class WorkbenchController:
    def __init__(
        self,
        workdir: Path,
        config_path: Optional[Path] = None,
        scheduler_factory=scheduler_from_config,
        activity_state_root=None,
    ) -> None:
        self.workdir = Path(workdir).expanduser().resolve()
        self.config_path = (
            Path(config_path).expanduser().resolve()
            if config_path is not None
            else self.workdir / CONFIG_FILENAME
        )
        self.scheduler_factory = scheduler_factory
        self.activity_state_root = activity_state_root
        self._hard_submission_receipt: Optional[SubmissionReceipt] = None
        self._active_plan: Optional[ActionPlan] = None
        self._active_payload: Optional[Any] = None

    def snapshot(self) -> WorkbenchSnapshot:
        snapshot = build_workbench_snapshot(
            self.workdir, config_path=self.config_path
        )
        receipt, receipt_error = self._submission_barrier()
        if receipt is None and not receipt_error:
            return snapshot
        accepted = (
            receipt is not None
            and receipt.status == "ACCEPTED"
            and bool(receipt.job_id)
        )
        job_id = receipt.job_id if accepted else "unknown"
        target_charge = (
            str(receipt.resources.get("target_job", "")).strip()
            if receipt is not None
            else ""
        )
        if target_charge:
            updated_charges = tuple(
                replace(
                    job,
                    status="SUBMIT_UNKNOWN",
                    recorded_status="RECOVERY_REQUIRED",
                    job_id=job_id,
                    diagnostics=job.diagnostics
                    + (
                        receipt_error
                        or "charge qsub may have been accepted; manual reconciliation is required",
                    ),
                )
                if job.name == target_charge
                else job
                for job in snapshot.charge_jobs
            )
            charge_by_name = {job.name: job for job in updated_charges}
            queue_rows = tuple(
                charge_by_name.get(job.name, job)
                if job.kind == "charge"
                else job
                for job in snapshot.queue_rows
            )
            recommendation = replace(
                snapshot.recommendation,
                name="submit-selected",
                title=f"人工核对带电任务 {target_charge}",
                reason=(
                    f"qsub 已返回 Job ID {job_id}；禁止再次提交。"
                    if accepted
                    else "存在 SUBMITTING 恢复屏障；必须先查询 PBS。"
                ),
                effect=ACTION_EFFECTS["submit-selected"],
                cli_command=None,
                selectable_jobs=(target_charge,),
            )
            return replace(
                snapshot,
                charge_jobs=updated_charges,
                queue_rows=queue_rows,
                recommendation=recommendation,
                stage="submission_recovery",
                error_count=snapshot.error_count + 1,
            )
        neutral = replace(
            snapshot.neutral,
            status="SUBMIT_UNKNOWN",
            recorded_status="RECOVERY_REQUIRED",
            job_id=job_id,
            diagnostics=snapshot.neutral.diagnostics + (
                receipt_error or "qsub succeeded; local state recovery is required",
            ),
        )
        recommendation = replace(
            snapshot.recommendation,
            name="repair-neutral-submit" if accepted else "submit-neutral",
            title="修复中性任务记录" if accepted else "人工核对 PBS 提交状态",
            reason=(
                "已有 qsub Job ID；只修复本地状态，绝不再次提交。"
                if accepted
                else "存在 SUBMITTING 恢复屏障；必须人工查询 PBS，禁止再次提交。"
            ),
            effect=(
                ACTION_EFFECTS["repair-neutral-submit"]
                if accepted else ACTION_EFFECTS["submit-neutral"]
            ),
            cli_command=None,
        )
        return replace(
            snapshot,
            neutral=neutral,
            queue_rows=(neutral,) + snapshot.charge_jobs,
            recommendation=recommendation,
            stage="submission_recovery",
            error_count=snapshot.error_count + 1,
        )

    def refresh_recorded_jobs(self) -> WorkbenchSnapshot:
        """Synchronous compatibility path; Textual uses the split methods."""
        captured = self.capture_recorded_jobs()
        if not captured.job_ids:
            return self.snapshot()
        collected = self.collect_recorded_job_statuses(captured)
        return self.apply_recorded_job_statuses(collected)

    def capture_recorded_jobs(self) -> RecordedJobsSnapshot:
        """Capture current Case and state identity before background I/O."""
        return capture_case_recorded_jobs(self.workdir)

    def collect_recorded_job_statuses(
        self, captured: RecordedJobsSnapshot
    ) -> RecordedJobStatuses:
        """Perform scheduler reads only; never mutate Case files."""
        config_path = self.config_path
        config = load_kit_config(config_path)
        scheduler = self.scheduler_factory(copy.deepcopy(config.scheduler))
        return collect_case_recorded_job_statuses(captured, scheduler)

    def apply_recorded_job_statuses(
        self, collected: RecordedJobStatuses
    ) -> WorkbenchSnapshot:
        """Commit a still-current result under the shared state lock."""
        apply_case_recorded_job_statuses(collected)
        refreshed_at = datetime.now().astimezone().isoformat(timespec="seconds")
        return build_workbench_snapshot(
            self.workdir,
            config_path=self.config_path,
            last_refresh=refreshed_at,
        )

    def for_case(self, workdir: Path) -> "WorkbenchController":
        next_workdir = Path(workdir).expanduser().resolve()
        try:
            config_relative = self.config_path.relative_to(self.workdir)
            config_path = next_workdir / config_relative
        except ValueError:
            config_path = self.config_path
        return type(self)(
            next_workdir,
            config_path=config_path,
            scheduler_factory=self.scheduler_factory,
            activity_state_root=self.activity_state_root,
        )

    def preview_resources(self, resources: ResourceRequest) -> ResourceRequest:
        if not isinstance(resources, ResourceRequest):
            raise TypeError("resources must be ResourceRequest")
        resources.validate()
        self._validate_script_path(resources)
        return resources

    def inspect_nodes(self):
        """Perform an explicit, read-only node discovery for the current Case."""
        config = load_kit_config(self.config_path)
        scheduler = self.scheduler_factory(copy.deepcopy(config.scheduler))
        inspect_nodes = getattr(scheduler, "inspect_nodes", None)
        if inspect_nodes is None:
            raise RuntimeError("当前调度器不支持节点探测；请手动输入节点名。")
        return tuple(
            inspect_nodes(
                min_node=config.workflow.qsub_min_node,
                ppn=config.scheduler.cores,
            )
        )

    def plan_resource_defaults(self, resources: ResourceRequest) -> ActionPlan:
        self.preview_resources(resources)
        if not resources.persist:
            raise ValueError("saving Case defaults requires persist=True")
        config_path = self.config_path
        if config_path.is_symlink() or not config_path.is_file():
            raise ValueError("vaspsolkit.json must be a regular file within the Case")
        before_bytes = config_path.read_bytes()
        config = load_kit_config(config_path)
        updated = _config_with_resources(config, resources)
        after = serialize_kit_config(updated).decode("utf-8")
        before = before_bytes.decode("utf-8")
        plan = ActionPlan(
            action_id="save-resources",
            effect=ACTION_EFFECTS["save-resources"],
            target_case=self.workdir,
            target_jobs=(),
            title=_ACTION_METADATA["save-resources"][0],
            reason=_ACTION_METADATA["save-resources"][1],
            file_diffs=(FileDiff(config_path, before, after, "update"),),
            scheduler_request=resources,
        )
        self._activate(
            plan,
            _ConfigPayload(
                _case_identity(self.workdir),
                _fingerprint(self.workdir, config_path),
                before_bytes,
                after,
            ),
        )
        return plan

    def plan_reconcile_job_id(self, job_id: str) -> ActionPlan:
        if not isinstance(job_id, str) or _ValidatedPBSScheduler._JOB_ID.fullmatch(job_id.strip()) is None:
            raise ValueError("Job ID 格式无效")
        receipt, error = self._submission_barrier()
        if receipt is None or error:
            raise RuntimeError(error or "没有提交恢复屏障")
        self._verify_receipt_case_identity(receipt)
        if receipt.status == "ACCEPTED":
            raise RuntimeError("Job ID 已记录，请使用本地状态修复")
        accepted = replace(
            receipt, status="ACCEPTED", job_id=job_id.strip(),
            version=receipt.version + 1,
        )
        state_path = self.workdir / STATE_FILENAME
        plan = self._reconcile_plan(accepted, state_path)
        self._activate(
            plan,
            _ReconcilePayload(
                receipt, accepted, _case_identity(self.workdir),
                _fingerprint(self.workdir, state_path),
            ),
        )
        return plan

    def plan_confirm_no_job(self, confirmation: str) -> ActionPlan:
        if confirmation != "PBS未创建任务":
            raise ValueError("必须准确输入：PBS未创建任务")
        receipt, error = self._submission_barrier()
        if receipt is None or error:
            raise RuntimeError(error or "没有提交恢复屏障")
        self._verify_receipt_case_identity(receipt)
        if receipt.status == "ACCEPTED":
            raise RuntimeError("已记录 Job ID，不能声明 PBS 未创建任务")
        plan = ActionPlan(
            action_id="clear-submit-barrier",
            effect=ACTION_EFFECTS["clear-submit-barrier"],
            target_case=self.workdir,
            target_jobs=("neutral",),
            title="确认 PBS 未创建任务并解除屏障",
            reason="仅在人工查询 PBS 后确认没有任务时使用。",
            warnings=("此操作不会调用 qsub；错误确认可能导致以后重复提交。",),
        )
        self._activate(
            plan,
            _ReconcilePayload(
                receipt, None, _case_identity(self.workdir),
                _fingerprint(self.workdir, self.workdir / STATE_FILENAME),
            ),
        )
        return plan

    def plan(
        self,
        action: str,
        resources: Optional[ResourceRequest] = None,
        *,
        selected: Tuple[str, ...] = (),
    ) -> ActionPlan:
        snapshot = self.snapshot()
        payload: Optional[Any] = None
        if action == "refresh":
            plan = ActionPlan(
                action_id=action,
                effect=ACTION_EFFECTS[action],
                target_case=self.workdir,
                target_jobs=tuple(job.name for job in snapshot.queue_rows if job.job_id),
                title="刷新已记录任务状态",
                reason="只查询已记录 Job ID，不扫描全局队列。",
            )
        elif action in {"monitor", "check"}:
            recorded = tuple(job.name for job in snapshot.queue_rows if job.job_id)
            plan = ActionPlan(
                action_id=action,
                effect=ACTION_EFFECTS[action],
                target_case=self.workdir,
                target_jobs=recorded,
                title=_ACTION_METADATA[action][0],
                reason=_ACTION_METADATA[action][1],
                commands_summary=((f"qstat × {len(recorded)}",) if recorded else ()),
                warnings=("只查询当前 Case 已记录的 Job ID，不扫描全局队列。",),
                blocked_reason="" if recorded else "当前 Case 没有可同步的 Job ID。",
            )
        elif action == "fix-inputs":
            plan = ActionPlan(
                action_id=action,
                effect=ACTION_EFFECTS[action],
                target_case=self.workdir,
                target_jobs=(),
                title=_ACTION_METADATA[action][0],
                reason=_ACTION_METADATA[action][1],
                warnings=("请先在 Case 根目录补齐 POSCAR、INCAR、KPOINTS、POTCAR 和提交脚本。",),
                blocked_reason="基础输入不完整；VASPsolKit 不会伪造体系输入或 POTCAR。",
            )
        elif action == "init":
            if resources is None:
                raise ValueError("init requires resources")
            resources.validate()
            scheduler = SchedulerConfig(
                kind="pbs", queue=resources.queue, cores=resources.cores,
                walltime=resources.walltime, script=resources.script,
                nodes=list(resources.nodes),
            )
            initialization = plan_case_initialization(self.workdir, scheduler)
            payload = initialization
            plan = ActionPlan(
                action_id=action,
                effect=ACTION_EFFECTS[action],
                target_case=self.workdir,
                target_jobs=(),
                title=_ACTION_METADATA[action][0],
                reason=_ACTION_METADATA[action][1],
                file_diffs=tuple(
                    FileDiff(change.path, change.before, change.after, change.change_type)
                    for change in initialization.file_changes
                ),
                scheduler_request=resources,
            )
        elif action == "prepare-neutral":
            try:
                plan, payload = self._plan_prepare_neutral()
            except (OSError, RuntimeError, ValueError) as exc:
                plan = ActionPlan(
                    action_id=action,
                    effect=ACTION_EFFECTS[action],
                    target_case=self.workdir,
                    target_jobs=("neutral",),
                    title=_ACTION_METADATA[action][0],
                    reason=_ACTION_METADATA[action][1],
                    warnings=(str(exc),),
                    blocked_reason=str(exc),
                )
        elif action == "repair-neutral-submit":
            plan, payload = self._plan_submission_recovery()
        elif action == "submit-neutral":
            if resources is None:
                raise ValueError("submit-neutral requires resources")
            self.preview_resources(resources)
            config_path = self.config_path
            state_path = self.workdir / STATE_FILENAME
            config_before = config_path.read_bytes()
            config = load_kit_config(config_path)
            receipt, receipt_error = self._submission_barrier()
            state = (
                WorkflowState.load(state_path) if state_path.is_file() else WorkflowState()
            ) if receipt is None and not receipt_error else WorkflowState()
            neutral_status = state.neutral.status if state.neutral is not None else "NOT_PREPARED"
            blocked_reason = ""
            if receipt is not None:
                blocked_reason = (
                    f"Job {receipt.job_id} 已由 qsub 接受，必须先修复本地状态；禁止再次提交。"
                    if receipt.status == "ACCEPTED"
                    else f"存在 {receipt.status} 提交恢复屏障；必须人工核对 PBS，禁止再次提交。"
                )
            elif receipt_error:
                blocked_reason = f"提交恢复屏障不可读：{receipt_error}；禁止再次提交。"
            elif neutral_status != "PREPARED":
                blocked_reason = "中性任务尚未处于 PREPARED 状态，不能提交。"
            watched_names = (
                "POSCAR", "INCAR", "POTCAR", "KPOINTS", resources.script,
                self._config_relative(), STATE_FILENAME,
            )
            fingerprints = tuple(
                _fingerprint(self.workdir, self.workdir / name)
                for name in dict.fromkeys(watched_names)
            )
            submit_config = _config_with_resources(config, resources)
            config_after = (
                serialize_kit_config(submit_config).decode("utf-8")
                if resources.persist else ""
            )
            file_diffs = (
                (FileDiff(config_path, config_before.decode("utf-8"), config_after, "update"),)
                if resources.persist else ()
            )
            payload = _SubmitPayload(
                config=submit_config,
                case_identity=_case_identity(self.workdir),
                fingerprints=fingerprints,
                state_before=_workflow_state_dict(state),
                config_before=config_before,
                config_after=config_after,
            )
            plan = ActionPlan(
                action_id=action, effect=ACTION_EFFECTS[action],
                target_case=self.workdir, target_jobs=("neutral",), title="提交中性任务",
                reason="中性结构优化输入已经准备好，可预览一次 qsub 提交。",
                file_diffs=file_diffs,
                scheduler_request=resources, commands_summary=("qsub × 1",),
                blocked_reason=blocked_reason,
            )
        elif action == "submit-selected":
            if resources is None:
                raise ValueError("submit-selected requires resources")
            self.preview_resources(resources)
            names = tuple(dict.fromkeys(str(name).strip() for name in selected if str(name).strip()))
            if not names:
                raise ValueError("submit-selected requires at least one selected job")
            state_path = self.workdir / STATE_FILENAME
            config_path = self.config_path
            state = WorkflowState.load(state_path)
            config_before = config_path.read_bytes()
            config = load_kit_config(config_path)
            unknown = tuple(name for name in names if name not in state.jobs)
            not_prepared = tuple(
                name
                for name in names
                if name in state.jobs and state.jobs[name].status != "PREPARED"
            )
            blocked_parts = []
            receipt, receipt_error = self._submission_barrier()
            if receipt is not None:
                target = receipt.resources.get("target_job", "unknown")
                blocked_parts.append(
                    f"带电任务 {target} 存在 {receipt.status} 提交恢复屏障；禁止再次提交。"
                )
            elif receipt_error:
                blocked_parts.append("提交恢复屏障不可读；禁止新的带电任务提交。")
            if not state.prepared_checked:
                blocked_parts.append("带电点输入尚未通过 check-prepared。")
            if unknown:
                blocked_parts.append("未知带电任务: " + ", ".join(unknown))
            if not_prepared:
                blocked_parts.append(
                    "所选任务不是 PREPARED: " + ", ".join(not_prepared)
                )
            submit_config = _config_with_resources(config, resources)
            config_after = (
                serialize_kit_config(submit_config).decode("utf-8")
                if resources.persist
                else ""
            )
            file_diffs = (
                (
                    FileDiff(
                        config_path,
                        config_before.decode("utf-8"),
                        config_after,
                        "update",
                    ),
                )
                if resources.persist
                else ()
            )
            watched = [config_path, state_path]
            for name in names:
                record = state.jobs.get(name)
                if record is None:
                    continue
                folder = self.workdir / record.folder
                watched.extend(
                    folder / filename
                    for filename in (
                        "POSCAR",
                        "INCAR",
                        "POTCAR",
                        "KPOINTS",
                        "CHGCAR",
                        resources.script,
                    )
                )
            fingerprints = tuple(
                _fingerprint(self.workdir, path) for path in dict.fromkeys(watched)
            )
            payload = _ChargeSubmitPayload(
                config=submit_config,
                selected=names,
                case_identity=_case_identity(self.workdir),
                fingerprints=fingerprints,
                state_before=_workflow_state_dict(state),
                config_before=config_before,
                config_after=config_after,
            )
            plan = ActionPlan(
                action_id=action,
                effect=ACTION_EFFECTS[action],
                target_case=self.workdir,
                target_jobs=names,
                title=_ACTION_METADATA[action][0],
                reason=_ACTION_METADATA[action][1],
                file_diffs=file_diffs,
                scheduler_request=resources,
                commands_summary=(f"qsub × {len(names)}",),
                blocked_reason=" ".join(blocked_parts),
            )
        elif action in {"prepare-charge", "check-prepared"}:
            try:
                plan, payload = self._plan_charge_workflow(action)
            except (OSError, RuntimeError, ValueError) as exc:
                plan = ActionPlan(
                    action_id=action,
                    effect=ACTION_EFFECTS[action],
                    target_case=self.workdir,
                    target_jobs=(),
                    title=_ACTION_METADATA[action][0],
                    reason=_ACTION_METADATA[action][1],
                    warnings=(str(exc),),
                    blocked_reason=str(exc),
                )
        elif action == "collect":
            try:
                plan, payload = self._plan_collect()
            except (OSError, RuntimeError, ValueError) as exc:
                plan = ActionPlan(
                    action_id=action,
                    effect=ACTION_EFFECTS[action],
                    target_case=self.workdir,
                    target_jobs=(),
                    title=_ACTION_METADATA[action][0],
                    reason=_ACTION_METADATA[action][1],
                    warnings=(str(exc),),
                    blocked_reason=str(exc),
                )
        elif action == "postprocess":
            config = load_kit_config(self.config_path)
            summary_path = result_file_path(
                self.workdir,
                config.workflow,
                config.workflow.summary_file,
            ).resolve()
            history_root = (
                results_root_path(self.workdir, config.workflow).resolve() / "history"
            )
            blocked = []
            safe_summary = True
            for label, path in (("summary", summary_path), ("history", history_root)):
                try:
                    path.relative_to(self.workdir)
                except ValueError:
                    blocked.append(f"{label} 路径位于 Case 之外")
                    if label == "summary":
                        safe_summary = False
            if safe_summary and not summary_path.is_file():
                blocked.append("缺少已收集的 summary.csv")
            elif safe_summary and summary_path.is_symlink():
                blocked.append("summary.csv 不能是符号链接")
            elif safe_summary:
                freshness = inspect_reference_freshness(summary_path, config.workflow)
                if freshness.status != "current":
                    blocked.append("summary.csv 的 SHE reference 已过期或无法确认；请先重新收集结果")
            run_id = datetime.now().strftime("%Y%m%dT%H%M%S-%f")
            payload = _PostprocessPayload(
                summary_path=summary_path,
                history_root=history_root,
                run_id=run_id,
                case_identity=_case_identity(self.workdir),
                summary_fingerprint=_fingerprint(
                    self.workdir,
                    summary_path if safe_summary else self.workdir / "__unsafe_summary__",
                ),
            )
            plan = ActionPlan(
                action_id=action,
                effect=ACTION_EFFECTS[action],
                target_case=self.workdir,
                target_jobs=tuple(
                    job.name for job in snapshot.charge_jobs if job.status == "CONVERGED"
                ),
                title=_ACTION_METADATA[action][0],
                reason=_ACTION_METADATA[action][1],
                commands_summary=("postprocess × 1",),
                warnings=(
                    f"将新建 results/history/{run_id}；不会覆盖既有分析。",
                ),
                blocked_reason="；".join(blocked),
            )
        else:
            try:
                title, reason = _ACTION_METADATA[action]
            except KeyError as exc:
                raise ValueError(f"unsupported action: {action}") from exc
            plan = ActionPlan(
                action_id=action, effect=ACTION_EFFECTS[action],
                target_case=self.workdir, target_jobs=(), title=title, reason=reason,
                warnings=("动作执行路径尚未接线；本计划不会更改 Case。",),
            )
        self._activate(plan, payload)
        return plan

    def plan_reset_queued(
        self,
        selected: Tuple[str, ...],
        resources: ResourceRequest,
    ) -> ActionPlan:
        """Preview the safe first half of a node change: qstat, then qdel.

        This action deliberately does not call qsub.  A successful reset leaves
        the selected charge points PREPARED so a second, separately reviewed
        submission preview can use ``resources``.
        """
        self.preview_resources(resources)
        names = tuple(
            dict.fromkeys(str(name).strip() for name in selected if str(name).strip())
        )
        if not names:
            raise ValueError("至少选择一个需要取消或更换节点的带电任务。")

        state_path = self.workdir / STATE_FILENAME
        state = WorkflowState.load(state_path)
        config = load_kit_config(self.config_path)
        reset_config = _config_with_resources(config, resources)
        scheduler = self.scheduler_factory(copy.deepcopy(config.scheduler))
        blocked = []
        for name in names:
            record = state.jobs.get(name)
            if record is None:
                blocked.append(f"未知带电任务: {name}")
                continue
            if record.status not in {"QUEUED", "SUBMITTED"}:
                blocked.append(f"任务 {name} 不是 QUEUED/SUBMITTED")
                continue
            if not record.job_id:
                blocked.append(f"任务 {name} 没有记录 Job ID")
                continue
            try:
                queue_state = str(scheduler.status(record.job_id).state).upper()
            except (OSError, RuntimeError, ValueError) as exc:
                blocked.append(f"任务 {name} 的 PBS 状态查询失败: {exc}")
                continue
            if queue_state in {"R", "RUNNING", "CONFIGURING", "COMPLETING", "UNKNOWN"}:
                blocked.append(f"任务 {name} 当前为 RUNNING/UNKNOWN，禁止 qdel")
            elif queue_state not in {"Q", "QUEUED", "PENDING", "SUBMITTED", "MISSING"}:
                blocked.append(f"任务 {name} 当前 PBS 状态不允许重置: {queue_state}")

        payload = _ResetQueuedPayload(
            config=reset_config,
            selected=names,
            case_identity=_case_identity(self.workdir),
            state_fingerprint=_fingerprint(self.workdir, state_path),
            state_before=_workflow_state_dict(state),
        )
        plan = ActionPlan(
            action_id="reset-queued",
            effect=ACTION_EFFECTS["reset-queued"],
            target_case=self.workdir,
            target_jobs=names,
            title="取消旧任务并准备更换节点",
            reason=(
                "第一步重新查询 PBS 并安全取消仍在排队的旧任务；成功后任务回到 "
                "PREPARED。第二步需由用户再次按 S，独立预览并提交到所选节点。"
            ),
            scheduler_request=resources,
            commands_summary=(f"qstat × {len(names)}", f"qdel ≤ {len(names)}"),
            warnings=("本操作不会调用 qsub，不会自动创建新任务。",),
            blocked_reason="；".join(blocked),
        )
        self._activate(plan, payload)
        return plan

    def execute(self, plan: ActionPlan, confirmed: bool = False) -> ActionResult:
        if not isinstance(plan, ActionPlan):
            raise TypeError("plan must be an ActionPlan")
        if plan.target_case != self.workdir:
            raise RuntimeError("ActionPlan 属于其他 Case，拒绝执行。")
        if self._active_plan is not plan:
            raise RuntimeError("ActionPlan 不属于当前 controller 或已经失效。")
        if plan.blocked_reason:
            raise RuntimeError(plan.blocked_reason)
        if ACTION_EFFECTS[plan.action_id] != "read-only" and not confirmed:
            raise PermissionError("非只读动作必须明确确认后才能执行。")
        payload = self._active_payload
        self._active_plan = None
        self._active_payload = None
        if plan.action_id == "refresh" and plan.effect == "read-only":
            return ActionResult(plan.action_id, "refreshed", self.snapshot(), "已重新读取当前 Case；未调用调度器。")
        if plan.action_id in {"monitor", "check"}:
            captured = self.capture_recorded_jobs()
            if not captured.job_ids:
                return ActionResult(plan.action_id, "refreshed", self.snapshot(), "当前 Case 没有可同步的 Job ID。")
            collected = self.collect_recorded_job_statuses(captured)
            snapshot = self.apply_recorded_job_statuses(collected)
            return ActionResult(
                plan.action_id,
                "refreshed",
                snapshot,
                f"已同步 {len(captured.job_ids)} 个当前 Case 任务。",
            )
        if plan.action_id == "init":
            if not isinstance(payload, CaseInitializationPlan):
                raise RuntimeError("初始化计划载荷已失效，请重新预览。")
            try:
                apply_case_initialization(payload, confirmed=True)
            except RuntimeError as exc:
                raise RuntimeError(f"Case 已变化，请重新预览：{exc}") from exc
            return ActionResult(plan.action_id, "completed", self.snapshot(), "Case 初始化完成。")
        if plan.action_id == "prepare-neutral":
            if not isinstance(payload, _NeutralPayload):
                raise RuntimeError("中性准备计划载荷已失效，请重新预览。")
            self._verify_neutral_payload(payload)
            prepared_state = prepare_neutral_job(
                self.workdir, copy.deepcopy(payload.config), archive_path=payload.archive_path
            )
            cleanup_warning = (
                prepared_state.neutral.metadata.get("cleanup_warning", "")
                if prepared_state.neutral is not None
                else ""
            )
            warnings = (cleanup_warning,) if cleanup_warning else ()
            message = "中性结构优化输入已准备。"
            if cleanup_warning:
                message += f" Warning: {cleanup_warning}"
            return ActionResult(
                plan.action_id, "completed", self.snapshot(), message, warnings
            )
        if plan.action_id == "save-resources":
            if not isinstance(payload, _ConfigPayload):
                raise RuntimeError("资源配置计划载荷已失效，请重新预览。")
            self._verify_config_payload(payload)
            _atomic_write_text(
                self.config_path,
                payload.after,
                expected_config=payload.before,
            )
            return ActionResult(plan.action_id, "completed", self.snapshot(), "Case 默认资源已保存。")
        if plan.action_id == "submit-neutral":
            if not isinstance(payload, _SubmitPayload):
                raise RuntimeError("中性提交计划载荷已失效，请重新预览。")
            self._verify_submit_payload(payload)
            if payload.config_after:
                _atomic_write_text(
                    self.config_path,
                    payload.config_after,
                    expected_config=payload.config_before,
                )
            intent = _submission_intent(self.workdir, payload, plan.scheduler_request)
            try:
                claim_submission_receipt(
                    self.workdir, intent, self.activity_state_root
                )
            except FileExistsError as exc:
                error = ActionError(
                    step="reconcile-neutral-submit",
                    summary="已有提交 owner 占用当前 Case，已取消 qsub",
                    command="qsub",
                    raw=f"atomic submission claim failed: {exc}",
                    suggestion="读取现有恢复屏障并人工核对 PBS；禁止再次提交。",
                    summary_en="Another submission owner already claimed this Case; qsub was cancelled",
                    suggestion_en="Inspect the existing barrier and reconcile PBS manually; do not resubmit.",
                )
                return ActionResult(plan.action_id, "blocked", self.snapshot(), error.summary, ok=False, error=error)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                error = ActionError(
                    step="submit-intent",
                    summary="提交意图无法安全落盘，已取消 qsub",
                    command="qsub",
                    raw=f"write-ahead intent failed: {exc}",
                    suggestion="修复用户状态目录写入问题后重新预览；本次没有调用 qsub。",
                    summary_en="The submission intent could not be persisted; qsub was cancelled",
                    suggestion_en="Fix the user-state write failure and review again; qsub was not called.",
                )
                return ActionResult(
                    plan.action_id, "failed", self.snapshot(), error.summary,
                    ok=False, error=error,
                )
            try:
                self._verify_submit_sources(payload, persisted=True)
            except (OSError, RuntimeError, ValueError):
                clear_submission_receipt(
                    self.workdir, self.activity_state_root, intent.owner_token,
                    expected_version=intent.version,
                    expected_status=intent.status,
                )
                raise
            scheduler = self.scheduler_factory(copy.deepcopy(payload.config.scheduler))
            guarded_scheduler = _ValidatedPBSScheduler(
                scheduler,
                strict_job_id=payload.config.scheduler.kind == "pbs",
                on_accepted=lambda job_id: update_submission_receipt(
                    self.workdir,
                    replace(
                        intent, status="ACCEPTED", job_id=job_id,
                        version=intent.version + 1,
                    ),
                    intent.owner_token,
                    self.activity_state_root,
                    expected_version=intent.version,
                    expected_status=intent.status,
                ),
            )
            try:
                submitted = submit_neutral_job(
                    self.workdir,
                    copy.deepcopy(payload.config),
                    scheduler=guarded_scheduler,
                    confirmed=True,
                    require_prepared=True,
                )
            except _QsubAttemptError as exc:
                try:
                    update_submission_receipt(
                        self.workdir,
                        replace(
                            intent, raw_output=exc.raw,
                            version=intent.version + 1,
                        ),
                        intent.owner_token,
                        self.activity_state_root,
                        expected_version=intent.version,
                        expected_status=intent.status,
                    )
                except (OSError, RuntimeError, TypeError, ValueError, PermissionError):
                    pass
                error = ActionError(
                    step="reconcile-neutral-submit",
                    summary="qsub 调用后发生异常，提交状态未知",
                    command="qsub",
                    raw=exc.raw,
                    suggestion="禁止再次提交；请人工查询 PBS 并录入 Job ID 或确认未创建任务。",
                    summary_en="An exception occurred after qsub was invoked; submission state is unknown",
                    suggestion_en="Do not resubmit; query PBS and record the Job ID or confirm no job was created.",
                )
                return ActionResult(
                    plan.action_id, "recovery-required", self.snapshot(),
                    error.summary, ok=False, error=error,
                )
            except _MalformedQsubOutputError as exc:
                try:
                    update_submission_receipt(
                        self.workdir,
                        replace(
                            intent, raw_output=exc.raw,
                            version=intent.version + 1,
                        ),
                        intent.owner_token,
                        self.activity_state_root,
                        expected_version=intent.version,
                        expected_status=intent.status,
                    )
                except (OSError, RuntimeError, TypeError, ValueError, PermissionError):
                    pass
                error = ActionError(
                    step="reconcile-neutral-submit",
                    summary="qsub 已被调用，但返回内容无法解析",
                    command="qsub",
                    raw=exc.raw,
                    suggestion="禁止再次提交；请人工查询 PBS 并录入实际 Job ID。",
                    summary_en="qsub was called, but its output could not be parsed",
                    suggestion_en="Do not submit again; query PBS manually and record the actual Job ID.",
                )
                return ActionResult(plan.action_id, "recovery-required", self.snapshot(), error.summary, ok=False, error=error)
            except _AcceptedReceiptUpdateError as exc:
                accepted = replace(
                    intent, status="ACCEPTED", job_id=exc.job_id,
                    version=intent.version + 1,
                )
                self._hard_submission_receipt = accepted
                error = ActionError(
                    step="reconcile-neutral-submit",
                    summary="qsub 已返回 Job ID，但 ACCEPTED receipt 更新失败",
                    command="qsub",
                    raw=f"Job ID: {exc.job_id}; receipt update failed: {exc.cause}",
                    suggestion="不要再次提交。当前进程已冻结该 Job ID；新进程必须人工查询 PBS。",
                    summary_en="qsub returned a Job ID, but the ACCEPTED receipt update failed",
                    suggestion_en="Do not submit again. This process retained the Job ID; a new process must query PBS manually.",
                )
                return ActionResult(
                    plan.action_id, "recovery-required", self.snapshot(), error.summary, ok=False,
                    job_ids={"neutral": exc.job_id},
                    error=error,
                )
            except PostSubmitPersistenceError as exc:
                error = ActionError(
                    step="recover-neutral-state",
                    summary="qsub 已接受任务，但本地状态保存失败",
                    command=exc.command,
                    raw=f"Job ID: {exc.job_id}; {exc.cause}",
                    suggestion="不要再次提交。请执行“修复本地 Job ID”或人工核对该 Job ID。",
                    summary_en="qsub accepted the job, but local state persistence failed",
                    suggestion_en="Do not submit again. Repair the local Job ID record or verify this Job ID manually.",
                )
                return ActionResult(
                    plan.action_id, "recovery-required", self.snapshot(), error.summary,
                    ok=False, job_ids={"neutral": exc.job_id}, error=error,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                cleanup_error = self._clear_failed_submission_intent(intent)
                error = ActionError(
                    step="reconcile-neutral-submit" if cleanup_error else "submit-neutral",
                    summary=(
                        "qsub 失败，但提交意图清理未完成"
                        if cleanup_error else "中性任务提交失败"
                    ),
                    command="qsub",
                    raw=str(exc) + (f"; intent cleanup failed: {cleanup_error}" if cleanup_error else ""),
                    suggestion=(
                        "提交恢复屏障仍存在；先人工核对并清理，禁止直接重试。"
                        if cleanup_error else "检查队列、节点、资源和 PBS 权限后，重新预览并重试。"
                    ),
                    summary_en=(
                        "qsub failed, but submission-intent cleanup is incomplete"
                        if cleanup_error else "Neutral job submission failed"
                    ),
                    suggestion_en=(
                        "The recovery barrier remains; reconcile it manually before any retry."
                        if cleanup_error else "Check the queue, node, resources, and PBS permissions, then review and retry."
                    ),
                )
                return ActionResult(
                    plan.action_id,
                    "failed",
                    self.snapshot(),
                    error.summary,
                    ok=False,
                    error=error,
                )
            job_id = submitted.neutral.job_id if submitted.neutral is not None else ""
            try:
                clear_submission_receipt(
                    self.workdir, self.activity_state_root, intent.owner_token,
                    expected_version=intent.version + 1,
                    expected_status="ACCEPTED",
                )
            except OSError as exc:
                error = ActionError(
                    step="recover-neutral-state",
                    summary="任务状态已保存，但 ACCEPTED receipt 清理失败",
                    command="qsub",
                    raw=f"Job ID: {job_id}; receipt cleanup failed: {exc}",
                    suggestion="不要再次提交；可执行本地状态修复以清理恢复屏障。",
                    summary_en="Job state was saved, but the ACCEPTED receipt could not be cleared",
                    suggestion_en="Do not submit again; use local-state repair to clear the recovery barrier.",
                )
                return ActionResult(
                    plan.action_id, "recovery-required", self.snapshot(), error.summary,
                    ok=False, job_ids={"neutral": job_id}, error=error,
                )
            return ActionResult(
                plan.action_id,
                "submitted",
                self.snapshot(),
                f"中性任务已提交：{job_id}",
                job_ids={"neutral": job_id},
            )
        if plan.action_id == "submit-selected":
            if not isinstance(payload, _ChargeSubmitPayload):
                raise RuntimeError("带电任务提交计划载荷已失效，请重新预览。")
            self._verify_charge_submit_payload(payload)
            if payload.config_after:
                _atomic_write_text(
                    self.config_path,
                    payload.config_after,
                    expected_config=payload.config_before,
                )
            submitted_ids = {}
            scheduler = self.scheduler_factory(copy.deepcopy(payload.config.scheduler))
            for name in payload.selected:
                state = WorkflowState.load(self.workdir / STATE_FILENAME)
                record = state.jobs.get(name)
                if record is None or record.status != "PREPARED":
                    raise RuntimeError(f"带电任务 {name} 状态已变化，请重新预览。")
                intent = _charge_submission_intent(
                    self.workdir,
                    payload,
                    plan.scheduler_request,
                    name,
                )
                try:
                    claim_submission_receipt(
                        self.workdir,
                        intent,
                        self.activity_state_root,
                    )
                except FileExistsError as exc:
                    error = ActionError(
                        step="reconcile-charge-submit",
                        summary="已有提交恢复屏障，已停止后续 qsub",
                        command="qsub",
                        raw=str(exc),
                        suggestion="先核对已记录的带电任务提交状态；不要重复提交。",
                        summary_en="A submission recovery barrier already exists; remaining qsub calls were stopped",
                        suggestion_en="Reconcile the recorded charge submission before retrying.",
                    )
                    return ActionResult(
                        plan.action_id,
                        "blocked",
                        self.snapshot(),
                        error.summary,
                        ok=False,
                        job_ids=submitted_ids,
                        error=error,
                    )
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    error = ActionError(
                        step="submit-intent",
                        summary="带电任务提交意图无法落盘，未调用 qsub",
                        command="qsub",
                        raw=str(exc),
                        suggestion="修复用户状态目录后重新预览。",
                        summary_en="The charge submission intent could not be persisted; qsub was not called",
                        suggestion_en="Fix the user-state directory and review again.",
                    )
                    return ActionResult(
                        plan.action_id,
                        "failed",
                        self.snapshot(),
                        error.summary,
                        ok=False,
                        job_ids=submitted_ids,
                        error=error,
                    )

                guarded = _ValidatedPBSScheduler(
                    scheduler,
                    strict_job_id=payload.config.scheduler.kind == "pbs",
                    on_accepted=lambda job_id, current=intent: update_submission_receipt(
                        self.workdir,
                        replace(
                            current,
                            status="ACCEPTED",
                            job_id=job_id,
                            version=current.version + 1,
                        ),
                        current.owner_token,
                        self.activity_state_root,
                        expected_version=current.version,
                        expected_status=current.status,
                    ),
                )
                try:
                    submitted = submit_selected_jobs(
                        self.workdir,
                        copy.deepcopy(payload.config),
                        state,
                        [name],
                        scheduler=guarded,
                        confirmed=True,
                        require_prepared_check=True,
                    )
                except (_QsubAttemptError, _MalformedQsubOutputError) as exc:
                    error = ActionError(
                        step="reconcile-charge-submit",
                        summary=f"带电任务 {name} 调用 qsub 后状态未知",
                        command="qsub",
                        raw=getattr(exc, "raw", str(exc)),
                        suggestion="禁止重复提交；请先查询 PBS 并核对该带电点。",
                        summary_en=f"Charge job {name} has an unknown state after qsub",
                        suggestion_en="Do not resubmit; query PBS and reconcile this charge point first.",
                    )
                    return ActionResult(
                        plan.action_id,
                        "recovery-required",
                        self.snapshot(),
                        error.summary,
                        ok=False,
                        job_ids=submitted_ids,
                        error=error,
                    )
                except _AcceptedReceiptUpdateError as exc:
                    self._hard_submission_receipt = replace(
                        intent,
                        status="ACCEPTED",
                        job_id=exc.job_id,
                        version=intent.version + 1,
                    )
                    error = ActionError(
                        step="reconcile-charge-submit",
                        summary=f"qsub 已接受带电任务 {name}，但 receipt 更新失败",
                        command="qsub",
                        raw=f"Job ID: {exc.job_id}; {exc.cause}",
                        suggestion="不要再次提交；人工核对并修复该 Job ID。",
                        summary_en=f"qsub accepted charge job {name}, but receipt persistence failed",
                        suggestion_en="Do not resubmit; reconcile this Job ID manually.",
                    )
                    return ActionResult(
                        plan.action_id,
                        "recovery-required",
                        self.snapshot(),
                        error.summary,
                        ok=False,
                        job_ids={**submitted_ids, name: exc.job_id},
                        error=error,
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    try:
                        current_receipt = read_submission_receipt(
                            self.workdir,
                            self.activity_state_root,
                        )
                    except (OSError, TypeError, ValueError):
                        current_receipt = None
                    accepted_id = (
                        current_receipt.job_id
                        if current_receipt is not None
                        and current_receipt.status == "ACCEPTED"
                        else ""
                    )
                    recovery_ids = dict(submitted_ids)
                    if accepted_id:
                        recovery_ids[name] = accepted_id
                    error = ActionError(
                        step="reconcile-charge-submit",
                        summary=(
                            f"qsub 已接受带电任务 {name}，但本地状态保存失败"
                            if accepted_id
                            else f"带电任务 {name} 提交未完成"
                        ),
                        command="qsub",
                        raw=str(exc),
                        suggestion="检查提交恢复屏障和 PBS 队列后再决定是否重试。",
                        summary_en=f"Charge job {name} submission did not complete",
                        suggestion_en="Inspect the recovery barrier and PBS queue before retrying.",
                    )
                    return ActionResult(
                        plan.action_id,
                        "recovery-required",
                        self.snapshot(),
                        error.summary,
                        ok=False,
                        job_ids=recovery_ids,
                        error=error,
                    )

                job_id = submitted[name]
                submitted_ids[name] = job_id
                try:
                    clear_submission_receipt(
                        self.workdir,
                        self.activity_state_root,
                        intent.owner_token,
                        expected_version=intent.version + 1,
                        expected_status="ACCEPTED",
                    )
                except OSError as exc:
                    error = ActionError(
                        step="reconcile-charge-submit",
                        summary=f"带电任务 {name} 已保存，但 receipt 清理失败",
                        command="qsub",
                        raw=f"Job ID: {job_id}; {exc}",
                        suggestion="不要重复提交；本地状态已包含该 Job ID。",
                        summary_en=f"Charge job {name} was saved, but receipt cleanup failed",
                        suggestion_en="Do not resubmit; local state already contains this Job ID.",
                    )
                    return ActionResult(
                        plan.action_id,
                        "recovery-required",
                        self.snapshot(),
                        error.summary,
                        ok=False,
                        job_ids=submitted_ids,
                        error=error,
                    )
            return ActionResult(
                plan.action_id,
                "submitted",
                self.snapshot(),
                f"已提交 {len(submitted_ids)} 个带电任务。",
                job_ids=submitted_ids,
            )

        if plan.action_id == "reset-queued":
            if not isinstance(payload, _ResetQueuedPayload):
                raise RuntimeError("排队任务重置计划载荷已失效，请重新预览。")
            if _case_identity(self.workdir) != payload.case_identity:
                raise RuntimeError("Case 身份已变化，请重新预览。")
            state_path = self.workdir / STATE_FILENAME
            if _fingerprint(self.workdir, state_path) != payload.state_fingerprint:
                raise RuntimeError("vaspsolkit.state.json 已变化，请重新预览。")
            state = WorkflowState.load(state_path)
            if _workflow_state_dict(state) != payload.state_before:
                raise RuntimeError("任务状态已变化，请重新预览。")
            scheduler = self.scheduler_factory(copy.deepcopy(payload.config.scheduler))
            reset = reset_queued_jobs(
                self.workdir,
                state,
                list(payload.selected),
                scheduler,
                confirmed=True,
                state_path=state_path,
            )
            return ActionResult(
                plan.action_id,
                "prepared",
                self.snapshot(),
                "旧任务已安全清理，所选带电点已回到 PREPARED；请按 S 预览新的 qsub。",
                job_ids=reset,
            )

        if plan.action_id == "postprocess":
            if not isinstance(payload, _PostprocessPayload):
                raise RuntimeError("后处理计划载荷已失效，请重新预览。")
            if _case_identity(self.workdir) != payload.case_identity:
                raise RuntimeError("Case 身份已变化，请重新预览。")
            if _fingerprint(self.workdir, payload.summary_path) != payload.summary_fingerprint:
                raise RuntimeError("summary.csv 已变化，请重新预览。")
            result = postprocess_versioned(
                payload.summary_path,
                payload.history_root,
                run_id=payload.run_id,
            )
            relative = result.run_dir.relative_to(self.workdir) if result.run_dir else Path("-")
            return ActionResult(
                plan.action_id,
                "completed",
                self.snapshot(),
                f"恒电势分析已保存：{relative}",
            )

        if plan.action_id in {"prepare-charge", "check-prepared"}:
            if not isinstance(payload, _WorkflowPayload) or payload.action != plan.action_id:
                raise RuntimeError("带电点工作流计划载荷已失效，请重新预览。")
            self._verify_workflow_payload(payload)
            if plan.action_id == "prepare-charge":
                state = prepare_charge_jobs(
                    self.workdir,
                    copy.deepcopy(payload.config),
                    strict=True,
                )
                message = f"已从中性 CONTCAR/CHGCAR 准备 {len(state.jobs)} 个带电点。"
            else:
                state = check_prepared_jobs(
                    self.workdir,
                    copy.deepcopy(payload.config),
                )
                message = f"{len(state.jobs)} 个带电点输入已通过提交前检查。"
            return ActionResult(
                plan.action_id,
                "completed",
                self.snapshot(),
                message,
            )

        if plan.action_id == "collect":
            if not isinstance(payload, _CollectPayload):
                raise RuntimeError("结果收集计划载荷已失效，请重新预览。")
            if _case_identity(self.workdir) != payload.case_identity:
                raise RuntimeError("Case 身份已变化，请重新预览。")
            for expected in payload.fingerprints:
                if _fingerprint(self.workdir, self.workdir / expected.relative) != expected:
                    raise RuntimeError(f"{expected.relative} 已变化，请重新预览。")
            payload.output_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(payload.output_path, payload.after)
            return ActionResult(
                plan.action_id,
                "completed",
                self.snapshot(),
                f"结果已收集：{payload.output_path.relative_to(self.workdir)}",
            )

        if plan.action_id == "repair-neutral-submit":
            if not isinstance(payload, _RecoveryPayload):
                raise RuntimeError("提交恢复计划载荷已失效，请重新预览。")
            self._verify_recovery_payload(payload)
            state_path = self.workdir / STATE_FILENAME
            state = _workflow_state_from_receipt(payload.receipt, state_path)
            metadata = dict(state.neutral.metadata) if state.neutral is not None else {}
            state.neutral = JobRecord(
                folder=".", status="SUBMITTED", job_id=payload.receipt.job_id,
                metadata=metadata,
            )
            state.stage = "neutral_submitted"
            state.save(state_path)
            clear_submission_receipt(
                self.workdir,
                self.activity_state_root,
                payload.receipt.owner_token,
                expected_version=payload.receipt.version,
                expected_status=payload.receipt.status,
            )
            self._hard_submission_receipt = None
            return ActionResult(
                plan.action_id,
                "recovered",
                self.snapshot(),
                f"已修复中性任务记录：{payload.receipt.job_id}",
                job_ids={"neutral": payload.receipt.job_id},
            )
        if plan.action_id in {"reconcile-neutral-submit", "clear-submit-barrier"}:
            if not isinstance(payload, _ReconcilePayload):
                raise RuntimeError("人工 reconcile 计划已失效，请重新预览。")
            self._verify_reconcile_payload(payload)
            if payload.accepted is not None:
                update_submission_receipt(
                    self.workdir, payload.accepted, payload.current.owner_token,
                    self.activity_state_root,
                    expected_version=payload.current.version,
                    expected_status=payload.current.status,
                )
                state_path = self.workdir / STATE_FILENAME
                state = _workflow_state_from_receipt(payload.accepted, state_path)
                metadata = dict(state.neutral.metadata) if state.neutral else {}
                state.neutral = JobRecord(
                    folder=".", status="SUBMITTED", job_id=payload.accepted.job_id,
                    metadata=metadata,
                )
                state.stage = "neutral_submitted"
                state.save(state_path)
                clear_submission_receipt(
                    self.workdir, self.activity_state_root, payload.current.owner_token,
                    expected_version=payload.accepted.version,
                    expected_status=payload.accepted.status,
                )
                self._record_reconcile_activity("record-job-id", payload.accepted.job_id)
                return ActionResult(
                    plan.action_id, "reconciled", self.snapshot(),
                    f"已录入并修复 Job ID：{payload.accepted.job_id}",
                    job_ids={"neutral": payload.accepted.job_id},
                )
            failed_receipt = replace(
                payload.current, status="FAILED", version=payload.current.version + 1
            )
            update_submission_receipt(
                self.workdir, failed_receipt,
                payload.current.owner_token, self.activity_state_root,
                expected_version=payload.current.version,
                expected_status=payload.current.status,
            )
            clear_submission_receipt(
                self.workdir, self.activity_state_root, payload.current.owner_token,
                expected_version=failed_receipt.version,
                expected_status=failed_receipt.status,
            )
            self._record_reconcile_activity("confirm-no-job", "")
            return ActionResult(
                plan.action_id, "reconciled", self.snapshot(),
                "已确认 PBS 未创建任务并解除提交屏障。",
            )
        raise NotImplementedError("该动作的执行路径尚未接线")

    def _activate(self, plan: ActionPlan, payload: Optional[Any]) -> None:
        self._active_plan = plan
        self._active_payload = payload

    def _plan_prepare_neutral(self) -> tuple[ActionPlan, _NeutralPayload]:
        config_path = self.config_path
        config_relative = self._config_relative()
        _require_case_file(self.workdir, config_relative)
        config = load_kit_config(config_path)
        self._validate_script_path(_resources_from_config(config))
        required = {
            name: _require_case_file(self.workdir, name)
            for name in ("POSCAR", "INCAR", "POTCAR", "KPOINTS", config.scheduler.script)
        }
        state_path = self.workdir / STATE_FILENAME
        state = WorkflowState.load(state_path) if state_path.exists() else WorkflowState()
        active = []
        if state.neutral and state.neutral.status in {"SUBMITTED", "QUEUED", "RUNNING", "UNKNOWN"}:
            active.append("neutral")
        active.extend(name for name, record in state.jobs.items() if record.status in {"SUBMITTED", "QUEUED", "RUNNING", "UNKNOWN"})
        if active:
            raise RuntimeError("active jobs are recorded; check or stop them before re-preparing neutral")
        archive_path = self.workdir / ".vaspsolkit" / "archive" / datetime.now().strftime("restart-%Y%m%d-%H%M%S-%f")
        watched_names = tuple(
            dict.fromkeys(
                (
                    "POSCAR", "INCAR", "POTCAR", "KPOINTS",
                    config.scheduler.script, config_relative, STATE_FILENAME,
                    ".vaspsolkit/provenance/POSCAR.initial",
                )
                + _ARCHIVE_NAMES
            )
        )
        fingerprints_by_name = {
            name: _fingerprint(self.workdir, self.workdir / name)
            for name in watched_names
        }
        watched = tuple(fingerprints_by_name[name] for name in watched_names)
        poscar_fingerprint = fingerprints_by_name["POSCAR"]
        poscar_digest = (
            poscar_fingerprint.resolved_target.digest
            if poscar_fingerprint.kind == "symlink"
            and poscar_fingerprint.resolved_target is not None
            else poscar_fingerprint.digest
        )
        poscar = required["POSCAR"].read_text(encoding="utf-8", errors="ignore")
        archive_relative = archive_path.relative_to(self.workdir).as_posix()
        metadata = {
            "stage": "neutral_relax",
            "profile": config.workflow.neutral_profile,
            "source_poscar_sha256": poscar_digest,
            "archive": archive_relative,
        }
        after_state = WorkflowState(
            stage="neutral_prepared",
            neutral=JobRecord(folder=".", status="PREPARED", metadata=metadata),
            prepared_checked=False,
        )
        state_after = json.dumps(
            {
                "stage": after_state.stage, "jobs": {},
                "neutral": asdict(after_state.neutral), "prepared_checked": False,
            }, indent=2, sort_keys=True,
        )
        diffs = [
            FileDiff(
                self.workdir / ".vaspsolkit" / "provenance" / "POSCAR.initial",
                _read_regular_text(self.workdir / ".vaspsolkit" / "provenance" / "POSCAR.initial"),
                poscar,
                "update" if (self.workdir / ".vaspsolkit" / "provenance" / "POSCAR.initial").exists() else "create",
            ),
            FileDiff(
                state_path, _read_regular_text(state_path), state_after,
                "update" if state_path.exists() else "create",
            ),
        ]
        archive_changes = []
        for name in _ARCHIVE_NAMES:
            path = self.workdir / name
            if path.exists() or path.is_symlink():
                archive_changes.append(
                    _archive_change_from_fingerprint(
                        path,
                        archive_path / path.name,
                        "move",
                        fingerprints_by_name[name],
                    )
                )
        if state_path.exists() or state_path.is_symlink():
            archive_changes.append(
                _archive_change_from_fingerprint(
                    state_path,
                    archive_path / state_path.name,
                    "copy",
                    fingerprints_by_name[STATE_FILENAME],
                )
            )
        payload = _NeutralPayload(copy.deepcopy(config), _case_identity(self.workdir), watched, archive_path)
        plan = ActionPlan(
            action_id="prepare-neutral", effect=ACTION_EFFECTS["prepare-neutral"],
            target_case=self.workdir, target_jobs=("neutral",),
            title=_ACTION_METADATA["prepare-neutral"][0], reason=_ACTION_METADATA["prepare-neutral"][1],
            file_diffs=tuple(diffs), archive_changes=tuple(archive_changes),
            warnings=("旧计算输出如存在将归档",),
        )
        return plan, payload

    def _plan_charge_workflow(self, action: str) -> tuple[ActionPlan, _WorkflowPayload]:
        config = load_kit_config(self.config_path)
        state_path = self.workdir / STATE_FILENAME
        state = WorkflowState.load(state_path) if state_path.is_file() else WorkflowState()
        blocked = []
        if state.neutral is None or state.neutral.status != "CONVERGED":
            blocked.append("中性结构优化尚未收敛")
        elif state.neutral.metadata.get("stage") != "neutral_relax":
            blocked.append("缺少中性结构优化来源记录")
        active = tuple(
            name
            for name, record in state.jobs.items()
            if record.status in {"SUBMITTED", "QUEUED", "RUNNING", "UNKNOWN"}
        )
        if action == "prepare-charge" and active:
            blocked.append("仍有活动带电任务: " + ", ".join(active))
        if action == "check-prepared" and not state.jobs:
            blocked.append("尚未准备任何带电点目录")

        watched = [self.config_path, state_path]
        if action == "prepare-charge":
            watched.extend(
                self.workdir / name
                for name in (
                    "INCAR", "POTCAR", "KPOINTS", config.scheduler.script,
                    "OUTCAR", "CONTCAR", "CHGCAR", "LOCPOT",
                    config.workflow.job_root,
                )
            )
            targets = tuple(
                name
                for name, offset in zip(
                    config.workflow.folders,
                    config.workflow.nelect_offsets,
                )
                if config.workflow.charge_points_include_neutral or abs(offset) > 1.0e-12
            )
            warnings = (
                "每个带电点将复制中性 CONTCAR→POSCAR 和中性 CHGCAR；不会复制 WAVECAR。",
                "INCAR 仅设置 ISTART=0、ICHARG=1 和该点 NELECT，其余用户参数保持不变。",
                f"既有 {config.workflow.job_root} 如存在将按工作流规则归档。",
            )
        else:
            targets = tuple(state.jobs)
            for record in state.jobs.values():
                folder = self.workdir / record.folder
                watched.extend(
                    folder / name
                    for name in (
                        "POSCAR", "INCAR", "POTCAR", "KPOINTS", "CHGCAR",
                        config.scheduler.script,
                    )
                )
            watched.extend((self.workdir / "CONTCAR", self.workdir / "CHGCAR"))
            warnings = (
                "检查通过后会把 prepared_checked 写入状态文件；不会提交任务。",
            )

        fingerprints = tuple(
            _fingerprint(self.workdir, path) for path in dict.fromkeys(watched)
        )
        payload = _WorkflowPayload(
            action=action,
            config=copy.deepcopy(config),
            case_identity=_case_identity(self.workdir),
            fingerprints=fingerprints,
        )
        plan = ActionPlan(
            action_id=action,
            effect=ACTION_EFFECTS[action],
            target_case=self.workdir,
            target_jobs=targets,
            title=_ACTION_METADATA[action][0],
            reason=_ACTION_METADATA[action][1],
            commands_summary=(("prepare-charge × 1",) if action == "prepare-charge" else ("check-prepared × 1",)),
            warnings=warnings,
            blocked_reason="；".join(blocked),
        )
        return plan, payload

    def _verify_workflow_payload(self, payload: _WorkflowPayload) -> None:
        if _case_identity(self.workdir) != payload.case_identity:
            raise RuntimeError("Case 身份已变化，请重新预览。")
        for expected in payload.fingerprints:
            if _fingerprint(self.workdir, self.workdir / expected.relative) != expected:
                raise RuntimeError(f"{expected.relative} 已变化，请重新预览。")

    def _plan_collect(self) -> tuple[ActionPlan, _CollectPayload]:
        config = load_kit_config(self.config_path)
        state_path = self.workdir / STATE_FILENAME
        state = WorkflowState.load(state_path)
        blocked = []
        if not state.jobs:
            blocked.append("没有可收集的带电点任务")
        not_converged = tuple(
            name for name, record in state.jobs.items() if record.status != "CONVERGED"
        )
        if not_converged:
            blocked.append("以下任务尚未收敛: " + ", ".join(not_converged))
        output_path = result_file_path(
            self.workdir,
            config.workflow,
            config.workflow.summary_file,
        ).resolve()
        try:
            output_path.relative_to(self.workdir)
        except ValueError:
            blocked.append("summary 输出路径位于 Case 之外")
            output_path = self.workdir / "results" / "summary.csv"

        watched = [
            self.config_path,
            state_path,
            self.workdir / "OUTCAR",
            self.workdir / "LOCPOT",
            output_path.parent,
            output_path,
        ]
        for record in state.jobs.values():
            folder = self.workdir / record.folder
            watched.extend(folder / name for name in ("INCAR", "OUTCAR", "LOCPOT"))
        fingerprints = tuple(
            _fingerprint(self.workdir, path) for path in dict.fromkeys(watched)
        )
        after = ""
        if not blocked:
            try:
                with tempfile.TemporaryDirectory(prefix="vaspsolkit-collect-") as temporary:
                    preview_path = Path(temporary) / "summary.csv"
                    collect_results(self.workdir, config.workflow, output=preview_path)
                    after = preview_path.read_text(encoding="utf-8")
            except (OSError, RuntimeError, ValueError) as exc:
                blocked.append(f"结果解析失败: {exc}")
        before = _read_regular_text(output_path)
        payload = _CollectPayload(
            config=copy.deepcopy(config),
            output_path=output_path,
            after=after,
            case_identity=_case_identity(self.workdir),
            fingerprints=fingerprints,
        )
        plan = ActionPlan(
            action_id="collect",
            effect=ACTION_EFFECTS["collect"],
            target_case=self.workdir,
            target_jobs=tuple(state.jobs),
            title=_ACTION_METADATA["collect"][0],
            reason=_ACTION_METADATA["collect"][1],
            file_diffs=(
                FileDiff(
                    output_path,
                    before,
                    after,
                    "update" if before is not None else "create",
                ),
            ) if after else (),
            commands_summary=("collect × 1",),
            blocked_reason="；".join(blocked),
        )
        return plan, payload

    def _verify_neutral_payload(self, payload: _NeutralPayload) -> None:
        if _case_identity(self.workdir) != payload.case_identity:
            raise RuntimeError("Case 身份已变化，请重新预览。")
        for expected in payload.fingerprints:
            current = _fingerprint(self.workdir, self.workdir / expected.relative)
            if current != expected:
                raise RuntimeError(f"{expected.relative} 已变化，请重新预览。")
        if payload.archive_path.exists():
            raise RuntimeError("归档目标已变化，请重新预览。")
        for target in (
            self.workdir / ".vaspsolkit" / "provenance" / "POSCAR.initial",
            payload.archive_path,
        ):
            try:
                target.parent.resolve().relative_to(self.workdir)
            except (OSError, RuntimeError, ValueError) as exc:
                raise RuntimeError("写入目标已变化，请重新预览。") from exc

    def _verify_config_payload(self, payload: _ConfigPayload) -> None:
        if _case_identity(self.workdir) != payload.case_identity:
            raise RuntimeError("Case 身份已变化，请重新预览。")
        if _fingerprint(self.workdir, self.config_path) != payload.fingerprint:
            raise RuntimeError("vaspsolkit.json 已变化，请重新预览。")

    def _verify_submit_payload(self, payload: _SubmitPayload) -> None:
        if _case_identity(self.workdir) != payload.case_identity:
            raise RuntimeError("Case 身份已变化，请重新预览。")
        receipt, receipt_error = self._submission_barrier()
        if receipt is not None:
            detail = f"Job {receipt.job_id} 已接受" if receipt.job_id else receipt.status
            raise RuntimeError(f"存在提交恢复屏障（{detail}）；禁止再次提交。")
        if receipt_error:
            raise RuntimeError("提交恢复屏障不可读；禁止再次提交。")
        self._verify_submit_sources(payload, persisted=False)

    def _verify_charge_submit_payload(self, payload: _ChargeSubmitPayload) -> None:
        if _case_identity(self.workdir) != payload.case_identity:
            raise RuntimeError("Case 身份已变化，请重新预览。")
        receipt, receipt_error = self._submission_barrier()
        if receipt is not None or receipt_error:
            raise RuntimeError("存在提交恢复屏障；禁止新的带电任务提交。")
        for expected in payload.fingerprints:
            if _fingerprint(self.workdir, self.workdir / expected.relative) != expected:
                raise RuntimeError(f"{expected.relative} 已变化，请重新预览。")
        state = WorkflowState.load(self.workdir / STATE_FILENAME)
        if _workflow_state_dict(state) != payload.state_before:
            raise RuntimeError("vaspsolkit.state.json 已变化，请重新预览。")
        if not state.prepared_checked:
            raise RuntimeError("带电点输入尚未通过 check-prepared。")
        changed = tuple(
            name
            for name in payload.selected
            if name not in state.jobs or state.jobs[name].status != "PREPARED"
        )
        if changed:
            raise RuntimeError("所选带电任务状态已变化: " + ", ".join(changed))

    def _verify_submit_sources(self, payload: _SubmitPayload, *, persisted: bool) -> None:
        for expected in payload.fingerprints:
            if expected.relative == self._config_relative() and payload.config_after and persisted:
                if self.config_path.read_text(encoding="utf-8") != payload.config_after:
                    raise RuntimeError("vaspsolkit.json 已变化，请重新预览。")
                continue
            if _fingerprint(self.workdir, self.workdir / expected.relative) != expected:
                raise RuntimeError(f"{expected.relative} 已变化，请重新预览。")
        state = WorkflowState.load(self.workdir / STATE_FILENAME)
        if state.neutral is None or state.neutral.status != "PREPARED":
            raise RuntimeError("中性任务状态已变化，请重新预览。")

    def _plan_submission_recovery(self) -> tuple[ActionPlan, _RecoveryPayload]:
        receipt, receipt_error = self._submission_barrier()
        if receipt is None:
            raise RuntimeError(receipt_error or "没有需要修复的中性提交记录。")
        if receipt.status != "ACCEPTED" or not receipt.job_id:
            raise RuntimeError("提交状态未知；必须先人工查询 PBS 并录入 Job ID。")
        identity = _case_identity(self.workdir)
        if (receipt.case_device, receipt.case_inode) != identity:
            raise RuntimeError("提交恢复记录属于不同的 Case 身份。")
        current_info = self.workdir.stat()
        if receipt.case_mode and receipt.case_mode != stat.S_IFMT(current_info.st_mode):
            raise RuntimeError("提交恢复记录的 Case 类型已变化。")
        if receipt.case_path != str(self.workdir):
            raise RuntimeError("提交恢复记录属于不同的 Case 路径。")
        state_path = self.workdir / STATE_FILENAME
        _require_safe_state_target(self.workdir, state_path)
        state = _workflow_state_from_receipt(receipt, state_path)
        metadata = dict(state.neutral.metadata) if state.neutral is not None else {}
        repaired = WorkflowState(
            stage="neutral_submitted",
            neutral=JobRecord(
                folder=".", status="SUBMITTED", job_id=receipt.job_id,
                metadata=metadata,
            ),
            jobs=state.jobs,
            prepared_checked=state.prepared_checked,
        )
        after = json.dumps(
            {
                "stage": repaired.stage,
                "jobs": {name: asdict(record) for name, record in repaired.jobs.items()},
                "neutral": asdict(repaired.neutral),
                "prepared_checked": repaired.prepared_checked,
            },
            indent=2,
            sort_keys=True,
        )
        plan = ActionPlan(
            action_id="repair-neutral-submit",
            effect=ACTION_EFFECTS["repair-neutral-submit"],
            target_case=self.workdir,
            target_jobs=("neutral",),
            title=_ACTION_METADATA["repair-neutral-submit"][0],
            reason=_ACTION_METADATA["repair-neutral-submit"][1],
            file_diffs=(
                FileDiff(
                    state_path,
                    state_path.read_text(encoding="utf-8"),
                    after,
                    "update",
                ),
            ),
            commands_summary=("repair state only (no qsub)",),
        )
        return plan, _RecoveryPayload(
            receipt=receipt,
            case_identity=identity,
            state_fingerprint=_fingerprint(self.workdir, state_path),
        )

    def _verify_recovery_payload(self, payload: _RecoveryPayload) -> None:
        if _case_identity(self.workdir) != payload.case_identity:
            raise RuntimeError("Case 身份已变化，请重新预览。")
        _require_safe_state_target(self.workdir, self.workdir / STATE_FILENAME)
        current, error = self._submission_barrier()
        if error or current != payload.receipt:
            raise RuntimeError("提交恢复记录已变化，请重新预览。")
        if _fingerprint(self.workdir, self.workdir / STATE_FILENAME) != payload.state_fingerprint:
            raise RuntimeError("vaspsolkit.state.json 已变化，请重新预览。")

    def _submission_barrier(self) -> tuple[Optional[SubmissionReceipt], str]:
        if self._hard_submission_receipt is not None:
            return self._hard_submission_receipt, ""
        try:
            return read_submission_receipt(
                self.workdir, self.activity_state_root
            ), ""
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            path = submission_receipt_path(self.workdir, self.activity_state_root)
            if path.exists():
                return None, str(exc)
            return None, ""

    def _clear_failed_submission_intent(self, intent: SubmissionReceipt) -> str:
        try:
            failed_receipt = replace(
                intent, status="FAILED", version=intent.version + 1
            )
            update_submission_receipt(
                self.workdir,
                failed_receipt,
                intent.owner_token,
                self.activity_state_root,
                expected_version=intent.version,
                expected_status=intent.status,
            )
            clear_submission_receipt(
                self.workdir, self.activity_state_root, intent.owner_token,
                expected_version=failed_receipt.version,
                expected_status=failed_receipt.status,
            )
            return ""
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._hard_submission_receipt = intent
            return str(exc)

    def _reconcile_plan(self, accepted: SubmissionReceipt, state_path: Path) -> ActionPlan:
        _require_safe_state_target(self.workdir, state_path)
        state = _workflow_state_from_receipt(accepted, state_path)
        metadata = dict(state.neutral.metadata) if state.neutral else {}
        state.neutral = JobRecord(
            folder=".", status="SUBMITTED", job_id=accepted.job_id, metadata=metadata
        )
        state.stage = "neutral_submitted"
        after = json.dumps(_workflow_state_dict(state), indent=2, sort_keys=True)
        return ActionPlan(
            action_id="reconcile-neutral-submit",
            effect=ACTION_EFFECTS["reconcile-neutral-submit"],
            target_case=self.workdir,
            target_jobs=("neutral",),
            title="录入已查询到的 PBS Job ID",
            reason="只修复本地状态，不会调用 qsub。",
            file_diffs=(FileDiff(state_path, _read_regular_text(state_path), after, "update"),),
            commands_summary=("record Job ID only (no qsub)",),
        )

    def _verify_reconcile_payload(self, payload: _ReconcilePayload) -> None:
        if _case_identity(self.workdir) != payload.case_identity:
            raise RuntimeError("Case 身份已变化，请重新预览。")
        self._verify_receipt_case_identity(payload.current)
        current, error = self._submission_barrier()
        if error or current != payload.current:
            raise RuntimeError("提交恢复屏障已变化，请重新预览。")
        assert current is not None
        self._verify_receipt_case_identity(current)
        if _fingerprint(self.workdir, self.workdir / STATE_FILENAME) != payload.state_fingerprint:
            raise RuntimeError("vaspsolkit.state.json 已变化，请重新预览。")

    def _verify_receipt_case_identity(self, receipt: SubmissionReceipt) -> None:
        try:
            entry = self.workdir.lstat()
            target = self.workdir.stat()
            canonical = str(self.workdir.resolve(strict=True))
        except OSError as exc:
            raise RuntimeError("Case 身份无法验证。") from exc
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
            raise RuntimeError("Case 路径必须是非符号链接目录。")
        if receipt.case_path != canonical:
            raise RuntimeError("提交恢复记录属于不同的 Case 路径。")
        if (receipt.case_device, receipt.case_inode) != (target.st_dev, target.st_ino):
            raise RuntimeError("提交恢复记录属于不同的 Case inode。")
        if receipt.case_mode and receipt.case_mode != stat.S_IFMT(target.st_mode):
            raise RuntimeError("提交恢复记录属于不同的 Case 类型。")

    def _record_reconcile_activity(self, action: str, job_id: str) -> None:
        append_activity(
            self.workdir,
            ActivityRecord(
                timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
                action=action,
                target="neutral",
                result="reconciled",
                new_job_id=job_id,
                message="manual PBS reconciliation",
            ),
            self.activity_state_root,
        )

    def _validate_script_path(self, resources: ResourceRequest) -> None:
        try:
            script = (self.workdir / resources.script).resolve(strict=True)
            script.relative_to(self.workdir)
            if not script.is_file():
                raise ValueError
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("scheduler script must remain within the Case") from exc

    def _config_relative(self) -> str:
        try:
            relative = self.config_path.relative_to(self.workdir).as_posix()
        except ValueError as exc:
            raise ValueError(
                "file-changing workbench actions require config within the Case"
            ) from exc
        if relative in {"", "."}:
            raise ValueError("config path must name a file within the Case")
        return relative


def _resources_from_config(config: KitConfig) -> ResourceRequest:
    return ResourceRequest.create(
        allocation="specified" if config.scheduler.nodes else "auto",
        nodes=tuple(config.scheduler.nodes), cores=config.scheduler.cores,
        queue=config.scheduler.queue, walltime=config.scheduler.walltime,
        script=config.scheduler.script,
    )


def _config_with_resources(config: KitConfig, resources: ResourceRequest) -> KitConfig:
    result = copy.deepcopy(config)
    result.scheduler.nodes = list(resources.nodes)
    result.scheduler.cores = resources.cores
    result.scheduler.queue = resources.queue
    result.scheduler.walltime = resources.walltime
    result.scheduler.script = resources.script
    result.workflow.qsub_ppn = resources.cores
    result.workflow.qsub_queue = resources.queue
    result.workflow.qsub_walltime = resources.walltime
    result.workflow.pbs_file = resources.script
    result.validate()
    return result


def _workflow_state_dict(state: WorkflowState) -> dict:
    return {
        "stage": state.stage,
        "jobs": {name: asdict(record) for name, record in state.jobs.items()},
        "neutral": asdict(state.neutral) if state.neutral is not None else None,
        "prepared_checked": state.prepared_checked,
    }


def _workflow_state_from_receipt(
    receipt: SubmissionReceipt, state_path: Path
) -> WorkflowState:
    data = receipt.state_before
    if data:
        neutral_data = data.get("neutral")
        return WorkflowState(
            stage=str(data.get("stage", "neutral_prepared")),
            jobs={
                str(name): JobRecord(**dict(record))
                for name, record in dict(data.get("jobs", {})).items()
            },
            neutral=JobRecord(**dict(neutral_data)) if neutral_data else None,
            prepared_checked=bool(data.get("prepared_checked", False)),
        )
    return WorkflowState.load(state_path)


def _submission_intent(
    workdir: Path,
    payload: _SubmitPayload,
    resources: Optional[ResourceRequest],
) -> SubmissionReceipt:
    if resources is None:
        raise RuntimeError("submission resources are missing")
    script_entry = next(
        item for item in payload.fingerprints if item.relative == resources.script
    )
    script_fingerprint = (
        script_entry.resolved_target.digest
        if script_entry.resolved_target is not None
        else script_entry.digest
    )
    plan_data = {
        "case_identity": payload.case_identity,
        "resources": asdict(resources),
        "inputs": [
            {
                "relative": item.relative,
                "kind": item.kind,
                "digest": item.digest,
                "modified_ns": item.modified_ns,
                "changed_ns": item.changed_ns,
            }
            for item in payload.fingerprints
        ],
    }
    plan_fingerprint = hashlib.sha256(
        json.dumps(plan_data, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return SubmissionReceipt(
        case_path=str(workdir),
        case_device=payload.case_identity[0],
        case_inode=payload.case_identity[1],
        case_mode=stat.S_IFMT(workdir.stat().st_mode),
        job_id="",
        command="qsub",
        resources=asdict(resources),
        timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
        state_before=payload.state_before,
        plan_fingerprint=plan_fingerprint,
        script_fingerprint=script_fingerprint,
        owner_token=new_submission_owner_token(),
        status="SUBMITTING",
    )


def _charge_submission_intent(
    workdir: Path,
    payload: _ChargeSubmitPayload,
    resources: Optional[ResourceRequest],
    job_name: str,
) -> SubmissionReceipt:
    if resources is None:
        raise RuntimeError("submission resources are missing")
    record = dict(payload.state_before.get("jobs", {})).get(job_name)
    if not isinstance(record, dict) or not record.get("folder"):
        raise RuntimeError(f"charge job {job_name} is missing from the preview state")
    script_relative = (
        Path(str(record["folder"])) / resources.script
    ).as_posix()
    script_entry = next(
        (
            item
            for item in payload.fingerprints
            if item.relative == script_relative
        ),
        None,
    )
    if script_entry is None:
        raise RuntimeError(f"{script_relative} was not captured by the preview")
    script_fingerprint = (
        script_entry.resolved_target.digest
        if script_entry.resolved_target is not None
        else script_entry.digest
    )
    resource_payload = asdict(resources)
    resource_payload["target_job"] = job_name
    resource_payload["target_folder"] = str(record["folder"])
    plan_data = {
        "case_identity": payload.case_identity,
        "target_job": job_name,
        "resources": resource_payload,
        "inputs": [
            {
                "relative": item.relative,
                "kind": item.kind,
                "digest": item.digest,
                "modified_ns": item.modified_ns,
                "changed_ns": item.changed_ns,
            }
            for item in payload.fingerprints
            if item.relative.startswith(str(record["folder"]) + "/")
        ],
    }
    return SubmissionReceipt(
        case_path=str(workdir),
        case_device=payload.case_identity[0],
        case_inode=payload.case_identity[1],
        case_mode=stat.S_IFMT(workdir.stat().st_mode),
        job_id="",
        command="qsub",
        resources=resource_payload,
        timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
        state_before=payload.state_before,
        plan_fingerprint=hashlib.sha256(
            json.dumps(plan_data, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        script_fingerprint=script_fingerprint,
        owner_token=new_submission_owner_token(),
        status="SUBMITTING",
    )


class _AcceptedReceiptUpdateError(RuntimeError):
    def __init__(self, job_id: str, cause: BaseException) -> None:
        self.job_id = job_id
        self.cause = cause
        super().__init__(f"job {job_id} accepted but receipt update failed: {cause}")


class _MalformedQsubOutputError(RuntimeError):
    def __init__(self, raw: str) -> None:
        self.raw = raw
        super().__init__(f"qsub returned malformed output: {raw!r}")


class _QsubAttemptError(RuntimeError):
    def __init__(self, cause: BaseException) -> None:
        self.cause = cause
        self.raw = str(cause)
        super().__init__(f"qsub attempt raised: {cause}")


class _ValidatedPBSScheduler(PBSScheduler):
    """Preserve PBS keyword overrides while rejecting non-job qsub output."""

    _JOB_ID = re.compile(r"^\d+(?:\[\d+\])?(?:\.[A-Za-z0-9._-]+)?$")

    def __init__(self, delegate: Any, on_accepted=None, *, strict_job_id: bool = True) -> None:
        self.delegate = delegate
        self.on_accepted = on_accepted
        self.strict_job_id = strict_job_id

    def submit(self, workdir: Path, pbs_file: str, **kwargs: Any) -> str:
        try:
            job_id = self.delegate.submit(workdir, pbs_file, **kwargs)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            raise _QsubAttemptError(exc) from exc
        valid = (
            isinstance(job_id, str)
            and bool(job_id.strip())
            and (
                self._JOB_ID.fullmatch(job_id.strip()) is not None
                if self.strict_job_id else not any(char.isspace() for char in job_id.strip())
            )
        )
        if not valid:
            raise _MalformedQsubOutputError(str(job_id))
        job_id = job_id.strip()
        if self.on_accepted is not None:
            try:
                self.on_accepted(job_id)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise _AcceptedReceiptUpdateError(job_id, exc) from exc
        return job_id


def _case_identity(case: Path) -> Tuple[int, int]:
    info = case.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(str(case))
    return info.st_dev, info.st_ino


def _require_safe_state_target(case: Path, path: Path) -> None:
    case = case.resolve(strict=True)
    if path.parent.resolve(strict=True) != case:
        raise RuntimeError("state path must remain directly inside the Case")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("state target must be a regular non-symlink file")


def _fingerprint(
    case: Path,
    path: Path,
    visited: Optional[frozenset[tuple[int, int, int]]] = None,
) -> _EntryFingerprint:
    visited = frozenset() if visited is None else visited
    relative = path.relative_to(case).as_posix()
    try:
        info = path.lstat()
    except FileNotFoundError:
        return _EntryFingerprint(relative, "absent")
    common = dict(mode=info.st_mode, size=info.st_size, modified_ns=info.st_mtime_ns, changed_ns=info.st_ctime_ns)
    if stat.S_ISLNK(info.st_mode):
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(case)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(
                f"watched symlink target must remain within the Case: {relative}"
            ) from exc
        target_info = resolved.stat()
        target_common = dict(
            mode=target_info.st_mode,
            size=target_info.st_size,
            modified_ns=target_info.st_mtime_ns,
            changed_ns=target_info.st_ctime_ns,
        )
        target_relative = resolved.relative_to(case).as_posix()
        if stat.S_ISREG(target_info.st_mode):
            target = _EntryFingerprint(
                target_relative,
                "file",
                digest=_sha256_file(resolved),
                **target_common,
            )
        elif stat.S_ISDIR(target_info.st_mode):
            target = _EntryFingerprint(
                target_relative,
                "directory-target",
                **target_common,
            )
        else:
            raise ValueError(
                f"watched symlink target must be a file or directory: {relative}"
            )
        link_target = os.readlink(path)
        return _EntryFingerprint(
            relative,
            "symlink",
            digest=hashlib.sha256(
                link_target.encode("utf-8", errors="surrogateescape")
            ).hexdigest(),
            link_target=link_target,
            resolved_target=target,
            **common,
        )
    if stat.S_ISREG(info.st_mode):
        return _EntryFingerprint(relative, "file", digest=_sha256_file(path), **common)
    if stat.S_ISDIR(info.st_mode):
        identity = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
        if identity in visited:
            raise ValueError(f"directory fingerprint cycle detected: {relative}")
        next_visited = visited | {identity}
        children = tuple(
            _fingerprint(case, child, next_visited)
            for child in sorted(path.iterdir(), key=lambda item: item.name)
        )
        return _EntryFingerprint(relative, "directory", children=children, **common)
    return _EntryFingerprint(relative, "other", **common)


def _read_regular_text(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"planned target is not a safe regular file: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def _require_case_file(case: Path, relative: str) -> Path:
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts:
        raise ValueError(f"required file must remain within the Case: {relative}")
    try:
        path = (case / raw).resolve(strict=True)
        path.relative_to(case)
    except FileNotFoundError:
        raise FileNotFoundError(f"required file is missing or empty: {case / raw}") from None
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"required file must remain within the Case: {relative}") from exc
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"required file is missing or empty: {case / raw}")
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_change_from_fingerprint(
    source: Path,
    destination: Path,
    operation: str,
    fingerprint: _EntryFingerprint,
) -> ArchiveChange:
    if fingerprint.kind == "file":
        size, digest = fingerprint.size, fingerprint.digest
    elif fingerprint.kind == "symlink":
        size = len(fingerprint.link_target.encode("utf-8", errors="surrogateescape"))
        digest = fingerprint.digest
    elif fingerprint.kind == "directory":
        size, digest = _directory_fingerprint_stats(fingerprint)
    else:
        raise ValueError(f"unsupported archive entry: {source}")
    return ArchiveChange(
        source, destination, operation, fingerprint.kind, size, digest
    )


def _directory_fingerprint_stats(root: _EntryFingerprint) -> tuple[int, str]:
    digest = hashlib.sha256()
    total_size = 0
    stack = deque(root.children)
    while stack:
        entry = stack.popleft()
        relative = entry.relative.removeprefix(root.relative + "/")
        if entry.kind == "file":
            payload = bytes.fromhex(entry.digest)
            total_size += entry.size
        elif entry.kind == "symlink":
            payload = entry.link_target.encode("utf-8", errors="surrogateescape")
        elif entry.kind == "directory":
            payload = b""
            stack.extendleft(reversed(entry.children))
        else:
            raise ValueError(f"unsupported archive directory entry: {entry.relative}")
        digest.update(entry.kind.encode("ascii") + b"\0")
        digest.update(relative.encode("utf-8", errors="surrogateescape") + b"\0")
        digest.update(payload + b"\0")
    return total_size, digest.hexdigest()


def _atomic_write_text(
    path: Path,
    value: str,
    *,
    expected_config: Any = NO_EXPECTATION,
) -> None:
    if expected_config is not NO_EXPECTATION:
        write_config_bytes(
            path,
            value.encode("utf-8"),
            expected_current=expected_config,
        )
        return
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_and_fsync(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _replace_and_fsync(temp: Path, path: Path) -> None:
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
