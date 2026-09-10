from __future__ import annotations

import ast
import csv
import json
import math
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
    analyses: Dict[str, Dict[str, object]] = {}
    for name, source in spec.get("systems", {}).items():
        data = json.loads((spec_path.parent / source).read_text(encoding="utf-8"))
        fit = data.get("energy_fit", data)
        fits[name] = {key: float(fit[key]) for key in ("a", "b", "c")}
        analyses[name] = data
    constants = {name: float(value) for name, value in spec.get("constants", {}).items()}
    curves = spec.get("curves", [])
    if not fits or not curves:
        raise ValueError("reaction spec requires systems and curves")
    grid = spec.get("grid", {})
    u_min = float(grid.get("u_min", -1.5))
    u_max = float(grid.get("u_max", 0.5))
    count = int(grid.get("points", 301))
    if count < 2 or not all(math.isfinite(u) for u in (u_min, u_max)) or u_max <= u_min:
        raise ValueError("reaction grid requires points >= 2 and u_max > u_min")
    compatibility = _validate_compatibility(analyses, spec, u_min=u_min, u_max=u_max)
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
    lines = [
        f"# {spec.get('name', 'Reaction')} reaction report",
        "",
        "- Compatibility checks: " + (
            "overrides enabled; not a strict compatibility pass"
            if compatibility["overrides"] else "passed (metadata and requested grid only)"
        ),
        "- Enabled overrides: " + (", ".join(compatibility["overrides"]) or "none"),
        f"- Requested grid: {u_min:.6f} to {u_max:.6f} V vs. SHE ({count} samples)",
        "- Quantity: Electronic energy expression; it is not labelled Gibbs free energy unless the constants and source analyses explicitly include all corrections.",
        "- Scope: matching metadata does not verify input files, structures, thermochemical constants, or transformed E(system, U) arguments.",
    ]
    if compatibility["domain_complete"]:
        lines.append(f"- Common fitted domain: {compatibility['u_min']:.6f} to {compatibility['u_max']:.6f} V vs. SHE")
    else:
        lines.append("- Common fitted domain: unknown (one or more systems lack a domain)")
        if compatibility["domains"]:
            lines.append(f"- Intersection of known domains only: {compatibility['u_min']:.6f} to {compatibility['u_max']:.6f} V vs. SHE")
    lines.extend(f"- Warning: {warning}" for warning in compatibility["warnings"])
    lines.extend(["", "## Inputs", "", f"- Spec: `{spec_path.resolve()}`"])
    for name, source in spec["systems"].items():
        lines.append(f"- `{name}`: `{(spec_path.parent / source).resolve()}`")
        lines.append(f"  - Metadata: `{json.dumps(analyses[name].get('provenance'), sort_keys=True)}`")
        lines.append(f"  - Fitted domain: `{json.dumps(analyses[name].get('fit_domain'), sort_keys=True)}`")
    lines.extend(["", "## Constants", ""])
    lines.extend(f"- `{name}` = {value:.12g}" for name, value in constants.items())
    if not constants:
        lines.append("- none")
    lines.extend(["", "## Formulas", ""])
    for curve in curves:
        lines.append(f"- `{curve['name']}`: `{curve['formula']}`")
    lines.extend(["", "![Reaction curves](reaction_curves.png)", ""])
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return ReactionOutputs(csv_path, plot_path, report_path)


_PROVENANCE_KEYS = (
    "she_reference_eV",
    "potential_convention",
    "energy_convention",
    "fermi_shift_handling",
    "method_fingerprint",
)


def _validate_compatibility(analyses, spec, *, u_min: float, u_max: float):
    options = spec.get("compatibility", {})
    if not isinstance(options, dict):
        raise ValueError("reaction compatibility must be an object")
    allow_unknown = _option_bool(options, "allow_unknown_provenance", False)
    allow_mixed = _option_bool(options, "allow_mixed_provenance", False)
    allow_extrapolation = _option_bool(options, "allow_extrapolation", False)

    signatures = {}
    domains = {}
    warnings = []
    for name, analysis in analyses.items():
        provenance = analysis.get("provenance")
        domain = analysis.get("fit_domain")
        if not isinstance(provenance, dict):
            if not allow_unknown:
                raise ValueError(
                    f"reaction system {name!r} has unknown provenance or fitted potential domain; "
                    "regenerate analysis.json or explicitly allow legacy provenance"
                )
            provenance = {}
        missing = [key for key in _PROVENANCE_KEYS if provenance.get(key) in (None, "")]
        if missing and not allow_unknown:
            raise ValueError(
                f"reaction system {name!r} provenance is missing: {', '.join(missing)}"
            )
        if missing:
            warnings.append(f"{name}: unknown provenance fields: {', '.join(missing)}")
        signatures[name] = {key: provenance.get(key) for key in _PROVENANCE_KEYS}
        if domain is None:
            if not allow_unknown:
                raise ValueError(f"reaction system {name!r} has an unknown fitted potential domain")
            warnings.append(f"{name}: unknown fitted potential domain")
            continue
        try:
            bounds = (float(domain["u_min"]), float(domain["u_max"]))
            if not all(math.isfinite(u) for u in bounds) or bounds[0] > bounds[1]:
                raise ValueError("nonfinite or reversed domain")
            domains[name] = bounds
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"reaction system {name!r} has an invalid fitted potential domain") from exc

    for key in _PROVENANCE_KEYS:
        known = [(name, signature[key]) for name, signature in signatures.items() if signature[key] not in (None, "")]
        if not known:
            continue
        first_name, reference = known[0]
        for name, value in known[1:]:
            if value != reference:
                message = f"reaction systems {first_name!r} and {name!r} have incompatible {key}"
                if not allow_mixed:
                    raise ValueError(message)
                warnings.append(message)

    if domains:
        common_min = max(domain[0] for domain in domains.values())
        common_max = min(domain[1] for domain in domains.values())
        if common_min > common_max:
            raise ValueError("reaction systems have no common fitted potential domain")
        if u_min < common_min or u_max > common_max:
            message = "reaction grid is outside the common fitted potential domain " + f"[{common_min:g}, {common_max:g}] V vs. SHE"
            if not allow_extrapolation:
                raise ValueError(message)
            warnings.append(message)
    elif not allow_unknown:
        raise ValueError("reaction systems do not provide fitted potential domains")
    else:
        common_min = common_max = None

    return {
        "u_min": common_min,
        "u_max": common_max,
        "domains": domains,
        "domain_complete": len(domains) == len(analyses),
        "overrides": [key for key in ("allow_unknown_provenance", "allow_mixed_provenance", "allow_extrapolation") if options.get(key, False)],
        "warnings": warnings,
    }


def _option_bool(options, key: str, default: bool) -> bool:
    value = options.get(key, default)
    if type(value) is not bool:
        raise ValueError(f"reaction compatibility.{key} must be a boolean")
    return value


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
    ax.set_ylabel(spec.get("plot", {}).get("ylabel", "Electronic energy expression / eV"), fontsize=13)
    ax.yaxis.set_major_formatter(FormatStrFormatter("%.1f"))
    ax.tick_params(labelsize=11, direction="in")
    ax.legend(frameon=False, fontsize=10, loc="best")
    fig.tight_layout()
    fig.savefig(path, dpi=300)
    plt.close(fig)
