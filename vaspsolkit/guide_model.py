"""Read-only beginner guide model for VASPsolKit.

This module never submits jobs and never writes calculation files.  It turns the
current on-disk case state into one user-facing recommended action.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .tui_model import BASE_INPUTS, CaseSnapshot, action_effect, inspect_case


@dataclass(frozen=True)
class GuideSnapshot:
    case: CaseSnapshot
    missing_base_inputs: Tuple[str, ...]
    has_config: bool


@dataclass(frozen=True)
class GuideAction:
    name: str
    title_zh: str
    reason_zh: str
    effect: str = "read-only"
    cli_command: Optional[str] = None
    selectable_jobs: Tuple[str, ...] = ()
    enables_zh: str = "继续下一阶段"

    @property
    def requires_confirmation(self) -> bool:
        return self.effect != "read-only"


def build_snapshot(workdir: Path, config_path: Optional[Path] = None) -> GuideSnapshot:
    case = inspect_case(workdir, config_path=config_path)
    missing_base = tuple(name for name in BASE_INPUTS if name in case.missing_files)
    return GuideSnapshot(
        case=case,
        missing_base_inputs=missing_base,
        has_config=case.config_path.is_file(),
    )


def recommend_action(snapshot: GuideSnapshot) -> GuideAction:
    case = snapshot.case
    if snapshot.missing_base_inputs:
        return GuideAction(
            name="fix-inputs",
            title_zh="补齐基础输入文件",
            reason_zh="当前 case 缺少基础 VASP 输入：" + ", ".join(snapshot.missing_base_inputs),
            effect="read-only",
            cli_command=None,
            enables_zh="初始化 workflow 配置",
        )
    if not snapshot.has_config:
        return GuideAction(
            name="init",
            title_zh="初始化 vaspsolkit 配置",
            reason_zh="检测到基础输入已存在，但缺少 vaspsolkit.json。",
            effect="file-changing",
            cli_command="init",
            enables_zh="准备中性结构优化",
        )

    if not case.reference_confirmed:
        return GuideAction(
            name="configure-reference",
            title_zh="确认 SHE reference",
            reason_zh="当前 Case 的 SHE reference 尚未经过显式确认。",
            effect=action_effect("configure-reference"),
            cli_command="configure-reference",
            enables_zh="可追溯的电势换算",
        )

    statuses = dict(case.charge_statuses)
    queued = tuple(name for name, status in case.charge_statuses if status in {"QUEUED", "SUBMITTED"})
    if queued:
        return GuideAction(
            name="monitor",
            title_zh="同步 SLURM 队列状态",
            reason_zh="本地记录显示任务可能排队；先向 SLURM 查询实时状态，再决定是否换节点或重新提交。",
            effect=action_effect("monitor"),
            cli_command="monitor",
            enables_zh="安全判断是否需要节点调整或重新提交",
        )

    if case.neutral_status == "NOT_PREPARED":
        return GuideAction(
            name="prepare-neutral",
            title_zh="准备中性结构优化",
            reason_zh="当前还没有中性结构优化 workflow 记录。",
            effect=action_effect("prepare-neutral"),
            cli_command="prepare-neutral",
            enables_zh="提交中性任务",
        )
    if case.neutral_status == "PREPARED":
        return GuideAction(
            name="submit-neutral",
            title_zh="提交中性任务",
            reason_zh="中性结构优化输入已经准备好。",
            effect=action_effect("submit-neutral"),
            cli_command="submit-neutral",
            enables_zh="等待中性计算完成后检查收敛",
        )
    if case.neutral_status in {"SUBMITTED", "QUEUED", "RUNNING", "UNKNOWN"}:
        return GuideAction(
            name="monitor",
            title_zh="刷新任务状态",
            reason_zh="中性任务可能仍在队列或运行中，本步骤只刷新状态，不提交新任务。",
            effect=action_effect("monitor"),
            cli_command="monitor",
            enables_zh="确认是否可以检查中性收敛",
        )
    if case.neutral_status == "CONVERGED" and case.charge_total == 0:
        return GuideAction(
            name="prepare-charge",
            title_zh="准备带电点计算",
            reason_zh="中性结构优化已经收敛，可以用 CONTCAR 和 CHGCAR 生成带电点目录。",
            effect=action_effect("prepare-charge"),
            cli_command="prepare-charge",
            enables_zh="检查带电点输入",
        )
    if case.charge_total and not case.prepared_checked:
        return GuideAction(
            name="check-prepared",
            title_zh="检查带电点输入",
            reason_zh="带电点目录已经存在，但还没有通过提交前输入检查。",
            effect=action_effect("check-prepared"),
            cli_command="check-prepared",
            enables_zh="选择带电点提交",
        )
    if any(status in {"RUNNING", "UNKNOWN"} for status in statuses.values()):
        return GuideAction(
            name="monitor",
            title_zh="刷新带电任务状态",
            reason_zh="至少一个带电点可能仍在运行或状态未知。",
            effect=action_effect("monitor"),
            cli_command="monitor",
            enables_zh="检查带电点收敛",
        )
    ready = tuple(name for name, status in case.charge_statuses if status == "PREPARED")
    if ready:
        return GuideAction(
            name="submit-selected",
            title_zh="选择带电点提交",
            reason_zh="带电点输入已经通过检查，请明确选择要提交的点。",
            effect=action_effect("submit-selected"),
            cli_command="submit-selected",
            selectable_jobs=ready,
            enables_zh="带电点计算执行",
        )
    if statuses and all(status == "CONVERGED" for status in statuses.values()):
        return GuideAction(
            name="collect",
            title_zh="收集计算结果",
            reason_zh="所有记录的带电点已经收敛。",
            effect=action_effect("collect"),
            cli_command="collect",
            enables_zh="结果审计和后处理",
        )
    return GuideAction(
        name="check",
        title_zh="检查计算输出",
        reason_zh="当前有任务状态需要进一步检查。",
        effect=action_effect("check"),
        cli_command="check",
        enables_zh="更新 workflow 状态",
    )


def action_cli_argv(
    workdir: Path,
    action: GuideAction,
    selected_jobs: Optional[Sequence[str]] = None,
    config_path: Optional[Path] = None,
) -> List[str]:
    if action.cli_command is None:
        raise ValueError(f"action has no CLI command: {action.name}")
    argv = [action.cli_command, "--workdir", str(Path(workdir).resolve())]
    if config_path is not None:
        argv.extend(["--config", str(Path(config_path).resolve())])
    if action.cli_command in {"prepare-neutral", "submit-neutral", "submit-selected", "reset-queued"}:
        argv.append("--yes")
    if action.cli_command in {"submit-selected", "reset-queued"}:
        jobs = list(selected_jobs or action.selectable_jobs)
        if not jobs:
            raise ValueError("selected jobs are required for this action")
        argv.extend(jobs)
    return argv
