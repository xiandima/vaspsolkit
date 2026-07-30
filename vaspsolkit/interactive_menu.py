"""Dependency-free fixed-number menu for one VASPsolKit Case."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from .config import load_kit_config
from .guide_model import action_cli_argv, build_snapshot, recommend_action
from .menu_actions import MENU_ACTIONS, MenuAction, action_by_code
from .orchestrator import (
    apply_recorded_job_statuses,
    capture_recorded_jobs,
    collect_recorded_job_statuses,
)
from .scheduler import scheduler_from_config
from .state import WorkflowState
from .submission_resources import prompt_submission_resources, resource_cli_argv
from .terminal_display import TerminalTheme
from .terminal_menu_renderer import MenuItemView, MenuView, render_menu


InputFn = Callable[[str], str]
OutputFn = Callable[[str], object]
TERMINAL_STATUSES = {"CONVERGED", "NEEDS_REVIEW", "FAILED", "BLOCKED"}
@dataclass(frozen=True)
class SyncResult:
    attempted: int = 0
    ok: bool = True
    warning: str = ""
    synced_at: str = ""


def resolve_action(code: str, snapshot) -> MenuAction:
    selected = action_by_code(code)
    if selected.code != "02":
        return selected
    recommended = recommend_action(snapshot)
    return MenuAction(
        "02",
        recommended.title_zh,
        recommended.cli_command,
        recommended.effect,
        "status",
        bool(recommended.selectable_jobs),
    )


def confirm_effect(effect: str, input_fn: InputFn = input) -> bool:
    if effect == "read-only":
        return True
    if effect == "file-changing":
        return input_fn("该操作将修改 Case 文件，输入 y 继续 >> ").strip().lower() == "y"
    if effect in {"external", "external-submit"}:
        return input_fn("该操作将提交调度任务，输入 SUBMIT 继续 >> ").strip() == "SUBMIT"
    if effect == "external-cancel":
        return input_fn("该操作将取消调度任务，输入 CANCEL 继续 >> ").strip() == "CANCEL"
    return False


def action_availability(action: MenuAction, snapshot) -> Tuple[bool, str]:
    case = snapshot.case
    statuses = dict(case.charge_statuses)
    if action.code in {"00", "01", "03", "10", "90"}:
        return True, ""
    if action.code == "02":
        return (bool(action.command), "请先按 10 查看并补齐基础输入")
    if action.code == "11":
        return (not snapshot.has_config, "Case 已经初始化")
    if action.code == "12":
        return (snapshot.has_config, "请先初始化 Case")
    if action.code == "13":
        return (snapshot.has_config, "请先初始化 Case")
    if action.code == "20":
        return (
            snapshot.has_config and case.neutral_status == "NOT_PREPARED",
            "仅可在已初始化且中性任务未准备时执行",
        )
    if action.code == "21":
        return (case.neutral_status == "PREPARED", "中性任务尚未准备或已经提交")
    if action.code == "22":
        return (case.neutral_status != "NOT_PREPARED", "中性任务尚未准备")
    if action.code == "30":
        return (
            case.neutral_status == "CONVERGED" and case.charge_total == 0,
            "需要中性任务收敛且尚未生成带电点",
        )
    if action.code == "31":
        return (case.charge_total > 0, "尚未生成带电点")
    if action.code == "32":
        ready = any(status == "PREPARED" for status in statuses.values())
        return (case.prepared_checked and ready, "没有已检查且可提交的 PREPARED 带电点")
    if action.code == "40":
        active = case.neutral_status in {"SUBMITTED", "QUEUED", "RUNNING", "UNKNOWN"}
        active = active or any(
            status in {"SUBMITTED", "QUEUED", "RUNNING", "UNKNOWN"}
            for status in statuses.values()
        )
        return (active, "当前没有已记录的活动任务")
    if action.code in {"41", "42"}:
        queued = any(status in {"SUBMITTED", "QUEUED"} for status in statuses.values())
        return (queued, "当前没有可取消的排队带电任务")
    if action.code == "50":
        return (case.neutral_status != "NOT_PREPARED" or bool(statuses), "没有任务记录")
    if action.code == "51":
        failed = any(status in {"FAILED", "NEEDS_REVIEW", "BLOCKED"} for status in statuses.values())
        return (failed, "没有待修复的失败带电任务")
    if action.code == "60":
        complete = bool(statuses) and all(status == "CONVERGED" for status in statuses.values())
        return (complete, "所有带电点收敛后才可执行")
    if action.code == "61":
        complete = bool(statuses) and all(status == "CONVERGED" for status in statuses.values())
        current = case.reference_results_status == "current"
        return (complete and current, "请先用当前 SHE 参考值重新收集结果")
    if action.code == "62":
        current = case.reference_results_status == "current"
        return (case.results_available and current, "请先用当前 SHE 参考值重新收集结果")
    return (bool(action.command), "该任务尚未接入")


def synchronize_case(
    workdir: Path,
    *,
    config_path: Optional[Path] = None,
    scheduler_factory=scheduler_from_config,
) -> SyncResult:
    """Refresh recorded active Job IDs only; never inspect the global queue."""
    root = Path(workdir).expanduser().resolve()
    selected_config = (
        Path(config_path).expanduser().resolve()
        if config_path is not None
        else root / "vaspsolkit.json"
    )
    state_path = root / "vaspsolkit.state.json"
    if not selected_config.is_file() or not state_path.is_file():
        return SyncResult()
    try:
        state = WorkflowState.load(state_path)
        records = ([state.neutral] if state.neutral is not None else []) + list(state.jobs.values())
        if not any(
            record.job_id and record.status not in TERMINAL_STATUSES
            for record in records
        ):
            return SyncResult()
        snapshot = capture_recorded_jobs(root, state_path=state_path)
        if not snapshot.job_ids:
            return SyncResult()
        config = load_kit_config(selected_config)
        scheduler = scheduler_factory(config.scheduler)
        collected = collect_recorded_job_statuses(snapshot, scheduler)
        apply_recorded_job_statuses(collected)
        return SyncResult(
            attempted=len(snapshot.job_ids),
            synced_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return SyncResult(
            attempted=len(snapshot.job_ids) if "snapshot" in locals() else 0,
            ok=False,
            warning=str(exc),
        )


def _select_jobs(
    candidates: Sequence[str], input_fn: InputFn, output: OutputFn
) -> List[str]:
    if not candidates:
        return []
    output("可选择任务: " + ", ".join(candidates))
    raw = input_fn("输入任务名，逗号分隔；直接回车表示全选 >> ").strip()
    selected = list(candidates) if not raw else [item.strip() for item in raw.split(",")]
    unknown = [item for item in selected if item not in candidates]
    if unknown:
        raise ValueError("未知或当前不可选的任务: " + ", ".join(unknown))
    return list(dict.fromkeys(selected))


def _fixed_cli_argv(
    action: MenuAction,
    snapshot,
    selected_jobs: Sequence[str] = (),
) -> List[str]:
    case = snapshot.case
    if action.command is None:
        raise ValueError(f"任务 {action.code} 没有对应命令")
    if action.command == "postprocess":
        config = case.config
        if config is None:
            raise ValueError("Case 配置不可用")
        results = case.workdir / config.workflow.results_root
        return [
            "postprocess",
            "--summary",
            str(results / config.workflow.summary_file),
            "--output",
            str(results),
        ]
    argv = [action.command, "--workdir", str(case.workdir)]
    if snapshot.has_config:
        argv.extend(["--config", str(case.config_path)])
    if action.effect in {"file-changing", "external", "external-submit", "external-cancel"}:
        if action.command in {"prepare-neutral", "submit-neutral", "submit-selected", "reset-queued", "repair"}:
            argv.append("--yes")
    if action.command == "repair":
        if len(selected_jobs) != 1:
            raise ValueError("修复操作一次只能选择一个任务")
        argv.extend(["--job", selected_jobs[0], "--no-submit"])
    elif selected_jobs:
        argv.extend(selected_jobs)
    return argv


def _print_case_details(snapshot, output: OutputFn) -> None:
    case = snapshot.case
    output(f"Case: {case.workdir}")
    output(f"阶段: {case.stage}")
    output(f"中性任务: {case.neutral_status} job={case.neutral_job_id or '-'}")
    for name, status in case.charge_statuses:
        output(f"带电点 {name}: {status}")
    if not case.charge_statuses:
        output("带电点: 尚未生成")
    for diagnostic in case.diagnostics:
        output(f"[{diagnostic.severity}] {diagnostic.summary}: {diagnostic.detail}")


def _print_input_status(snapshot, output: OutputFn) -> None:
    missing = snapshot.missing_base_inputs
    if missing:
        output("基础输入缺失: " + ", ".join(missing))
    else:
        output("基础输入检查通过: POSCAR, INCAR, KPOINTS, POTCAR")


def _print_help(output: OutputFn) -> None:
    output("任务号  作用类型          等效命令")
    for action in MENU_ACTIONS:
        command = action.command or "内置操作"
        output(f"{action.code:>4}  {action.effect:<16} {command}  # {action.title_zh}")


def _print_submission_summary(
    snapshot,
    action: MenuAction,
    resources,
    selected_jobs: Sequence[str],
    output: OutputFn,
) -> None:
    jobs = ", ".join(selected_jobs) if selected_jobs else "neutral"
    output("最终提交配置")
    output(f"  任务：{jobs}")
    output(f"  Case：{snapshot.case.workdir}")
    output(f"  节点策略：{'指定节点' if resources.nodes else '自动分配'}")
    output(f"  节点：{','.join(resources.nodes) if resources.nodes else '自动'}")
    output(f"  核心数：{resources.cores}")
    output(f"  队列：{resources.queue or '集群默认'}")
    output(f"  Walltime：{resources.walltime}")
    output(f"  提交脚本：{resources.script}")
    output(f"  保存为 Case 默认值：{'是' if resources.persist else '否'}")


def run_menu_action(
    code: str,
    snapshot,
    *,
    cli_main=None,
    input_fn: InputFn = input,
    output: OutputFn = print,
    synchronize_fn=synchronize_case,
    resource_selector=prompt_submission_resources,
) -> int:
    action = resolve_action(code, snapshot)
    available, reason = action_availability(action, snapshot)
    if not available:
        output(f"任务 {action.code} 当前不可执行：{reason}")
        return 1
    if action.code == "01":
        result = synchronize_fn(snapshot.case.workdir, config_path=snapshot.case.config_path)
        if result is not None and not result.ok:
            output(f"警告：队列同步失败：{result.warning}")
            return 1
        output("当前 Case 状态已刷新。")
        return 0
    if action.code == "03":
        _print_case_details(snapshot, output)
        return 0
    if action.code == "10":
        _print_input_status(snapshot, output)
        return 0
    if action.code == "90":
        _print_help(output)
        return 0

    guide_action = recommend_action(snapshot) if action.code == "02" else None
    statuses = dict(snapshot.case.charge_statuses)
    candidates: Sequence[str] = ()
    if action.command == "submit-selected":
        candidates = (
            guide_action.selectable_jobs
            if guide_action is not None and guide_action.selectable_jobs
            else tuple(name for name, status in statuses.items() if status == "PREPARED")
        )
    elif action.command == "reset-queued" or action.code == "41":
        candidates = tuple(
            name for name, status in statuses.items() if status in {"SUBMITTED", "QUEUED"}
        )
    elif action.command == "repair":
        candidates = tuple(
            name
            for name, status in statuses.items()
            if status in {"FAILED", "NEEDS_REVIEW", "BLOCKED"}
        )
    selected_jobs = _select_jobs(candidates, input_fn, output) if candidates else []

    resources = None
    if action.command in {"submit-neutral", "submit-selected"}:
        if snapshot.case.config is None:
            output("提交资源不可用：Case 配置无法读取。")
            return 1
        resources = resource_selector(
            snapshot.case.config,
            input_fn=input_fn,
            output=output,
        )
        if resources is None:
            output("提交已取消；未修改资源配置。")
            return 1

    if guide_action is not None:
        argv = action_cli_argv(
            snapshot.case.workdir,
            guide_action,
            selected_jobs=selected_jobs,
            config_path=snapshot.case.config_path if snapshot.has_config else None,
        )
    elif action.code == "41":
        cancel_action = action_by_code("42")
        argv = _fixed_cli_argv(cancel_action, snapshot, selected_jobs)
    else:
        argv = _fixed_cli_argv(action, snapshot, selected_jobs)

    if resources is not None:
        argv.extend(resource_cli_argv(resources))
        _print_submission_summary(
            snapshot,
            action,
            resources,
            selected_jobs,
            output,
        )

    output(f"将执行: vaspsolkit {' '.join(argv)}")
    if action.command == "configure-reference":
        if cli_main is None:
            from .cli import main as cli_main
        return int(cli_main(argv, input_fn=input_fn, output=output))
    if not confirm_effect(action.effect, input_fn):
        output("操作已取消；未执行命令。")
        return 1
    if cli_main is None:
        from .cli import main as cli_main
    code_result = int(cli_main(argv, input_fn=input_fn, output=output))
    if code_result != 0 or action.code != "41":
        return code_result
    output("排队任务已取消并恢复为 PREPARED；接下来只修改资源配置，不会重新提交。")
    configure = [
        "configure-scheduler",
        "--workdir",
        str(snapshot.case.workdir),
        "--config",
        str(snapshot.case.config_path),
    ]
    return int(cli_main(configure, input_fn=input_fn, output=output))


def run_menu(
    workdir: Path,
    *,
    config_path: Optional[Path] = None,
    input_fn: InputFn = input,
    output: OutputFn = print,
    synchronize_fn=synchronize_case,
    theme: Optional[TerminalTheme] = None,
) -> int:
    root = Path(workdir).expanduser().resolve()
    selected_theme = theme if theme is not None else TerminalTheme.detect()
    while True:
        sync = (
            synchronize_fn(root, config_path=config_path)
            if synchronize_fn is not None
            else SyncResult()
        )
        if sync is not None and not sync.ok:
            output(f"警告：队列同步失败：{sync.warning}")
        snapshot = build_snapshot(root, config_path=config_path)
        recommendation = recommend_action(snapshot)
        _render_menu(snapshot, recommendation, output, theme=selected_theme)
        try:
            selected = action_by_code(
                input_fn(f"输入任务编号 {selected_theme.action('>>')} ")
            )
        except (ValueError, KeyError) as exc:
            output(str(exc).strip("'"))
            continue
        except (KeyboardInterrupt, EOFError):
            output("\n已退出；未改变服务器任务。")
            return 0
        if selected.code == "00":
            output("已退出；未改变服务器任务。")
            return 0
        try:
            run_menu_action(
                selected.code,
                snapshot,
                input_fn=input_fn,
                output=output,
                synchronize_fn=synchronize_fn,
            )
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            output(f"任务执行失败：{exc}")
            output("恢复建议：先执行任务 03 查看完整状态与诊断。")


def _case_label(workdir: Path) -> str:
    root = Path(workdir)
    if root.parent.name:
        return f"{root.parent.name} / {root.name}"
    return root.name or str(root)


def _menu_view(snapshot, recommendation) -> MenuView:
    case = snapshot.case
    items = []
    for action in MENU_ACTIONS:
        resolved = resolve_action(action.code, snapshot)
        available, reason = action_availability(resolved, snapshot)
        items.append(
            MenuItemView(
                code=action.code,
                title=action.title_zh,
                group=action.group,
                available=available,
                reason="" if available else reason,
            )
        )
    recommendation_code = next(
        (
            action.code
            for action in MENU_ACTIONS
            if recommendation.cli_command is not None
            and action.command == recommendation.cli_command
        ),
        "02",
    )
    charge_done = sum(
        status == "CONVERGED" for _, status in case.charge_statuses
    )
    return MenuView(
        case_label=_case_label(case.workdir),
        stage=case.stage,
        neutral_status=case.neutral_status,
        charge_done=charge_done,
        charge_total=case.charge_total,
        recommendation_code=recommendation_code,
        recommendation_title=recommendation.title_zh,
        recommendation_reason=recommendation.reason_zh,
        items=tuple(items),
    )


def _render_menu(
    snapshot,
    recommendation,
    output: OutputFn,
    *,
    theme: Optional[TerminalTheme] = None,
) -> None:
    selected_theme = theme if theme is not None else TerminalTheme.detect()
    for line in render_menu(
        _menu_view(snapshot, recommendation),
        theme=selected_theme,
    ):
        output(line)
