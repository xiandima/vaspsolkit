from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .analysis import analyze_rows, evaluate_quadratic, read_summary, write_analysis


@dataclass(frozen=True)
class PostprocessResult:
    point_count: int
    analysis_path: Path
    curve_path: Path
    plot_path: Path
    report_path: Path
    run_dir: Optional[Path] = None


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def postprocess_versioned(
    summary_path: Path,
    history_root: Path,
    allow_excluded: bool = False,
    *,
    run_id: Optional[str] = None,
) -> PostprocessResult:
    """Create one immutable, provenance-bearing postprocessing record."""
    summary_path = Path(summary_path)
    if not summary_path.is_file():
        raise FileNotFoundError(summary_path)
    if summary_path.is_symlink():
        raise ValueError("summary input must be a regular file, not a symlink")
    run_id = run_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
        + uuid.uuid4().hex[:8]
    )
    if _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run_id may contain only letters, digits, dot, underscore, and hyphen")

    history_root = Path(history_root)
    history_root.mkdir(parents=True, exist_ok=True)
    destination = history_root / run_id
    staging = history_root / f".{run_id}.tmp"
    if destination.exists() or staging.exists():
        raise FileExistsError(destination)
    staging.mkdir()
    try:
        summary_copy = staging / "summary.csv"
        shutil.copy2(summary_path, summary_copy)
        summary_digest = hashlib.sha256(summary_copy.read_bytes()).hexdigest()
        generated = postprocess_summary(
            summary_copy,
            staging,
            allow_excluded=allow_excluded,
        )
        analysis = json.loads(generated.analysis_path.read_text(encoding="utf-8"))
        source_rows = read_summary(summary_copy)
        reference_values = {row.get("she_reference_eV", "") for row in source_rows}
        reference_sources = {row.get("she_reference_source", "") for row in source_rows}
        reference_known = len(reference_values) == 1 and len(reference_sources) == 1 and "" not in reference_values
        reference_value = float(next(iter(reference_values))) if reference_known else None
        reference_source = next(iter(reference_sources)) if reference_known else ""
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        analysis["provenance"] = {
            "run_id": run_id,
            "created_at": created_at,
            "source_summary": str(summary_path.resolve()),
            "summary_sha256": summary_digest,
            "point_count": generated.point_count,
            "she_reference_eV": reference_value,
            "she_reference_source": reference_source,
            "she_reference_status": "recorded" if reference_known else "unknown",
        }
        generated.analysis_path.write_text(
            json.dumps(analysis, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (staging / "analysis-log.md").write_text(
            "# VASPsolKit analysis record\n\n"
            f"- Run: {run_id}\n"
            f"- Created: {created_at}\n"
            f"- Source: {summary_path.resolve()}\n"
            f"- Summary SHA256: `{summary_digest}`\n"
            f"- Points: {generated.point_count}\n"
            f"- SHE reference: {reference_value if reference_value is not None else 'unknown'} eV\n"
            f"- SHE reference source: {reference_source or 'unknown'}\n",
            encoding="utf-8",
        )
        os.replace(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return PostprocessResult(
        generated.point_count,
        destination / generated.analysis_path.name,
        destination / generated.curve_path.name,
        destination / generated.plot_path.name,
        destination / generated.report_path.name,
        run_dir=destination,
    )


def postprocess_summary(
    summary_path: Path,
    output_dir: Path,
    allow_excluded: bool = False,
) -> PostprocessResult:
    rows = read_summary(Path(summary_path))
    if len(rows) != 5:
        raise ValueError(f"constant-potential postprocessing requires exactly five points; found {len(rows)}")
    neutral = [row for row in rows if abs(_float(row, "delta_electrons")) <= 1.0e-9]
    if len(neutral) != 1:
        raise ValueError("five-point dataset must contain exactly one neutral point")
    excluded = [row for row in rows if not _included(row)]
    if excluded and not allow_excluded:
        names = ", ".join(str(row.get("folder", "?")) for row in excluded)
        raise ValueError(f"points would be excluded from fitting ({names}); rerun or explicitly allow exclusion")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    analysis = analyze_rows(rows)
    analysis_path = output_dir / "analysis.json"
    write_analysis(analysis_path, analysis)
    curve_path = output_dir / "eu_curve.csv"
    _write_curve(curve_path, analysis)
    plot_path = output_dir / "eu_curve.png"
    _plot_eu(plot_path, rows, analysis)
    _write_quality_files(output_dir, rows)
    report_path = output_dir / "report.md"
    _write_report(report_path, rows, analysis, excluded)
    return PostprocessResult(len(rows), analysis_path, curve_path, plot_path, report_path)


def _write_curve(path: Path, analysis: Dict[str, object], points: int = 301) -> None:
    fit = analysis["energy_fit"]
    source = analysis["points"]
    values = [float(row["u_vs_she"]) for row in source]
    lo, hi = min(values), max(values)
    span = hi - lo
    lo -= 0.2 * span
    hi += 0.2 * span
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["u_vs_she", "energy_eV"])
        writer.writeheader()
        for index in range(points):
            u = lo + (hi - lo) * index / (points - 1)
            writer.writerow(
                {
                    "u_vs_she": f"{u:.8f}",
                    "energy_eV": f"{evaluate_quadratic(fit['a'], fit['b'], fit['c'], u):.10f}",
                }
            )


def _plot_eu(path: Path, rows: List[Dict[str, object]], analysis: Dict[str, object]) -> None:
    cache = Path(tempfile.gettempdir()) / "vaspsolkit-matplotlib"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FormatStrFormatter

    fit = analysis["energy_fit"]
    xs = [float(row["u_vs_she"]) for row in rows]
    ys = [float(row.get("energy_at_potential", row.get("toten"))) for row in rows]
    lo, hi = min(xs), max(xs)
    margin = 0.2 * (hi - lo)
    curve_x = [lo - margin + (hi - lo + 2 * margin) * i / 300 for i in range(301)]
    curve_y = [evaluate_quadratic(fit["a"], fit["b"], fit["c"], value) for value in curve_x]
    included_x = [x for x, row in zip(xs, rows) if _included(row)]
    included_y = [y for y, row in zip(ys, rows) if _included(row)]
    excluded_x = [x for x, row in zip(xs, rows) if not _included(row)]
    excluded_y = [y for y, row in zip(ys, rows) if not _included(row)]

    fig, ax = plt.subplots(figsize=(7.0, 5.0))
    ax.plot(curve_x, curve_y, color="#176B87", linewidth=2.2, label="quadratic fit")
    ax.scatter(included_x, included_y, color="#64B5D9", edgecolor="white", linewidth=0.7, s=55, zorder=3, label="five points")
    if excluded_x:
        ax.scatter(excluded_x, excluded_y, marker="x", color="#D84A3A", s=65, zorder=4, label="excluded")
    ax.set_xlabel("U / V vs. SHE", fontsize=13)
    ax.set_ylabel("E(U) / eV", fontsize=13)
    ax.tick_params(labelsize=11, direction="in")
    ax.yaxis.set_major_formatter(FormatStrFormatter("%.1f"))
    ax.ticklabel_format(axis="x", style="plain", useOffset=False)
    ax.legend(frameon=False, fontsize=10, loc="best")
    ax.text(
        0.03,
        0.97,
        f"$R^2$ = {fit['r_squared']:.5f}\n$U_0$ = {fit['u0']:.4f} V",
        transform=ax.transAxes,
        va="top",
        fontsize=10,
    )
    fig.tight_layout()
    fig.savefig(path, dpi=300)
    plt.close(fig)


def _write_quality_files(output_dir: Path, rows: List[Dict[str, object]]) -> None:
    fields = ["folder", "converged", "fit_included", "warnings"]
    quality = []
    for row in rows:
        warnings = []
        if str(row.get("converged", "1")) not in {"1", "1.0"}:
            warnings.append("unconverged")
        if str(row.get("fit_included", "1")) not in {"1", "1.0"}:
            warnings.append("fit_excluded")
        quality.append(
            {
                "folder": row.get("folder", ""),
                "converged": row.get("converged", ""),
                "fit_included": row.get("fit_included", 1),
                "warnings": ";".join(warnings),
            }
        )
    for filename, selected in (
        ("quality_report.csv", quality),
        ("points_to_rerun.csv", [row for row in quality if row["warnings"]]),
    ):
        with (output_dir / filename).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(selected)


def _write_report(path: Path, rows, analysis, excluded) -> None:
    fit = analysis["energy_fit"]
    text = [
        "# vaspsolkit postprocessing report",
        "",
        f"- Raw points: {len(rows)}",
        f"- Fitted points: {len(analysis['points'])}",
        f"- Excluded points: {len(excluded)}",
        f"- U0 / PZC: {fit['u0']:.6f} V vs. SHE",
        f"- R2: {fit['r_squared']:.8f}",
        f"- RMSE: {fit['rmse']:.8f} eV",
        f"- Maximum absolute residual: {fit['max_abs_residual']:.8f} eV",
        "",
        "![E-U curve](eu_curve.png)",
        "",
    ]
    path.write_text("\n".join(text), encoding="utf-8")


def _included(row: Dict[str, object]) -> bool:
    return str(row.get("converged", "1")) in {"1", "1.0"} and str(row.get("fit_included", "1")) in {"1", "1.0"}


def _float(row: Dict[str, object], key: str) -> float:
    if key not in row:
        raise ValueError(f"summary row is missing {key}: {row}")
    return float(row[key])
