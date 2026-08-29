from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .reference_settings import validate_she_reference


DEFAULT_FOLDERS = ["1", "2", "3", "4", "5"]
DEFAULT_OFFSETS = [-1.0, -0.5, 0.0, 0.5, 1.0]
DEFAULT_COPY_FILES = ["INCAR", "POTCAR", "KPOINTS", "CHGCAR"]


@dataclass
class WorkflowConfig:
    poll_interval: int = 60
    job_root: str = "charge_sweep"
    results_root: str = "results"
    folders: List[str] = field(default_factory=lambda: list(DEFAULT_FOLDERS))
    nelect_offsets: List[float] = field(default_factory=lambda: list(DEFAULT_OFFSETS))
    copy_files: List[str] = field(default_factory=lambda: list(DEFAULT_COPY_FILES))
    vacuum_level_reference: str = "neutral"
    she_reference: float = 4.70
    she_reference_source: str = ""
    she_reference_confirmed: bool = False
    interface_count: int = 1
    target_potentials: List[float] = field(default_factory=list)
    nelect_ref: Optional[float] = None
    job_state_file: str = ".vaspsolkit_jobs.json"
    summary_file: str = "summary.csv"
    analysis_file: str = "analysis.json"
    neutral_profile: str = "vaspsol-neutral-relax"
    charge_profile: str = "vaspsol-charge-relax"
    charge_points_include_neutral: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowConfig":
        allowed = {field_name for field_name in cls.__dataclass_fields__}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ValueError(f"unknown config field(s): {', '.join(unknown)}")
        kwargs = {key: value for key, value in data.items() if key in allowed}
        if "folders" in kwargs:
            kwargs["folders"] = [str(item) for item in kwargs["folders"]]
        if "nelect_offsets" in kwargs:
            kwargs["nelect_offsets"] = [float(item) for item in kwargs["nelect_offsets"]]
        if "copy_files" in kwargs:
            kwargs["copy_files"] = [str(item) for item in kwargs["copy_files"]]
        if "target_potentials" in kwargs:
            kwargs["target_potentials"] = [float(item) for item in kwargs["target_potentials"]]
        if "nelect_ref" in kwargs and kwargs["nelect_ref"] is not None:
            kwargs["nelect_ref"] = float(kwargs["nelect_ref"])
        if "she_reference" in kwargs:
            kwargs["she_reference"] = float(kwargs["she_reference"])
        if "she_reference_source" in kwargs:
            kwargs["she_reference_source"] = str(kwargs["she_reference_source"])
        if "she_reference_confirmed" in kwargs:
            kwargs["she_reference_confirmed"] = bool(kwargs["she_reference_confirmed"])
        if "vacuum_level_reference" in kwargs:
            kwargs["vacuum_level_reference"] = str(kwargs["vacuum_level_reference"])
        if "neutral_profile" in kwargs:
            kwargs["neutral_profile"] = str(kwargs["neutral_profile"])
        if "charge_profile" in kwargs:
            kwargs["charge_profile"] = str(kwargs["charge_profile"])
        if "charge_points_include_neutral" in kwargs:
            kwargs["charge_points_include_neutral"] = bool(kwargs["charge_points_include_neutral"])
        return cls(**kwargs)

    @classmethod
    def load(cls, path: Optional[Path]) -> "WorkflowConfig":
        if path is None:
            return cls()
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"config file is missing: {path}")
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def validate(self, require_neutral: bool = False) -> None:
        validate_she_reference(self.she_reference)
        if self.interface_count <= 0:
            raise ValueError("interface_count must be positive")
        if self.vacuum_level_reference != "neutral":
            raise ValueError("vacuum_level_reference must be 'neutral'")
        if len(self.folders) != len(self.nelect_offsets):
            raise ValueError("folders and nelect_offsets must have the same length")
        if require_neutral and sum(abs(value) <= 1.0e-12 for value in self.nelect_offsets) != 1:
            raise ValueError("charge sweep must contain exactly one neutral point")


@dataclass
class SchedulerConfig:
    kind: str = "slurm"
    partition: str = "compute"
    nodes: List[str] = field(default_factory=list)
    node_count: int = 1
    tasks: int = 96
    tasks_per_node: int = 96
    memory: str = ""
    walltime: str = "72:00:00"
    max_inflight: Optional[int] = None
    script: str = "vasp.slurm"
    launcher: str = "mpirun"
    executable: str = "vasp_std"
    module_init: str = ""
    modules: List[str] = field(default_factory=list)
    submit_command: List[str] = field(default_factory=list)
    inspect_command: List[str] = field(default_factory=list)
    status_command: List[str] = field(default_factory=list)
    cancel_command: List[str] = field(default_factory=list)
    job_id_pattern: str = r"(?P<job_id>\S+)"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SchedulerConfig":
        allowed = {field_name for field_name in cls.__dataclass_fields__}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ValueError(f"unknown scheduler config field(s): {', '.join(unknown)}")
        values = dict(data)
        for key in ("submit_command", "inspect_command", "status_command", "cancel_command"):
            if key in values:
                values[key] = [str(item) for item in values[key]]
        if "modules" in values:
            values["modules"] = [str(item) for item in values["modules"]]
        for key in ("node_count", "tasks", "tasks_per_node"):
            if key in values:
                values[key] = int(values[key])
        if "max_inflight" in values and values["max_inflight"] is not None:
            values["max_inflight"] = int(values["max_inflight"])
        if "nodes" in values:
            values["nodes"] = [str(item).strip() for item in values["nodes"]]
        return cls(**values)

    def validate(self) -> None:
        if self.kind not in {"slurm", "custom"}:
            raise ValueError("scheduler kind must be slurm or custom")
        if self.node_count <= 0:
            raise ValueError("scheduler node_count must be positive")
        if self.tasks <= 0:
            raise ValueError("scheduler tasks must be positive")
        if self.tasks_per_node <= 0:
            raise ValueError("scheduler tasks_per_node must be positive")
        if self.tasks > self.node_count * self.tasks_per_node:
            raise ValueError("scheduler tasks exceed node capacity")
        if self.max_inflight is not None and self.max_inflight <= 0:
            raise ValueError("scheduler max_inflight must be positive when configured")
        normalized_nodes = [node.strip() for node in self.nodes]
        if any(not node for node in normalized_nodes):
            raise ValueError("scheduler nodes must be non-empty")
        if len(set(normalized_nodes)) != len(normalized_nodes):
            raise ValueError("scheduler nodes must not contain duplicates")
        if self.nodes and len(self.nodes) != self.node_count:
            raise ValueError("scheduler explicit nodes must match node_count")
        if self.kind == "custom" and not self.submit_command:
            raise ValueError("custom scheduler requires submit_command")


@dataclass
class KitConfig:
    config_version: int = 2
    profile: str = "vaspsol-sweep"
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KitConfig":
        unknown = sorted(set(data) - {"config_version", "profile", "workflow", "scheduler"})
        if unknown:
            raise ValueError(f"unknown kit config field(s): {', '.join(unknown)}")
        return cls(
            config_version=int(data.get("config_version", 2)),
            profile=str(data.get("profile", "vaspsol-sweep")),
            workflow=WorkflowConfig.from_dict(dict(data.get("workflow", {}))),
            scheduler=SchedulerConfig.from_dict(dict(data.get("scheduler", {}))),
        )

    def validate(self) -> None:
        if self.config_version != 2:
            raise ValueError(f"unsupported config_version: {self.config_version}")
        self.workflow.validate(require_neutral=self.profile == "vaspsol-sweep")
        self.scheduler.validate()

    def to_dict(self) -> Dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


_PBS_WORKFLOW_FIELDS = {"pbs_file", "qsub_queue", "qsub_ppn", "qsub_min_node", "qsub_walltime"}
_V1_SCHEDULER_FIELDS = {
    "kind",
    "queue",
    "cores",
    "partition",
    "nodes",
    "node_count",
    "tasks",
    "tasks_per_node",
    "memory",
    "walltime",
    "max_inflight",
    "script",
    "launcher",
    "executable",
    "module_init",
    "modules",
    "submit_command",
    "inspect_command",
    "status_command",
    "cancel_command",
    "job_id_pattern",
}


def migrate_config_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return a validated config-v2 dictionary without changing ``data``."""
    if not isinstance(data, dict):
        raise ValueError("configuration must be a JSON object")
    source = deepcopy(data)
    try:
        version = int(source.get("config_version", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("config_version must be an integer") from exc

    if version == 2:
        config = KitConfig.from_dict(source)
        config.validate()
        return config.to_dict()
    if version != 1:
        raise ValueError(f"unsupported config_version: {version}")
    if "workflow" not in source or "scheduler" not in source:
        raise ValueError("legacy PBS configuration cannot be migrated; select a SLURM profile")

    unknown = sorted(set(source) - {"config_version", "profile", "workflow", "scheduler"})
    if unknown:
        raise ValueError(f"unknown kit config field(s): {', '.join(unknown)}")
    workflow_v1 = dict(source.get("workflow") or {})
    scheduler_v1 = dict(source.get("scheduler") or {})
    kind = str(scheduler_v1.get("kind", "pbs"))
    if kind == "pbs":
        raise ValueError("PBS configuration cannot be migrated; select a SLURM profile")

    unknown_scheduler = sorted(set(scheduler_v1) - _V1_SCHEDULER_FIELDS)
    if unknown_scheduler:
        raise ValueError(f"unknown scheduler config field(s): {', '.join(unknown_scheduler)}")

    workflow = {key: value for key, value in workflow_v1.items() if key not in _PBS_WORKFLOW_FIELDS}
    nodes = list(scheduler_v1.get("nodes", []))
    cores = scheduler_v1.get("cores", workflow_v1.get("qsub_ppn", 96))
    scheduler = {
        key: deepcopy(value)
        for key, value in scheduler_v1.items()
        if key in SchedulerConfig.__dataclass_fields__
    }
    scheduler.update(
        {
            "kind": kind,
            "partition": scheduler_v1.get("partition", scheduler_v1.get("queue", "compute")),
            "nodes": nodes,
            "node_count": scheduler_v1.get("node_count", len(nodes) if nodes else 1),
            "tasks": scheduler_v1.get("tasks", cores),
            "tasks_per_node": scheduler_v1.get("tasks_per_node", cores),
        }
    )
    if "walltime" not in scheduler_v1 and "qsub_walltime" in workflow_v1:
        scheduler["walltime"] = workflow_v1["qsub_walltime"]

    migrated = {
        "config_version": 2,
        "profile": str(source.get("profile", "vaspsol-sweep")),
        "workflow": workflow,
        "scheduler": scheduler,
    }
    config = KitConfig.from_dict(migrated)
    config.validate()
    return config.to_dict()


def load_config(path: Optional[str]) -> WorkflowConfig:
    config = WorkflowConfig.load(Path(path) if path else None)
    config.validate()
    return config


def load_kit_config(path: Optional[Path]) -> KitConfig:
    if path is None:
        config = KitConfig()
    else:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"config file is missing: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        config = KitConfig.from_dict(migrate_config_data(data))
    config.validate()
    return config


def write_kit_config(path: Path, config: KitConfig) -> None:
    config.validate()
    target = Path(path)
    data = json.dumps(config.to_dict(), indent=2, sort_keys=True).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
