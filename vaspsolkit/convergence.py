from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Sequence

from .incar import replace_or_append
from .parsers import outcar_converged_from_text


ERROR_PATTERNS = {
    "brmix_error": (r"\bBRMIX\b.*(?:error|very serious problems)",),
    "zbrent_error": (
        r"\bZBRENT\b[^\n]*(?:fatal\s+error|error\s+in\s+bracketing)",
    ),
    "edddav_error": (r"\bEDDDAV\b",),
    "diagonalization_error": (r"ZHEGV|sub-space-matrix.*not hermitian|ERROR in EDDIAG",),
    "vaspsol_minimize_l_failed": (r"MINIMIZE_L:\s*failed to converge",),
    "incomplete_chgcar": (r"chargedensity file is incomplete",),
}

ACTIVE_STATES = {"Q", "R", "QUEUED", "RUNNING", "PENDING", "CONFIGURING", "COMPLETING"}


@dataclass
class DiagnosticResult:
    status: str
    diagnostics: List[str] = field(default_factory=list)
    electronic_steps: int = 0
    ionic_steps: int = 0
    can_resubmit: bool = False
    scheduler_state: str = ""


@dataclass
class RepairProposal:
    reason: str
    seed_mode: str
    incar_changes: Dict[str, str]


def check_job(
    folder: Path,
    scheduler_state: str = "",
    required_outputs: Sequence[str] = (),
) -> DiagnosticResult:
    folder = Path(folder)
    scheduler_state = scheduler_state.upper() if scheduler_state else "MISSING"
    outcar = folder / "OUTCAR"
    if not outcar.exists():
        if scheduler_state == "UNKNOWN":
            return DiagnosticResult(status="UNKNOWN", scheduler_state=scheduler_state, can_resubmit=False)
        if scheduler_state in ACTIVE_STATES:
            status = "RUNNING" if scheduler_state in {"R", "RUNNING", "COMPLETING"} else "QUEUED"
            return DiagnosticResult(status=status, scheduler_state=scheduler_state)
        return DiagnosticResult(
            status="NEEDS_REVIEW",
            diagnostics=["outcar_missing"],
            scheduler_state=scheduler_state,
            can_resubmit=True,
        )

    text = outcar.read_text(encoding="utf-8", errors="ignore")
    diagnostics = _error_diagnostics(text + "\n" + _job_log_text(folder))
    electronic_steps = max(
        [int(value) for value in re.findall(r"Iteration\s+\d+\(\s*(\d+)\)", text)] or [0]
    )
    ionic_steps = len(re.findall(r"POSITION\s+TOTAL-FORCE", text))
    nelm = _last_int(text, r"\bNELM\s*=\s*(\d+)")
    if nelm and electronic_steps >= nelm:
        diagnostics.append("electronic_nelm_reached")
    diagnostics.extend(_nelect_diagnostics(folder / "INCAR", text))

    converged = outcar_converged_from_text(text)
    if converged:
        for filename in required_outputs:
            if not _nonempty(folder / filename):
                diagnostics.append(f"missing_or_empty_{filename.lower()}")
        status = "CONVERGED" if not diagnostics else "NEEDS_REVIEW"
        return DiagnosticResult(
            status=status,
            diagnostics=_unique(diagnostics),
            electronic_steps=electronic_steps,
            ionic_steps=ionic_steps,
            can_resubmit=status == "NEEDS_REVIEW",
            scheduler_state=scheduler_state,
        )

    if diagnostics:
        status = "NEEDS_REVIEW"
    elif scheduler_state == "UNKNOWN":
        status = "UNKNOWN"
    elif scheduler_state in ACTIVE_STATES:
        status = "RUNNING" if scheduler_state in {"R", "RUNNING", "COMPLETING"} else "QUEUED"
    else:
        status = "NEEDS_REVIEW"
        diagnostics.append("calculation_incomplete")
    return DiagnosticResult(
        status=status,
        diagnostics=_unique(diagnostics),
        electronic_steps=electronic_steps,
        ionic_steps=ionic_steps,
        can_resubmit=status == "NEEDS_REVIEW" and scheduler_state != "UNKNOWN",
        scheduler_state=scheduler_state,
    )


def propose_repair(folder: Path, result: DiagnosticResult) -> RepairProposal:
    if result.status != "NEEDS_REVIEW":
        raise ValueError("repair proposals require NEEDS_REVIEW status")
    folder = Path(folder)
    chgcar_ok = _nonempty(folder / "CHGCAR") and "incomplete_chgcar" not in result.diagnostics
    if chgcar_ok:
        return RepairProposal(
            reason=", ".join(result.diagnostics) or "retry from neutral charge density",
            seed_mode="chgcar",
            incar_changes={"ISTART": "0", "ICHARG": "1", "LWAVE": ".FALSE.", "LCHARG": ".TRUE."},
        )
    return RepairProposal(
        reason=", ".join(result.diagnostics) or "retry from atomic charge density",
        seed_mode="fresh",
        incar_changes={"ISTART": "0", "ICHARG": "2", "LWAVE": ".FALSE.", "LCHARG": ".TRUE."},
    )


def apply_repair(folder: Path, proposal: RepairProposal, confirmed: bool = False) -> Path:
    if not confirmed:
        raise PermissionError("repair requires explicit confirmation")
    folder = Path(folder)
    archive = folder / ".vaspsolkit" / "archive" / datetime.now().strftime("restart-%Y%m%d-%H%M%S")
    archive.mkdir(parents=True, exist_ok=False)
    output_names = {
        "OUTCAR",
        "OSZICAR",
        "CONTCAR",
        "LOCPOT",
        "vasprun.xml",
        "WAVECAR",
        "CHG",
    }
    for path in folder.iterdir():
        if path.is_file() and (path.name in output_names or path.suffix in {".log", ".out"}):
            shutil.move(str(path), archive / path.name)
    chgcar = folder / "CHGCAR"
    if chgcar.exists():
        shutil.copy2(chgcar, archive / "CHGCAR")
        if proposal.seed_mode != "chgcar":
            chgcar.unlink()
    incar_path = folder / "INCAR"
    incar = incar_path.read_text(encoding="utf-8", errors="ignore")
    for key, value in proposal.incar_changes.items():
        incar = replace_or_append(incar, key, value)
    incar_path.write_text(incar, encoding="utf-8")
    return archive


def _error_diagnostics(text: str) -> List[str]:
    diagnostics = []
    for name, patterns in ERROR_PATTERNS.items():
        if any(re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) for pattern in patterns):
            diagnostics.append(name)
    return diagnostics


def _nelect_diagnostics(incar_path: Path, outcar_text: str) -> List[str]:
    if not incar_path.exists():
        return []
    incar_nelect = _last_float(incar_path.read_text(encoding="utf-8", errors="ignore"), r"\bNELECT\s*=\s*([-+0-9.Ee]+)")
    outcar_nelect = _last_float(outcar_text, r"NELECT\s*=\s*([-+0-9.Ee]+)")
    if incar_nelect is None or outcar_nelect is None:
        return []
    return ["nelect_mismatch"] if abs(incar_nelect - outcar_nelect) > 1.0e-3 else []


def _job_log_text(folder: Path) -> str:
    parts = []
    for pattern in ("*.log", "*.out", "*.o*"):
        for path in folder.glob(pattern):
            if path.is_file():
                parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


def _last_int(text: str, pattern: str) -> int:
    values = re.findall(pattern, text, flags=re.IGNORECASE)
    return int(values[-1]) if values else 0


def _last_float(text: str, pattern: str):
    values = re.findall(pattern, text, flags=re.IGNORECASE)
    try:
        return float(values[-1]) if values else None
    except ValueError:
        return None


def _nonempty(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def _unique(values: Sequence[str]) -> List[str]:
    return list(dict.fromkeys(values))
