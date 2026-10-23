"""Read-only selection and validation of the active VASP sampling policy."""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Optional, Tuple

from .incar import incar_assignments


_REAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[EeDd][+-]?[0-9]+)?")
_LOGICALS = {"T": True, "TRUE": True, ".TRUE.": True,
             "F": False, "FALSE": False, ".FALSE.": False}


def _has_kpoints(workdir: Path) -> bool:
    path = workdir / "KPOINTS"
    # Even a broken link must be diagnosed as KPOINTS, never bypassed.
    return path.exists() or path.is_symlink()


def _sampling_tags(workdir: Path, incar_text: Optional[str] = None) -> dict:
    text = incar_text
    if text is None:
        try:
            text = (workdir / "INCAR").read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
    tags = {}
    for key, value in incar_assignments(text):
        if key in {"KSPACING", "KGAMMA"}:
            tags.setdefault(key, []).append(value)
    return tags


def _native_sampling(workdir: Path, incar_text: Optional[str] = None) -> Optional[Tuple[float, bool, bool]]:
    tags = _sampling_tags(workdir, incar_text)
    if "KSPACING" not in tags:
        return None
    for key, values in tags.items():
        if len(values) != 1:
            raise ValueError(f"{workdir / 'INCAR'}: duplicate {key} assignments in KSPACING-only sampling")
    raw = tags["KSPACING"][0]
    spacing = float(raw.replace("D", "E").replace("d", "e")) if _REAL.fullmatch(raw) else math.nan
    if not math.isfinite(spacing) or spacing <= 0:
        raise ValueError(f"{workdir / 'INCAR'}: KSPACING must be a finite positive real number; got {raw!r}")
    gamma_raw = tags.get("KGAMMA", [".TRUE."])[0].upper()
    if gamma_raw not in _LOGICALS:
        raise ValueError(f"{workdir / 'INCAR'}: KGAMMA must be .TRUE./.FALSE. (T/F); got {gamma_raw!r}")
    return spacing, _LOGICALS[gamma_raw], "KGAMMA" not in tags


def kpoints_required(workdir: Path, *, incar_text: Optional[str] = None) -> bool:
    """Whether existing KPOINTS checks apply; validate native sampling otherwise.

    KPOINTS always takes precedence, including malformed files and broken links.
    With neither KPOINTS nor an explicit KSPACING, retain the legacy requirement.
    Invalid active KSPACING-only settings raise ValueError, never a fallback mesh.
    """
    workdir = Path(workdir)
    return _has_kpoints(workdir) or _native_sampling(workdir, incar_text) is None


def describe_sampling(workdir: Path) -> str:
    """Describe the actual policy without generating or validating a mesh file."""
    workdir = Path(workdir)
    if _has_kpoints(workdir):
        return "KPOINTS file (takes precedence; INCAR KSPACING/KGAMMA ignored)"
    native = _native_sampling(workdir)
    if native is None:
        return "KPOINTS required (no explicit KSPACING in INCAR)"
    spacing, gamma, default = native
    logical = ".TRUE." if gamma else ".FALSE."
    mode = "Gamma-centered" if gamma else "Monkhorst-Pack"
    return (
        f"native KSPACING={spacing:g} A^-1; KGAMMA={logical}"
        f"{' (default)' if default else ''}; {mode}; no KPOINTS"
    )
