from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence


def read_summary(path: Path) -> List[Dict[str, object]]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_analysis(path: Path, analysis: Dict[str, object]) -> None:
    Path(path).write_text(json.dumps(analysis, indent=2, sort_keys=True), encoding="utf-8")


def analyze_rows(
    rows: Sequence[Dict[str, object]],
    target_potentials: Optional[Sequence[float]] = None,
) -> Dict[str, object]:
    all_numeric_rows = [_numeric_row(row) for row in rows]
    numeric_rows = [row for row in all_numeric_rows if _row_fit_included(row)]
    excluded_rows = [row for row in all_numeric_rows if not _row_fit_included(row)]
    energy_key = _energy_key(numeric_rows)
    energy_fit = fit_energy_vs_potential(numeric_rows, energy_key=energy_key)
    result: Dict[str, object] = {
        "points": numeric_rows,
        "excluded_points": excluded_rows,
        "energy_key": energy_key,
        "energy_fit": energy_fit,
        "targets": [],
    }
    if target_potentials:
        result["targets"] = [
            {
                "u_vs_she": float(u),
                "energy": evaluate_quadratic(energy_fit["a"], energy_fit["b"], energy_fit["c"], float(u)),
            }
            for u in target_potentials
        ]
    if all("electrode_charge_e" in row for row in numeric_rows) and len(numeric_rows) >= 2:
        result["charge_fit"] = fit_polynomial(
            [row["u_vs_she"] for row in numeric_rows],
            [row["electrode_charge_e"] for row in numeric_rows],
            degree=1,
        )
    return result


def analyze_adsorption(
    clean_rows: Sequence[Dict[str, object]],
    adsorbate_rows: Sequence[Dict[str, object]],
    target_potentials: Sequence[float],
    reference_energy: float = 0.0,
) -> Dict[str, object]:
    clean_fit = fit_energy_vs_potential([_numeric_row(row) for row in clean_rows])
    adsorbate_fit = fit_energy_vs_potential([_numeric_row(row) for row in adsorbate_rows])
    targets = []
    for potential in target_potentials:
        clean_energy = evaluate_quadratic(clean_fit["a"], clean_fit["b"], clean_fit["c"], potential)
        adsorbate_energy = evaluate_quadratic(
            adsorbate_fit["a"],
            adsorbate_fit["b"],
            adsorbate_fit["c"],
            potential,
        )
        targets.append(
            {
                "u_vs_she": potential,
                "clean_energy": clean_energy,
                "adsorbate_energy": adsorbate_energy,
                "adsorption_energy": adsorbate_energy - clean_energy - reference_energy,
            }
        )
    return {
        "clean_energy_fit": clean_fit,
        "adsorbate_energy_fit": adsorbate_fit,
        "reference_energy": reference_energy,
        "adsorption_targets": targets,
    }


def thermodynamic_potential_rows(rows: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    numeric_rows = [_numeric_row(row) for row in rows]
    fit_rows = [row for row in numeric_rows if _row_fit_included(row)]
    if len(fit_rows) < 3:
        raise ValueError("at least three included points are required for thermodynamic potential conversion")
    for row in fit_rows:
        if "delta_electrons" not in row:
            raise ValueError(f"row is missing delta_electrons: {row}")
        if "efermi" not in row:
            raise ValueError(f"row is missing efermi: {row}")
        if "toten" not in row:
            raise ValueError(f"row is missing toten: {row}")

    toten_fit = fit_polynomial(
        [row["delta_electrons"] for row in fit_rows],
        [row["toten"] for row in fit_rows],
        degree=2,
    )
    offset = sum(row["u_vs_she"] + row["efermi"] for row in fit_rows) / len(fit_rows)
    transformed: List[Dict[str, object]] = []
    for original, numeric in zip(rows, numeric_rows):
        if "delta_electrons" not in numeric:
            raise ValueError(f"row is missing delta_electrons: {original}")
        if "toten" not in numeric:
            raise ValueError(f"row is missing toten: {original}")
        q = numeric["delta_electrons"]
        derivative = 2.0 * toten_fit["a"] * q + toten_fit["b"]
        converted = dict(original)
        converted["fermi_u_vs_she"] = numeric["u_vs_she"]
        if "energy_at_potential" in numeric:
            converted["fermi_energy_at_potential"] = numeric["energy_at_potential"]
        converted["thermodynamic_dToten_dq"] = derivative
        converted["thermodynamic_u_offset"] = offset
        converted["thermodynamic_toten_fit_a"] = toten_fit["a"]
        converted["thermodynamic_toten_fit_b"] = toten_fit["b"]
        converted["thermodynamic_toten_fit_c"] = toten_fit["c"]
        converted["u_vs_she"] = -derivative + offset
        converted["energy_at_potential"] = numeric["toten"] - q * derivative
        transformed.append(converted)
    return transformed


def fit_energy_vs_potential(
    rows: Sequence[Dict[str, float]],
    energy_key: Optional[str] = None,
) -> Dict[str, float]:
    if len(rows) < 3:
        raise ValueError("at least three points are required for quadratic energy fitting")
    energy_key = energy_key or _energy_key(rows)
    xs = [row["u_vs_she"] for row in rows]
    ys = [row[energy_key] for row in rows]
    coeffs = fit_polynomial(
        xs,
        ys,
        degree=2,
    )
    a = coeffs["a"]
    b = coeffs["b"]
    c = coeffs["c"]
    capacitance = -2.0 * a
    u0 = -b / (2.0 * a) if a != 0 else float("nan")
    e0 = c + 0.5 * capacitance * (u0**2) if a != 0 else float("nan")
    fit_quality = _fit_quality(xs, ys, [evaluate_quadratic(a, b, c, x) for x in xs])
    return {
        "energy_key": energy_key,
        "a": a,
        "b": b,
        "c": c,
        "capacitance": capacitance,
        "u0": u0,
        "pzc": u0,
        "e0": e0,
        **fit_quality,
    }


def fit_polynomial(xs: Sequence[float], ys: Sequence[float], degree: int) -> Dict[str, float]:
    if degree not in (1, 2):
        raise ValueError("only linear and quadratic fits are supported")
    if len(xs) != len(ys):
        raise ValueError("x and y arrays must have the same length")
    if len(xs) < degree + 1:
        raise ValueError("not enough points for requested polynomial degree")

    powers = [sum(x ** power for x in xs) for power in range(2 * degree + 1)]
    matrix = []
    rhs = []
    for row in range(degree + 1):
        matrix.append([powers[row + col] for col in range(degree + 1)])
        rhs.append(sum((x**row) * y for x, y in zip(xs, ys)))
    coeffs_low_to_high = _solve_linear_system(matrix, rhs)
    if degree == 1:
        return {"slope": coeffs_low_to_high[1], "intercept": coeffs_low_to_high[0]}
    return {"a": coeffs_low_to_high[2], "b": coeffs_low_to_high[1], "c": coeffs_low_to_high[0]}


def evaluate_quadratic(a: float, b: float, c: float, x: float) -> float:
    return a * x * x + b * x + c


def _fit_quality(xs: Sequence[float], ys: Sequence[float], predicted: Sequence[float]) -> Dict[str, float]:
    residuals = [y - y_fit for y, y_fit in zip(ys, predicted)]
    ss_res = sum(residual * residual for residual in residuals)
    y_mean = sum(ys) / len(ys)
    ss_tot = sum((y - y_mean) ** 2 for y in ys)
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0.0 else 1.0
    return {
        "r_squared": r_squared,
        "rmse": math.sqrt(ss_res / len(ys)),
        "max_abs_residual": max(abs(residual) for residual in residuals),
    }


def _numeric_row(row: Dict[str, object]) -> Dict[str, float]:
    converted: Dict[str, float] = {}
    for key, value in row.items():
        if key == "folder":
            continue
        try:
            converted[key] = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    if "u_vs_she" not in converted:
        raise ValueError(f"row is missing u_vs_she: {row}")
    if "energy_at_potential" not in converted and "toten" not in converted:
        raise ValueError(f"row is missing energy_at_potential or toten: {row}")
    return converted


def _row_fit_included(row: Dict[str, float]) -> bool:
    if row.get("converged", 1.0) != 1.0:
        return False
    return row.get("fit_included", 1.0) == 1.0


def _energy_key(rows: Sequence[Dict[str, float]]) -> str:
    if rows and all("energy_at_potential" in row for row in rows):
        return "energy_at_potential"
    if rows and all("toten" in row for row in rows):
        return "toten"
    raise ValueError("all rows must contain energy_at_potential or toten")


def _solve_linear_system(matrix: List[List[float]], rhs: List[float]) -> List[float]:
    n = len(rhs)
    aug = [row[:] + [rhs_value] for row, rhs_value in zip(matrix, rhs)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(aug[row][col]))
        if abs(aug[pivot][col]) < 1e-14:
            raise ValueError("singular fit matrix")
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pivot_value = aug[col][col]
        aug[col] = [value / pivot_value for value in aug[col]]
        for row in range(n):
            if row == col:
                continue
            factor = aug[row][col]
            aug[row] = [
                value - factor * pivot_component
                for value, pivot_component in zip(aug[row], aug[col])
            ]
    return [aug[row][-1] for row in range(n)]
