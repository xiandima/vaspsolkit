from __future__ import annotations

import csv
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .analysis import analyze_rows, evaluate_quadratic, write_analysis
from .config import WorkflowConfig
from .incar import replace_or_append
from .parsers import (
    LocpotData,
    calculate_vacuum_reference_potential_vs_she,
    calculate_vacuum_reference_work_function,
    corrected_energy_at_potential,
    parse_locpot,
    parse_outcar,
    slab_area_from_poscar,
    surface_charge,
)
from .reference_settings import summary_reference_fields
from .scheduler import SlurmScheduler


NELECT_TOLERANCE = 1.0e-3
MAGNETIZATION_JUMP_THRESHOLD = 0.5
RESIDUAL_WARNING_THRESHOLD = 0.03
SUBMISSION_SCRIPT = "vasp.slurm"


@dataclass
class PreparedJob:
    folder: Path
    offset: float
    nelect: float
    job_name: str


def is_converged(folder: Path) -> bool:
    outcar = Path(folder) / "OUTCAR"
    if not outcar.exists():
        return False
    try:
        return parse_outcar(outcar).converged
    except ValueError as error:
        # OUTCAR is written incrementally. Missing NELECT/E-fermi/TOTEN
        # means the file is not parseable yet, not that the job failed.
        if "does not contain" in str(error):
            return False
        raise


def prepare_jobs(
    base: Path,
    config: WorkflowConfig,
    dry_run: bool = False,
    resume: bool = False,
) -> List[PreparedJob]:
    base = Path(base)
    config.validate()
    nelect_ref = config.nelect_ref
    if nelect_ref is None:
        nelect_ref = parse_outcar(base / "OUTCAR").nelect

    _require_existing(base / "INCAR")
    _require_existing(base / "CONTCAR")
    _require_existing(base / SUBMISSION_SCRIPT)

    root_script = (base / SUBMISSION_SCRIPT).read_text(encoding="utf-8", errors="ignore")
    base_job_name = _slurm_job_name(root_script) or base.name
    prepared: List[PreparedJob] = []
    jobs_root = job_root_path(base, config)
    if not dry_run:
        jobs_root.mkdir(parents=True, exist_ok=True)

    for folder_name, offset in zip(config.folders, config.nelect_offsets):
        target = job_folder_path(base, config, folder_name)
        nelect = nelect_ref + offset
        job_name = _child_job_name(base_job_name, folder_name)
        prepared.append(PreparedJob(folder=target, offset=offset, nelect=nelect, job_name=job_name))
        if dry_run:
            continue
        if resume and is_converged(target):
            continue

        target.mkdir(parents=True, exist_ok=True)
        for filename in config.copy_files:
            source = base / filename
            if not source.exists():
                continue
            destination = target / filename
            if filename in {"WAVECAR", "CHGCAR"}:
                atomic_copy(source, destination)
            else:
                shutil.copy2(source, destination)

        shutil.copy2(base / "CONTCAR", target / "POSCAR")
        shutil.copy2(base / SUBMISSION_SCRIPT, target / SUBMISSION_SCRIPT)
        _write_charge_incar(target / "INCAR", nelect)
        _write_child_slurm(target / SUBMISSION_SCRIPT, job_name)

    return prepared


def submit_jobs(
    base: Path,
    config: WorkflowConfig,
    scheduler: SlurmScheduler,
    dry_run: bool = False,
) -> Dict[str, str]:
    job_ids: Dict[str, str] = {}
    for folder_name in config.folders:
        folder = job_folder_path(Path(base), config, folder_name)
        _validate_submit_folder(folder, SUBMISSION_SCRIPT)
        job_ids[folder_name] = scheduler.submit(
            folder,
            SUBMISSION_SCRIPT,
            dry_run=dry_run,
        )
    state_path = result_file_path(Path(base), config, config.job_state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _write_job_state(state_path, job_ids)
    return job_ids


def monitor_jobs(
    base: Path,
    config: WorkflowConfig,
    scheduler: SlurmScheduler,
    job_ids: Optional[Dict[str, str]] = None,
) -> None:
    if job_ids is None:
        job_ids = _read_job_state(result_file_path(Path(base), config, config.job_state_file))
    remaining = dict(job_ids)
    while remaining:
        finished = []
        for folder_name, job_id in remaining.items():
            folder = job_folder_path(Path(base), config, folder_name)
            if _job_finished_and_converged(folder, scheduler, job_id):
                finished.append(folder_name)
        for folder_name in finished:
            remaining.pop(folder_name, None)
        if remaining:
            time.sleep(config.poll_interval)


def collect_results(
    base: Path,
    config: WorkflowConfig,
    output: Optional[Path] = None,
) -> List[Dict[str, object]]:
    base = Path(base)
    config.validate()
    nelect_ref = config.nelect_ref
    if nelect_ref is None:
        nelect_ref = parse_outcar(base / "OUTCAR").nelect

    locpot_cache: Dict[Path, LocpotData] = {}
    reference_vacuum_level = _reference_vacuum_level(base, config, locpot_cache, nelect_ref)

    rows = [
        _collect_charge_row(
            base=base,
            config=config,
            folder_name=folder_name,
            nelect_ref=nelect_ref,
            reference_vacuum_level=reference_vacuum_level,
            locpot_cache=locpot_cache,
        )
        for folder_name in config.folders
    ]

    output = output or result_file_path(base, config, config.summary_file)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_summary(output, rows)
    return rows


def audit_results(
    base: Path,
    config: WorkflowConfig,
    rows: Optional[List[Dict[str, object]]] = None,
    nelect_tolerance: float = NELECT_TOLERANCE,
    magnetization_jump_threshold: float = MAGNETIZATION_JUMP_THRESHOLD,
    residual_warning_threshold: float = RESIDUAL_WARNING_THRESHOLD,
) -> List[Dict[str, object]]:
    base = Path(base)
    config.validate()
    nelect_ref = config.nelect_ref
    if nelect_ref is None:
        nelect_ref = parse_outcar(base / "OUTCAR").nelect
    rows = rows if rows is not None else collect_results(base, config)
    residuals = _fit_residuals(rows)
    previous_magnetization: Optional[float] = None
    report = []
    for index, folder_name in enumerate(config.folders):
        folder = job_folder_path(base, config, folder_name)
        outcar = parse_outcar(folder / "OUTCAR")
        expected_nelect = (
            nelect_ref + config.nelect_offsets[index]
            if index < len(config.nelect_offsets)
            else None
        )
        incar_nelect = _active_incar_nelect(folder / "INCAR")
        warnings = []
        if not outcar.converged:
            warnings.append("unconverged")
        if expected_nelect is not None and abs(outcar.nelect - expected_nelect) > nelect_tolerance:
            warnings.append("nelect_mismatch")
        if incar_nelect is not None:
            if outcar.input_nelect is None:
                warnings.append("outcar_echo_nelect_missing")
            elif abs(incar_nelect - outcar.input_nelect) > nelect_tolerance:
                warnings.append("incar_outcar_echo_nelect_mismatch")
            if abs(incar_nelect - outcar.nelect) > nelect_tolerance:
                warnings.append("incar_outcar_nelect_mismatch")
        if _input_modified_after_outcar_start(folder / "INCAR", outcar.start_timestamp):
            warnings.append("input_modified_after_start")
        if outcar.initial_charge_density_supplied and outcar.old_mixing_mesh:
            warnings.append("seed_mixing_mesh_changed")
        if (
            previous_magnetization is not None
            and outcar.magnetization is not None
            and abs(outcar.magnetization - previous_magnetization) > magnetization_jump_threshold
        ):
            warnings.append("spin_jump")
        if outcar.magnetization is not None:
            previous_magnetization = outcar.magnetization
        residual = residuals.get(folder_name)
        if residual is not None and abs(residual) > residual_warning_threshold:
            warnings.append("high_residual")
        report.append(
            {
                "folder": folder_name,
                "expected_nelect": "" if expected_nelect is None else expected_nelect,
                "outcar_nelect": outcar.nelect,
                "delta_electrons": outcar.nelect - nelect_ref,
                "incar_nelect": "" if incar_nelect is None else incar_nelect,
                "outcar_input_nelect": "" if outcar.input_nelect is None else outcar.input_nelect,
                "efermi": outcar.efermi,
                "toten": outcar.toten,
                "converged": int(outcar.converged),
                "magnetization": "" if outcar.magnetization is None else outcar.magnetization,
                "residual_eV": "" if residual is None else residual,
                "outcar_start_time": "" if outcar.start_time is None else outcar.start_time,
                "warnings": ";".join(warnings),
            }
        )
    return report


def write_quality_report(path: Path, rows: Iterable[Dict[str, object]]) -> None:
    _write_csv(path, rows)


def write_points_to_rerun(path: Path, rows: Iterable[Dict[str, object]]) -> None:
    _write_csv(path, [row for row in rows if row.get("warnings")])


def write_summary(path: Path, rows: Iterable[Dict[str, object]]) -> None:
    rows = list(rows)
    if not rows:
        raise ValueError("no rows to write")
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _write_csv(path: Path, rows: Iterable[Dict[str, object]]) -> None:
    rows = list(rows)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _collect_charge_row(
    base: Path,
    config: WorkflowConfig,
    folder_name: str,
    nelect_ref: float,
    reference_vacuum_level: Optional[float],
    locpot_cache: Dict[Path, LocpotData],
) -> Dict[str, object]:
    folder = job_folder_path(base, config, folder_name)
    outcar = parse_outcar(folder / "OUTCAR")
    area = slab_area_from_poscar(folder / "POSCAR")
    charge = surface_charge(outcar.nelect, nelect_ref, area, config.interface_count)
    if reference_vacuum_level is None:
        locpot = _cached_locpot(folder / "LOCPOT", locpot_cache)
        vacuum_level = max(locpot.planar_average)
    else:
        vacuum_level = reference_vacuum_level
    work_function = calculate_vacuum_reference_work_function(outcar.efermi, vacuum_level)
    return {
        "folder": folder_name,
        "converged": int(outcar.converged),
        "delta_electrons": charge.delta_electrons,
        "electrode_charge_e": charge.electrode_charge_e,
        "surface_charge_uC_cm2": charge.surface_charge_uC_cm2,
        "efermi": outcar.efermi,
        "vacuum_level": vacuum_level,
        "work_function": work_function,
        "u_vs_she": calculate_vacuum_reference_potential_vs_she(
            outcar.efermi,
            vacuum_level,
            config.she_reference,
        ),
        "toten": outcar.toten,
        "energy_at_potential": corrected_energy_at_potential(
            outcar.toten,
            charge.delta_electrons,
            outcar.efermi,
        ),
        **summary_reference_fields(config),
    }


def run_workflow(
    base: Path,
    config: WorkflowConfig,
    scheduler: Optional[SlurmScheduler] = None,
    dry_run: bool = False,
    resume: bool = False,
) -> Dict[str, object]:
    scheduler = scheduler or SlurmScheduler()
    base = Path(base)
    if not dry_run and not is_converged(base):
        root_job = scheduler.submit(
            base,
            SUBMISSION_SCRIPT,
            dry_run=False,
        )
        monitor_job(base, config, scheduler, root_job)
    prepared = prepare_jobs(base, config, dry_run=dry_run, resume=resume)
    job_ids = submit_jobs(base, config, scheduler, dry_run=dry_run)
    if dry_run:
        return {"prepared": [job.folder.name for job in prepared], "job_ids": job_ids}
    monitor_jobs(base, config, scheduler, job_ids)
    rows = collect_results(base, config)
    analysis = analyze_rows(rows, target_potentials=config.target_potentials)
    analysis_path = result_file_path(base, config, config.analysis_file)
    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    write_analysis(analysis_path, analysis)
    return {"prepared": [job.folder.name for job in prepared], "job_ids": job_ids, "rows": rows}


def monitor_job(folder: Path, config: WorkflowConfig, scheduler: SlurmScheduler, job_id: str) -> None:
    folder = Path(folder)
    while True:
        if _job_finished_and_converged(folder, scheduler, job_id):
            return
        time.sleep(config.poll_interval)


def atomic_copy(source: Path, destination: Path) -> None:
    destination = Path(destination)
    tmp = destination.with_name(destination.name + ".part")
    if tmp.exists():
        tmp.unlink()
    shutil.copy2(source, tmp)
    tmp.replace(destination)


def job_root_path(base: Path, config: WorkflowConfig) -> Path:
    root = Path(config.job_root)
    if root.is_absolute():
        return root
    return Path(base) / root


def job_folder_path(base: Path, config: WorkflowConfig, folder_name: str) -> Path:
    folder = Path(folder_name)
    if folder.is_absolute():
        return folder
    return job_root_path(base, config) / folder


def results_root_path(base: Path, config: WorkflowConfig) -> Path:
    root = Path(config.results_root)
    if root.is_absolute():
        return root
    return Path(base) / root


def result_file_path(base: Path, config: WorkflowConfig, filename: str) -> Path:
    path = Path(filename)
    if path.is_absolute():
        return path
    return results_root_path(base, config) / path


def _cached_locpot(
    path: Path,
    cache: Dict[Path, LocpotData],
) -> LocpotData:
    path = Path(path)
    if path not in cache:
        cache[path] = parse_locpot(path)
    return cache[path]


def _reference_vacuum_level(
    base: Path,
    config: WorkflowConfig,
    cache: Dict[Path, LocpotData],
    nelect_ref: float,
) -> Optional[float]:
    folder_name = _neutral_reference_folder(base, config, nelect_ref)
    locpot_path = _neutral_locpot_path(base, config, folder_name)
    locpot = _cached_locpot(locpot_path, cache)
    return max(locpot.planar_average)


def _neutral_locpot_path(base: Path, config: WorkflowConfig, folder_name: str) -> Path:
    candidates = []
    base = Path(base)
    if _outcar_is_neutral(base / "OUTCAR", parse_outcar(job_folder_path(base, config, folder_name) / "OUTCAR").nelect):
        candidates.append(base / "LOCPOT")
    candidates.append(job_folder_path(base, config, folder_name) / "LOCPOT")
    seen = set()
    for path in candidates:
        path = Path(path)
        if path in seen:
            continue
        seen.add(path)
        if path.exists():
            return path
    raise FileNotFoundError(
        "neutral/PZC LOCPOT is missing; checked: "
        + ", ".join(str(path) for path in candidates)
    )


def _neutral_reference_folder(base: Path, config: WorkflowConfig, nelect_ref: float) -> str:
    neutral_folders = []
    for folder_name in config.folders:
        outcar = parse_outcar(job_folder_path(base, config, folder_name) / "OUTCAR")
        if abs(outcar.nelect - nelect_ref) <= NELECT_TOLERANCE:
            neutral_folders.append(folder_name)
    if neutral_folders:
        return neutral_folders[0]
    raise ValueError(f"no neutral charge folder has NELECT={nelect_ref:.4f}")


def _outcar_is_neutral(path: Path, nelect_ref: float) -> bool:
    if not Path(path).exists():
        return False
    return abs(parse_outcar(path).nelect - nelect_ref) <= NELECT_TOLERANCE


def _active_incar_nelect(path: Path) -> Optional[float]:
    if not Path(path).exists():
        return None
    for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        match = re.match(r"\s*NELECT\s*=\s*([-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)", line)
        if match:
            return float(match.group(1))
    return None


def _input_modified_after_outcar_start(path: Path, outcar_start_timestamp: Optional[float]) -> bool:
    if outcar_start_timestamp is None or not Path(path).exists():
        return False
    return Path(path).stat().st_mtime > outcar_start_timestamp


def _fit_residuals(rows: List[Dict[str, object]]) -> Dict[str, float]:
    fit_rows = [
        row
        for row in rows
        if float(row.get("converged", 1)) == 1.0
        and float(row.get("fit_included", 1)) == 1.0
    ]
    if len(fit_rows) < 3:
        return {}
    try:
        fit = analyze_rows(fit_rows)["energy_fit"]
    except ValueError:
        return {}
    return {
        str(row["folder"]): float(row["energy_at_potential"])
        - evaluate_quadratic(float(fit["a"]), float(fit["b"]), float(fit["c"]), float(row["u_vs_she"]))
        for row in fit_rows
    }


def _write_charge_incar(path: Path, nelect: float, profile: str = "vaspsol-charge-relax") -> None:
    incar = path.read_text(encoding="utf-8", errors="ignore")
    incar = replace_or_append(incar, "NELECT", f"{nelect:.4f}")
    incar = replace_or_append(incar, "ISTART", "0")
    incar = replace_or_append(incar, "ICHARG", "1")
    path.write_text(incar, encoding="utf-8")


def _write_child_slurm(path: Path, job_name: str) -> None:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if _slurm_job_name(text):
        lines = []
        for line in text.splitlines():
            if re.match(r"^\s*#SBATCH\s+(?:-J|--job-name(?:=|\s))", line):
                lines.append(f"#SBATCH -J {job_name}")
            else:
                lines.append(line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        path.write_text(f"#SBATCH -J {job_name}\n" + text, encoding="utf-8")


def _slurm_job_name(text: str) -> Optional[str]:
    for line in text.splitlines():
        stripped = line.strip()
        match = re.match(r"^#SBATCH\s+(?:-J\s+|--job-name(?:=|\s+))(\S+)", stripped)
        if match:
            return match.group(1)
    return None


def _child_job_name(base_job_name: str, folder_name: str) -> str:
    if base_job_name.endswith("-0"):
        base_job_name = base_job_name[:-2]
    return f"{base_job_name}-{folder_name}"


def _validate_submit_folder(folder: Path, script: str) -> None:
    for filename in ("INCAR", "POSCAR", "POTCAR", "KPOINTS", script):
        _require_existing(folder / filename)


def _require_existing(path: Path) -> None:
    if not Path(path).exists():
        raise FileNotFoundError(f"required file is missing: {path}")


def _job_finished_and_converged(folder: Path, scheduler: SlurmScheduler, job_id: str) -> bool:
    state = scheduler.status(job_id)
    if state.exists:
        return False
    if not is_converged(folder):
        raise RuntimeError(f"{folder} job ended but OUTCAR is not converged")
    return True


def _write_job_state(path: Path, job_ids: Dict[str, str]) -> None:
    Path(path).write_text(json.dumps(job_ids, indent=2, sort_keys=True), encoding="utf-8")


def _read_job_state(path: Path) -> Dict[str, str]:
    if not Path(path).exists():
        raise FileNotFoundError(f"job state file is missing: {path}")
    return json.loads(Path(path).read_text(encoding="utf-8"))
