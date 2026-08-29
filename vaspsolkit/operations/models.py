"""Immutable presentation models for the Textual workbench."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


@dataclass(frozen=True)
class NavigationItem:
    key: str
    label_zh: str
    label_en: str
    shortcut: str


@dataclass(frozen=True)
class WorkflowStep:
    key: str
    label: str
    state: str


@dataclass(frozen=True)
class JobView:
    name: str
    kind: str
    status: str
    recorded_status: str
    job_id: str
    folder: Path
    scheduler_state: Optional[str] = None
    diagnostics: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SchedulerView:
    kind: str
    partition: str
    tasks: int
    nodes: Tuple[str, ...]
    script: str
    walltime: str
    resource_syntax: str = "unmanaged"
    last_refresh: str = ""
    refresh_error: Optional[str] = None


@dataclass(frozen=True)
class RecommendationView:
    name: str
    title: str
    reason: str
    effect: str
    cli_command: Optional[str]
    selectable_jobs: Tuple[str, ...]


@dataclass(frozen=True)
class InputCheckRow:
    name: str
    path: Path
    exists: bool
    status: str = ""
    required: bool = True
    summary: str = ""
    role: str = ""


@dataclass(frozen=True)
class ResultRow:
    name: str
    path: str
    exists: bool
    status: str = ""

    @property
    def value(self) -> str:
        return "存在" if self.exists else "尚未生成"

    @property
    def quality(self) -> str:
        return "AVAILABLE" if self.exists else "INCOMPLETE"

    @property
    def demo(self) -> bool:
        """Compatibility with the stage-A result table."""
        return False


@dataclass(frozen=True)
class NeutralOutputView:
    contcar_status: str
    chgcar_status: str
    outcar_status: str
    toten: Optional[float] = None
    efermi: Optional[float] = None
    diagnostic: str = ""
    outcar_size: int = 0
    scanned_bytes: int = 0


@dataclass(frozen=True)
class WorkbenchSnapshot:
    workdir: Path
    system_text: str
    navigation: Tuple[NavigationItem, ...]
    workflow_steps: Tuple[WorkflowStep, ...]
    neutral: JobView
    charge_jobs: Tuple[JobView, ...]
    queue_rows: Tuple[JobView, ...]
    scheduler: SchedulerView
    recommendation: RecommendationView
    input_rows: Tuple[InputCheckRow, ...]
    result_rows: Tuple[ResultRow, ...]
    neutral_output: NeutralOutputView
    stage: str
    analysis_runs: Tuple[ResultRow, ...] = ()
    warning_count: int = 0
    error_count: int = 0


# Temporary stage-A compatibility models. They contain case-derived rows only.
@dataclass(frozen=True)
class ChargeRow:
    name: str
    offset: float
    nelect: str
    status: str
    job_id: str = "-"
    node: str = "-"
    convergence: str = "-"
    demo: bool = False


@dataclass(frozen=True)
class QueueRow:
    name: str
    status: str
    job_id: str
    node: str
    scope: str
    demo: bool = False


@dataclass(frozen=True)
class ActivityEntry:
    time: str
    message: str
    level: str = "info"
    demo: bool = False


@dataclass(frozen=True)
class WorkbenchModel:
    workdir: Path
    system_text: str
    scheduler_text: str
    node_text: str
    current_stage: str
    recommendation_title: str
    recommendation_reason: str
    neutral_status: str
    charge_summary: str
    warning_count: int
    error_count: int
    navigation: Tuple[NavigationItem, ...]
    workflow_steps: Tuple[WorkflowStep, ...]
    charge_rows: Tuple[ChargeRow, ...]
    case_queue_rows: Tuple[QueueRow, ...]
    node_rows: Tuple[QueueRow, ...]
    global_queue_rows: Tuple[QueueRow, ...]
    queue_tabs: Tuple[str, ...]
    result_rows: Tuple[ResultRow, ...]
    activities: Tuple[ActivityEntry, ...]
    input_details: Tuple[str, ...]
    read_only_prototype: bool = True
    external_commands_enabled: bool = False


def build_workbench_model(
    workdir: Path, snapshot: Optional[WorkbenchSnapshot] = None
) -> WorkbenchModel:
    """Adapt a real snapshot to the temporary stage-A screen interface."""
    from ..inputs import plan_neutral_vaspsol_update
    from .i18n import tr
    from .snapshot import build_workbench_snapshot

    snapshot = snapshot or build_workbench_snapshot(workdir)
    input_details = [
        f"{row.name}: {'存在' if row.exists else '缺少'}"
        for row in snapshot.input_rows
        if row.name in {"POSCAR", "INCAR", "KPOINTS", "POTCAR"}
    ]
    warning_count = snapshot.warning_count
    incar = next(row for row in snapshot.input_rows if row.name == "INCAR")
    if incar.exists:
        try:
            incar_text = incar.path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            warning_count += 1
            input_details.append("INCAR: 无法读取；请检查权限或文件状态")
        else:
            update = plan_neutral_vaspsol_update(incar_text)
            warning_count += len(update.additions) + len(update.duplicates) + len(update.conflicts)
            input_details.extend(f"建议补充 {key} = {value}" for key, value in update.additions)
            input_details.extend(f"重复参数 {key}" for key in update.duplicates)
            input_details.extend(
                f"冲突 {key}: {current} -> {required}"
                for key, current, required in update.conflicts
            )
    legacy_navigation = tuple(
        NavigationItem(key, tr("zh", f"nav.{key}"), tr("en", f"nav.{key}"), str(index))
        for index, key in enumerate(
            ("overview", "inputs", "neutral", "charges", "queue", "results", "settings"), 1
        )
    )
    charge_rows = tuple(
        ChargeRow(job.name, 0.0, "-", job.status, job.job_id or "-")
        for job in snapshot.charge_jobs
    )
    queue_rows = tuple(
        QueueRow(job.name, job.status, job.job_id or "-", "-", "case")
        for job in snapshot.queue_rows
    )
    converged = sum(job.status == "CONVERGED" for job in snapshot.charge_jobs)
    scheduler = snapshot.scheduler
    scheduler_text = (
        "调度器未配置"
        if scheduler.kind == "unknown"
        else f"{scheduler.kind.upper()} · {scheduler.partition or '集群默认'} · {scheduler.tasks} 核"
    )
    return WorkbenchModel(
        workdir=snapshot.workdir,
        system_text=snapshot.system_text,
        scheduler_text=scheduler_text,
        node_text=", ".join(scheduler.nodes) if scheduler.nodes else "自动分配",
        current_stage=snapshot.stage,
        recommendation_title=snapshot.recommendation.title,
        recommendation_reason=snapshot.recommendation.reason,
        neutral_status=snapshot.neutral.status,
        charge_summary=f"{converged}/{len(snapshot.charge_jobs)}",
        warning_count=warning_count,
        error_count=snapshot.error_count,
        navigation=legacy_navigation,
        workflow_steps=snapshot.workflow_steps,
        charge_rows=charge_rows,
        case_queue_rows=queue_rows,
        node_rows=(),
        global_queue_rows=(),
        queue_tabs=("case", "nodes", "global"),
        result_rows=snapshot.result_rows,
        activities=(
            ActivityEntry("刚刚", "只读加载当前 Case"),
            ActivityEntry("--", "未请求调度器刷新"),
            ActivityEntry("--", "所有外部操作均已禁用"),
        ),
        input_details=tuple(input_details),
    )
