from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .config import KitConfig
from .convergence import check_job
from .parsers import parse_outcar
from .scheduler import PBSScheduler, plan_submission_batches, scheduler_from_config
from .state import JobRecord, WorkflowState, workflow_state_lock
from .workflow import _write_charge_incar, atomic_copy, job_folder_path


STATE_FILENAME = "vaspsolkit.state.json"
ACTIVE_QUEUE_STATES = {
    "Q",
    "R",
    "QUEUED",
    "RUNNING",
    "PENDING",
    "CONFIGURING",
    "COMPLETING",
    "UNKNOWN",
}
NEUTRAL_REQUIRED_OUTPUTS = ("OUTCAR", "CONTCAR", "CHGCAR", "LOCPOT")
CHARGE_REQUIRED_OUTPUTS = ("CONTCAR", "CHGCAR", "LOCPOT")
NEUTRAL_ACTIVE_STATUSES = ACTIVE_QUEUE_STATES | {"SUBMITTED"}
RESETTABLE_QUEUE_STATES = {"Q", "QUEUED", "PENDING", "SUBMITTED"}
UNCANCELLABLE_QUEUE_STATES = {"R", "RUNNING", "CONFIGURING", "COMPLETING", "UNKNOWN"}
TERMINAL_JOB_STATUSES = {"CONVERGED", "NEEDS_REVIEW", "FAILED", "BLOCKED"}


@dataclass(frozen=True)
class RecordedJobsSnapshot:
    base: Path
    state_path: Path
    case_identity: tuple
    state_fingerprint: tuple
    job_ids: tuple


@dataclass(frozen=True)
class RecordedJobStatuses:
    snapshot: RecordedJobsSnapshot
    statuses: tuple


class PostSubmitPersistenceError(RuntimeError):
    """qsub succeeded, but recording its accepted Job ID failed."""

    def __init__(self, job_id: str, command: str, cause: BaseException) -> None:
        self.job_id = job_id
        self.command = command
        self.cause = cause
        super().__init__(
            f"{command} accepted job {job_id}, but state persistence failed: {cause}"
        )


@dataclass(frozen=True)
class _SourceFileFingerprint:
    path: Path
    resolved_path: Path
    link_target: str
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int
    sha256: str


def _state_path(base: Path, state_path: Optional[Path] = None) -> Path:
    return Path(state_path) if state_path else Path(base) / STATE_FILENAME


def _has_no_vasp_runtime_outputs(folder: Path) -> bool:
    """Return whether a prepared charge folder has never produced VASP output."""
    folder = Path(folder)
    return all(
        not (folder / filename).exists() or (folder / filename).stat().st_size == 0
        for filename in ("OUTCAR", "OSZICAR")
    )


def _mark_charge_prepared(record: JobRecord) -> None:
    record.job_id = ""
    record.status = "PREPARED"
    record.diagnostics = []


def _reconcile_charge_record(
    base: Path,
    record: JobRecord,
    queue_state,
    required_outputs=CHARGE_REQUIRED_OUTPUTS,
) -> str:
    """Reconcile one charge record from a fresh scheduler result and local outputs."""
    scheduler_state = str(queue_state.state).upper()
    folder = Path(base) / record.folder
    if scheduler_state == "MISSING" and _has_no_vasp_runtime_outputs(folder):
        _mark_charge_prepared(record)
        return "missing-unstarted"
    diagnostic = check_job(
        folder,
        scheduler_state=scheduler_state,
        required_outputs=required_outputs,
    )
    record.status = diagnostic.status
    record.diagnostics = diagnostic.diagnostics
    return scheduler_state


def _update_charge_stage(state: WorkflowState) -> None:
    statuses = {record.status for record in state.jobs.values()}
    if statuses and statuses == {"CONVERGED"}:
        state.stage = "converged"
    elif "NEEDS_REVIEW" in statuses or "FAILED" in statuses:
        state.stage = "needs_review"
    elif "PREPARED" in statuses and not statuses.intersection(ACTIVE_QUEUE_STATES | {"SUBMITTED"}):
        state.stage = "charge_ready"
    else:
        state.stage = "monitor"


def _is_missing_cancel_error(error: RuntimeError) -> bool:
    message = str(error).lower()
    return any(
        phrase in message
        for phrase in ("nonexistent job id", "unknown job", "job not found")
    )


def _load_or_create_state(base: Path, state_path: Optional[Path] = None) -> WorkflowState:
    path = _state_path(base, state_path)
    return WorkflowState.load(path) if path.exists() else WorkflowState()


def _require_nonempty(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        raise FileNotFoundError(f"required file is missing or empty: {path}")


def _validate_neutral_inputs(base: Path, script: str) -> None:
    for filename in ("INCAR", "POSCAR", "POTCAR", "KPOINTS", script):
        _require_nonempty(base / filename)


def prepare_neutral_job(
    base: Path,
    config: KitConfig,
    state_path: Optional[Path] = None,
    archive_path: Optional[Path] = None,
) -> WorkflowState:
    """Archive old results and prepare a neutral geometry-optimization stage."""
    base = Path(base)
    config.validate()
    _validate_neutral_inputs(base, config.scheduler.script)
    state_file = _state_path(base, state_path)
    with workflow_state_lock(state_file):
        old_state = (
            WorkflowState.load(state_file)
            if state_file.exists()
            else WorkflowState()
        )
        if _state_has_active_jobs(old_state):
            raise RuntimeError(
                "active jobs are recorded; check or stop them before re-preparing neutral"
            )

        source_incar = base / "INCAR"
        incar_text = source_incar.read_text(encoding="utf-8", errors="ignore")
        archive = _neutral_archive_path(base, archive_path)
        poscar_fingerprint = _capture_source_file(base, base / "POSCAR")
        state, cleanup_warning = _apply_neutral_preparation_transaction(
            base=base,
            state_file=state_file,
            archive=archive,
            neutral_profile=config.workflow.neutral_profile,
            incar_text=incar_text,
            poscar_fingerprint=poscar_fingerprint,
        )
    if cleanup_warning and state.neutral is not None:
        state.neutral.metadata["cleanup_warning"] = cleanup_warning
    return state


def submit_neutral_job(
    base: Path,
    config: KitConfig,
    scheduler=None,
    confirmed: bool = False,
    dry_run: bool = False,
    state_path: Optional[Path] = None,
    require_prepared: bool = False,
) -> WorkflowState:
    """Submit exactly one neutral job and return without polling."""
    if not confirmed and not dry_run:
        raise PermissionError("submission requires explicit confirmation or --yes")
    base = Path(base)
    config.validate()
    _validate_neutral_inputs(base, config.scheduler.script)
    state = _load_or_create_state(base, state_path)
    if require_prepared and (
        state.neutral is None
        or state.neutral.status != "PREPARED"
        or state.neutral.metadata.get("stage") != "neutral_relax"
    ):
        raise RuntimeError("run prepare-neutral before submit-neutral")
    if state.neutral is not None:
        if state.neutral.job_id:
            raise RuntimeError(
                f"neutral job already recorded: {state.neutral.job_id}; run check-neutral before resubmitting"
            )
        if state.neutral.status == "CONVERGED":
            raise RuntimeError("neutral result is already marked CONVERGED; do not resubmit")
    scheduler = scheduler or scheduler_from_config(config.scheduler)
    kwargs = {"dry_run": dry_run}
    if isinstance(scheduler, PBSScheduler):
        kwargs.update(
            {
                "queue": config.scheduler.queue,
                "node": config.scheduler.nodes[0] if config.scheduler.nodes else None,
                "ppn": config.scheduler.cores,
                "walltime": config.scheduler.walltime,
            }
        )
    job_id = scheduler.submit(base, config.scheduler.script, **kwargs)
    if dry_run:
        return state
    metadata = dict(state.neutral.metadata) if state.neutral is not None else {}
    state.neutral = JobRecord(folder=".", status="SUBMITTED", job_id=job_id, metadata=metadata)
    state.stage = "neutral_submitted"
    try:
        state.save(_state_path(base, state_path))
    except (OSError, RuntimeError, ValueError) as exc:
        raise PostSubmitPersistenceError(job_id, "qsub", exc) from exc
    return state


def check_neutral_job(
    base: Path,
    config: KitConfig,
    scheduler=None,
    job_id: Optional[str] = None,
    state_path: Optional[Path] = None,
    require_relaxation: bool = False,
) -> WorkflowState:
    """Perform one neutral scheduler/output check without submitting or waiting."""
    base = Path(base)
    config.validate()
    state = _load_or_create_state(base, state_path)
    if require_relaxation:
        if state.neutral is None:
            raise RuntimeError("neutral relaxation provenance is missing; run prepare-neutral first")
        if state.neutral.metadata.get("stage") != "neutral_relax":
            # Repair states written by the first implementation, which lost
            # metadata when submit-neutral replaced the JobRecord.
            legacy_tags = _read_incar_tags(base / "INCAR")
            if _is_geometry_optimization(legacy_tags):
                state.neutral.metadata.update(
                    {"stage": "neutral_relax", "profile": config.workflow.neutral_profile}
                )
            else:
                raise RuntimeError("neutral relaxation provenance is missing; run prepare-neutral first")
        tags = _read_incar_tags(base / "INCAR")
        if not _is_geometry_optimization(tags):
            raise RuntimeError("neutral INCAR is not a geometry optimization")
    scheduler = scheduler or scheduler_from_config(config.scheduler)
    neutral = state.neutral or JobRecord(folder=".", status="PREPARED")
    if job_id:
        if neutral.job_id and neutral.job_id != job_id:
            raise RuntimeError(
                f"neutral state already records job {neutral.job_id}; refusing to replace it with {job_id}"
            )
        neutral.job_id = job_id
        if neutral.status == "PREPARED":
            neutral.status = "SUBMITTED"
    scheduler_state = "MISSING"
    if neutral.job_id:
        scheduler_state = scheduler.status(neutral.job_id).state
    diagnostic = check_job(
        base,
        scheduler_state=scheduler_state,
        required_outputs=NEUTRAL_REQUIRED_OUTPUTS,
    )
    neutral.status = diagnostic.status
    neutral.diagnostics = diagnostic.diagnostics
    state.neutral = neutral
    if diagnostic.status == "CONVERGED":
        neutral.metadata.update(
            {
                "optimized_contcar_sha256": _sha256(base / "CONTCAR"),
                "neutral_chgcar_sha256": _sha256(base / "CHGCAR"),
                "nelect_ref": f"{parse_outcar(base / 'OUTCAR').nelect:.8f}",
            }
        )
        state.stage = "neutral_converged"
    elif diagnostic.status in {"QUEUED", "RUNNING", "SUBMITTED", "UNKNOWN"}:
        state.stage = "neutral_submitted"
    else:
        state.stage = "neutral_needs_review"
    state.save(_state_path(base, state_path))
    return state


def refresh_neutral_scheduler_state(
    base: Path,
    config: KitConfig,
    state: WorkflowState,
    scheduler=None,
    state_path: Optional[Path] = None,
) -> WorkflowState:
    """Refresh only the neutral scheduler state; do not claim VASP convergence."""
    if state.neutral is None or not state.neutral.job_id:
        return state
    scheduler = scheduler or scheduler_from_config(config.scheduler)
    scheduler_state = scheduler.status(state.neutral.job_id).state.upper()
    if scheduler_state in {"Q", "QUEUED", "PENDING"}:
        state.neutral.status = "QUEUED"
    elif scheduler_state in {"R", "RUNNING", "CONFIGURING", "COMPLETING"}:
        state.neutral.status = "RUNNING"
    elif scheduler_state == "UNKNOWN":
        state.neutral.status = "UNKNOWN"
    else:
        state.neutral.status = "SUBMITTED"
    state.stage = "neutral_submitted"
    state.save(_state_path(base, state_path))
    return state


def refresh_recorded_jobs(
    base: Path,
    config: KitConfig,
    state: WorkflowState,
    scheduler=None,
    state_path: Optional[Path] = None,
) -> WorkflowState:
    """Synchronous compatibility wrapper around capture, collect and apply."""
    config.validate()
    snapshot = capture_recorded_jobs(
        base, state_path=state_path, expected_state=state
    )
    if not snapshot.job_ids:
        return copy.deepcopy(state)
    scheduler = scheduler or scheduler_from_config(config.scheduler)
    statuses = collect_recorded_job_statuses(snapshot, scheduler)
    return apply_recorded_job_statuses(statuses)


def capture_recorded_jobs(
    base: Path,
    state_path: Optional[Path] = None,
    expected_state: Optional[WorkflowState] = None,
) -> RecordedJobsSnapshot:
    """Capture immutable Case/state identity before any scheduler I/O."""
    requested_base = Path(base).expanduser().absolute()
    case_identity = _refresh_case_identity(requested_base)
    base = Path(case_identity[0])
    state_file = _state_path(base, state_path)
    try:
        state_file.absolute().relative_to(base)
    except ValueError as exc:
        raise RuntimeError("workflow state must remain inside the Case") from exc
    with workflow_state_lock(state_file):
        state_fingerprint = _refresh_file_fingerprint(state_file)
        if state_fingerprint is None:
            if expected_state is None or not _refreshable_records(expected_state):
                return RecordedJobsSnapshot(
                    base, state_file, case_identity, (), ()
                )
            raise RuntimeError("recorded jobs require an existing workflow state file")
        disk_state = WorkflowState.load(state_file)
        if (
            expected_state is not None
            and _state_payload(disk_state) != _state_payload(expected_state)
        ):
            raise RuntimeError("workflow state changed before scheduler refresh")
        job_ids = tuple(
            dict.fromkeys(record.job_id for record in _refreshable_records(disk_state))
        )
        return RecordedJobsSnapshot(
            base, state_file, case_identity, state_fingerprint, job_ids
        )


def collect_recorded_job_statuses(
    snapshot: RecordedJobsSnapshot, scheduler
) -> RecordedJobStatuses:
    """Query recorded IDs only; this function never reads or writes Case files."""
    statuses = tuple(scheduler.status(job_id) for job_id in snapshot.job_ids)
    mismatched = [
        (expected, actual.job_id)
        for expected, actual in zip(snapshot.job_ids, statuses)
        if actual.job_id != expected
    ]
    if mismatched:
        expected, actual = mismatched[0]
        raise RuntimeError(
            f"scheduler returned a different Job ID: expected {expected}, got {actual}"
        )
    unknown = [
        queue_state.job_id
        for queue_state in statuses
        if str(queue_state.state).upper() == "UNKNOWN"
    ]
    if unknown:
        raise RuntimeError(
            "scheduler returned UNKNOWN for recorded Job ID(s): "
            + ", ".join(unknown)
        )
    return RecordedJobStatuses(snapshot, statuses)


def apply_recorded_job_statuses(collected: RecordedJobStatuses) -> WorkflowState:
    """Commit one collected result under the shared cross-process state lock."""
    snapshot = collected.snapshot
    returned_ids = tuple(status.job_id for status in collected.statuses)
    if returned_ids != snapshot.job_ids:
        raise RuntimeError("STALE: collected Job IDs do not match the captured query")
    if _refresh_case_identity(snapshot.base) != snapshot.case_identity:
        raise RuntimeError("STALE: Case identity changed during scheduler refresh")
    by_id = {status.job_id: status for status in collected.statuses}

    def update(current: WorkflowState) -> WorkflowState:
        if _refresh_file_fingerprint(snapshot.state_path) != snapshot.state_fingerprint:
            raise RuntimeError("STALE: workflow state changed during scheduler refresh")
        refreshed = copy.deepcopy(current)
        for record in _refreshable_records(refreshed):
            queue_state = by_id.get(record.job_id)
            if queue_state is not None:
                _apply_recorded_scheduler_state(snapshot.base, record, queue_state)
        _update_refreshed_stage(refreshed)
        return refreshed

    return WorkflowState.locked_update(snapshot.state_path, update)


def _refreshable_records(state: WorkflowState) -> List[JobRecord]:
    records: List[JobRecord] = []
    if (
        state.neutral is not None
        and state.neutral.job_id
        and state.neutral.status not in TERMINAL_JOB_STATUSES
    ):
        records.append(state.neutral)
    records.extend(
        record
        for record in state.jobs.values()
        if record.job_id and record.status not in TERMINAL_JOB_STATUSES
    )
    return records


def _apply_recorded_scheduler_state(
    base: Path, record: JobRecord, queue_state
) -> None:
    scheduler_state = str(queue_state.state).upper()
    if scheduler_state in {"Q", "QUEUED", "PENDING"}:
        record.status = "QUEUED"
        return
    if scheduler_state in {"R", "RUNNING", "CONFIGURING", "COMPLETING"}:
        record.status = "RUNNING"
        return
    if scheduler_state == "UNKNOWN":
        # A failed/ambiguous qstat must not overwrite the last trusted state.
        return
    if record.folder != ".":
        _reconcile_charge_record(base, record, queue_state)
        return
    folder = (base / record.folder).resolve()
    try:
        folder.relative_to(base)
    except ValueError as exc:
        raise RuntimeError("recorded job folder must remain inside the Case") from exc
    required_outputs = (
        NEUTRAL_REQUIRED_OUTPUTS if record.folder == "." else CHARGE_REQUIRED_OUTPUTS
    )
    diagnostic = check_job(
        folder,
        scheduler_state=scheduler_state,
        required_outputs=required_outputs,
    )
    record.status = diagnostic.status
    record.diagnostics = diagnostic.diagnostics


def _update_refreshed_stage(state: WorkflowState) -> None:
    if state.jobs:
        _update_charge_stage(state)
        return
    records = (
        ([state.neutral] if state.neutral is not None else [])
        + list(state.jobs.values())
    )
    statuses = {record.status for record in records}
    if statuses and statuses <= {"CONVERGED"}:
        state.stage = "converged" if state.jobs else "neutral_converged"
    elif statuses.intersection({"NEEDS_REVIEW", "FAILED", "BLOCKED"}):
        state.stage = "needs_review" if state.jobs else "neutral_needs_review"
    elif statuses.intersection({"SUBMITTED", "QUEUED", "RUNNING", "UNKNOWN"}):
        state.stage = "monitor" if state.jobs else "neutral_submitted"


def _refresh_case_identity(base: Path) -> tuple:
    entry = base.lstat()
    target = base.stat()
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
        raise RuntimeError("Case path must remain a non-symlink directory")
    return (
        str(base.resolve(strict=True)),
        target.st_dev,
        target.st_ino,
        stat.S_IFMT(target.st_mode),
    )


def _refresh_file_fingerprint(path: Path) -> Optional[tuple]:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
        raise RuntimeError("workflow state must remain a regular non-symlink file")
    return (
        entry.st_dev,
        entry.st_ino,
        entry.st_mode,
        entry.st_size,
        entry.st_mtime_ns,
        entry.st_ctime_ns,
        _sha256(path),
    )


def _state_payload(state: WorkflowState) -> dict:
    return {
        "stage": state.stage,
        "jobs": {name: asdict(record) for name, record in state.jobs.items()},
        "neutral": asdict(state.neutral) if state.neutral is not None else None,
        "prepared_checked": state.prepared_checked,
    }


def prepare_kit_jobs(base: Path, config: KitConfig, state_path: Optional[Path] = None) -> WorkflowState:
    """Compatibility entry point for callers of the old prepare API."""
    return prepare_charge_jobs(base, config, state_path=state_path, strict=False)


def prepare_charge_jobs(
    base: Path,
    config: KitConfig,
    state_path: Optional[Path] = None,
    strict: bool = True,
) -> WorkflowState:
    """Prepare charge-point geometry optimizations from neutral CONTCAR/CHGCAR."""
    base = Path(base)
    config.validate()
    workflow = config.workflow
    state_file = _state_path(base, state_path)
    state = WorkflowState.load(state_file) if state_file.exists() else None
    if state is None or state.neutral is None or state.neutral.status != "CONVERGED":
        raise RuntimeError("neutral relaxation must be CONVERGED before preparing charge jobs")
    if strict and state.neutral.metadata.get("stage") != "neutral_relax":
        raise RuntimeError("neutral relaxation provenance is missing; re-run prepare-neutral")
    if _state_has_active_jobs(state):
        raise RuntimeError("active jobs are recorded; check them before preparing charge jobs")
    for filename in ("INCAR", "POTCAR", "KPOINTS", config.scheduler.script, *NEUTRAL_REQUIRED_OUTPUTS):
        _require_nonempty(base / filename)
    if strict:
        _archive_charge_sweep(base, state_file)

    nelect_ref = workflow.nelect_ref
    if nelect_ref is None:
        nelect_ref = parse_outcar(base / "OUTCAR").nelect
    neutral_metadata = state.neutral.metadata
    neutral_metadata.update(
        {
            "optimized_contcar_sha256": _sha256(base / "CONTCAR"),
            "neutral_chgcar_sha256": _sha256(base / "CHGCAR"),
            "nelect_ref": f"{nelect_ref:.8f}",
        }
    )

    state.stage = "charge_prepared"
    state.prepared_checked = False
    state.jobs = {}
    for folder_name, offset in zip(workflow.folders, workflow.nelect_offsets):
        if not workflow.charge_points_include_neutral and abs(offset) <= 1.0e-12:
            continue
        target = job_folder_path(base, workflow, folder_name)
        target.mkdir(parents=True, exist_ok=True)
        for filename in ("POTCAR", "KPOINTS", config.scheduler.script):
            shutil.copy2(base / filename, target / filename)
        for filename in workflow.copy_files:
            if filename in {"POTCAR", "KPOINTS", config.scheduler.script, "CHGCAR", "INCAR"}:
                continue
            source = base / filename
            if not source.exists():
                continue
            shutil.copy2(source, target / filename)
        shutil.copy2(base / "INCAR", target / "INCAR")
        atomic_copy(base / "CHGCAR", target / "CHGCAR")
        shutil.copy2(base / "CONTCAR", target / "POSCAR")
        _write_charge_incar(
            target / "INCAR",
            nelect_ref + offset,
            profile=workflow.charge_profile,
        )
        state.jobs[folder_name] = JobRecord(
            folder=str(target.relative_to(base)),
            status="PREPARED",
            metadata={
                "stage": "charge_relax",
                "offset": f"{offset:.8f}",
                "nelect": f"{nelect_ref + offset:.8f}",
                "source_contcar_sha256": neutral_metadata["optimized_contcar_sha256"],
                "source_chgcar_sha256": neutral_metadata["neutral_chgcar_sha256"],
            },
        )
    state.save(state_file)
    return state


def check_prepared_jobs(
    base: Path,
    config: KitConfig,
    state: Optional[WorkflowState] = None,
    state_path: Optional[Path] = None,
) -> WorkflowState:
    """Validate every charge input and mark the state safe for submission."""
    base = Path(base)
    config.validate()
    state_file = _state_path(base, state_path)
    state = state or (WorkflowState.load(state_file) if state_file.exists() else None)
    if state is None or state.neutral is None or state.neutral.status != "CONVERGED":
        raise RuntimeError("neutral relaxation must be CONVERGED before checking charge inputs")
    if state.neutral.metadata.get("stage") != "neutral_relax":
        raise RuntimeError("neutral relaxation provenance is missing; re-run prepare-neutral")
    if not state.jobs:
        raise RuntimeError("no charge jobs have been prepared")
    errors = []
    nelect_ref = config.workflow.nelect_ref
    if nelect_ref is None:
        value = state.neutral.metadata.get("nelect_ref")
        if value is None:
            nelect_ref = parse_outcar(base / "OUTCAR").nelect
        else:
            nelect_ref = float(value)
    offsets = dict(zip(config.workflow.folders, config.workflow.nelect_offsets))
    for name, record in state.jobs.items():
        folder = base / record.folder
        try:
            _validate_job_inputs(folder, config.scheduler.script)
            _require_nonempty(folder / "CHGCAR")
            if _sha256(folder / "POSCAR") != _sha256(base / "CONTCAR"):
                raise ValueError("POSCAR is not copied from neutral CONTCAR")
            if _sha256(folder / "CHGCAR") != _sha256(base / "CHGCAR"):
                raise ValueError("CHGCAR is not copied from neutral CHGCAR")
            tags = _read_incar_tags(folder / "INCAR")
            if not _is_geometry_optimization(tags):
                raise ValueError("INCAR is not a geometry optimization")
            if tags.get("ISTART") != "0" or tags.get("ICHARG") != "1":
                raise ValueError("INCAR must read neutral CHGCAR with ISTART=0 and ICHARG=1")
            expected = nelect_ref + offsets[name]
            if abs(float(tags.get("NELECT", "nan")) - expected) > 1.0e-6:
                raise ValueError(f"NELECT does not match expected value {expected:.6f}")
        except (FileNotFoundError, KeyError, ValueError) as error:
            errors.append(f"{name}: {error}")
    if errors:
        raise RuntimeError("charge input validation failed: " + "; ".join(errors))
    state.prepared_checked = True
    state.stage = "charge_ready"
    state.save(state_file)
    return state

def submission_preview(config: KitConfig, state: WorkflowState, scheduler=None) -> Dict[str, object]:
    scheduler = scheduler or scheduler_from_config(config.scheduler)
    queue = scheduler.inspect()
    global_active = sum(entry.state.upper() in ACTIVE_QUEUE_STATES for entry in queue)
    prepared = [name for name, job in state.jobs.items() if job.status == "PREPARED"]
    node_slots: List[str] = []
    if isinstance(scheduler, PBSScheduler):
        active = _workflow_active_count(state)
        if config.scheduler.nodes:
            if config.scheduler.max_inflight is None:
                node_slots = [
                    config.scheduler.nodes[index % len(config.scheduler.nodes)]
                    for index in range(len(prepared))
                ]
            else:
                node_slots = scheduler.available_nodes(
                    count=config.scheduler.max_inflight,
                    min_node=config.workflow.qsub_min_node,
                    ppn=config.scheduler.cores,
                    selected_nodes=config.scheduler.nodes,
                )
        capacity = (
            len(node_slots)
            if config.scheduler.nodes and config.scheduler.max_inflight is not None
            else None
        )
    else:
        active = global_active
        capacity = None
    return {
        "queue": queue,
        "active": active,
        "global_active": global_active,
        "node_slots": node_slots,
        "prepared": prepared,
        "batches": plan_submission_batches(
            prepared,
            config.scheduler.max_inflight,
            active=active,
            capacity=capacity,
        ),
    }


def submit_ready_jobs(
    base: Path,
    config: KitConfig,
    state: WorkflowState,
    scheduler=None,
    confirmed: bool = False,
    dry_run: bool = False,
    state_path: Optional[Path] = None,
    require_prepared_check: bool = False,
) -> Dict[str, str]:
    if not confirmed and not dry_run:
        raise PermissionError("submission requires explicit confirmation or --yes")
    base = Path(base)
    if require_prepared_check and not state.prepared_checked:
        raise RuntimeError("run check-prepared before submitting charge jobs")
    scheduler = scheduler or scheduler_from_config(config.scheduler)
    preview = submission_preview(config, state, scheduler=scheduler)
    first_batch = preview["batches"][0] if preview["batches"] else []
    node_slots = preview.get("node_slots", [])
    submitted: Dict[str, str] = {}
    for index, name in enumerate(first_batch):
        record = state.jobs[name]
        folder = base / record.folder
        _validate_job_inputs(folder, config.scheduler.script)
        kwargs = {"dry_run": dry_run}
        if isinstance(scheduler, PBSScheduler):
            kwargs.update(
                {
                    "job_name": f"{base.name}-{name}",
                    "queue": config.scheduler.queue,
                    "ppn": config.scheduler.cores,
                    "walltime": config.scheduler.walltime,
                }
            )
            if index < len(node_slots):
                kwargs["node"] = node_slots[index]
        job_id = scheduler.submit(folder, config.scheduler.script, **kwargs)
        submitted[name] = job_id
        if not dry_run:
            record.job_id = job_id
            record.status = "SUBMITTED"
    if not dry_run:
        state.stage = "submitted" if submitted else state.stage
        state.save(Path(state_path) if state_path else base / STATE_FILENAME)
    return submitted


def submit_selected_jobs(
    base: Path,
    config: KitConfig,
    state: WorkflowState,
    selected: List[str],
    scheduler=None,
    confirmed: bool = False,
    dry_run: bool = False,
    state_path: Optional[Path] = None,
    require_prepared_check: bool = False,
) -> Dict[str, str]:
    if not confirmed and not dry_run:
        raise PermissionError("submission requires explicit confirmation or --yes")
    if require_prepared_check and not state.prepared_checked:
        raise RuntimeError("run check-prepared before submitting charge jobs")
    base = Path(base)
    names = [str(name) for name in selected]
    if not names:
        raise ValueError("at least one charge job must be selected")
    unknown = [name for name in names if name not in state.jobs]
    if unknown:
        raise KeyError(f"unknown charge job(s): {', '.join(unknown)}")
    not_ready = [name for name in names if state.jobs[name].status != "PREPARED"]
    if not_ready:
        raise RuntimeError(f"selected charge job(s) are not PREPARED: {', '.join(not_ready)}")

    scheduler = scheduler or scheduler_from_config(config.scheduler)
    submitted: Dict[str, str] = {}
    for index, name in enumerate(names):
        record = state.jobs[name]
        folder = base / record.folder
        _validate_job_inputs(folder, config.scheduler.script)
        kwargs = {"dry_run": dry_run}
        if isinstance(scheduler, PBSScheduler):
            kwargs.update(
                {
                    "job_name": f"{base.name}-{name}",
                    "queue": config.scheduler.queue,
                    "ppn": config.scheduler.cores,
                    "walltime": config.scheduler.walltime,
                }
            )
            if config.scheduler.nodes:
                kwargs["node"] = config.scheduler.nodes[index % len(config.scheduler.nodes)]
        job_id = scheduler.submit(folder, config.scheduler.script, **kwargs)
        submitted[name] = job_id
        if not dry_run:
            record.job_id = job_id
            record.status = "SUBMITTED"
    if not dry_run:
        state.stage = "submitted" if submitted else state.stage
        state.save(Path(state_path) if state_path else base / STATE_FILENAME)
    return submitted


def reset_queued_jobs(
    base: Path,
    state: WorkflowState,
    selected: List[str],
    scheduler,
    confirmed: bool = False,
    state_path: Optional[Path] = None,
) -> Dict[str, str]:
    if not confirmed:
        raise PermissionError("resetting queued jobs requires explicit confirmation")
    base = Path(base)
    names = [str(name) for name in selected]
    if not names:
        raise ValueError("at least one queued charge job must be selected")
    unknown = [name for name in names if name not in state.jobs]
    if unknown:
        raise KeyError(f"unknown charge job(s): {', '.join(unknown)}")
    invalid = [
        name
        for name in names
        if state.jobs[name].status not in {"QUEUED", "SUBMITTED"}
    ]
    if invalid:
        raise RuntimeError(f"selected charge job(s) are not QUEUED/SUBMITTED: {', '.join(invalid)}")

    state_file = _state_path(base, state_path)
    preflight = {}
    for name in names:
        record = state.jobs[name]
        if not record.job_id:
            raise RuntimeError(f"selected charge job has no recorded job id: {name}")
        preflight[name] = scheduler.status(record.job_id)

    blocked = [
        name
        for name, queue_state in preflight.items()
        if str(queue_state.state).upper() in UNCANCELLABLE_QUEUE_STATES
    ]
    if blocked:
        raise RuntimeError(
            "selected charge job(s) are RUNNING/UNKNOWN; no job was cancelled: "
            + ", ".join(blocked)
        )
    unsupported = [
        name
        for name, queue_state in preflight.items()
        if str(queue_state.state).upper() not in RESETTABLE_QUEUE_STATES | {"MISSING"}
    ]
    if unsupported:
        raise RuntimeError(
            "selected charge job(s) are not currently QUEUED/SUBMITTED in PBS: "
            + ", ".join(unsupported)
        )

    reset: Dict[str, str] = {}
    needs_review = []
    for name, queue_state in preflight.items():
        if str(queue_state.state).upper() != "MISSING":
            continue
        record = state.jobs[name]
        old_job_id = record.job_id
        _reconcile_charge_record(base, record, queue_state)
        if record.status == "PREPARED":
            reset[name] = old_job_id
        elif record.status != "CONVERGED":
            needs_review.append(name)
        _update_charge_stage(state)
        state.save(state_file)
    if needs_review:
        raise RuntimeError(
            "selected charge job(s) disappeared from PBS but have local outputs requiring review: "
            + ", ".join(needs_review)
        )

    for name, initial_queue_state in preflight.items():
        if str(initial_queue_state.state).upper() == "MISSING":
            continue
        record = state.jobs[name]
        old_job_id = record.job_id
        current_queue_state = scheduler.status(old_job_id)
        current_state = str(current_queue_state.state).upper()
        if current_state in UNCANCELLABLE_QUEUE_STATES:
            raise RuntimeError(
                "selected charge job became RUNNING/UNKNOWN before cancellation; no further job was cancelled: "
                + name
            )
        if current_state == "MISSING":
            _reconcile_charge_record(base, record, current_queue_state)
            if record.status == "PREPARED":
                reset[name] = old_job_id
                _update_charge_stage(state)
                state.save(state_file)
                continue
            _update_charge_stage(state)
            state.save(state_file)
            raise RuntimeError(
                "selected charge job disappeared from PBS but has local outputs requiring review: " + name
            )
        if current_state not in RESETTABLE_QUEUE_STATES:
            raise RuntimeError(
                "selected charge job is no longer QUEUED/SUBMITTED in PBS: " + name
            )
        try:
            scheduler.cancel(old_job_id)
        except RuntimeError as exc:
            if not _is_missing_cancel_error(exc):
                raise
            after_cancel_state = scheduler.status(old_job_id)
            if str(after_cancel_state.state).upper() != "MISSING":
                raise
            _reconcile_charge_record(base, record, after_cancel_state)
            if record.status != "PREPARED":
                _update_charge_stage(state)
                state.save(state_file)
                raise RuntimeError(
                    "selected charge job disappeared from PBS but has local outputs requiring review: " + name
                )
        else:
            _mark_charge_prepared(record)
        reset[name] = old_job_id
        _update_charge_stage(state)
        state.save(state_file)
    _update_charge_stage(state)
    state.save(state_file)
    return reset


def refresh_state(
    base: Path,
    config: KitConfig,
    state: WorkflowState,
    scheduler=None,
    state_path: Optional[Path] = None,
) -> WorkflowState:
    base = Path(base)
    scheduler = scheduler or scheduler_from_config(config.scheduler)
    for record in state.jobs.values():
        if record.status == "PREPARED" or not record.job_id:
            continue
        queue_state = scheduler.status(record.job_id)
        _reconcile_charge_record(base, record, queue_state)
    _update_charge_stage(state)
    state.save(Path(state_path) if state_path else base / STATE_FILENAME)
    return state


def load_state(base: Path, state_path: Optional[Path] = None) -> WorkflowState:
    return WorkflowState.load(Path(state_path) if state_path else Path(base) / STATE_FILENAME)


def _validate_job_inputs(folder: Path, script: str) -> None:
    for filename in ("INCAR", "POSCAR", "POTCAR", "KPOINTS", script):
        path = folder / filename
        if not path.exists() or path.stat().st_size == 0:
            raise FileNotFoundError(f"required submission input is missing or empty: {path}")


def _read_incar_tags(path: Path) -> Dict[str, str]:
    tags: Dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        clean = line.split("!", 1)[0].split("#", 1)[0]
        if "=" not in clean:
            continue
        key, value = clean.split("=", 1)
        tags[key.strip().upper()] = value.strip()
    return tags


def _is_geometry_optimization(tags: Dict[str, str]) -> bool:
    try:
        return int(float(tags.get("IBRION", "-1"))) > 0 and int(float(tags.get("NSW", "0"))) > 0
    except ValueError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _neutral_archive_sources(base: Path) -> List[Path]:
    names = (
        "INCAR",
        "CONTCAR",
        "OUTCAR",
        "CHGCAR",
        "LOCPOT",
        "CHG",
        "WAVECAR",
        "vasprun.xml",
        "OSZICAR",
        "DOSCAR",
        "REPORT",
    )
    existing = [
        base / name
        for name in names
        if (base / name).exists() or (base / name).is_symlink()
    ]
    charge = base / "charge_sweep"
    if charge.exists() or charge.is_symlink():
        existing.append(charge)
    return existing


def _neutral_archive_path(base: Path, archive_path: Optional[Path]) -> Path:
    archive = (
        Path(archive_path)
        if archive_path is not None
        else base / ".vaspsolkit" / "archive" / datetime.now().strftime("restart-%Y%m%d-%H%M%S-%f")
    )
    try:
        archive.resolve().relative_to(base.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("neutral archive path must remain within the Case") from exc
    return archive


def _apply_neutral_preparation_transaction(
    *,
    base: Path,
    state_file: Path,
    archive: Path,
    neutral_profile: str,
    incar_text: str,
    poscar_fingerprint: _SourceFileFingerprint,
) -> tuple[WorkflowState, str]:
    """Apply neutral preparation as one rollback-capable same-filesystem transaction."""
    base = base.resolve()
    state_file = Path(state_file)
    try:
        state_file.resolve().relative_to(base)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("neutral state path must remain within the Case") from exc
    if archive.exists() or archive.is_symlink():
        raise FileExistsError(f"neutral archive already exists: {archive}")

    created_directories: List[Path] = []
    moved_to_archive: List[tuple[Path, Path]] = []
    copied_to_archive: List[Path] = []
    backed_up_targets: List[tuple[Path, Path]] = []
    installed_targets: List[Path] = []
    transaction: Optional[Path] = None
    try:
        _mkdir_tracked(base / ".vaspsolkit" / "transactions", created_directories)
        transaction = Path(
            tempfile.mkdtemp(prefix="prepare-neutral-", dir=base / ".vaspsolkit" / "transactions")
        )
        staged = transaction / "staged"
        backups = transaction / "backups"
        staged.mkdir()
        backups.mkdir()

        staged_incar = staged / "INCAR"
        shutil.copy2(base / "INCAR", staged_incar)
        if staged_incar.read_text(encoding="utf-8", errors="ignore") != incar_text:
            raise RuntimeError("INCAR changed while neutral preparation was staged")
        staged_poscar = staged / "POSCAR.initial"
        shutil.copy2(base / "POSCAR", staged_poscar)
        staged_poscar_sha256 = _sha256(staged_poscar)
        if staged_poscar_sha256 != poscar_fingerprint.sha256:
            raise RuntimeError("POSCAR changed while neutral preparation was staged")
        state = WorkflowState(
            stage="neutral_prepared",
            neutral=JobRecord(
                folder=".",
                status="PREPARED",
                metadata={
                    "stage": "neutral_relax",
                    "profile": neutral_profile,
                    "source_poscar_sha256": staged_poscar_sha256,
                    "archive": str(archive.relative_to(base)),
                },
            ),
            prepared_checked=False,
        )
        staged_state = staged / state_file.name
        _write_staged_state(staged_state, state, state_file)
        _verify_source_file(base, poscar_fingerprint)

        _mkdir_tracked(archive.parent, created_directories)
        archive.mkdir()
        created_directories.append(archive)
        for source in _neutral_archive_sources(base):
            destination = archive / source.name
            _move_archive_entry(source, destination)
            moved_to_archive.append((source, destination))
        if state_file.exists() or state_file.is_symlink():
            archived_state = archive / state_file.name
            copied_to_archive.append(archived_state)
            shutil.copy2(state_file, archived_state, follow_symlinks=False)

        provenance = base / ".vaspsolkit" / "provenance"
        _mkdir_tracked(provenance, created_directories)
        provenance_target = provenance / "POSCAR.initial"
        for target in (provenance_target, state_file):
            if target.exists() or target.is_symlink():
                backup = backups / target.name
                _move_archive_entry(target, backup)
                backed_up_targets.append((target, backup))

        for source, destination in (
            (staged_incar, base / "INCAR"),
            (staged_poscar, provenance_target),
            (staged_state, state_file),
        ):
            installed_targets.append(destination)
            _install_prepared_entry(source, destination)
        _verify_source_file(base, poscar_fingerprint)
        _fsync_directory(base)
    except BaseException:
        _rollback_neutral_preparation(
            moved_to_archive=moved_to_archive,
            copied_to_archive=copied_to_archive,
            backed_up_targets=backed_up_targets,
            installed_targets=installed_targets,
            created_directories=created_directories,
            transaction=transaction,
        )
        raise
    else:
        cleanup_warning = ""
        if transaction is not None:
            try:
                shutil.rmtree(transaction, ignore_errors=False)
            except OSError as exc:
                cleanup_warning = (
                    f"transaction cleanup failed: {exc}; path={transaction}"
                )
        _remove_empty_created_directories(created_directories, keep={archive, archive.parent, provenance})
        return state, cleanup_warning


def _move_archive_entry(source: Path, destination: Path) -> None:
    """Move one entry on the Case filesystem; kept separate for fault injection."""
    os.replace(source, destination)


def _install_prepared_entry(source: Path, destination: Path) -> None:
    """Atomically install one staged preparation target."""
    os.replace(source, destination)
    _fsync_directory(destination.parent)


def _write_staged_state(path: Path, state: WorkflowState, old_path: Path) -> None:
    for job in state.jobs.values():
        job.validate()
    if state.neutral is not None:
        state.neutral.validate()
    payload = {
        "stage": state.stage,
        "jobs": {name: asdict(job) for name, job in state.jobs.items()},
        "neutral": asdict(state.neutral) if state.neutral is not None else None,
        "prepared_checked": state.prepared_checked,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    mode = old_path.stat().st_mode & 0o777 if old_path.exists() else 0o644
    path.chmod(mode)


def _rollback_neutral_preparation(
    *,
    moved_to_archive: List[tuple[Path, Path]],
    copied_to_archive: List[Path],
    backed_up_targets: List[tuple[Path, Path]],
    installed_targets: List[Path],
    created_directories: List[Path],
    transaction: Optional[Path],
) -> None:
    rollback_errors: List[BaseException] = []
    for target in reversed(installed_targets):
        try:
            _remove_entry(target)
        except BaseException as exc:
            rollback_errors.append(exc)
    for target, backup in reversed(backed_up_targets):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, target)
        except BaseException as exc:
            rollback_errors.append(exc)
    for copied in reversed(copied_to_archive):
        try:
            _remove_entry(copied)
        except BaseException as exc:
            rollback_errors.append(exc)
    for source, destination in reversed(moved_to_archive):
        try:
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, source)
        except BaseException as exc:
            rollback_errors.append(exc)
    if transaction is not None:
        shutil.rmtree(transaction, ignore_errors=True)
    _remove_empty_created_directories(created_directories)
    if rollback_errors:
        raise RuntimeError(
            "neutral preparation failed and rollback was incomplete: "
            + "; ".join(str(error) for error in rollback_errors)
        )


def _remove_entry(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _mkdir_tracked(path: Path, created: List[Path]) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir()
        created.append(directory)


def _remove_empty_created_directories(
    created: List[Path], keep: Optional[set[Path]] = None
) -> None:
    retained = keep or set()
    for directory in reversed(created):
        if directory in retained:
            continue
        try:
            directory.rmdir()
        except OSError:
            pass


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _capture_source_file(base: Path, path: Path) -> _SourceFileFingerprint:
    base = base.resolve()
    entry = path.lstat()
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"source file must remain within the Case: {path.name}") from exc
    target = resolved.stat()
    if not resolved.is_file():
        raise ValueError(f"source file must be regular: {path.name}")
    return _SourceFileFingerprint(
        path=path,
        resolved_path=resolved,
        link_target=os.readlink(path) if path.is_symlink() else "",
        device=target.st_dev,
        inode=target.st_ino,
        mode=target.st_mode,
        size=target.st_size,
        modified_ns=target.st_mtime_ns,
        changed_ns=target.st_ctime_ns,
        sha256=_sha256(resolved),
    )


def _verify_source_file(base: Path, expected: _SourceFileFingerprint) -> None:
    try:
        current = _capture_source_file(base, expected.path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"{expected.path.name} changed during neutral preparation") from exc
    if current != expected:
        raise RuntimeError(f"{expected.path.name} changed during neutral preparation")


def _archive_charge_sweep(base: Path, state_file: Path) -> Optional[Path]:
    charge = base / "charge_sweep"
    if not charge.exists():
        return None
    archive = base / ".vaspsolkit" / "archive" / datetime.now().strftime("charge-%Y%m%d-%H%M%S-%f")
    archive.mkdir(parents=True, exist_ok=False)
    shutil.move(str(charge), archive / "charge_sweep")
    if state_file.exists():
        shutil.copy2(state_file, archive / state_file.name)
    return archive


def _state_has_active_jobs(state: WorkflowState) -> bool:
    active = ACTIVE_QUEUE_STATES | {"SUBMITTED"}
    records = list(state.jobs.values())
    if state.neutral is not None:
        records.append(state.neutral)
    return any(record.job_id and record.status in active for record in records)


def _workflow_active_count(state: WorkflowState) -> int:
    active_statuses = {"SUBMITTED", "QUEUED", "RUNNING", "UNKNOWN"}
    return sum(
        bool(record.job_id) and record.status in active_statuses
        for record in state.jobs.values()
    )
