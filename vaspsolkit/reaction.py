from __future__ import annotations

import ast
import csv
import json
import operator
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping

from .analysis import evaluate_quadratic


@dataclass(frozen=True)
class ReactionOutputs:
    csv_path: Path
    plot_path: Path
    report_path: Path


_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def evaluate_formula(
    formula: str,
    fits: Mapping[str, Mapping[str, float]],
    constants: Mapping[str, float],
    potential: float,
) -> float:
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid reaction formula: {exc.msg}") from exc
    return float(_evaluate_node(tree.body, fits, constants, float(potential)))


def run_reaction_spec(spec_path: Path, output_dir: Path) -> ReactionOutputs:
    spec_path = Path(spec_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    fits: Dict[str, Dict[str, float]] = {}
    for name, source in spec.get("systems", {}).items():
        data = json.loads((spec_path.parent / source).read_text(encoding="utf-8"))
        fit = data.get("energy_fit", data)
        fits[name] = {key: float(fit[key]) for key in ("a", "b", "c")}
    constants = {name: float(value) for name, value in spec.get("constants", {}).items()}
    curves = spec.get("curves", [])
    if not fits or not curves:
        raise ValueError("reaction spec requires systems and curves")
    grid = spec.get("grid", {})
    u_min = float(grid.get("u_min", -1.5))
    u_max = float(grid.get("u_max", 0.5))
    count = int(grid.get("points", 301))
    if count < 2 or u_max <= u_min:
        raise ValueError("reaction grid requires points >= 2 and u_max > u_min")
    potentials = [u_min + (u_max - u_min) * index / (count - 1) for index in range(count)]
    values = {
        curve["name"]: [evaluate_formula(curve["formula"], fits, constants, u) for u in potentials]
        for curve in curves
    }

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "reaction_curves.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = ["u_vs_she", *values]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for index, potential in enumerate(potentials):
            writer.writerow({"u_vs_she": f"{potential:.8f}", **{name: f"{series[index]:.10f}" for name, series in values.items()}})
    plot_path = output_dir / "reaction_curves.png"
    _plot_reaction(plot_path, potentials, values, spec)
    report_path = output_dir / "reaction_report.md"
    lines = [f"# {spec.get('name', 'Reaction')} reaction report", ""]
    for curve in curves:
        lines.append(f"- `{curve['name']}`: `{curve['formula']}`")
    lines.extend(["", "![Reaction curves](reaction_curves.png)", ""])
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return ReactionOutputs(csv_path, plot_path, report_path)


def _evaluate_node(node, fits, constants, potential):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        raise ValueError("string values are not allowed outside E(system, U)")
    if isinstance(node, ast.Name):
        if node.id == "U":
            return potential
        if node.id in constants:
            return float(constants[node.id])
        raise ValueError(f"name is not allowed in reaction formula: {node.id}")
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        return _BINARY[type(node.op)](
            _evaluate_node(node.left, fits, constants, potential),
            _evaluate_node(node.right, fits, constants, potential),
        )
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_evaluate_node(node.operand, fits, constants, potential))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id != "E" or len(node.args) != 2 or node.keywords:
            raise ValueError("function call is not allowed; only E(\"system\", U) is supported")
        system_node = node.args[0]
        if not isinstance(system_node, ast.Constant) or not isinstance(system_node.value, str):
            raise ValueError("E first argument must be a quoted system name")
        if system_node.value not in fits:
            raise ValueError(f"unknown reaction system: {system_node.value}")
        u = _evaluate_node(node.args[1], fits, constants, potential)
        fit = fits[system_node.value]
        return evaluate_quadratic(fit["a"], fit["b"], fit["c"], u)
    raise ValueError(f"expression is not allowed in reaction formula: {type(node).__name__}")


def _plot_reaction(path: Path, potentials, values, spec) -> None:
    cache = Path(tempfile.gettempdir()) / "vaspsolkit-matplotlib"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FormatStrFormatter

    fig, ax = plt.subplots(figsize=(7.0, 5.0))
    for name, series in values.items():
        ax.plot(potentials, series, linewidth=2.0, label=name)
    ax.set_xlabel(spec.get("plot", {}).get("xlabel", "U / V vs. SHE"), fontsize=13)
    ax.set_ylabel(spec.get("plot", {}).get("ylabel", "Free energy / eV"), fontsize=13)
    ax.yaxis.set_major_formatter(FormatStrFormatter("%.1f"))
    ax.tick_params(labelsize=11, direction="in")
    ax.legend(frameon=False, fontsize=10, loc="best")
    fig.tight_layout()
    fig.savefig(path, dpi=300)
    plt.close(fig)
