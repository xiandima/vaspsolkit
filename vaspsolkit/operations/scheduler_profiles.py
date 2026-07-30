"""Detect and safely override reusable PBS scheduler profiles."""
from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Literal, Optional


ResourceSyntax = Literal["nodes-ppn", "select-ncpus", "unmanaged"]

_QUEUE = re.compile(r"^\s*#PBS\s+-q\s+(\S+)\s*$", re.MULTILINE)
_WALLTIME = re.compile(r"^\s*#PBS\s+-l\s+walltime=(\S+)\s*$", re.MULTILINE)
_NODES_PPN = re.compile(
    r"^\s*#PBS\s+-l\s+nodes=(?P<node>[^:\s]+):ppn=(?P<cores>\d+)\s*$",
    re.MULTILINE,
)
_SELECT_NCPUS = re.compile(
    r"^\s*#PBS\s+-l\s+select=(?P<count>\d+):ncpus=(?P<cores>\d+)(?P<suffix>[^\n\r]*)$",
    re.MULTILINE,
)
_NODE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_WALLTIME_VALUE = re.compile(r"^\d+:\d{2}:\d{2}$")


@dataclass(frozen=True)
class SchedulerProfile:
    name: str
    kind: str
    resource_syntax: ResourceSyntax
    queue: str
    cores: int
    walltime: str
    script: str
    nodes: tuple[str, ...] = ()
    launch_command: str = ""
    submit_command: str = "qsub"
    status_command: str = "qstat"
    cancel_command: str = "qdel"

    def __post_init__(self) -> None:
        if self.kind != "pbs":
            raise ValueError("scheduler profile kind must be pbs")
        if self.resource_syntax not in {"nodes-ppn", "select-ncpus", "unmanaged"}:
            raise ValueError("unsupported PBS resource syntax")
        if self.cores < 0:
            raise ValueError("cores must not be negative")
        if self.resource_syntax != "unmanaged" and self.cores == 0:
            raise ValueError("managed scheduler profiles require positive cores")
        if any(_NODE_NAME.fullmatch(node) is None for node in self.nodes):
            raise ValueError("invalid PBS node name")
        if self.walltime and _WALLTIME_VALUE.fullmatch(self.walltime) is None:
            raise ValueError("walltime must use H+:MM:SS")


@dataclass(frozen=True)
class CaseResourceOverride:
    nodes: Optional[tuple[str, ...]] = None
    cores: Optional[int] = None
    queue: Optional[str] = None
    walltime: Optional[str] = None

    def __post_init__(self) -> None:
        if self.nodes is not None and any(
            _NODE_NAME.fullmatch(node) is None for node in self.nodes
        ):
            raise ValueError("invalid PBS node name")
        if self.cores is not None and self.cores <= 0:
            raise ValueError("override cores must be positive")
        if self.walltime is not None and _WALLTIME_VALUE.fullmatch(self.walltime) is None:
            raise ValueError("override walltime must use H+:MM:SS")


def detect_scheduler_profile(
    script_text: str,
    *,
    name: str = "detected-pbs",
    script_name: str = "vasp.pbs",
) -> SchedulerProfile:
    """Read supported PBS resources without inventing missing values."""
    if not isinstance(script_text, str):
        raise TypeError("script_text must be a string")
    nodes_match = _NODES_PPN.search(script_text)
    select_match = _SELECT_NCPUS.search(script_text)
    if nodes_match is not None:
        syntax: ResourceSyntax = "nodes-ppn"
        cores = int(nodes_match["cores"])
        raw_node = nodes_match["node"]
        nodes = () if raw_node.isdigit() else (raw_node,)
    elif select_match is not None:
        syntax = "select-ncpus"
        cores = int(select_match["cores"])
        nodes = ()
    else:
        syntax = "unmanaged"
        cores = 0
        nodes = ()
    queue_match = _QUEUE.search(script_text)
    walltime_match = _WALLTIME.search(script_text)
    return SchedulerProfile(
        name=name,
        kind="pbs",
        resource_syntax=syntax,
        queue=queue_match.group(1) if queue_match else "",
        cores=cores,
        walltime=walltime_match.group(1) if walltime_match else "",
        script=script_name,
        nodes=nodes,
        launch_command=_last_shell_command(script_text),
    )


def apply_case_override(
    profile: SchedulerProfile,
    override: CaseResourceOverride,
) -> SchedulerProfile:
    """Return an immutable resolved profile; never mutate the user default."""
    if not isinstance(profile, SchedulerProfile):
        raise TypeError("profile must be SchedulerProfile")
    if not isinstance(override, CaseResourceOverride):
        raise TypeError("override must be CaseResourceOverride")
    return replace(
        profile,
        nodes=profile.nodes if override.nodes is None else override.nodes,
        cores=profile.cores if override.cores is None else override.cores,
        queue=profile.queue if override.queue is None else override.queue.strip(),
        walltime=(
            profile.walltime
            if override.walltime is None
            else override.walltime.strip()
        ),
    )


def rewrite_pbs_resources(script_text: str, profile: SchedulerProfile) -> str:
    """Rewrite only recognized PBS resource directives and preserve launch lines."""
    if profile.resource_syntax == "unmanaged":
        raise ValueError("unmanaged PBS resources require manual confirmation")
    newline = "\r\n" if "\r\n" in script_text else "\n"
    text = script_text.replace("\r\n", "\n")
    if profile.resource_syntax == "nodes-ppn":
        node = profile.nodes[0] if profile.nodes else "1"
        resource = f"#PBS -l nodes={node}:ppn={profile.cores}"
        text, count = _NODES_PPN.subn(resource, text, count=1)
    else:
        if profile.nodes:
            raise ValueError(
                "explicit nodes for select/ncpus require a server-specific vnode rule"
            )
        existing = _SELECT_NCPUS.search(text)
        if existing is None:
            count = 0
        else:
            resource = (
                f"#PBS -l select={existing['count']}:ncpus={profile.cores}"
                f"{existing['suffix']}"
            )
            text, count = _SELECT_NCPUS.subn(resource, text, count=1)
    if count != 1:
        raise ValueError("recognized PBS resource directive changed before preview")

    text = _replace_or_insert_directive(
        text,
        _QUEUE,
        f"#PBS -q {profile.queue}" if profile.queue else "",
    )
    text = _replace_or_insert_directive(
        text,
        _WALLTIME,
        f"#PBS -l walltime={profile.walltime}" if profile.walltime else "",
    )
    if newline == "\r\n":
        text = text.replace("\n", "\r\n")
    return text


def _replace_or_insert_directive(text: str, pattern: re.Pattern, line: str) -> str:
    match = pattern.search(text)
    if match is not None:
        if line:
            return text[: match.start()] + line + text[match.end() :]
        return text[: match.start()] + text[match.end() :]
    if not line:
        return text
    lines = text.splitlines(keepends=True)
    insert_at = 1 if lines and lines[0].startswith("#!") else 0
    lines.insert(insert_at, line + "\n")
    return "".join(lines)


def _last_shell_command(script_text: str) -> str:
    commands = []
    for raw in script_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        commands.append(line)
    return commands[-1] if commands else ""

