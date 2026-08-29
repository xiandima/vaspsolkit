from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .config import SchedulerConfig


Runner = Callable[[Sequence[str], Optional[Path]], subprocess.CompletedProcess]


@dataclass
class JobState:
    job_id: str
    exists: bool
    state: str
    raw: str = ""
    exit_code: str = ""


@dataclass(frozen=True)
class PBSNodeInfo:
    name: str
    state: str
    total_cores: int
    used_cores: int
    free_cores: int


class PBSScheduler:
    def __init__(self, runner: Optional[Runner] = None):
        self._uses_default_runner = runner is None
        self.runner = runner or self._default_runner

    def submit(
        self,
        workdir: Path,
        pbs_file: str,
        dry_run: bool = False,
        job_name: Optional[str] = None,
        queue: Optional[str] = None,
        node: Optional[str] = None,
        ppn: Optional[int] = None,
        walltime: Optional[str] = None,
    ) -> str:
        if dry_run:
            return f"DRY-RUN:{Path(workdir).name}"
        command = ["qsub"]
        if queue:
            command.extend(["-q", queue])
        if job_name:
            command.extend(["-N", job_name])
        resource = pbs_resource(node=node, ppn=ppn, walltime=walltime)
        if resource:
            command.extend(["-l", resource])
        command.append(pbs_file)
        result = self.runner(command, Path(workdir))
        if result.returncode != 0:
            raise RuntimeError(f"qsub failed in {workdir}: {result.stderr.strip()}")
        stdout = result.stdout.strip()
        if not stdout:
            raise RuntimeError(f"qsub in {workdir} returned an empty job id")
        return stdout.split()[0]

    def status(self, job_id: str) -> JobState:
        if job_id.startswith("DRY-RUN:"):
            return JobState(job_id=job_id, exists=False, state="DRY-RUN")
        result = (
            self._default_runner(["qstat", job_id], None, timeout=30)
            if self._uses_default_runner
            else self.runner(["qstat", job_id], None)
        )
        if result.returncode != 0:
            raw = result.stderr or result.stdout
            if re.search(r"unknown\s+job|unknown\s+job\s+id|job\s+not\s+found", raw, flags=re.IGNORECASE):
                return JobState(job_id=job_id, exists=False, state="MISSING", raw=raw)
            return JobState(job_id=job_id, exists=True, state="UNKNOWN", raw=raw)
        state = _parse_qstat_state(job_id, result.stdout)
        return JobState(job_id=job_id, exists=True, state=state, raw=result.stdout)

    def cancel(self, job_id: str) -> None:
        result = self.runner(["qdel", job_id], None)
        if result.returncode != 0:
            raise RuntimeError(f"qdel failed for {job_id}: {result.stderr.strip()}")

    def inspect(self) -> List["QueueEntry"]:
        result = self.runner(["qstat"], None)
        if result.returncode != 0:
            return [QueueEntry(job_id="", state="UNKNOWN", name="", raw=result.stderr)]
        entries = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0][0:1].isdigit():
                entries.append(QueueEntry(job_id=parts[0], name=parts[1], state=parts[4], raw=line))
        return entries

    def inspect_nodes(self, min_node: int = 17, ppn: int = 48) -> List[PBSNodeInfo]:
        result = self.runner(["pbsnodes", "-a"], None)
        if result.returncode != 0:
            raise RuntimeError(f"pbsnodes failed: {result.stderr.strip()}")
        return parse_pbs_nodes(result.stdout, min_node=min_node, ppn=ppn)

    def available_nodes(
        self,
        count: int,
        min_node: int = 17,
        ppn: int = 48,
        selected_nodes: Optional[Sequence[str]] = None,
    ) -> List[str]:
        result = self.runner(["pbsnodes", "-a"], None)
        if result.returncode != 0:
            raise RuntimeError(f"pbsnodes failed: {result.stderr.strip()}")
        return select_pbs_nodes(
            result.stdout,
            count=count,
            min_node=min_node,
            ppn=ppn,
            selected_nodes=selected_nodes,
        )

    @staticmethod
    def _default_runner(
        args: Sequence[str], cwd: Optional[Path], timeout: Optional[float] = None
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            args, cwd=cwd, text=True, capture_output=True, timeout=timeout
        )


def pbs_resource(
    node: Optional[str] = None,
    ppn: Optional[int] = None,
    walltime: Optional[str] = None,
) -> str:
    resources = []
    if ppn is not None:
        node_request = f"nodes={node}:ppn={ppn}" if node else f"nodes=1:ppn={ppn}"
        resources.append(node_request)
    elif node:
        resources.append(f"nodes={node}")
    if walltime:
        resources.append(f"walltime={walltime}")
    return ",".join(resources)


def node_number(node: str) -> Optional[int]:
    match = re.search(r"node(\d+)", node)
    return int(match.group(1)) if match else None


def select_pbs_nodes(
    stdout: str,
    count: int,
    min_node: int = 17,
    ppn: int = 48,
    selected_nodes: Optional[Sequence[str]] = None,
) -> List[str]:
    if count <= 0:
        return []
    slots = _pbs_node_slots(
        stdout,
        min_node=min_node,
        ppn=ppn,
        selected_nodes=selected_nodes,
    )
    if not slots:
        return []
    return slots[:count]


def _pbs_node_slots(
    stdout: str,
    min_node: int,
    ppn: int,
    selected_nodes: Optional[Sequence[str]] = None,
) -> List[str]:
    selected = {str(node).strip() for node in (selected_nodes or []) if str(node).strip()}
    candidates = []
    for block in re.split(r"\n\s*\n", stdout.strip()):
        lines = [line.rstrip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        node = lines[0].strip()
        number = node_number(node)
        if selected:
            if node not in selected:
                continue
        elif number is None or number < min_node:
            continue
        values = _pbs_block_values(lines[1:])
        state = values.get("state", "")
        if any(flag in state for flag in ("down", "offline", "unknown")):
            continue
        np_value = _parse_int(values.get("np", ""))
        used_cores = _used_cores(values.get("jobs", ""))
        if np_value is None:
            free_cores = ppn if not used_cores else 0
        else:
            free_cores = max(np_value - len(used_cores), 0)
        slot_count = free_cores // ppn
        if slot_count <= 0:
            continue
        priority = 0 if not used_cores else 1
        candidates.append((priority, number, node, slot_count))
    slots = []
    for _, _, node, slot_count in sorted(candidates):
        slots.extend([node] * slot_count)
    return slots


def parse_pbs_nodes(stdout: str, min_node: int = 17, ppn: int = 48) -> List[PBSNodeInfo]:
    """Parse pbsnodes output for interactive resource selection."""
    nodes = []
    for block in re.split(r"\n\s*\n", stdout.strip()):
        lines = [line.rstrip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        name = lines[0].strip()
        number = node_number(name)
        if number is not None and number < min_node:
            continue
        values = _pbs_block_values(lines[1:])
        state = values.get("state", "unknown").strip()
        total = _parse_int(values.get("np", "")) or ppn
        used = len(_used_cores(values.get("jobs", "")))
        nodes.append(
            PBSNodeInfo(
                name=name,
                state=state,
                total_cores=total,
                used_cores=used,
                free_cores=max(total - used, 0),
            )
        )
    return nodes


def _pbs_block_values(lines: Sequence[str]) -> dict:
    values = {}
    current_key = ""
    for line in lines:
        stripped = line.strip()
        if "=" in stripped:
            key, value = stripped.split("=", 1)
            current_key = key.strip()
            values[current_key] = value.strip()
        elif current_key:
            values[current_key] = values[current_key] + " " + stripped
    return values


def _parse_int(value: str) -> Optional[int]:
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _used_cores(jobs: str) -> set:
    used = set()
    for match in re.finditer(r"(\d+)(?:-(\d+))?/", jobs):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        used.update(range(start, end + 1))
    return used


def _parse_qstat_state(job_id: str, stdout: str) -> str:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    for line in lines:
        if line.startswith(job_id):
            parts = line.split()
            if len(parts) >= 5:
                return parts[4]
            if len(parts) >= 2:
                return parts[1]
    return "UNKNOWN"


def active_states(states: List[JobState]) -> List[JobState]:
    return [state for state in states if state.exists]


@dataclass(frozen=True)
class QueueEntry:
    job_id: str
    state: str
    name: str = ""
    raw: str = ""


def plan_submission_batches(
    job_names: Sequence[str],
    max_inflight: Optional[int],
    active: int = 0,
    capacity: Optional[int] = None,
) -> List[List[str]]:
    if max_inflight is None:
        return [list(job_names)] if job_names else []
    if max_inflight <= 0:
        raise ValueError("max_inflight must be positive")
    remaining = list(job_names)
    if not remaining:
        return []
    first_capacity = max(max_inflight - max(active, 0), 0)
    if capacity is not None:
        first_capacity = min(first_capacity, max(capacity, 0))
    batches: List[List[str]] = []
    if first_capacity:
        batches.append(remaining[:first_capacity])
        remaining = remaining[first_capacity:]
    else:
        batches.append([])
    while remaining:
        batches.append(remaining[:max_inflight])
        remaining = remaining[max_inflight:]
    return batches


class SlurmScheduler:
    def __init__(self, runner: Optional[Runner] = None):
        self.runner = runner or PBSScheduler._default_runner

    def submit(
        self, workdir: Path, script: str, dry_run: bool = False,
        partition: Optional[str] = None, nodes: Sequence[str] = (),
        node_count: Optional[int] = None, tasks: Optional[int] = None,
        tasks_per_node: Optional[int] = None, walltime: Optional[str] = None,
        **_: object,
    ) -> str:
        if dry_run:
            return f"DRY-RUN:{Path(workdir).name}"
        command = ["sbatch", "--parsable"]
        for option, value in (
            ("--partition", partition), ("--nodes", node_count),
            ("--ntasks", tasks), ("--ntasks-per-node", tasks_per_node),
            ("--time", walltime),
        ):
            if value not in (None, ""):
                command.extend([option, str(value)])
        if nodes:
            command.extend(["--nodelist", ",".join(nodes)])
        command.append(script)
        result = self.runner(command, Path(workdir))
        if result.returncode != 0:
            raise RuntimeError(f"sbatch failed in {workdir}: {result.stderr.strip()}")
        job_id = result.stdout.strip().split(";", 1)[0]
        if not job_id:
            raise RuntimeError(f"sbatch in {workdir} returned an empty job id")
        return job_id

    def status(self, job_id: str) -> JobState:
        if job_id.startswith("DRY-RUN:"):
            return JobState(job_id=job_id, exists=False, state="DRY-RUN")
        result = self.runner(["squeue", "-h", "-j", job_id, "-o", "%T"], None)
        if result.returncode != 0:
            return JobState(job_id=job_id, exists=True, state="UNKNOWN", raw=result.stderr)
        state = result.stdout.strip().splitlines()
        if not state:
            history = self.runner(
                ["sacct", "-X", "-n", "-P", "-j", job_id,
                 "--format=JobIDRaw,State,ExitCode"], None,
            )
            if history.returncode != 0:
                return JobState(job_id, True, "UNKNOWN", history.stderr or history.stdout)
            for line in history.stdout.splitlines():
                parts = line.split("|", 2)
                if len(parts) == 3 and parts[0].strip() == job_id:
                    canonical = parts[1].strip().upper().split()[0].rstrip("+")
                    return JobState(job_id, True, canonical, history.stdout, parts[2].strip())
            return JobState(job_id=job_id, exists=False, state="MISSING", raw=history.stdout)
        return JobState(job_id=job_id, exists=True, state=state[0].strip().upper(), raw=result.stdout)

    def inspect(self) -> List[QueueEntry]:
        result = self.runner(["squeue", "-h", "-o", "%i|%T|%j"], None)
        if result.returncode != 0:
            return [QueueEntry(job_id="", state="UNKNOWN", raw=result.stderr)]
        entries = []
        for line in result.stdout.splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:
                entries.append(QueueEntry(job_id=parts[0], state=parts[1].upper(), name=parts[2], raw=line))
        return entries

    def cancel(self, job_id: str) -> None:
        result = self.runner(["scancel", job_id], None)
        if result.returncode != 0:
            raise RuntimeError(f"scancel failed for {job_id}: {result.stderr.strip()}")

    def inspect_partitions(self) -> List[str]:
        result = self.runner(["sinfo", "-h", "-o", "%P"], None)
        if result.returncode != 0:
            raise RuntimeError(f"sinfo failed: {result.stderr.strip()}")
        return sorted({line.strip().rstrip("*") for line in result.stdout.splitlines() if line.strip()})

    def inspect_nodes(self, partition: str) -> List["SlurmNodeInfo"]:
        result = self.runner(
            ["sinfo", "-N", "-h", "-p", partition, "-o", "%N|%P|%T|%c|%C"], None
        )
        if result.returncode != 0:
            raise RuntimeError(f"sinfo failed for partition {partition}: {result.stderr.strip()}")
        return parse_slurm_nodes(result.stdout, partition)


@dataclass(frozen=True)
class SlurmNodeInfo:
    name: str
    partition: str
    state: str
    total_cores: int
    allocated_cores: int
    idle_cores: int
    other_cores: int


def parse_slurm_nodes(stdout: str, partition: str) -> List[SlurmNodeInfo]:
    found = {}
    for line in stdout.splitlines():
        parts = line.split("|", 4)
        if len(parts) != 5:
            continue
        name, raw_partition, state, total, usage = (part.strip() for part in parts)
        clean_partition = raw_partition.rstrip("*")
        if clean_partition != partition:
            continue
        try:
            allocated, idle, other, _ = (int(value) for value in usage.split("/"))
            total_cores = int(total)
        except (ValueError, TypeError):
            continue
        found[name] = SlurmNodeInfo(
            name, clean_partition, state.lower().rstrip("*~#"), total_cores,
            allocated, idle, other,
        )
    return [found[name] for name in sorted(found)]


class CustomScheduler:
    def __init__(self, config: SchedulerConfig, runner: Optional[Runner] = None):
        config.validate()
        self.config = config
        self.runner = runner or PBSScheduler._default_runner

    def submit(self, workdir: Path, script: str, dry_run: bool = False, **_: object) -> str:
        if dry_run:
            return f"DRY-RUN:{Path(workdir).name}"
        command = _format_command(self.config.submit_command, script=script, job_id="")
        result = self.runner(command, Path(workdir))
        if result.returncode != 0:
            raise RuntimeError(f"custom submit failed in {workdir}: {result.stderr.strip()}")
        match = re.search(self.config.job_id_pattern, result.stdout)
        if match is None:
            raise RuntimeError("custom submit output did not contain a job id")
        return match.groupdict().get("job_id") or match.group(0)

    def status(self, job_id: str) -> JobState:
        command = _format_command(self.config.status_command, script=self.config.script, job_id=job_id)
        if not command:
            return JobState(job_id=job_id, exists=True, state="UNKNOWN", raw="status command not configured")
        result = self.runner(command, None)
        if result.returncode != 0:
            return JobState(job_id=job_id, exists=True, state="UNKNOWN", raw=result.stderr)
        state = result.stdout.strip().splitlines()
        return JobState(
            job_id=job_id,
            exists=True,
            state=state[0].strip().upper() if state else "UNKNOWN",
            raw=result.stdout,
        )

    def inspect(self) -> List[QueueEntry]:
        if not self.config.inspect_command:
            return []
        result = self.runner(_format_command(self.config.inspect_command, script=self.config.script, job_id=""), None)
        if result.returncode != 0:
            return [QueueEntry(job_id="", state="UNKNOWN", raw=result.stderr)]
        return [QueueEntry(job_id="", state="CUSTOM", raw=result.stdout)]

    def cancel(self, job_id: str) -> None:
        command = _format_command(self.config.cancel_command, script=self.config.script, job_id=job_id)
        if not command:
            raise RuntimeError("custom cancel command is not configured")
        result = self.runner(command, None)
        if result.returncode != 0:
            raise RuntimeError(f"custom cancel failed for {job_id}: {result.stderr.strip()}")


def scheduler_from_config(config: SchedulerConfig):
    if config.kind == "pbs":
        return PBSScheduler()
    if config.kind == "slurm":
        return SlurmScheduler()
    return CustomScheduler(config)


def _format_command(command: Sequence[str], script: str, job_id: str) -> List[str]:
    return [part.format(script=script, job_id=job_id) for part in command]
