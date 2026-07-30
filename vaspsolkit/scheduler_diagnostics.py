from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

from .config import SchedulerConfig


@dataclass(frozen=True)
class SchedulerCheck:
    code: str
    label: str
    status: str
    detail: str
    suggestion: str
    repair_action: str = ""


@dataclass(frozen=True)
class SubmitErrorInfo:
    title: str
    cause_code: str
    summary: str
    suggestion: str
    technical_detail: str
    repair_action: str = ""


def diagnose_pbs_script(workdir: Path, scheduler: SchedulerConfig) -> List[SchedulerCheck]:
    """Return read-only checks for the configured PBS submit script."""
    script = Path(workdir) / scheduler.script
    if not script.is_file():
        return [
            SchedulerCheck(
                "script-exists",
                "Submit script exists",
                "FAIL",
                str(script),
                "Add the configured submit script or update vaspsolkit.json.",
            )
        ]

    data = script.read_bytes()
    checks = [
        SchedulerCheck(
            "script-exists",
            "Submit script exists",
            "PASS",
            str(script),
            "No action needed.",
        )
    ]
    if b"\r\n" in data:
        checks.append(
            SchedulerCheck(
                "script-line-endings",
                "Unix line endings",
                "FAIL",
                f"{script} contains DOS/Windows CRLF line endings.",
                "Convert the script to Unix line endings before qsub.",
                "fix-line-endings",
            )
        )
    else:
        checks.append(
            SchedulerCheck(
                "script-line-endings",
                "Unix line endings",
                "PASS",
                f"{script} uses Unix-compatible line endings.",
                "No action needed.",
            )
        )

    text = data.decode("utf-8", errors="ignore")
    first_line = text.splitlines()[0].strip() if text.splitlines() else ""
    checks.append(
        SchedulerCheck(
            "script-shebang",
            "Shebang",
            "PASS" if first_line.startswith("#!") else "WARN",
            first_line or "empty first line",
            "Add '#!/bin/bash' if this server requires executable scripts.",
            "add-shebang" if not first_line.startswith("#!") else "",
        )
    )
    checks.append(
        SchedulerCheck(
            "script-executable",
            "Executable bit",
            "PASS" if os.access(script, os.X_OK) else "WARN",
            str(script),
            "qsub usually accepts readable scripts; add execute permission if this server requires it.",
            "chmod-executable" if not os.access(script, os.X_OK) else "",
        )
    )
    checks.extend(_pbs_resource_checks(text, scheduler))
    return checks


def classify_submit_error(exc: Exception) -> SubmitErrorInfo:
    detail = f"{type(exc).__name__}: {exc}"
    message = str(exc)
    if re.search(r"DOS/Windows text format|CRLF|line endings", message, re.IGNORECASE):
        return SubmitErrorInfo(
            "PBS submit failed",
            "dos-line-endings",
            "qsub reports that the submit script uses DOS/Windows line endings.",
            "Choose 'fix line endings and retry' or run dos2unix on the submit script.",
            detail,
            "fix-line-endings",
        )
    if re.search(r"permission denied", message, re.IGNORECASE):
        return SubmitErrorInfo(
            "PBS submit failed",
            "permission-denied",
            "The scheduler reported a permission error for the submit script or directory.",
            "Check script permissions and the case directory permissions before retrying.",
            detail,
            "chmod-executable",
        )
    return SubmitErrorInfo(
        "Submit failed",
        "unknown-submit-error",
        "The scheduler rejected the submit command.",
        "Review the scheduler page, script path, queue, node, cores, and raw error.",
        detail,
    )


def repair_submit_script(
    workdir: Path,
    scheduler: SchedulerConfig,
    action: str,
    confirmed: bool = False,
) -> Path:
    if not confirmed:
        raise PermissionError("script repair requires explicit confirmation")
    script = Path(workdir) / scheduler.script
    if not script.is_file():
        raise FileNotFoundError(f"submit script is missing: {script}")
    if action == "fix-line-endings":
        script.write_bytes(script.read_bytes().replace(b"\r\n", b"\n"))
        return script
    if action == "add-shebang":
        text = script.read_text(encoding="utf-8", errors="ignore")
        if not text.startswith("#!"):
            script.write_text("#!/bin/bash\n" + text, encoding="utf-8")
        return script
    if action == "chmod-executable":
        mode = script.stat().st_mode
        script.chmod(mode | 0o111)
        return script
    raise ValueError(f"unknown submit-script repair action: {action}")


def _pbs_resource_checks(text: str, scheduler: SchedulerConfig) -> List[SchedulerCheck]:
    checks: List[SchedulerCheck] = []
    if scheduler.queue and f"#PBS -q {scheduler.queue}" not in text:
        checks.append(
            SchedulerCheck(
                "pbs-queue-line",
                "PBS queue line",
                "WARN",
                f"Expected '#PBS -q {scheduler.queue}' when script resources are managed inline.",
                "Ensure the script or qsub arguments use the selected queue.",
                "sync-pbs-resources",
            )
        )
    node = scheduler.nodes[0] if scheduler.nodes else "1"
    expected_nodes = f"nodes={node}:ppn={scheduler.cores}" if scheduler.nodes else f"nodes=1:ppn={scheduler.cores}"
    if expected_nodes not in text:
        checks.append(
            SchedulerCheck(
                "pbs-resource-line",
                "PBS resource line",
                "WARN",
                f"Expected resource containing '{expected_nodes}'.",
                "Ensure qsub arguments or script resources match selected nodes and cores.",
                "sync-pbs-resources",
            )
        )
    if scheduler.walltime and scheduler.walltime not in text:
        checks.append(
            SchedulerCheck(
                "pbs-walltime-line",
                "PBS walltime line",
                "WARN",
                f"Expected walltime '{scheduler.walltime}'.",
                "Ensure qsub arguments or script resources use the selected walltime.",
                "sync-pbs-resources",
            )
        )
    return checks
