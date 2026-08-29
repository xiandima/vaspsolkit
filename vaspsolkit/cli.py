from __future__ import annotations

import argparse
import copy
import difflib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .analysis import analyze_adsorption, analyze_rows, read_summary, write_analysis
from .case_setup import apply_case_initialization, plan_case_initialization
from .config import (
    EXPECT_ABSENT,
    KitConfig,
    SchedulerConfig,
    WorkflowConfig,
    load_kit_config,
    migrate_config_data,
    serialize_kit_config,
    write_kit_config,
)
from .convergence import DiagnosticResult, apply_repair, propose_repair
from .inputs import (
    CRITICAL_INCAR_TAGS,
    PROFILE_TAGS,
    apply_incar_profile,
    plan_neutral_vaspsol_update,
    suggest_encut,
    validate_potcar_order,
    vaspkit_executable,
)
from .incar import replace_or_append
from .orchestrator import (
    STATE_FILENAME,
    check_neutral_job,
    check_prepared_jobs,
    load_state,
    prepare_charge_jobs,
    prepare_neutral_job,
    prepare_kit_jobs,
    refresh_neutral_scheduler_state,
    refresh_state,
    reset_queued_jobs,
    submission_preview,
    submit_neutral_job,
    submit_ready_jobs,
    submit_selected_jobs,
)
from .postprocess import postprocess_summary
from .operations.actions import ResourceRequest
from .reaction import run_reaction_spec
from .reference_settings import inspect_reference_freshness, prompt_reference_settings
from .scheduler import PBSScheduler, QueueEntry, scheduler_from_config
from .state import JobRecord, WorkflowState
from .submission_resources import resources_from_config
from .workflow import (
    audit_results,
    collect_results,
    result_file_path,
    write_points_to_rerun,
    write_quality_report,
)


InputFn = Callable[[str], str]
OutputFn = Callable[[str], object]


def main(
    argv: Optional[List[str]] = None,
    input_fn: InputFn = input,
    output: OutputFn = print,
) -> int:
    argv = list(argv) if argv is not None else None
    if argv == []:
        if not _has_interactive_tty():
            output("未检测到交互终端；请使用 vaspsolkit menu 或显式 CLI 子命令。")
            return 2
        return _open_menu(Path("."), input_fn=input_fn, output=output)
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        if not _has_interactive_tty():
            output("未检测到交互终端；请使用 vaspsolkit menu 或显式 CLI 子命令。")
            return 2
        return _open_menu(Path("."), input_fn=input_fn, output=output)
    if args.command == "init":
        return _cmd_init(args, input_fn, output)
    if args.command == "configure-reference":
        return _cmd_configure_reference(args, input_fn, output)
    if args.command == "migrate":
        source = Path(args.input)
        target = Path(args.output)
        source_bytes = source.read_bytes()
        same_file = source.resolve() == target.resolve()
        target_exists = target.exists()
        if target_exists and not same_file and not args.force:
            raise FileExistsError(f"output already exists; use --force to replace it: {target}")
        target_snapshot = target.read_bytes() if target_exists else None

        current = json.loads(source_bytes.decode("utf-8"))
        migrated = migrate_config_data(current)
        config = KitConfig.from_dict(migrated)
        migrated_text = serialize_kit_config(config).decode("utf-8")
        if args.force and target_exists and not same_file:
            preview_bytes = target_snapshot
            preview_path = target
        else:
            preview_bytes = source_bytes
            preview_path = source
        before = preview_bytes.decode("utf-8", errors="replace").splitlines()
        after = migrated_text.splitlines()
        preview = "\n".join(
            difflib.unified_diff(
                before,
                after,
                fromfile=str(preview_path),
                tofile=str(target),
                lineterm="",
            )
        )
        output(preview or "\n".join(after))
        if not args.yes and not _confirm("Write migrated config?", input_fn):
            output("migration cancelled")
            return 1
        expected_current = target_snapshot if target_exists else EXPECT_ABSENT
        write_kit_config(target, config, expected_current=expected_current)
        output(f"wrote {args.output}")
        return 0
    if args.command == "reaction":
        result = run_reaction_spec(Path(args.spec), Path(args.output))
        output(f"wrote {result.csv_path}")
        output(f"wrote {result.plot_path}")
        output(f"wrote {result.report_path}")
        return 0
    if args.command == "postprocess":
        result = postprocess_summary(
            Path(args.summary),
            Path(args.output),
            allow_excluded=args.allow_excluded,
        )
        output(f"wrote {result.analysis_path}")
        output(f"wrote {result.plot_path}")
        return 0
    if args.command == "analyze":
        return _cmd_analyze(args, output)
    if args.command in {"configure-scheduler", "select-node"}:
        return _cmd_configure_scheduler(args, input_fn, output)
    if args.command in {"menu", "wizard"}:
        config_path = Path(args.config) if args.config else None
        return _open_menu(
            Path(args.workdir),
            config_path=config_path,
            input_fn=input_fn,
            output=output,
        )
    if args.command in {"ui", "workbench-ui", "legacy-ui"}:
        output("该界面已归档。请直接运行 vaspsolkit 使用交互菜单。")
        return 2

    workdir = Path(args.workdir).resolve()
    config_path = Path(args.config) if args.config else workdir / "vaspsolkit.json"
    config = load_kit_config(config_path)
    if args.command == "submit-neutral":
        resources = _submission_resources(config, args)
        confirmed = args.yes or (not args.dry_run and _confirm("Submit neutral job?", input_fn))
        return _submit_neutral_once(
            workdir, config, config_path, confirmed=confirmed,
            dry_run=args.dry_run, resources=resources, output=output,
        )
    if args.command == "check-neutral":
        state = check_neutral_job(
            workdir,
            config,
            scheduler=scheduler_from_config(config.scheduler),
            job_id=args.job_id,
            require_relaxation=True,
        )
        _print_neutral_state(state, output)
        return 0
    if args.command == "plan":
        state = _state_for_plan(workdir, config)
        scheduler = None if not args.offline else _OfflineScheduler()
        _print_plan(config, submission_preview(config, state, scheduler=scheduler), output)
        return 0
    if args.command == "prepare-neutral":
        if not args.yes and not _confirm("Archive old outputs and prepare neutral relaxation?", input_fn):
            output("neutral preparation cancelled")
            return 1
        state = prepare_neutral_job(workdir, config)
        output(f"prepared neutral relaxation; state={workdir / STATE_FILENAME}")
        return 0
    if args.command in {"prepare", "prepare-charge"}:
        state = prepare_charge_jobs(workdir, config)
        output(f"prepared {len(state.jobs)} jobs; state={workdir / STATE_FILENAME}")
        return 0
    if args.command == "check-prepared":
        state = check_prepared_jobs(workdir, config)
        output(f"charge inputs validated: {len(state.jobs)} jobs")
        return 0
    if args.command == "submit":
        state = load_state(workdir)
        scheduler = scheduler_from_config(config.scheduler)
        preview = submission_preview(config, state, scheduler=scheduler)
        _print_plan(config, preview, output)
        confirmed = args.yes or (not args.dry_run and _confirm("Submit the first batch?", input_fn))
        if not confirmed and not args.dry_run:
            output("submission cancelled")
            return 1
        submitted = submit_ready_jobs(
            workdir,
            config,
            state,
            scheduler=scheduler,
            confirmed=confirmed,
            dry_run=args.dry_run,
            require_prepared_check=True,
        )
        for name, job_id in submitted.items():
            output(f"{name}: {job_id}")
        return 0
    if args.command == "submit-selected":
        resources = _submission_resources(config, args)
        confirmed = args.yes or (not args.dry_run and _confirm("Submit selected charge jobs?", input_fn))
        if not confirmed and not args.dry_run:
            output("submission cancelled")
            return 1
        return _submit_selected_once(
            workdir, config, config_path, list(args.jobs),
            resources=resources,
            confirmed=confirmed,
            dry_run=args.dry_run,
            output=output,
        )
    if args.command == "reset-queued":
        state = load_state(workdir)
        confirmed = args.yes or _confirm("Cancel selected queued jobs and mark them PREPARED?", input_fn)
        if not confirmed:
            output("reset cancelled")
            return 1
        reset = reset_queued_jobs(
            workdir,
            state,
            list(args.jobs),
            scheduler=scheduler_from_config(config.scheduler),
            confirmed=confirmed,
        )
        for name, old_job_id in reset.items():
            output(f"{name}: reset old_job={old_job_id}")
        return 0
    if args.command in {"status", "monitor", "check"}:
        return _cmd_status(args, workdir, config, output)
    if args.command == "repair":
        return _cmd_repair(args, workdir, config, input_fn, output)
    if args.command == "collect":
        rows = collect_results(workdir, config.workflow)
        output(f"wrote {result_file_path(workdir, config.workflow, config.workflow.summary_file)} ({len(rows)} rows)")
        return 0
    if args.command == "audit":
        summary_path = result_file_path(workdir, config.workflow, config.workflow.summary_file)
        freshness = inspect_reference_freshness(summary_path, config.workflow)
        if freshness.status != "current":
            raise RuntimeError("summary.csv 的 SHE reference 已过期或无法确认；请先执行 collect")
        rows = collect_results(workdir, config.workflow)
        report = audit_results(workdir, config.workflow, rows)
        quality = Path(args.output) if args.output else result_file_path(workdir, config.workflow, "quality_report.csv")
        rerun = Path(args.rerun_output) if args.rerun_output else result_file_path(workdir, config.workflow, "points_to_rerun.csv")
        write_quality_report(quality, report)
        write_points_to_rerun(rerun, report)
        output(f"wrote {quality}")
        output(f"wrote {rerun}")
        return 0
    if args.command == "run":
        return _cmd_run(args, workdir, config, config_path, input_fn, output)
    parser.error(f"unknown command: {args.command}")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vaspsolkit", description="Interactive VASP/VASPsol workflow")
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("init", help="initialize inputs and vaspsolkit configuration")
    init.add_argument("--workdir", default=".")
    init.add_argument("--config", default=None)
    init.add_argument("--profile", choices=sorted(PROFILE_TAGS), default=None)
    init.add_argument("--scheduler", choices=["slurm", "custom"], default=None)
    init.add_argument("--script", default=None)
    init.add_argument("--set", action="append", default=[], metavar="TAG=VALUE")
    init.add_argument("--she-reference", type=float, default=None)
    init.add_argument("--she-reference-source", default=None)
    init.add_argument("--yes", action="store_true")

    reference = sub.add_parser("configure-reference", help="configure the Case SHE reference")
    reference.add_argument("--workdir", default=".")
    reference.add_argument("--config", default=None)
    reference.add_argument("--she-reference", type=float, default=None)
    reference.add_argument("--she-reference-source", default=None)
    reference.add_argument("--yes", action="store_true")

    migrate = sub.add_parser("migrate", help="migrate a v1 SLURM configuration")
    migrate.add_argument("--input", required=True)
    migrate.add_argument("--output", default="vaspsolkit.json")
    migrate.add_argument("--yes", action="store_true")
    migrate.add_argument("--force", action="store_true")

    for name, help_text in (
        ("menu", "open the fixed-number interactive menu"),
        ("wizard", "compatibility alias for the fixed-number menu"),
        ("ui", "archived UI command; use menu"),
        ("workbench-ui", "archived UI command; use menu"),
        ("legacy-ui", "archived UI command; use menu"),
        ("prepare-neutral", "archive old outputs and prepare neutral geometry optimization"),
        ("submit-neutral", "submit the neutral job and return immediately"),
        ("check-neutral", "check one neutral job/output state without submitting"),
        ("configure-scheduler", "interactively configure PBS nodes and cores"),
        ("select-node", "interactively select PBS nodes and cores"),
        ("plan", "show queue snapshot and initial submission batches"),
        ("prepare-charge", "prepare charge-point geometry optimization folders"),
        ("prepare", "compatibility alias for prepare-charge"),
        ("check-prepared", "validate charge-point inputs before submission"),
        ("submit", "submit the next prepared batch"),
        ("submit-selected", "submit explicitly selected charge jobs"),
        ("reset-queued", "cancel selected queued charge jobs and mark them prepared"),
        ("status", "refresh or watch scheduler and convergence status"),
        ("monitor", "monitor scheduler state without submitting jobs"),
        ("check", "run one convergence status refresh"),
        ("repair", "preview and confirm a convergence repair"),
        ("collect", "collect VASP outputs into summary.csv"),
        ("audit", "write quality and rerun reports"),
        ("run", "non-blocking neutral submit/check convenience entry point"),
    ):
        command = sub.add_parser(name, help=help_text)
        _add_common_args(command)
        if name == "wizard":
            command.add_argument("--once", action="store_true")
        if name == "prepare-neutral":
            command.add_argument("--yes", action="store_true")
        if name == "submit-neutral":
            command.add_argument("--yes", action="store_true")
            command.add_argument("--dry-run", action="store_true")
            _add_submission_resource_args(command)
        if name == "check-neutral":
            command.add_argument("--job-id", default=None)
        if name == "plan":
            command.add_argument("--offline", action="store_true")
        if name == "submit":
            command.add_argument("--yes", action="store_true")
            command.add_argument("--dry-run", action="store_true")
        if name == "submit-selected":
            command.add_argument("jobs", nargs="+")
            command.add_argument("--yes", action="store_true")
            command.add_argument("--dry-run", action="store_true")
            _add_submission_resource_args(command)
        if name == "reset-queued":
            command.add_argument("jobs", nargs="+")
            command.add_argument("--yes", action="store_true")
        if name in {"status", "monitor"}:
            command.add_argument("--watch", action="store_true")
            command.add_argument("--interval", type=int, default=60)
        if name == "repair":
            command.add_argument("--job", default=None)
            command.add_argument("--yes", action="store_true")
            command.add_argument("--no-submit", action="store_true")
        if name == "audit":
            command.add_argument("--output", default=None)
            command.add_argument("--rerun-output", default=None)
        if name == "run":
            command.add_argument("--yes", action="store_true")
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--interval", type=int, default=60)

    postprocess = sub.add_parser("postprocess", help="enforce and plot a five-point E-U fit")
    postprocess.add_argument("--summary", default="results/summary.csv")
    postprocess.add_argument("--output", default="results")
    postprocess.add_argument("--allow-excluded", action="store_true")

    reaction = sub.add_parser("reaction", help="evaluate a reaction specification")
    reaction.add_argument("--spec", required=True)
    reaction.add_argument("--output", required=True)

    analyze = sub.add_parser("analyze", help="low-level fit from summary.csv")
    analyze.add_argument("--summary", default="summary.csv")
    analyze.add_argument("--output", default=None)
    analyze.add_argument("--target-u", default="")
    analyze.add_argument("--clean-summary", default=None)
    analyze.add_argument("--adsorbate-summary", default=None)
    analyze.add_argument("--reference-energy", type=float, default=0.0)
    return parser


def _has_interactive_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _cmd_init(args, input_fn: InputFn, output: OutputFn) -> int:
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    incar_path = workdir / "INCAR"
    preserve_user_incar = incar_path.is_file() and args.profile is None
    if preserve_user_incar:
        profile = "vaspsol-neutral-relax"
        output("Detected existing INCAR; preserving user settings and checking required VASPsol tags.")
    else:
        profile = args.profile or _choose("INCAR profile", sorted(PROFILE_TAGS), input_fn, output)
    reference = prompt_reference_settings(
        default_value=4.70,
        default_source="",
        explicit_value=args.she_reference,
        explicit_source=args.she_reference_source,
        input_fn=input_fn,
        output=output,
    )
    workflow = WorkflowConfig(
        she_reference=reference.value,
        she_reference_source=reference.source,
        she_reference_confirmed=True,
    )
    output(f"SHE reference：{reference.value:.6g} eV")
    output(f"参考来源：{reference.source or '未填写'}")
    scheduler_kind = args.scheduler or _choose("Scheduler", ["slurm", "custom"], input_fn, output)
    default_script = {"slurm": "vasp.slurm", "custom": "submit.sh"}[scheduler_kind]
    script = args.script or input_fn(f"Submission script [{default_script}]: ").strip() or default_script
    _ensure_standard_inputs(workdir, args.yes, input_fn, output)
    script_path = workdir / script
    if not script_path.exists():
        raise FileNotFoundError(f"submission script is missing: {script_path}")
    use_case_setup = (
        preserve_user_incar
        and args.yes
        and not args.set
        and args.config is None
        and scheduler_kind != "custom"
    )
    if use_case_setup:
        scheduler = SchedulerConfig(kind=scheduler_kind, script=script)
        plan = plan_case_initialization(workdir, scheduler, workflow)
        elements = validate_potcar_order(workdir / "POSCAR", workdir / "POTCAR")
        encut = suggest_encut(workdir / "POTCAR")
        output("User INCAR values will be preserved.")
        output(f"POSCAR/POTCAR order: {' '.join(elements)}")
        output(f"Suggested ENCUT from 1.3 x max(ENMAX): {encut} eV")
        _print_critical_incar(plan.incar_after, output)
        written = apply_case_initialization(plan, confirmed=True)
        for path in written:
            output(f"wrote {path}")
        return 0
    elements = validate_potcar_order(workdir / "POSCAR", workdir / "POTCAR")
    encut = suggest_encut(workdir / "POTCAR")
    existing = incar_path.read_text(encoding="utf-8", errors="ignore") if incar_path.exists() else ""
    overrides = _parse_overrides(args.set)
    if preserve_user_incar:
        update = plan_neutral_vaspsol_update(existing)
        output("User INCAR values will be preserved.")
        for key, value in update.additions:
            output(f"  add {key} = {value}")
        if update.duplicates:
            raise ValueError("duplicate INCAR tags require manual resolution: " + ", ".join(update.duplicates))
        if update.conflicts:
            details = "; ".join(f"{key}: current={current}, required={required}" for key, current, required in update.conflicts)
            raise ValueError("conflicting INCAR settings require manual resolution: " + details)
        candidate = update.candidate
        for key, value in overrides.items():
            candidate = replace_or_append(candidate, key, value)
    else:
        candidate = apply_incar_profile(existing, profile, overrides=overrides, suggested_encut=encut)
    output(f"POSCAR/POTCAR order: {' '.join(elements)}")
    output(f"Suggested ENCUT from 1.3 x max(ENMAX): {encut} eV")
    _print_critical_incar(candidate, output)
    if not args.yes:
        extra = input_fn("Additional TAG=VALUE overrides (comma-separated, blank to keep): ").strip()
        if extra:
            for key, value in _parse_overrides(extra.split(",")).items():
                candidate = replace_or_append(candidate, key, value)
        if not _confirm("Write INCAR, vaspsolkit.json, and state file?", input_fn):
            output("initialization cancelled")
            return 1
    scheduler = SchedulerConfig(kind=scheduler_kind, script=script)
    if scheduler_kind == "custom":
        submit = input_fn("Custom submit command tokens (use {script}): ").strip() if not args.yes else ""
        if not submit:
            raise ValueError("custom scheduler requires an interactive submit command")
        scheduler.submit_command = submit.split()
    config = KitConfig(profile=profile, workflow=workflow, scheduler=scheduler)
    config_path = Path(args.config) if args.config else workdir / "vaspsolkit.json"
    config_before = config_path.read_bytes() if config_path.exists() else EXPECT_ABSENT
    incar_path.write_text(candidate, encoding="utf-8")
    write_kit_config(config_path, config, expected_current=config_before)
    WorkflowState(stage="setup").save(workdir / STATE_FILENAME)
    output(f"wrote {incar_path}")
    output(f"wrote {config_path}")
    output(f"wrote {workdir / STATE_FILENAME}")
    return 0


def _cmd_configure_reference(args, input_fn: InputFn, output: OutputFn) -> int:
    workdir = Path(args.workdir).resolve()
    config_path = Path(args.config) if args.config else workdir / "vaspsolkit.json"
    before = config_path.read_bytes()
    config = load_kit_config(config_path)
    if args.yes and args.she_reference is None:
        raise ValueError("--yes requires --she-reference")
    settings = prompt_reference_settings(
        default_value=config.workflow.she_reference,
        default_source=config.workflow.she_reference_source,
        explicit_value=args.she_reference,
        explicit_source=args.she_reference_source,
        input_fn=input_fn,
        output=output,
    )
    output(f"SHE reference：{config.workflow.she_reference:g} → {settings.value:g} eV")
    output(f"参考来源：{config.workflow.she_reference_source or '未填写'} → {settings.source or '未填写'}")
    if not args.yes and not _confirm("保存电化学参考参数？", input_fn):
        output("reference configuration cancelled")
        return 1
    updated = copy.deepcopy(config)
    updated.workflow.she_reference = settings.value
    updated.workflow.she_reference_source = settings.source
    updated.workflow.she_reference_confirmed = True
    write_kit_config(config_path, updated, expected_current=before)
    output(f"wrote {config_path}")
    return 0


def _cmd_configure_scheduler(args, input_fn: InputFn, output: OutputFn) -> int:
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    config_path = Path(args.config) if args.config else workdir / "vaspsolkit.json"
    config_before = config_path.read_bytes() if config_path.exists() else EXPECT_ABSENT
    config = load_kit_config(config_path) if config_path.exists() else KitConfig()
    if config.scheduler.kind != "pbs":
        raise ValueError("configure-scheduler currently supports only PBS")
    scheduler = scheduler_from_config(config.scheduler)
    if not isinstance(scheduler, PBSScheduler):
        raise ValueError("configure-scheduler requires a PBS scheduler")

    nodes_info = scheduler.inspect_nodes(
        min_node=config.workflow.qsub_min_node,
        ppn=config.scheduler.cores,
    )
    output("PBS nodes:")
    for info in nodes_info:
        output(
            f"  {info.name}: state={info.state} total={info.total_cores} "
            f"used={info.used_cores} free={info.free_cores}"
        )

    nodes = _parse_node_names(input_fn("Node names (comma-separated): "))
    if not nodes:
        raise ValueError("at least one PBS node is required")
    by_name = {info.name: info for info in nodes_info}
    missing = [node for node in nodes if node not in by_name]
    if missing:
        raise ValueError(f"selected node(s) not found in pbsnodes output: {', '.join(missing)}")
    cores = _prompt_positive_int("Cores per job", config.scheduler.cores, input_fn)
    queue = _prompt_value("PBS queue", config.scheduler.queue, input_fn)
    walltime = _prompt_value("Walltime", config.scheduler.walltime, input_fn)

    unavailable = [
        node
        for node in nodes
        if by_name[node].state.lower().find("down") >= 0
        or by_name[node].state.lower().find("offline") >= 0
        or by_name[node].free_cores < cores
    ]
    if unavailable:
        raise ValueError(
            "selected node(s) are not currently idle enough for the requested cores: "
            + ", ".join(unavailable)
        )

    config.scheduler.nodes = nodes
    config.scheduler.cores = cores
    config.scheduler.queue = queue
    config.scheduler.walltime = walltime
    config.workflow.qsub_ppn = cores
    config.workflow.qsub_queue = queue
    config.workflow.qsub_walltime = walltime
    write_kit_config(config_path, config, expected_current=config_before)
    output(f"wrote {config_path}")
    output(f"nodes={','.join(nodes)} cores/job={cores}")
    return 0


def _cmd_status(args, workdir: Path, config: KitConfig, output: OutputFn) -> int:
    scheduler = scheduler_from_config(config.scheduler)
    state = load_state(workdir)
    while True:
        if state.neutral is not None and state.neutral.job_id and state.neutral.status != "CONVERGED":
            refresh_neutral_scheduler_state(workdir, config, state, scheduler=scheduler)
        refresh_state(workdir, config, state, scheduler=scheduler)
        _print_state(state, output)
        if args.command == "check" or not getattr(args, "watch", False):
            return 0
        statuses = {record.status for record in state.jobs.values()}
        if statuses == {"CONVERGED"} or statuses.intersection({"NEEDS_REVIEW", "FAILED", "BLOCKED"}):
            return 0
        neutral_active = state.neutral is not None and state.neutral.status in {"SUBMITTED", "QUEUED", "RUNNING", "UNKNOWN"}
        if not neutral_active and not any(record.status in {"SUBMITTED", "QUEUED", "RUNNING", "UNKNOWN"} for record in state.jobs.values()):
            return 0
        time.sleep(max(args.interval, 1))


def _cmd_repair(args, workdir: Path, config: KitConfig, input_fn: InputFn, output: OutputFn) -> int:
    state = load_state(workdir)
    name = args.job or input_fn("Job folder key to repair: ").strip()
    if name not in state.jobs:
        raise KeyError(f"unknown job key: {name}")
    record = state.jobs[name]
    diagnostic = DiagnosticResult(status=record.status, diagnostics=record.diagnostics)
    proposal = propose_repair(workdir / record.folder, diagnostic)
    output(f"reason: {proposal.reason}")
    output(f"seed mode: {proposal.seed_mode}")
    for key, value in proposal.incar_changes.items():
        output(f"  {key} -> {value}")
    confirmed = args.yes or _confirm("Archive outputs, apply this repair, and continue?", input_fn)
    if not confirmed:
        output("repair cancelled")
        return 1
    archive = apply_repair(workdir / record.folder, proposal, confirmed=confirmed)
    record.status = "PREPARED"
    record.job_id = ""
    record.restart_count += 1
    record.diagnostics = []
    state.stage = "prepared"
    state.save(workdir / STATE_FILENAME)
    output(f"archived previous outputs to {archive}")
    if not args.no_submit:
        submitted = submit_ready_jobs(workdir, config, state, confirmed=True)
        for key, job_id in submitted.items():
            output(f"{key}: {job_id}")
    return 0


def _cmd_run(
    args, workdir: Path, config: KitConfig, config_path: Path,
    input_fn: InputFn, output: OutputFn,
) -> int:
    """Perform one non-blocking action only after explicit stage preparation."""
    state_path = workdir / STATE_FILENAME
    if not state_path.exists():
        output("run requires prepare-neutral first; no workflow state exists")
        return 1
    state = load_state(workdir)
    if state.neutral is None or state.neutral.metadata.get("stage") != "neutral_relax":
        output("run requires prepare-neutral first; old/static outputs are not accepted")
        return 1
    if state.neutral.job_id:
        output(
            f"neutral: {state.neutral.status} job={state.neutral.job_id}; "
            "use monitor or check-neutral to refresh"
        )
        return 0
    if state.neutral.status == "CONVERGED":
        output("neutral relaxation is converged; run prepare-charge next")
        return 0
    return _submit_neutral_once(
        workdir,
        config,
        config_path,
        resources=resources_from_config(config),
        confirmed=args.yes or (not args.dry_run and _confirm("Submit neutral job?", input_fn)),
        dry_run=args.dry_run,
        output=output,
    )


def _submission_resources(config: KitConfig, args) -> ResourceRequest:
    current = resources_from_config(config)
    allocation = args.resource_allocation or current.allocation
    nodes = tuple(args.resource_node) if args.resource_node else current.nodes
    if allocation == "auto":
        if args.resource_node:
            raise ValueError("auto allocation cannot specify nodes")
        nodes = ()
    return ResourceRequest.create(
        allocation=allocation,
        nodes=nodes,
        cores=args.resource_cores or current.cores,
        queue=current.queue,
        walltime=current.walltime,
        script=current.script,
        persist=args.save_resources,
    )


def _config_with_submission_resources(
    config: KitConfig, resources: ResourceRequest
) -> KitConfig:
    effective = copy.deepcopy(config)
    effective.scheduler.nodes = list(resources.nodes)
    effective.scheduler.cores = resources.cores
    effective.workflow.qsub_ppn = resources.cores
    effective.validate()
    return effective


def _submit_neutral_once(
    workdir: Path,
    config: KitConfig,
    config_path: Path,
    *,
    resources: ResourceRequest,
    confirmed: bool,
    dry_run: bool,
    output: OutputFn,
) -> int:
    """Submit through the durable receipt barrier, or perform a receipt-free dry run."""
    if dry_run:
        effective = _config_with_submission_resources(config, resources)
        submit_neutral_job(
            workdir,
            effective,
            scheduler=scheduler_from_config(effective.scheduler),
            confirmed=confirmed,
            dry_run=True,
            require_prepared=True,
        )
        output("neutral: DRY-RUN")
        return 0

    from .operations.controller import WorkbenchController
    controller = WorkbenchController(
        workdir,
        config_path=config_path,
        scheduler_factory=scheduler_from_config,
    )
    plan = controller.plan("submit-neutral", resources)
    if plan.blocked_reason:
        raise RuntimeError(
            f"{plan.blocked_reason} 请先运行 `vaspsolkit menu --workdir {workdir}` "
            "查看诊断或执行人工 reconcile；不要再次 qsub。"
        )
    result = controller.execute(plan, confirmed=confirmed)
    if not result.ok:
        detail = result.error.suggestion_zh if result.error is not None else result.message
        raise RuntimeError(
            f"{result.message} {detail} 请运行 `vaspsolkit menu --workdir {workdir}` "
            "查看提交恢复屏障；不要再次 qsub。"
        )
    output(f"neutral: {result.job_ids['neutral']}")
    return 0


def _submit_selected_once(
    workdir: Path,
    config: KitConfig,
    config_path: Path,
    jobs: Sequence[str],
    *,
    resources: ResourceRequest,
    confirmed: bool,
    dry_run: bool,
    output: OutputFn,
) -> int:
    if dry_run:
        effective = _config_with_submission_resources(config, resources)
        state = load_state(workdir)
        submitted = submit_selected_jobs(
            workdir,
            effective,
            state,
            list(jobs),
            scheduler=scheduler_from_config(effective.scheduler),
            confirmed=confirmed,
            dry_run=True,
            require_prepared_check=True,
        )
        for name, job_id in submitted.items():
            output(f"{name}: {job_id}")
        return 0

    from .operations.controller import WorkbenchController

    controller = WorkbenchController(
        workdir,
        config_path=config_path,
        scheduler_factory=scheduler_from_config,
    )
    plan = controller.plan("submit-selected", resources, selected=tuple(jobs))
    if plan.blocked_reason:
        raise RuntimeError(f"{plan.blocked_reason}；禁止重复 qsub。")
    result = controller.execute(plan, confirmed=confirmed)
    if not result.ok:
        detail = result.error.suggestion_zh if result.error is not None else result.message
        raise RuntimeError(f"{result.message} {detail}；禁止重复 qsub。")
    for name, job_id in result.job_ids.items():
        output(f"{name}: {job_id}")
    return 0


def _cmd_analyze(args, output: OutputFn) -> int:
    target_potentials = _parse_float_list(args.target_u)
    output_path = Path(args.output) if args.output else Path(args.summary).with_name("analysis.json")
    if args.clean_summary and args.adsorbate_summary:
        analysis = analyze_adsorption(
            read_summary(Path(args.clean_summary)),
            read_summary(Path(args.adsorbate_summary)),
            target_potentials,
            reference_energy=args.reference_energy,
        )
    else:
        analysis = analyze_rows(read_summary(Path(args.summary)), target_potentials=target_potentials)
    write_analysis(output_path, analysis)
    output(f"wrote {output_path}")
    return 0


def _open_menu(
    workdir: Path,
    config_path: Optional[Path] = None,
    input_fn: InputFn = input,
    output: OutputFn = print,
) -> int:
    from .interactive_menu import run_menu

    return run_menu(
        workdir,
        config_path=config_path,
        input_fn=input_fn,
        output=output,
    )


def _ensure_standard_inputs(workdir: Path, noninteractive: bool, input_fn: InputFn, output: OutputFn) -> None:
    if not (workdir / "POSCAR").exists():
        raise FileNotFoundError(f"POSCAR is missing: {workdir / 'POSCAR'}")
    missing = [name for name in ("POTCAR", "KPOINTS") if not (workdir / name).exists()]
    if not missing:
        return
    if noninteractive:
        raise FileNotFoundError(f"missing standard VASP inputs: {', '.join(missing)}")
    executable = vaspkit_executable()
    if executable is None:
        raise FileNotFoundError(f"missing {', '.join(missing)} and vaspkit is unavailable")
    output(f"Missing {', '.join(missing)}; launching VASPKIT in {workdir}")
    if not _confirm("Open interactive VASPKIT now?", input_fn):
        raise FileNotFoundError(f"missing standard VASP inputs: {', '.join(missing)}")
    subprocess.run([executable], cwd=workdir, check=False)
    remaining = [name for name in missing if not (workdir / name).exists()]
    if remaining:
        raise FileNotFoundError(f"VASPKIT did not create: {', '.join(remaining)}")


def _print_plan(config: KitConfig, preview, output: OutputFn) -> None:
    max_inflight = config.scheduler.max_inflight
    output(
        f"scheduler={config.scheduler.kind} queue={config.scheduler.queue} cores/job={config.scheduler.cores} "
        f"memory={config.scheduler.memory or '-'} walltime={config.scheduler.walltime} "
        f"max_inflight={max_inflight if max_inflight is not None else 'unlimited'}"
    )
    if preview.get("global_active", preview["active"]) != preview["active"]:
        output(f"workflow active jobs: {preview['active']}")
        output(f"global queue active/unknown jobs (informational): {preview['global_active']}")
    else:
        output(f"current active/unknown jobs: {preview['active']}")
    if preview.get("node_slots"):
        output(f"available selected-node slots: {', '.join(preview['node_slots'])}")
    for entry in preview["queue"]:
        output(f"  queue {entry.job_id or '-'} {entry.state} {entry.name}")
    for index, batch in enumerate(preview["batches"], 1):
        output(f"  batch {index}: {', '.join(batch)}")


def _print_state(state: WorkflowState, output: OutputFn) -> None:
    output(f"stage={state.stage}")
    if state.neutral is not None:
        output(f"  neutral: {state.neutral.status} job={state.neutral.job_id or '-'}")
    for name, record in state.jobs.items():
        diagnostics = ";".join(record.diagnostics)
        output(f"  {name}: {record.status} job={record.job_id or '-'} restart={record.restart_count} {diagnostics}")


def _print_neutral_state(state: WorkflowState, output: OutputFn) -> None:
    if state.neutral is None:
        output("neutral: NOT_SUBMITTED")
        return
    diagnostics = ";".join(state.neutral.diagnostics)
    output(f"neutral: {state.neutral.status} job={state.neutral.job_id or '-'} {diagnostics}")


def _state_for_plan(workdir: Path, config: KitConfig) -> WorkflowState:
    path = workdir / STATE_FILENAME
    if path.exists():
        state = WorkflowState.load(path)
        if state.jobs:
            return state
    return WorkflowState(
        stage="planned",
        jobs={name: JobRecord(folder=str(Path(config.workflow.job_root) / name)) for name in config.workflow.folders},
    )


def _print_critical_incar(text: str, output: OutputFn) -> None:
    values = {}
    for line in text.splitlines():
        clean = line.split("!", 1)[0].split("#", 1)[0]
        if "=" in clean:
            key, value = clean.split("=", 1)
            values[key.strip().upper()] = value.strip()
    output("Critical INCAR parameters:")
    for key in CRITICAL_INCAR_TAGS:
        output(f"  {key} = {values.get(key, '<unset>')}")


def _parse_overrides(values) -> dict:
    parsed = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"INCAR override must be TAG=VALUE: {item}")
        key, value = item.split("=", 1)
        parsed[key.strip().upper()] = value.strip()
    return parsed


def _choose(label: str, choices: List[str], input_fn: InputFn, output: OutputFn) -> str:
    output(f"{label}:")
    for index, choice in enumerate(choices, 1):
        output(f"  {index}. {choice}")
    value = input_fn("Select: ").strip()
    try:
        return choices[int(value) - 1]
    except (ValueError, IndexError) as exc:
        raise ValueError(f"invalid {label} selection: {value}") from exc


def _confirm(prompt: str, input_fn: InputFn) -> bool:
    return input_fn(f"{prompt} [y/N]: ").strip().lower() in {"y", "yes"}


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--config", default=None)


def _add_submission_resource_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--resource-allocation",
        choices=("auto", "specified"),
        default=None,
    )
    parser.add_argument("--resource-node", action="append", default=[])
    parser.add_argument("--resource-cores", type=int, default=None)
    parser.add_argument("--save-resources", action="store_true")


def _parse_float_list(value: str) -> List[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()] if value else []


def _parse_node_names(value: str) -> List[str]:
    return list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def _prompt_value(prompt: str, default: str, input_fn: InputFn) -> str:
    value = input_fn(f"{prompt} [{default}]: ").strip()
    return value or default


def _prompt_positive_int(prompt: str, default: int, input_fn: InputFn) -> int:
    value = input_fn(f"{prompt} [{default}]: ").strip()
    try:
        parsed = int(value) if value else int(default)
    except ValueError as exc:
        raise ValueError(f"{prompt} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{prompt} must be positive")
    return parsed


class _OfflineScheduler:
    def inspect(self):
        return []
