"""Pure view models for the guided terminal interface.

This module performs read-only inspection.  It intentionally has no curses
dependency and never submits jobs or changes calculation files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

from .config import KitConfig, load_kit_config
from .reference_settings import inspect_reference_freshness, unusual_she_reference
from .state import WorkflowState


BASE_INPUTS = ("POSCAR", "INCAR", "KPOINTS", "POTCAR")


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: str
    summary: str
    detail: str
    suggestion: str


@dataclass(frozen=True)
class SchedulerSummary:
    kind: str = "unknown"
    queue: str = "-"
    cores: int = 0
    max_inflight: Optional[int] = None
    script: str = "-"
    nodes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CaseSnapshot:
    workdir: Path
    config_path: Path
    initialized: bool = False
    stage: str = "uninitialized"
    missing_files: Tuple[str, ...] = ()
    scheduler: SchedulerSummary = field(default_factory=SchedulerSummary)
    neutral_status: str = "NOT_PREPARED"
    neutral_job_id: str = ""
    charge_names: Tuple[str, ...] = ()
    charge_statuses: Tuple[Tuple[str, str], ...] = ()
    charge_total: int = 0
    prepared_checked: bool = False
    results_available: bool = False
    reference_confirmed: bool = False
    reference_results_status: str = "missing"
    diagnostics: Tuple[Diagnostic, ...] = ()
    config: Optional[KitConfig] = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class Recommendation:
    action: str
    title: str
    reason: str
    effect: str
    enables: str


@dataclass(frozen=True)
class ErrorCard:
    title: str
    summary: str
    suggestion: str
    technical_detail: str


_ACTION_EFFECTS = {
    "refresh": "read-only",
    "open-inputs": "read-only",
    "monitor": "read-only",
    "check-neutral": "read-only",
    "check-prepared": "read-only",
    "check": "read-only",
    "prepare-neutral": "file-changing",
    "configure-scheduler": "file-changing",
    "configure-reference": "file-changing",
    "select-node": "file-changing",
    "prepare-charge": "file-changing",
    "collect": "file-changing",
    "audit": "file-changing",
    "postprocess": "file-changing",
    "repair": "file-changing",
    "submit-neutral": "external",
    "submit-selected": "external",
    "reset-queued": "external",
    "submit": "external",
}


def inspect_case(workdir: Path, config_path: Optional[Path] = None) -> CaseSnapshot:
    """Inspect a case without changing it or querying its scheduler."""
    root = Path(workdir).expanduser().resolve()
    selected_config = (
        Path(config_path).expanduser().resolve()
        if config_path is not None
        else root / "vaspsolkit.json"
    )
    diagnostics = []
    if not root.is_dir():
        diagnostics.append(
            Diagnostic(
                "invalid-case",
                "error",
                "Case directory is unavailable",
                str(root),
                "Choose an existing readable directory.",
            )
        )
        return CaseSnapshot(root, selected_config, diagnostics=tuple(diagnostics))

    if not selected_config.is_file():
        missing = (selected_config.name,) + tuple(
            name for name in BASE_INPUTS if not (root / name).is_file()
        )
        diagnostics.append(
            Diagnostic(
                "missing-config",
                "error",
                "Workflow configuration is missing",
                str(selected_config),
                "Initialize this case or choose another case.",
            )
        )
        return CaseSnapshot(
            root,
            selected_config,
            missing_files=missing,
            diagnostics=tuple(diagnostics),
        )

    try:
        config = load_kit_config(selected_config)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        diagnostics.append(
            Diagnostic(
                "invalid-config",
                "error",
                "Workflow configuration cannot be loaded",
                f"{selected_config}: {exc}",
                "Correct the configuration file, then refresh.",
            )
        )
        return CaseSnapshot(root, selected_config, diagnostics=tuple(diagnostics))

    required = BASE_INPUTS + (config.scheduler.script,)
    missing = tuple(name for name in required if not (root / name).is_file())
    state_path = root / "vaspsolkit.state.json"
    state = None
    if state_path.is_file():
        try:
            state = WorkflowState.load(state_path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            diagnostics.append(
                Diagnostic(
                    "invalid-state",
                    "error",
                    "Workflow state cannot be loaded",
                    f"{state_path}: {exc}",
                    "Inspect or restore vaspsolkit.state.json.",
                )
            )
    if missing:
        diagnostics.append(
            Diagnostic(
                "missing-inputs",
                "error",
                "Required case files are missing",
                ", ".join(missing),
                "Add the listed files before preparing or submitting.",
            )
        )

    jobs = state.jobs if state is not None else {}
    names = tuple(jobs) if jobs else ()
    statuses = tuple((name, record.status) for name, record in jobs.items())
    neutral = state.neutral if state is not None else None
    scheduler = SchedulerSummary(
        kind=config.scheduler.kind,
        queue=config.scheduler.queue,
        cores=config.scheduler.cores,
        max_inflight=config.scheduler.max_inflight,
        script=config.scheduler.script,
        nodes=tuple(config.scheduler.nodes),
    )
    summary_path = root / config.workflow.results_root / config.workflow.summary_file
    reference = inspect_reference_freshness(summary_path, config.workflow)
    if not config.workflow.she_reference_confirmed:
        diagnostics.append(Diagnostic(
            "she-reference-unconfirmed", "warning", "SHE reference 尚未显式确认",
            f"当前值为 {config.workflow.she_reference:g} eV", "选择任务 13 完成确认。",
        ))
    if unusual_she_reference(config.workflow.she_reference):
        diagnostics.append(Diagnostic(
            "she-reference-unusual", "warning", "SHE reference 位于非常用范围",
            f"当前值为 {config.workflow.she_reference:g} eV", "核对项目采用的参考约定。",
        ))
    if reference.status in {"stale", "unknown"}:
        diagnostics.append(Diagnostic(
            "she-reference-results-" + reference.status, "warning", "派生结果的 SHE reference 需要更新",
            reference.detail, "重新执行 60 → 61 → 62。",
        ))
    has_errors = any(item.severity == "error" for item in diagnostics)
    return CaseSnapshot(
        workdir=root,
        config_path=selected_config,
        initialized=not missing and not has_errors,
        stage=state.stage if state is not None else "setup",
        missing_files=missing,
        scheduler=scheduler,
        neutral_status=neutral.status if neutral is not None else "NOT_PREPARED",
        neutral_job_id=neutral.job_id if neutral is not None else "",
        charge_names=names,
        charge_statuses=statuses,
        charge_total=len(names),
        prepared_checked=state.prepared_checked if state is not None else False,
        results_available=(
            root / config.workflow.results_root / config.workflow.summary_file
        ).is_file(),
        reference_confirmed=config.workflow.she_reference_confirmed,
        reference_results_status=reference.status,
        diagnostics=tuple(diagnostics),
        config=config,
    )


def action_effect(action: str) -> str:
    return _ACTION_EFFECTS.get(action, "read-only")


def _recommendation(
    action: str, title: str, reason: str, enables: str
) -> Recommendation:
    return Recommendation(action, title, reason, action_effect(action), enables)


def recommend(snapshot: CaseSnapshot) -> Recommendation:
    """Return one safe next action for the current on-disk state."""
    errors = [item for item in snapshot.diagnostics if item.severity == "error"]
    if errors:
        return _recommendation(
            "open-inputs",
            "Review case requirements",
            errors[0].summary,
            "case preparation",
        )
    if snapshot.neutral_status == "NOT_PREPARED":
        return _recommendation(
            "prepare-neutral",
            "Prepare neutral relaxation",
            "No neutral workflow state exists.",
            "neutral submission",
        )
    if snapshot.neutral_status == "PREPARED":
        return _recommendation(
            "submit-neutral",
            "Submit neutral relaxation",
            "Neutral inputs are prepared.",
            "scheduler execution",
        )
    if snapshot.neutral_status in {"SUBMITTED", "QUEUED", "RUNNING", "UNKNOWN"}:
        return _recommendation(
            "monitor",
            "Refresh neutral job state",
            "The neutral job may still be active.",
            "neutral convergence check",
        )
    if snapshot.neutral_status == "CONVERGED" and snapshot.charge_total == 0:
        return _recommendation(
            "prepare-charge",
            "Prepare charge relaxations",
            "The neutral relaxation is converged.",
            "charge input validation",
        )

    statuses = dict(snapshot.charge_statuses)
    if snapshot.charge_total and not snapshot.prepared_checked:
        return _recommendation(
            "check-prepared",
            "Validate charge inputs",
            "Charge folders exist but have not passed validation.",
            "charge submission",
        )
    if any(
        status in {"SUBMITTED", "QUEUED", "RUNNING", "UNKNOWN"}
        for status in statuses.values()
    ):
        return _recommendation(
            "monitor",
            "Refresh active charge jobs",
            "At least one charge job may still be active.",
            "convergence checking",
        )
    if any(status == "PREPARED" for status in statuses.values()):
        return _recommendation(
            "submit-selected",
            "Select charge jobs to submit",
            "Validated charge jobs are ready; choose exactly which ones to submit.",
            "charge execution",
        )
    if statuses and all(status == "CONVERGED" for status in statuses.values()):
        return _recommendation(
            "collect",
            "Collect converged results",
            "All configured charge jobs are converged.",
            "audit and postprocessing",
        )
    return _recommendation(
        "check",
        "Check calculation outputs",
        "One or more job states require review.",
        "updated workflow state",
    )


def present_error(exc: Exception, title: str = "Action failed") -> ErrorCard:
    raw = str(exc)
    lowered = raw.lower()
    if "qsub" in lowered or "sbatch" in lowered or "scheduler" in lowered:
        suggestion = "Review scheduler settings and the submission script, then retry."
    elif "outcar" in lowered or "e-fermi" in lowered:
        suggestion = "Confirm the calculation finished and rerun the relevant check."
    elif "config" in lowered or "json" in lowered:
        suggestion = "Correct vaspsolkit.json, then refresh the case."
    else:
        suggestion = "Open technical details, correct the reported problem, then retry."
    return ErrorCard(
        title=title,
        summary=raw or exc.__class__.__name__,
        suggestion=suggestion,
        technical_detail=f"{exc.__class__.__name__}: {raw}",
    )


def layout_mode(width: int) -> str:
    return "wide" if width >= 90 else "narrow"
