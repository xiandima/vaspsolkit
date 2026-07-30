from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple


E_CHARGE_UC_PER_E = 1.602176634e-13
A2_TO_CM2 = 1.0e-16
E_PER_A2_TO_UC_PER_CM2 = E_CHARGE_UC_PER_E / A2_TO_CM2


@dataclass
class OutcarData:
    nelect: float
    efermi: float
    toten: float
    converged: bool
    input_nelect: Optional[float] = None
    magnetization: Optional[float] = None
    start_time: Optional[str] = None
    start_timestamp: Optional[float] = None
    initial_charge_density_supplied: bool = False
    old_mixing_mesh: bool = False


@dataclass
class LocpotData:
    grid: Tuple[int, int, int]
    planar_average: List[float]


@dataclass
class SurfaceChargeData:
    delta_electrons: float
    electrode_charge_e: float
    surface_charge_e_per_a2: float
    surface_charge_uC_cm2: float


def parse_outcar(path: Path) -> OutcarData:
    text = Path(path).read_text(errors="ignore")
    total_nelect_matches = re.findall(
        r"NELECT\s*=\s*([-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)\s+total number of electrons",
        text,
    )
    nelect_matches = re.findall(r"NELECT\s*=\s*([-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)", text)
    efermi_matches = re.findall(r"E-fermi\s*:\s*([-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)", text)
    toten_matches = re.findall(r"TOTEN\s*=\s*([-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)", text)
    if not total_nelect_matches and not nelect_matches:
        raise ValueError(f"{path} does not contain NELECT")
    if not efermi_matches:
        raise ValueError(f"{path} does not contain E-fermi")
    if not toten_matches:
        raise ValueError(f"{path} does not contain TOTEN")
    nelect = float(total_nelect_matches[-1] if total_nelect_matches else nelect_matches[-1])
    input_nelect = _input_nelect(text)
    magnetization = _last_magnetization(text)
    start_time, start_timestamp = _outcar_start_time(text)
    return OutcarData(
        nelect=nelect,
        efermi=float(efermi_matches[-1]),
        toten=float(toten_matches[-1]),
        converged=outcar_converged_from_text(text),
        input_nelect=input_nelect,
        magnetization=magnetization,
        start_time=start_time,
        start_timestamp=start_timestamp,
        initial_charge_density_supplied="initial charge density was supplied" in text,
        old_mixing_mesh="Broyden mixing: mesh for mixing (old mesh)" in text,
    )


def outcar_converged_from_text(text: str) -> bool:
    if "reached required accuracy" in text:
        return True
    if "aborting loop because EDIFF is reached" in text:
        return not _outcar_is_relaxation(text)
    return False


def _outcar_is_relaxation(text: str) -> bool:
    nsw_matches = re.findall(r"^\s*NSW\s*=\s*([-+]?\d+)", text, flags=re.MULTILINE)
    if not nsw_matches:
        return False
    try:
        nsw = int(nsw_matches[-1])
    except ValueError:
        return False
    if nsw <= 0:
        return False
    ibrion_matches = re.findall(r"^\s*IBRION\s*=\s*([-+]?\d+)", text, flags=re.MULTILINE)
    if not ibrion_matches:
        return True
    try:
        ibrion = int(ibrion_matches[-1])
    except ValueError:
        return True
    return ibrion >= 0


def _input_nelect(text: str) -> Optional[float]:
    values = []
    for line in text.splitlines():
        if "total number of electrons" in line:
            break
        match = re.match(r"\s*NELECT\s*=\s*([-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)", line)
        if match:
            values.append(float(match.group(1)))
    return values[-1] if values else None


def _last_magnetization(text: str) -> Optional[float]:
    matches = re.findall(
        r"number of electron\s+[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?\s+magnetization\s+([-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)",
        text,
    )
    return float(matches[-1]) if matches else None


def _outcar_start_time(text: str) -> Tuple[Optional[str], Optional[float]]:
    match = re.search(
        r"executed on\s+\S+\s+date\s+(\d{4})\.(\d{2})\.(\d{2})\s+(\d{2}:\d{2}:\d{2})",
        text,
    )
    if not match:
        return None, None
    start_time = f"{match.group(1)}-{match.group(2)}-{match.group(3)} {match.group(4)}"
    return start_time, datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S").timestamp()


def parse_locpot(path: Path) -> LocpotData:
    lines = Path(path).read_text(errors="ignore").splitlines()
    data_start = _volumetric_data_start(lines)
    grid = _parse_grid_line(lines[data_start])
    nx, ny, nz = grid
    total = nx * ny * nz
    values = _read_floats(lines[data_start + 1 :], total)
    if len(values) < total:
        raise ValueError(f"{path} has {len(values)} volumetric values, expected {total}")

    plane_size = nx * ny
    planar = []
    for z_index in range(nz):
        start = z_index * plane_size
        plane = values[start : start + plane_size]
        planar.append(sum(plane) / len(plane))

    return LocpotData(
        grid=grid,
        planar_average=planar,
    )


def vacuum_level_from_locpot(path: Path) -> float:
    return max(parse_locpot(path).planar_average)


def slab_area_from_poscar(path: Path) -> float:
    lines = Path(path).read_text(errors="ignore").splitlines()
    if len(lines) < 5:
        raise ValueError(f"{path} is too short to be a POSCAR-like file")
    scale = float(lines[1].split()[0])
    a = [float(value) * scale for value in lines[2].split()[:3]]
    b = [float(value) * scale for value in lines[3].split()[:3]]
    cross = (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )
    return math.sqrt(sum(component * component for component in cross))


def calculate_vacuum_reference_work_function(efermi: float, vacuum_level: float) -> float:
    return vacuum_level - efermi


def calculate_vacuum_reference_potential_vs_she(
    efermi: float,
    vacuum_level: float,
    she_reference: float = 4.70,
) -> float:
    return calculate_vacuum_reference_work_function(efermi, vacuum_level) - she_reference


def corrected_energy_at_potential(
    toten: float,
    delta_electrons: float,
    efermi: float,
) -> float:
    return toten - delta_electrons * efermi


def surface_charge(
    nelect: float,
    nelect_ref: float,
    area_a2: float,
    interface_count: int = 1,
) -> SurfaceChargeData:
    if area_a2 <= 0:
        raise ValueError("area_a2 must be positive")
    if interface_count <= 0:
        raise ValueError("interface_count must be positive")
    delta_electrons = nelect - nelect_ref
    electrode_charge_e = -delta_electrons
    surface_charge_e_per_a2 = electrode_charge_e / (area_a2 * interface_count)
    surface_charge_uC_cm2 = surface_charge_e_per_a2 * E_PER_A2_TO_UC_PER_CM2
    return SurfaceChargeData(
        delta_electrons=delta_electrons,
        electrode_charge_e=electrode_charge_e,
        surface_charge_e_per_a2=surface_charge_e_per_a2,
        surface_charge_uC_cm2=surface_charge_uC_cm2,
    )


def _volumetric_data_start(lines: Sequence[str]) -> int:
    counts_index = _counts_line_index(lines)
    atom_count = sum(int(float(token)) for token in lines[counts_index].split())
    index = counts_index + 1
    if index < len(lines) and lines[index].strip().lower().startswith("s"):
        index += 1
    if index >= len(lines):
        raise ValueError("missing coordinate mode line")
    index += 1 + atom_count
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines):
        raise ValueError("missing volumetric grid line")
    return index


def _counts_line_index(lines: Sequence[str]) -> int:
    if len(lines) < 7:
        raise ValueError("file is too short to contain a POSCAR header")
    if _all_numeric_ints(lines[5].split()):
        return 5
    if _all_numeric_ints(lines[6].split()):
        return 6
    raise ValueError("could not locate POSCAR atom counts line")


def _all_numeric_ints(tokens: Iterable[str]) -> bool:
    tokens = list(tokens)
    if not tokens:
        return False
    try:
        return all(float(token).is_integer() for token in tokens)
    except ValueError:
        return False


def _parse_grid_line(line: str) -> Tuple[int, int, int]:
    tokens = line.split()
    if len(tokens) < 3:
        raise ValueError(f"invalid grid line: {line!r}")
    return int(tokens[0]), int(tokens[1]), int(tokens[2])


def _read_floats(lines: Sequence[str], limit: int) -> List[float]:
    values: List[float] = []
    for line in lines:
        for token in line.split():
            try:
                values.append(float(token))
            except ValueError:
                continue
            if len(values) == limit:
                return values
    return values
