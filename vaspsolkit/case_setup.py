"""Read-only planning and confirmed application for Case initialization."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .config import (
    EXPECT_ABSENT,
    KitConfig,
    SchedulerConfig,
    WorkflowConfig,
    serialize_kit_config,
    write_config_bytes,
)
from .inputs import plan_neutral_vaspsol_update, suggest_encut, validate_potcar_order
from .state import workflow_state_lock


CONFIG_FILENAME = "vaspsolkit.json"
STATE_FILENAME = "vaspsolkit.state.json"


@dataclass(frozen=True)
class PlannedFileChange:
    path: Path
    before: Optional[str]
    after: str
    change_type: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, Path)
            or not self.path.is_absolute()
            or self.path != Path(os.path.abspath(self.path))
        ):
            raise ValueError("path must be a normalized absolute path")
        if self.before is not None and not isinstance(self.before, str):
            raise TypeError("before must be a string or None")
        if not isinstance(self.after, str):
            raise TypeError("after must be a string")
        if self.change_type not in {"create", "update"}:
            raise ValueError("change_type must be create or update")


@dataclass(frozen=True)
class CaseSourceFingerprint:
    label: str
    source_path: Path
    resolved_path: Path
    sha256: str
    is_symlink: bool
    symlink_target: Optional[str]

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label:
            raise ValueError("source label must be non-empty")
        for name in ("source_path", "resolved_path"):
            path = getattr(self, name)
            if not isinstance(path, Path) or not path.is_absolute():
                raise ValueError(f"{name} must be an absolute Path")
        if not isinstance(self.sha256, str) or len(self.sha256) != 64:
            raise ValueError("sha256 must be a hexadecimal SHA-256 digest")
        try:
            int(self.sha256, 16)
        except ValueError as exc:
            raise ValueError("sha256 must be a hexadecimal SHA-256 digest") from exc
        if not isinstance(self.is_symlink, bool):
            raise TypeError("is_symlink must be a bool")
        if self.symlink_target is not None and not isinstance(self.symlink_target, str):
            raise TypeError("symlink_target must be a string or None")
        if self.is_symlink != (self.symlink_target is not None):
            raise ValueError("symlink_target must match is_symlink")


@dataclass(frozen=True)
class CaseTargetFingerprint:
    label: str
    path: Path
    entry_type: str
    symlink_target: Optional[str]
    sha256: Optional[str]

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label:
            raise ValueError("target label must be non-empty")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("target path must be an absolute Path")
        if self.entry_type not in {"absent", "regular", "symlink", "other"}:
            raise ValueError("invalid target entry_type")
        if self.symlink_target is not None and not isinstance(self.symlink_target, str):
            raise TypeError("symlink_target must be a string or None")
        if (self.entry_type == "symlink") != (self.symlink_target is not None):
            raise ValueError("symlink_target must match entry_type")
        if self.sha256 is not None:
            if not isinstance(self.sha256, str) or len(self.sha256) != 64:
                raise ValueError("sha256 must be a hexadecimal SHA-256 digest or None")
            try:
                int(self.sha256, 16)
            except ValueError as exc:
                raise ValueError("sha256 must be a hexadecimal SHA-256 digest or None") from exc


@dataclass(frozen=True)
class CaseDirectoryFingerprint:
    path: Path
    device: int
    inode: int
    mode: int

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("Case directory fingerprint path must be absolute")
        for name in ("device", "inode", "mode"):
            if isinstance(getattr(self, name), bool) or not isinstance(getattr(self, name), int):
                raise TypeError(f"{name} must be an integer")


class CaseInitializationApplyError(RuntimeError):
    """A structured staging or replacement failure.

    Each individual replacement is atomic. A failure after one replacement may
    leave that earlier file updated; this is deliberately not a multi-file
    transaction.
    """

    def __init__(self, phase: str, path: Path, cause: BaseException) -> None:
        self.phase = phase
        self.path = path
        self.cause = cause
        super().__init__(f"initialization {phase} failed for {path}: {cause}")


@dataclass(frozen=True)
class CaseInitializationPlan:
    workdir: Path
    case_fingerprint: CaseDirectoryFingerprint
    config: KitConfig
    config_before_bytes: Optional[bytes]
    incar_before: str
    incar_after: str
    file_changes: Tuple[PlannedFileChange, ...]
    source_fingerprints: Tuple[CaseSourceFingerprint, ...]
    target_fingerprints: Tuple[CaseTargetFingerprint, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.workdir, Path) or self.workdir != self.workdir.resolve():
            raise ValueError("workdir must be resolved")
        raw_config = object.__getattribute__(self, "config")
        if not isinstance(raw_config, KitConfig):
            raise TypeError("config must be a KitConfig")
        raw_config.validate()
        if self.config_before_bytes is not None and not isinstance(
            self.config_before_bytes, bytes
        ):
            raise TypeError("config_before_bytes must be bytes or None")
        if not isinstance(self.case_fingerprint, CaseDirectoryFingerprint):
            raise TypeError("case_fingerprint must be CaseDirectoryFingerprint")
        if self.case_fingerprint.path != self.workdir:
            raise ValueError("case_fingerprint must describe the planned Case")
        if not isinstance(self.incar_before, str) or not isinstance(self.incar_after, str):
            raise TypeError("INCAR snapshots must be strings")
        if not isinstance(self.file_changes, tuple):
            raise TypeError("file_changes must be a tuple")
        if any(not isinstance(change, PlannedFileChange) for change in self.file_changes):
            raise TypeError("file_changes must contain PlannedFileChange values")
        config_changes = tuple(
            change for change in self.file_changes if change.path.name == CONFIG_FILENAME
        )
        if len(config_changes) != 1:
            raise ValueError("file_changes must contain exactly one config change")
        if (self.config_before_bytes is None) != (config_changes[0].before is None):
            raise ValueError("config byte snapshot must match config preview presence")
        for change in self.file_changes:
            try:
                change.path.relative_to(self.workdir)
            except ValueError as exc:
                raise ValueError("planned path must remain within the Case") from exc
        if not isinstance(self.source_fingerprints, tuple):
            raise TypeError("source_fingerprints must be a tuple")
        if any(
            not isinstance(fingerprint, CaseSourceFingerprint)
            for fingerprint in self.source_fingerprints
        ):
            raise TypeError("source_fingerprints must contain CaseSourceFingerprint values")
        labels = tuple(fingerprint.label for fingerprint in self.source_fingerprints)
        if labels != ("POSCAR", "INCAR", "KPOINTS", "POTCAR", "script"):
            raise ValueError("source_fingerprints must cover all five initialization sources")
        for fingerprint in self.source_fingerprints:
            try:
                fingerprint.source_path.relative_to(self.workdir)
                fingerprint.resolved_path.relative_to(self.workdir)
            except ValueError as exc:
                raise ValueError("source fingerprint must remain within the Case") from exc
        if not isinstance(self.target_fingerprints, tuple):
            raise TypeError("target_fingerprints must be a tuple")
        if any(
            not isinstance(fingerprint, CaseTargetFingerprint)
            for fingerprint in self.target_fingerprints
        ):
            raise TypeError("target_fingerprints must contain CaseTargetFingerprint values")
        target_labels = tuple(fingerprint.label for fingerprint in self.target_fingerprints)
        if target_labels != ("INCAR", CONFIG_FILENAME, STATE_FILENAME):
            raise ValueError("target_fingerprints must cover all initialization targets")
        for fingerprint in self.target_fingerprints:
            try:
                fingerprint.path.relative_to(self.workdir)
            except ValueError as exc:
                raise ValueError("target fingerprint must remain within the Case") from exc
        object.__setattr__(self, "config", copy.deepcopy(raw_config))

    def __getattribute__(self, name: str):
        value = object.__getattribute__(self, name)
        if name == "config":
            return copy.deepcopy(value)
        return value


def plan_case_initialization(
    workdir: Path,
    scheduler_config: SchedulerConfig,
    workflow_config: Optional[WorkflowConfig] = None,
) -> CaseInitializationPlan:
    """Build a complete initialization preview without changing the filesystem."""
    if not isinstance(scheduler_config, SchedulerConfig):
        raise TypeError("scheduler_config must be a SchedulerConfig")
    scheduler = copy.deepcopy(scheduler_config)
    scheduler.validate()
    case = _resolved_case(workdir)
    script_path = _resolve_script(case, scheduler.script)
    required = {
        name: _required_file(case, name)
        for name in ("POSCAR", "INCAR", "KPOINTS", "POTCAR")
    }
    _require_nonempty(script_path, scheduler.script)
    source_fingerprints = tuple(
        _fingerprint_source(case, label, source_path, resolved_path)
        for label, source_path, resolved_path in (
            ("POSCAR", case / "POSCAR", required["POSCAR"]),
            ("INCAR", case / "INCAR", required["INCAR"]),
            ("KPOINTS", case / "KPOINTS", required["KPOINTS"]),
            ("POTCAR", case / "POTCAR", required["POTCAR"]),
            ("script", case / Path(scheduler.script), script_path),
        )
    )

    validate_potcar_order(required["POSCAR"], required["POTCAR"])
    suggest_encut(required["POTCAR"])
    incar_before = required["INCAR"].read_text(encoding="utf-8", errors="ignore")
    update = plan_neutral_vaspsol_update(incar_before)
    if update.duplicates:
        raise ValueError(
            "duplicate INCAR tags require manual resolution: " + ", ".join(update.duplicates)
        )
    if update.conflicts:
        details = "; ".join(
            f"{key}: current={current}, required={required}"
            for key, current, required in update.conflicts
        )
        raise ValueError("conflicting INCAR settings require manual resolution: " + details)

    workflow = copy.deepcopy(workflow_config) if workflow_config is not None else WorkflowConfig()
    workflow.pbs_file = scheduler.script
    workflow.qsub_ppn = scheduler.cores
    workflow.qsub_queue = scheduler.queue
    workflow.qsub_walltime = scheduler.walltime
    config = KitConfig(profile="vaspsol-sweep", workflow=workflow, scheduler=scheduler)
    config.validate()
    config_after = serialize_kit_config(config).decode("utf-8")
    state_after = json.dumps(
        {"jobs": {}, "neutral": None, "prepared_checked": False, "stage": "setup"},
        indent=2,
        sort_keys=True,
    )

    config_path = _nominal_target_path(case, CONFIG_FILENAME)
    config_before_bytes = config_path.read_bytes() if config_path.is_file() else None
    target_fingerprints = tuple(
        _fingerprint_target(case, label, _nominal_target_path(case, label))
        for label in ("INCAR", CONFIG_FILENAME, STATE_FILENAME)
    )

    changes = []
    if update.candidate != incar_before:
        changes.append(
            PlannedFileChange(
                _nominal_target_path(case, "INCAR"),
                incar_before,
                update.candidate,
                "update",
            )
        )
    for name, after in ((CONFIG_FILENAME, config_after), (STATE_FILENAME, state_after)):
        path = _nominal_target_path(case, name)
        if name == CONFIG_FILENAME and config_before_bytes is not None:
            before = _normalized_preview_text(config_before_bytes)
        else:
            before = path.read_text(encoding="utf-8") if path.is_file() else None
        changes.append(
            PlannedFileChange(path, before, after, "update" if before is not None else "create")
        )
    return CaseInitializationPlan(
        case,
        _fingerprint_case_directory(case),
        config,
        config_before_bytes,
        incar_before,
        update.candidate,
        tuple(changes),
        source_fingerprints,
        target_fingerprints,
    )


def apply_case_initialization(
    plan: CaseInitializationPlan, confirmed: bool = False
) -> Tuple[Path, ...]:
    """Apply exactly the changes in a current, explicitly confirmed plan."""
    if not isinstance(plan, CaseInitializationPlan):
        raise TypeError("plan must be a CaseInitializationPlan")
    if not confirmed:
        raise PermissionError("case initialization requires explicit confirmation")
    case = _resolved_case(plan.workdir)
    if case != plan.workdir:
        raise RuntimeError("initialization plan belongs to another Case")
    for fingerprint in plan.target_fingerprints:
        _verify_target_fingerprint(case, fingerprint)
    with workflow_state_lock(case / STATE_FILENAME):
        return _apply_case_initialization_locked(plan, case)


def _apply_case_initialization_locked(
    plan: CaseInitializationPlan, case: Path
) -> Tuple[Path, ...]:
    if _fingerprint_case_directory(case) != plan.case_fingerprint:
        raise RuntimeError("stale initialization Case: directory identity has changed")
    for fingerprint in plan.source_fingerprints:
        _verify_source_fingerprint(case, fingerprint)
    for fingerprint in plan.target_fingerprints:
        _verify_target_fingerprint(case, fingerprint)

    changes = _ordered_changes(plan.file_changes)
    staged = []
    try:
        for change in changes:
            staged.append((_stage_file_change(change), change))
    except BaseException as exc:
        _cleanup_staged(staged)
        path = change.path if "change" in locals() else case
        raise CaseInitializationApplyError("stage", path, exc) from exc

    try:
        config_temp, config_change = next(
            item for item in staged if item[1].path.name == CONFIG_FILENAME
        )
        failed_path = config_change.path
        expected_config = (
            EXPECT_ABSENT
            if plan.config_before_bytes is None
            else plan.config_before_bytes
        )
        write_config_bytes(
            config_change.path,
            config_temp.read_bytes(),
            expected_current=expected_config,
        )
        config_temp.unlink()
        staged = [item for item in staged if item[1] is not config_change]
        for temp_path, change in staged:
            failed_path = change.path
            os.replace(temp_path, change.path)
    except BaseException as exc:
        _cleanup_staged(staged)
        raise CaseInitializationApplyError("replace", failed_path, exc) from exc
    return tuple(change.path for change in changes)


def _normalized_preview_text(data: bytes) -> str:
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _resolved_case(workdir: Path) -> Path:
    if not isinstance(workdir, (str, Path)):
        raise TypeError("workdir must be path-like")
    try:
        case = Path(workdir).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"invalid Case path: {workdir}") from exc
    if not case.is_dir():
        raise NotADirectoryError(f"Case is not a directory: {case}")
    return case


def _fingerprint_case_directory(case: Path) -> CaseDirectoryFingerprint:
    try:
        metadata = case.stat()
    except OSError as exc:
        raise ValueError(f"cannot inspect Case directory: {case}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"Case is not a directory: {case}")
    return CaseDirectoryFingerprint(
        path=case,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
    )


def _required_file(case: Path, name: str) -> Path:
    path = case / name
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(case)
    except FileNotFoundError:
        raise FileNotFoundError(f"required file is missing: {path}") from None
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"required file must remain within the Case: {name}") from exc
    _require_nonempty(resolved, name)
    return resolved


def _resolve_script(case: Path, script: str) -> Path:
    if not isinstance(script, str) or not script.strip():
        raise ValueError("scheduler script must be a non-empty relative path")
    raw = Path(script)
    if raw.is_absolute() or ".." in raw.parts:
        raise ValueError("scheduler script must remain within the Case")
    try:
        resolved = (case / raw).resolve(strict=True)
        resolved.relative_to(case)
    except FileNotFoundError:
        raise FileNotFoundError(f"submission script is missing: {case / raw}") from None
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("scheduler script must remain within the Case") from exc
    return resolved


def _nominal_target_path(case: Path, name: str) -> Path:
    path = Path(os.path.abspath(case / name))
    try:
        path.relative_to(case)
    except ValueError as exc:
        raise ValueError(f"initialization target must remain within the Case: {name}") from exc
    return path


def _fingerprint_target(
    case: Path, label: str, path: Path
) -> CaseTargetFingerprint:
    try:
        path.relative_to(case)
        metadata = path.lstat()
    except FileNotFoundError:
        return CaseTargetFingerprint(label, path, "absent", None, None)
    except (OSError, ValueError) as exc:
        raise ValueError(f"initialization target is unsafe: {label}") from exc
    if stat.S_ISREG(metadata.st_mode):
        return CaseTargetFingerprint(
            label, path, "regular", None, hashlib.sha256(path.read_bytes()).hexdigest()
        )
    if stat.S_ISLNK(metadata.st_mode):
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(case)
            if not resolved.is_file():
                raise ValueError("symlink target is not a regular file")
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
            target = os.readlink(path)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            raise ValueError(f"initialization target symlink is unsafe: {label}") from exc
        return CaseTargetFingerprint(label, path, "symlink", target, digest)
    return CaseTargetFingerprint(label, path, "other", None, None)


def _verify_target_fingerprint(case: Path, expected: CaseTargetFingerprint) -> None:
    try:
        current = _fingerprint_target(case, expected.label, expected.path)
    except ValueError as exc:
        raise RuntimeError(
            f"stale initialization target: {expected.label} is unavailable or unsafe"
        ) from exc
    if current.entry_type == "other" or current != expected:
        raise RuntimeError(f"stale initialization target: {expected.label} has changed")


def _ordered_changes(
    changes: Tuple[PlannedFileChange, ...]
) -> Tuple[PlannedFileChange, ...]:
    order = {"INCAR": 0, CONFIG_FILENAME: 1, STATE_FILENAME: 2}
    try:
        return tuple(sorted(changes, key=lambda change: order[change.path.name]))
    except KeyError as exc:
        raise RuntimeError(f"initialization plan contains an unknown target: {exc.args[0]}") from exc


def _stage_file_change(change: PlannedFileChange) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{change.path.name}.", suffix=".tmp", dir=change.path.parent
    )
    temp_path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(change.after)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def _cleanup_staged(staged) -> None:
    for temp_path, _change in staged:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _fingerprint_source(
    case: Path, label: str, source_path: Path, resolved_path: Path
) -> CaseSourceFingerprint:
    source_path = Path(os.path.abspath(source_path))
    try:
        source_path.relative_to(case)
        resolved_path.relative_to(case)
    except ValueError as exc:
        raise ValueError(f"source must remain within the Case: {label}") from exc
    is_symlink = source_path.is_symlink()
    return CaseSourceFingerprint(
        label=label,
        source_path=source_path,
        resolved_path=resolved_path,
        sha256=hashlib.sha256(resolved_path.read_bytes()).hexdigest(),
        is_symlink=is_symlink,
        symlink_target=os.readlink(source_path) if is_symlink else None,
    )


def _verify_source_fingerprint(
    case: Path, fingerprint: CaseSourceFingerprint
) -> None:
    try:
        fingerprint.source_path.relative_to(case)
        resolved = fingerprint.source_path.resolve(strict=True)
        resolved.relative_to(case)
        if not resolved.is_file():
            raise ValueError("source is not a file")
        is_symlink = fingerprint.source_path.is_symlink()
        symlink_target = os.readlink(fingerprint.source_path) if is_symlink else None
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            f"stale initialization source: {fingerprint.label} is unavailable or unsafe"
        ) from exc
    if (
        resolved != fingerprint.resolved_path
        or is_symlink != fingerprint.is_symlink
        or symlink_target != fingerprint.symlink_target
        or digest != fingerprint.sha256
    ):
        raise RuntimeError(f"stale initialization source: {fingerprint.label} has changed")


def _require_nonempty(path: Path, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"required file is missing: {path}")
    if path.stat().st_size <= 0:
        raise ValueError(f"required file is empty: {label}")
