from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Callable, List, Optional, Sequence

from .config import SchedulerConfig


Runner = Callable[[Sequence[str], Optional[Path]], subprocess.CompletedProcess]


def _default_runner(
    args: Sequence[str], cwd: Optional[Path], timeout: Optional[float] = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=cwd, text=True, capture_output=True, timeout=timeout
    )


@dataclass
class JobState:
    job_id: str
    exists: bool
    state: str
    raw: str = ""
    exit_code: str = ""


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
        self._uses_default_runner = runner is None
        self.runner = runner or _default_runner

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
        if not result.stdout.strip():
            raise RuntimeError(f"sbatch in {workdir} returned an empty job id")
        job_ids = []
        for line in result.stdout.splitlines():
            match = re.fullmatch(
                r"(?:Submitted batch job )?([0-9]+)(?:;[^;\s]+)?", line.strip()
            )
            if match is not None:
                job_ids.append(match.group(1))
        if len(job_ids) != 1:
            raise RuntimeError(
                f"sbatch in {workdir} did not return an unambiguous job id: {result.stdout!r}"
            )
        return job_ids[0]

    def status(self, job_id: str) -> JobState:
        if job_id.startswith("DRY-RUN:"):
            return JobState(job_id=job_id, exists=False, state="DRY-RUN")
        try:
            result = (
                _default_runner(["squeue", "-h", "-j", job_id, "-o", "%T"], None, timeout=30)
                if self._uses_default_runner
                else self.runner(["squeue", "-h", "-j", job_id, "-o", "%T"], None)
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return JobState(job_id, True, "UNKNOWN", str(exc))
        if result.returncode != 0:
            return JobState(job_id=job_id, exists=True, state="UNKNOWN", raw=result.stderr)
        state = result.stdout.strip().splitlines()
        if not state:
            command = ["sacct", "-X", "-n", "-P", "-j", job_id,
                       "--format=JobIDRaw,State,ExitCode"]
            try:
                history = (
                    _default_runner(command, None, timeout=30)
                    if self._uses_default_runner
                    else self.runner(command, None)
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return JobState(job_id, True, "UNKNOWN", str(exc))
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
        for name in ("submit_command", "status_command", "inspect_command", "cancel_command"):
            _validate_command_template(getattr(config, name), name)
        try:
            self._job_id_regex = re.compile(config.job_id_pattern)
        except re.error as exc:
            raise ValueError(f"scheduler.job_id_pattern is invalid: {exc}") from exc
        self.config = config
        self._uses_default_runner = runner is None
        self.runner = runner or _default_runner

    def submit(self, workdir: Path, script: str, dry_run: bool = False, **_: object) -> str:
        if dry_run:
            return f"DRY-RUN:{Path(workdir).name}"
        command = _format_command(self.config.submit_command, script=script, job_id="")
        result = self.runner(command, Path(workdir))
        if result.returncode != 0:
            raise RuntimeError(f"custom submit failed in {workdir}: {result.stderr.strip()}")
        match = self._job_id_regex.search(result.stdout)
        if match is None:
            raise RuntimeError("custom submit output did not contain a job id")
        return match.groupdict().get("job_id") or match.group(0)

    def status(self, job_id: str) -> JobState:
        command = _format_command(self.config.status_command, script=self.config.script, job_id=job_id)
        if not command:
            return JobState(job_id=job_id, exists=True, state="UNKNOWN", raw="status command not configured")
        try:
            result = (
                _default_runner(command, None, timeout=30)
                if self._uses_default_runner
                else self.runner(command, None)
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return JobState(job_id, True, "UNKNOWN", str(exc))
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
    if config.kind == "slurm":
        return SlurmScheduler()
    return CustomScheduler(config)


def _format_command(command: Sequence[str], script: str, job_id: str) -> List[str]:
    return [part.format(script=script, job_id=job_id) for part in command]


def _validate_command_template(command: Sequence[str], name: str) -> None:
    formatter = Formatter()
    for index, part in enumerate(command):
        try:
            for _literal, field, spec, conversion in formatter.parse(part):
                if field is None:
                    continue
                if field not in {"script", "job_id"}:
                    raise ValueError(f"unknown placeholder {{{field}}}")
                if spec or conversion:
                    raise ValueError("placeholder formatting and conversions are not supported")
        except ValueError as exc:
            raise ValueError(
                f"scheduler.{name}[{index}] is invalid: {exc}; "
                "use {script}, {job_id}, or doubled braces for literal text"
            ) from exc
