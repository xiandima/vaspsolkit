from __future__ import annotations

import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .incar import replace_or_append


COMMON_DEFAULTS = {
    "PREC": "Accurate",
    "EDIFF": "1E-5",
    "NELM": "300",
    "ALGO": "Fast",
    "ISMEAR": "0",
    "SIGMA": "0.05",
    "LREAL": "Auto",
    "LASPH": ".TRUE.",
    "ISYM": "0",
}

RELAX_TAGS = {
    "IBRION": "2",
    "NSW": "200",
    "POTIM": "0.2",
    "ISIF": "2",
    "EDIFFG": "-0.05",
}

STATIC_TAGS = {"IBRION": "-1", "NSW": "0"}

VASP_SOL_TAGS = {
    "LSOL": ".TRUE.",
    "ISOL": "1",
    "EB_K": "80",
    "TAU": "0",
    "LAMBDA_D_K": "3.0",
    "LVHAR": ".TRUE.",
    "LCHARG": ".TRUE.",
}

NEUTRAL_VASPSOL_REQUIRED_TAGS = {
    **VASP_SOL_TAGS,
    "ISTART": "0",
    "ICHARG": "2",
}


@dataclass(frozen=True)
class IncarUpdatePlan:
    candidate: str
    additions: Tuple[Tuple[str, str], ...]
    duplicates: Tuple[str, ...]
    conflicts: Tuple[Tuple[str, str, str], ...]

PROFILE_TAGS = {
    "relax": RELAX_TAGS,
    "static": STATIC_TAGS,
    "vaspsol-neutral": {**STATIC_TAGS, **VASP_SOL_TAGS, "ISTART": "0", "ICHARG": "2"},
    "vaspsol-sweep": {**STATIC_TAGS, **VASP_SOL_TAGS},
    "vaspsol-neutral-relax": {
        **RELAX_TAGS,
        **VASP_SOL_TAGS,
        "ISTART": "0",
        "ICHARG": "2",
        "LWAVE": ".FALSE.",
        "LCHARG": ".TRUE.",
    },
    "vaspsol-charge-relax": {
        **RELAX_TAGS,
        **VASP_SOL_TAGS,
        "ISTART": "0",
        "ICHARG": "1",
        "LWAVE": ".FALSE.",
        "LCHARG": ".TRUE.",
    },
}

CRITICAL_INCAR_TAGS = (
    "ENCUT",
    "ISPIN",
    "MAGMOM",
    "ALGO",
    "NELM",
    "EDIFF",
    "ISMEAR",
    "SIGMA",
    "IBRION",
    "NSW",
    "POTIM",
    "ISIF",
    "IVDW",
    "LDIPOL",
    "IDIPOL",
    "LSOL",
    "EB_K",
    "TAU",
    "LAMBDA_D_K",
)


def poscar_elements(path: Path) -> List[str]:
    lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
    if len(lines) < 7:
        raise ValueError(f"POSCAR is too short: {path}")
    tokens = lines[5].split()
    if not tokens or all(_is_number(token) for token in tokens):
        raise ValueError("POSCAR does not contain a VASP5 element line")
    return tokens


def potcar_elements(path: Path) -> List[str]:
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    elements = []
    for line in text.splitlines():
        if "TITEL" not in line.upper() or "=" not in line:
            continue
        tokens = line.split("=", 1)[1].split()
        candidate = next((token for token in tokens if re.match(r"^[A-Z][a-z]?(?:_|$)", token)), "")
        if candidate:
            elements.append(candidate.split("_", 1)[0])
    if not elements:
        raise ValueError(f"POTCAR contains no TITEL element records: {path}")
    return elements


def validate_potcar_order(poscar: Path, potcar: Path) -> List[str]:
    expected = poscar_elements(poscar)
    actual = potcar_elements(potcar)
    if expected != actual:
        raise ValueError(f"POTCAR element order {actual} does not match POSCAR element order {expected}")
    return expected


def suggest_encut(potcar: Path, scale: float = 1.3) -> int:
    text = Path(potcar).read_text(encoding="utf-8", errors="ignore")
    values = [float(value) for value in re.findall(r"\bENMAX\s*=\s*([0-9.]+)", text, flags=re.IGNORECASE)]
    if not values:
        raise ValueError(f"POTCAR contains no ENMAX values: {potcar}")
    return int(math.ceil(max(values) * scale / 5.0) * 5)


def apply_incar_profile(
    text: str,
    profile: str,
    overrides: Optional[Mapping[str, str]] = None,
    suggested_encut: Optional[int] = None,
) -> str:
    if profile not in PROFILE_TAGS:
        raise ValueError(f"unknown INCAR profile: {profile}")
    result = text
    existing = _active_incar_tags(result)
    common = dict(COMMON_DEFAULTS)
    if suggested_encut is not None:
        common["ENCUT"] = str(suggested_encut)
    for key, value in common.items():
        if key not in existing:
            result = replace_or_append(result, key, value)
    for key, value in PROFILE_TAGS[profile].items():
        result = replace_or_append(result, key, value)
    for key, value in (overrides or {}).items():
        result = replace_or_append(result, key.upper(), str(value))
    return result


def plan_neutral_vaspsol_update(text: str) -> IncarUpdatePlan:
    """Plan safe neutral VASPsol additions without replacing user settings."""
    assignments: Dict[str, List[str]] = {}
    for line in text.splitlines():
        clean = line.split("!", 1)[0].split("#", 1)[0]
        if "=" not in clean:
            continue
        key, value = clean.split("=", 1)
        normalized_key = key.strip().upper()
        if normalized_key:
            assignments.setdefault(normalized_key, []).append(value.strip())

    duplicates = tuple(sorted(key for key, values in assignments.items() if len(values) > 1))
    conflicts = []
    additions = []
    candidate = text
    for key, required in NEUTRAL_VASPSOL_REQUIRED_TAGS.items():
        values = assignments.get(key, [])
        if not values:
            additions.append((key, required))
            candidate = replace_or_append(candidate, key, required)
        elif _normalized_incar_value(values[-1]) != _normalized_incar_value(required):
            conflicts.append((key, values[-1], required))

    relax_conflict = _neutral_relaxation_conflict(assignments)
    if relax_conflict is not None:
        conflicts.append(relax_conflict)
    return IncarUpdatePlan(
        candidate=candidate,
        additions=tuple(additions),
        duplicates=duplicates,
        conflicts=tuple(conflicts),
    )


def vaspkit_executable() -> Optional[str]:
    return shutil.which("vaspkit")


def run_vaspkit(task_input: Sequence[str], workdir: Path) -> subprocess.CompletedProcess:
    executable = vaspkit_executable()
    if executable is None:
        raise FileNotFoundError("vaspkit is not available on PATH")
    return subprocess.run(
        [executable],
        cwd=Path(workdir),
        input="\n".join(task_input) + "\n",
        text=True,
        capture_output=True,
    )


def _active_incar_tags(text: str) -> Dict[str, str]:
    tags: Dict[str, str] = {}
    for line in text.splitlines():
        clean = line.split("!", 1)[0].split("#", 1)[0]
        if "=" not in clean:
            continue
        key, value = clean.split("=", 1)
        tags[key.strip().upper()] = value.strip()
    return tags


def _normalized_incar_value(value: str) -> str:
    normalized = value.strip().upper()
    if normalized in {"TRUE", ".TRUE.", "T"}:
        return ".TRUE."
    if normalized in {"FALSE", ".FALSE.", "F"}:
        return ".FALSE."
    return normalized


def _neutral_relaxation_conflict(
    assignments: Mapping[str, Sequence[str]],
) -> Optional[Tuple[str, str, str]]:
    ibrion = assignments.get("IBRION", [])
    nsw = assignments.get("NSW", [])
    if not ibrion or not nsw:
        return ("RELAXATION", "IBRION/NSW missing", "IBRION > 0 and NSW > 0")
    try:
        valid = int(float(ibrion[-1])) > 0 and int(float(nsw[-1])) > 0
    except ValueError:
        valid = False
    if valid:
        return None
    return ("RELAXATION", f"IBRION={ibrion[-1]}, NSW={nsw[-1]}", "IBRION > 0 and NSW > 0")


def _is_number(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False
