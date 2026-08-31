"""Build an immutable workbench view from files already present in a case."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Mapping, Optional

from ..config import KitConfig, SchedulerConfig, WorkflowConfig, load_kit_config
from ..guide_model import build_snapshot, recommend_action
from ..inputs import potcar_elements, poscar_elements
from ..orchestrator import STATE_FILENAME
from ..scheduler import JobState
from ..state import JobRecord, WorkflowState
from .i18n import tr
from .models import (
    InputCheckRow,
    JobView,
    NavigationItem,
    NeutralOutputView,
    RecommendationView,
    ResultRow,
    SchedulerView,
    WorkbenchSnapshot,
    WorkflowStep,
)


_NAVIGATION = ("workspace", "inputs", "tasks", "queue", "results", "settings", "exit")
_WORKFLOW = (
    ("inputs", "输入检查"),
    ("init", "初始化配置"),
    ("neutral", "中性优化"),
    ("charge-prepare", "带电点准备"),
    ("charge-run", "带电点计算"),
    ("convergence", "收敛检查"),
    ("collect", "结果收集"),
)
OUTCAR_SCAN_LIMIT = 4 * 1024 * 1024
_EFERMI_LINE = re.compile(r"E-fermi\s*:\s*([-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)")
_TOTEN_LINE = re.compile(r"TOTEN\s*=\s*([-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)")


def build_workbench_snapshot(
    workdir: Path,
    config_path: Optional[Path] = None,
    scheduler_overlay: Optional[Mapping[str, JobState]] = None,
    last_refresh: str = "",
    refresh_error: Optional[str] = None,
) -> WorkbenchSnapshot:
    """Inspect a case without changing files or contacting its scheduler."""
    root = Path(workdir).expanduser().resolve()
    selected_config = (
        Path(config_path).expanduser().resolve()
        if config_path is not None
        else root / "vaspsolkit.json"
    )
    configured = _load_config(selected_config)
    guide_config_path = selected_config
    if configured is not None and not _config_paths_are_safe(root, configured):
        guide_config_path = root / ".vaspsolkit.invalid-paths"
    guide = build_snapshot(root, config_path=guide_config_path)
    action = recommend_action(guide)
    case = guide.case
    state = _load_state(root / STATE_FILENAME)
    overlay = scheduler_overlay or {}

    neutral_record = state.neutral if state is not None else None
    neutral = (
        _job_view(root, "neutral", "neutral", neutral_record, overlay)
        if neutral_record is not None
        else JobView(
            name="neutral",
            kind="neutral",
            status="NOT_PREPARED",
            recorded_status="NOT_PREPARED",
            job_id="",
            folder=root,
        )
    )
    charges = tuple(
        _job_view(root, name, "charge", record, overlay)
        for name, record in (state.jobs.items() if state is not None else ())
    )
    recorded_jobs = (() if neutral_record is None else (neutral,)) + charges

    config = configured if configured is not None else case.config
    if config is None:
        config = KitConfig()
    scheduler = config.scheduler
    scheduler_walltime = config.scheduler.walltime
    recommendation = RecommendationView(
        name=action.name,
        title=action.title_zh,
        reason=action.reason_zh,
        effect=action.effect,
        cli_command=action.cli_command,
        selectable_jobs=action.selectable_jobs,
    )
    workflow = _workflow_steps(action.name, guide)
    workflow_config = config.workflow if config is not None else WorkflowConfig()
    input_names = (
        "POSCAR",
        "INCAR",
        "KPOINTS",
        "POTCAR",
        *((scheduler.script,) if scheduler.script not in {"", "-"} else ()),
        "vaspsolkit.json",
        STATE_FILENAME,
    )
    input_rows = tuple(
        _input_row(
            root,
            name,
            required=name != STATE_FILENAME,
            scheduler=scheduler,
            role=_input_role(name, scheduler.script, scheduler.script),
        )
        for name in dict.fromkeys(input_names)
    )
    result_names = (workflow_config.summary_file, workflow_config.analysis_file)
    result_rows = tuple(
        _result_row(root, workflow_config.results_root, name)
        for name in dict.fromkeys(result_names)
    )
    analysis_runs = _analysis_history_rows(root, workflow_config.results_root)

    return WorkbenchSnapshot(
        workdir=root,
        system_text=_system_summary(root / "POSCAR"),
        navigation=tuple(
            NavigationItem(key, tr("zh", f"nav.{key}"), tr("en", f"nav.{key}"), str(index))
            for index, key in enumerate(_NAVIGATION, 1)
        ),
        workflow_steps=workflow,
        neutral=neutral,
        charge_jobs=charges,
        queue_rows=recorded_jobs,
        scheduler=SchedulerView(
            kind=scheduler.kind,
            partition=scheduler.partition,
            tasks=scheduler.tasks,
            nodes=tuple(scheduler.nodes),
            script=scheduler.script,
            walltime=scheduler_walltime,
            resource_syntax=_scheduler_resource_syntax(root, scheduler.script),
            last_refresh=last_refresh,
            refresh_error=refresh_error,
        ),
        recommendation=recommendation,
        input_rows=input_rows,
        result_rows=result_rows,
        neutral_output=_inspect_neutral_outputs(root),
        stage=state.stage if state is not None else case.stage,
        analysis_runs=analysis_runs,
        warning_count=sum(item.severity == "warning" for item in case.diagnostics),
        error_count=sum(item.severity == "error" for item in case.diagnostics),
    )


def _load_state(path: Path) -> Optional[WorkflowState]:
    if not path.is_file():
        return None
    try:
        return WorkflowState.load(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _scheduler_resource_syntax(root: Path, script_name: str) -> str:
    script = _safe_case_path(root, script_name)
    if script is None:
        return "unmanaged"
    try:
        text = script.read_text(encoding="utf-8")
        if re.search(r"^\s*#SBATCH\s+--nodes(?:=|\s)", text, re.MULTILINE):
            return "nodes"
        if re.search(r"^\s*#SBATCH\s+-N(?:\s|\d)", text, re.MULTILINE):
            return "nodes"
        return "unmanaged"
    except (OSError, UnicodeDecodeError, TypeError, ValueError):
        return "unmanaged"


def _load_config(path: Path) -> Optional[KitConfig]:
    if not path.is_file():
        return None
    try:
        return load_kit_config(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _config_paths_are_safe(root: Path, config: KitConfig) -> bool:
    workflow = config.workflow
    return all(
        path is not None
        for path in (
            _safe_case_path(root, config.scheduler.script),
            _safe_case_path(root, workflow.results_root, workflow.summary_file),
            _safe_case_path(root, workflow.results_root, workflow.analysis_file),
        )
    )


def _job_view(
    root: Path,
    name: str,
    kind: str,
    record: JobRecord,
    overlay: Mapping[str, JobState],
) -> JobView:
    folder = _safe_case_path(root, record.folder)
    if folder is None:
        return JobView(
            name=name,
            kind=kind,
            status="ERROR",
            recorded_status=record.status,
            job_id=record.job_id,
            folder=root,
            diagnostics=("invalid folder",),
        )
    scheduler_state = None
    if record.job_id:
        candidate = overlay.get(record.job_id)
        if candidate is not None and candidate.job_id == record.job_id:
            scheduler_state = candidate
    display_state = scheduler_state.state if scheduler_state is not None else record.status
    return JobView(
        name=name,
        kind=kind,
        status=display_state,
        recorded_status=record.status,
        job_id=record.job_id,
        folder=folder,
        scheduler_state=scheduler_state.state if scheduler_state is not None else None,
        diagnostics=tuple(record.diagnostics),
    )


def _workflow_steps(action_name: str, guide) -> tuple[WorkflowStep, ...]:
    case = guide.case
    completed = 0
    if not guide.missing_base_inputs:
        completed = 1
    if guide.has_config:
        completed = 2
    if case.neutral_status == "CONVERGED":
        completed = 3
    if case.charge_total:
        completed = 4
    if case.charge_total and all(status == "CONVERGED" for _, status in case.charge_statuses):
        completed = 6
    if case.results_available:
        completed = 7
    current = {
        "fix-inputs": 0,
        "init": 1,
        "prepare-neutral": 2,
        "submit-neutral": 2,
        "prepare-charge": 3,
        "check-prepared": 3,
        "submit-selected": 4,
        "check": 5,
        "collect": 6,
    }.get(action_name, 4 if case.neutral_status == "CONVERGED" and case.charge_total else 2)
    return tuple(
        WorkflowStep(key, label, "current" if index == current else "completed" if index < completed else "pending")
        for index, (key, label) in enumerate(_WORKFLOW)
    )


def _safe_case_path(root: Path, *parts: str) -> Optional[Path]:
    """Resolve a configured path only when it remains below the case root."""
    try:
        root = Path(root).resolve()
        candidate = root.joinpath(*parts).resolve()
    except (OSError, RuntimeError):
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _input_role(name: str, workflow_script: str, scheduler_script: str) -> str:
    roles = {"POSCAR": "poscar", "INCAR": "incar", "KPOINTS": "kpoints", "POTCAR": "potcar"}
    if name in {workflow_script, scheduler_script}:
        return "script"
    return roles.get(name, "metadata")


def _input_row(root: Path, name: str, required: bool, scheduler, role: str) -> InputCheckRow:
    path = _safe_case_path(root, name)
    if path is None:
        return InputCheckRow(name, root, False, "ERROR", required, "configured path is unsafe", role)
    if not path.exists():
        return InputCheckRow(name, path, False, "MISSING", required, "file is missing", role)
    if not path.is_file():
        return InputCheckRow(name, path, False, "ERROR", required, "not a regular file", role)
    try:
        data = path.read_bytes()
        if not data:
            raise ValueError("file is empty")
        text = data.decode("utf-8")
        summary = _validate_input(root, name, text, data, scheduler, role)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return InputCheckRow(name, path, True, "ERROR", required, str(exc), role)
    return InputCheckRow(name, path, True, "READY", required, summary, role)


def _validate_input(root: Path, name: str, text: str, data: bytes, scheduler, role: str) -> str:
    if name == "POSCAR":
        return _validate_poscar(text)
    if name == "POTCAR":
        return _validate_potcar(root, text)
    if name == "KPOINTS":
        return _validate_kpoints(text)
    if role == "script":
        return _validate_script(text, data, scheduler)
    if name == "INCAR":
        assignments = sum("=" in line.split("!", 1)[0].split("#", 1)[0] for line in text.splitlines())
        if not assignments:
            raise ValueError("INCAR contains no active assignments")
        return f"active-tags={assignments}"
    return "readable UTF-8"


def _validate_poscar(text: str) -> str:
    lines = text.splitlines()
    if len(lines) < 8:
        raise ValueError("POSCAR is too short")
    try:
        scale = float(lines[1].split()[0])
        if not math.isfinite(scale) or scale == 0:
            raise ValueError
        lattice = []
        for line in lines[2:5]:
            values = line.split()
            if len(values) < 3:
                raise ValueError
            vector = tuple(float(value) for value in values[:3])
            if not all(math.isfinite(value) for value in vector):
                raise ValueError
            lattice.append(vector)
        elements = lines[5].split()
        counts = tuple(int(value) for value in lines[6].split())
    except (IndexError, ValueError) as exc:
        raise ValueError("POSCAR lattice or atom counts are invalid") from exc
    if not elements or len(elements) != len(counts) or any(count <= 0 for count in counts):
        raise ValueError("POSCAR element and atom-count lines do not match")
    determinant = (
        lattice[0][0] * (lattice[1][1] * lattice[2][2] - lattice[1][2] * lattice[2][1])
        - lattice[0][1] * (lattice[1][0] * lattice[2][2] - lattice[1][2] * lattice[2][0])
        + lattice[0][2] * (lattice[1][0] * lattice[2][1] - lattice[1][1] * lattice[2][0])
    )
    if not math.isfinite(determinant) or abs(determinant) <= 1.0e-12:
        raise ValueError("POSCAR lattice is degenerate")
    coordinate_index = 7
    if lines[coordinate_index].strip().lower().startswith("s"):
        coordinate_index += 1
    if coordinate_index >= len(lines):
        raise ValueError("POSCAR coordinate mode is missing")
    coordinate_token = lines[coordinate_index].strip().lower()
    if coordinate_token.startswith("d"):
        coordinate_mode = "Direct"
    elif coordinate_token.startswith(("c", "k")):
        coordinate_mode = "Cartesian"
    else:
        raise ValueError("POSCAR coordinate mode must be Direct or Cartesian")
    atom_count = sum(counts)
    coordinates = lines[coordinate_index + 1 : coordinate_index + 1 + atom_count]
    if len(coordinates) != atom_count:
        raise ValueError("POSCAR coordinate count does not match atom counts")
    try:
        for line in coordinates:
            values = line.split()
            if len(values) < 3:
                raise ValueError
            coordinates = tuple(float(value) for value in values[:3])
            if not all(math.isfinite(value) for value in coordinates):
                raise ValueError
    except ValueError as exc:
        raise ValueError("POSCAR contains invalid coordinates") from exc
    return f"elements={' '.join(elements)} · atoms={atom_count} · coordinates={coordinate_mode}"


def _validate_potcar(root: Path, text: str) -> str:
    actual = potcar_elements(root / "POTCAR")
    expected = poscar_elements(root / "POSCAR")
    if actual != expected:
        raise ValueError(f"POTCAR order {' '.join(actual)} does not match POSCAR {' '.join(expected)}")
    values = [float(value) for value in re.findall(r"\bENMAX\s*=\s*([0-9.]+)", text, re.IGNORECASE)]
    if len(values) < len(actual):
        raise ValueError("POTCAR is missing ENMAX records")
    maximum = max(values)
    rendered = str(int(maximum)) if maximum.is_integer() else f"{maximum:g}"
    return f"order={' '.join(actual)} · ENMAX={rendered} eV"


def _validate_kpoints(text: str) -> str:
    raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not raw_lines:
        raise ValueError("KPOINTS is empty")
    lines = [raw_lines[0], *_kpoints_payload(raw_lines[1:])]
    if len(lines) < 4:
        raise ValueError("KPOINTS is too short")
    try:
        count = int(lines[1].split()[0])
    except (IndexError, ValueError) as exc:
        raise ValueError("KPOINTS point count is invalid") from exc
    if re.sub(r"[\s_-]+", "", lines[2]).lower() == "linemode":
        return _validate_line_mode_kpoints(lines, count)
    if count == 0:
        if len(lines) < 5:
            raise ValueError("automatic KPOINTS requires mode, grid, and shift")
        mode = lines[2].split()[0]
        if not mode.lower().startswith(("g", "m")):
            raise ValueError("automatic KPOINTS mode must be Gamma or Monkhorst-Pack")
        try:
            grid = tuple(int(value) for value in lines[3].split()[:3])
            shift = tuple(float(value) for value in lines[4].split()[:3])
        except ValueError as exc:
            raise ValueError("automatic KPOINTS grid or shift is invalid") from exc
        if len(grid) != 3 or len(shift) != 3 or any(value <= 0 for value in grid) or not all(math.isfinite(value) for value in shift):
            raise ValueError("automatic KPOINTS grid must contain three positive integers")
        return f"{mode} {grid[0]}x{grid[1]}x{grid[2]} · shift={' '.join(f'{value:g}' for value in shift)}"
    coordinate_mode = _kpoints_coordinate_mode(lines[2])
    if count < 1:
        raise ValueError("explicit KPOINTS point count must be positive")
    payload = _kpoints_payload(lines[3:])
    if len(payload) < count:
        raise ValueError("explicit KPOINTS count does not match point rows")
    for line in payload[:count]:
        _finite_kpoint_row(line, 4, "explicit KPOINTS point")
    trailing = payload[count:]
    tetrahedra = _validate_tetrahedra(trailing, count) if trailing else 0
    suffix = f" · tetrahedra={tetrahedra}" if trailing else ""
    return f"explicit points={count} · coordinates={coordinate_mode}{suffix}"


def _validate_line_mode_kpoints(lines, points_per_segment: int) -> str:
    if points_per_segment <= 0:
        raise ValueError("Line-mode points per segment must be positive")
    if len(lines) < 6:
        raise ValueError("Line-mode KPOINTS requires a coordinate mode and point pairs")
    coordinate_mode = _kpoints_coordinate_mode(lines[3])
    points = _kpoints_payload(lines[4:])
    if len(points) < 2 or len(points) % 2:
        raise ValueError("Line-mode KPOINTS requires pairs of path endpoints")
    for line in points:
        _finite_kpoint_row(line, 3, "Line-mode endpoint")
    return (
        f"Line-mode {coordinate_mode} · segments={len(points) // 2}"
        f" · points-per-segment={points_per_segment}"
    )


def _kpoints_coordinate_mode(value: str) -> str:
    normalized = value.strip().lower()
    if normalized.startswith("r"):
        return "Reciprocal"
    if normalized.startswith(("c", "k")):
        return "Cartesian"
    raise ValueError("KPOINTS coordinates must be Reciprocal or Cartesian")


def _kpoints_payload(lines):
    payload = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith(("#", "!")):
            continue
        clean = stripped.split("!", 1)[0].split("#", 1)[0].strip()
        if clean:
            payload.append(clean)
    return payload


def _finite_kpoint_row(line: str, fields: int, label: str) -> tuple[float, ...]:
    values = line.split()
    if len(values) != fields:
        raise ValueError(f"{label} requires {fields} numeric values")
    try:
        parsed = tuple(float(value) for value in values)
    except ValueError as exc:
        raise ValueError(f"{label} contains a non-numeric value") from exc
    if not all(math.isfinite(value) for value in parsed):
        raise ValueError(f"{label} contains a non-finite value")
    return parsed


def _validate_tetrahedra(lines, point_count: int) -> int:
    if not lines or lines[0].strip().lower() != "tetrahedra":
        raise ValueError("unexpected content after explicit KPOINTS points")
    if len(lines) < 2:
        raise ValueError("Tetrahedra header is incomplete")
    header = lines[1].split()
    if len(header) != 2:
        raise ValueError("Tetrahedra count header requires count and volume weight")
    try:
        tetrahedron_count = int(header[0])
        volume_weight = float(header[1])
    except ValueError as exc:
        raise ValueError("Tetrahedra count or volume weight is invalid") from exc
    if tetrahedron_count < 0 or not math.isfinite(volume_weight) or volume_weight <= 0:
        raise ValueError("Tetrahedra count and volume weight must be valid")
    rows = lines[2:]
    if len(rows) != tetrahedron_count:
        raise ValueError("Tetrahedra count does not match tetrahedron rows")
    for line in rows:
        values = line.split()
        if len(values) != 5:
            raise ValueError("each Tetrahedra row requires a weight and four point indices")
        try:
            integers = tuple(int(value) for value in values)
        except ValueError as exc:
            raise ValueError("Tetrahedra rows must contain integers") from exc
        if integers[0] <= 0 or any(index < 1 or index > point_count for index in integers[1:]):
            raise ValueError("Tetrahedra row references an invalid explicit point")
    return tetrahedron_count


def _validate_script(text: str, data: bytes, scheduler) -> str:
    if b"\r\n" in data or b"\r" in data:
        raise ValueError("submission script uses DOS/Windows line endings")
    if "\x00" in text:
        raise ValueError("submission script contains NUL bytes")
    partition_match = re.search(r"^#SBATCH\s+(?:-p\s+|--partition(?:=|\s+))(\S+)", text, re.MULTILINE)
    tasks_match = re.search(r"^#SBATCH\s+(?:-n\s*|--ntasks(?:=|\s+))(\d+)", text, re.MULTILINE)
    walltime_match = re.search(r"^#SBATCH\s+(?:-t\s+|--time(?:=|\s+))(\S+)", text, re.MULTILINE)
    partition = partition_match.group(1) if partition_match else "unknown"
    tasks = tasks_match.group(1) if tasks_match else "unknown"
    walltime = walltime_match.group(1) if walltime_match else "unknown"
    lines = text.splitlines()
    kind = scheduler.kind
    has_command = any(line.strip() and not line.lstrip().startswith("#") for line in lines)
    directives = "#SBATCH" if kind == "slurm" else "#CUSTOM"
    if not any(line.startswith(directives) for line in lines) and not has_command:
        raise ValueError("submission script has no scheduler directive or executable command")
    return f"line-endings=Unix · partition={partition} · tasks={tasks} · walltime={walltime}"


def _result_row(root: Path, results_root: str, name: str) -> ResultRow:
    path = _safe_case_path(root, results_root, name)
    if path is None:
        return ResultRow(name, str(root), False, "ERROR")
    exists = path.is_file()
    return ResultRow(name, str(path), exists, "OK" if exists else "MISSING")


def _analysis_history_rows(root: Path, results_root: str) -> tuple[ResultRow, ...]:
    history = _safe_case_path(root, results_root, "history")
    if history is None or not history.is_dir() or history.is_symlink():
        return ()
    try:
        candidates = sorted(history.iterdir(), key=lambda path: path.name, reverse=True)
    except OSError:
        return ()
    rows = []
    required = ("summary.csv", "analysis.json", "eu_curve.png", "analysis-log.md")
    for path in candidates[:1000]:
        try:
            if path.is_symlink() or not path.is_dir():
                continue
            complete = all((path / name).is_file() and not (path / name).is_symlink() for name in required)
        except OSError:
            complete = False
        rows.append(
            ResultRow(
                path.name,
                str(path),
                complete,
                "COMPLETE" if complete else "INCOMPLETE",
            )
        )
        if len(rows) == 20:
            break
    return tuple(rows)


def _inspect_neutral_outputs(root: Path) -> NeutralOutputView:
    _, contcar_status, _, _ = _case_result_file(root, "CONTCAR")
    _, chgcar_status, _, _ = _case_result_file(root, "CHGCAR")
    outcar_path, outcar_status, outcar_size, diagnostic = _case_result_file(
        root, "OUTCAR"
    )
    if outcar_path is None:
        return NeutralOutputView(
            contcar_status=contcar_status,
            chgcar_status=chgcar_status,
            outcar_status=outcar_status,
            diagnostic=diagnostic,
            outcar_size=outcar_size,
        )
    return _scan_outcar_tail(
        outcar_path,
        contcar_status=contcar_status,
        chgcar_status=chgcar_status,
        outcar_size=outcar_size,
    )


def _case_result_file(root: Path, name: str):
    candidate = root / name
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None, "UNREADABLE", 0, "output path leaves the current Case"
    if not candidate.exists():
        return None, "MISSING", 0, ""
    try:
        if not resolved.is_file():
            return None, "UNREADABLE", 0, "output is not a regular file"
        size = resolved.stat().st_size
    except OSError as exc:
        return None, "UNREADABLE", 0, str(exc)
    return resolved, "AVAILABLE", size, ""


def _scan_outcar_tail(
    path: Path,
    *,
    contcar_status: str,
    chgcar_status: str,
    outcar_size: int,
) -> NeutralOutputView:
    start = max(0, outcar_size - OUTCAR_SCAN_LIMIT)
    efermi = None
    toten = None
    converged = False
    recognized = False
    scanned_bytes = 0
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            if start:
                scanned_bytes += len(handle.readline())
            for raw_line in handle:
                scanned_bytes += len(raw_line)
                line = raw_line.decode("utf-8")
                if any(
                    marker in line
                    for marker in (
                        "vasp.",
                        "Iteration",
                        "NELECT",
                        "E-fermi",
                        "TOTEN",
                        "reached required accuracy",
                    )
                ):
                    recognized = True
                if "E-fermi" in line:
                    match = _EFERMI_LINE.search(line)
                    if match:
                        efermi = float(match.group(1))
                if "TOTEN" in line:
                    match = _TOTEN_LINE.search(line)
                    if match:
                        toten = float(match.group(1))
                if "reached required accuracy" in line:
                    converged = True
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return NeutralOutputView(
            contcar_status=contcar_status,
            chgcar_status=chgcar_status,
            outcar_status="UNREADABLE",
            diagnostic=str(exc),
            outcar_size=outcar_size,
            scanned_bytes=scanned_bytes,
        )
    if not recognized:
        return NeutralOutputView(
            contcar_status=contcar_status,
            chgcar_status=chgcar_status,
            outcar_status="UNREADABLE",
            diagnostic="OUTCAR contains no recognizable VASP records",
            outcar_size=outcar_size,
            scanned_bytes=scanned_bytes,
        )
    return NeutralOutputView(
        contcar_status=contcar_status,
        chgcar_status=chgcar_status,
        outcar_status="CONVERGED" if converged else "IN_PROGRESS",
        toten=toten,
        efermi=efermi,
        outcar_size=outcar_size,
        scanned_bytes=scanned_bytes,
    )


def _system_summary(poscar: Path) -> str:
    try:
        lines = poscar.read_text(encoding="utf-8", errors="ignore").splitlines()
        elements = lines[5].split()
        counts = [int(value) for value in lines[6].split()]
        if not elements or len(elements) != len(counts):
            return "未知体系"
        return f"{' '.join(elements)} · {sum(counts)} atoms"
    except (OSError, ValueError, IndexError):
        return "未知体系"
