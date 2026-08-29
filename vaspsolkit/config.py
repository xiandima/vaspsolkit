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
_NO_EXPECTED_BYTES = object()


def _require_object(value: Any, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value


def _require_list(value: Any, path: str) -> List[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list")
    return value


def _require_string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    return value


def _require_bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{path} must be a boolean")
    return value


def _require_int(value: Any, path: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{path} must be an integer")
    return value


def _require_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a number")
    return float(value)


def _string_list(value: Any, path: str, *, strip: bool = False) -> List[str]:
    result = []
    for index, item in enumerate(_require_list(value, path)):
        parsed = _require_string(item, f"{path}[{index}]")
        result.append(parsed.strip() if strip else parsed)
    return result


def _number_list(value: Any, path: str) -> List[float]:
    return [
        _require_number(item, f"{path}[{index}]")
        for index, item in enumerate(_require_list(value, path))
    ]


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
        data = _require_object(data, "workflow")
        allowed = {field_name for field_name in cls.__dataclass_fields__}
        unknown = sorted(set(data) - allowed)
        if unknown:
            paths = ", ".join(f"workflow.{name}" for name in unknown)
            raise ValueError(f"unknown workflow config field(s): {paths}")
        kwargs = {key: value for key, value in data.items() if key in allowed}
        if "folders" in kwargs:
            kwargs["folders"] = _string_list(kwargs["folders"], "workflow.folders")
        if "nelect_offsets" in kwargs:
            kwargs["nelect_offsets"] = _number_list(
                kwargs["nelect_offsets"], "workflow.nelect_offsets"
            )
        if "copy_files" in kwargs:
            kwargs["copy_files"] = _string_list(kwargs["copy_files"], "workflow.copy_files")
        if "target_potentials" in kwargs:
            kwargs["target_potentials"] = _number_list(
                kwargs["target_potentials"], "workflow.target_potentials"
            )
        if "nelect_ref" in kwargs and kwargs["nelect_ref"] is not None:
            kwargs["nelect_ref"] = _require_number(kwargs["nelect_ref"], "workflow.nelect_ref")
        if "she_reference" in kwargs:
            kwargs["she_reference"] = _require_number(
                kwargs["she_reference"], "workflow.she_reference"
            )
        if "she_reference_confirmed" in kwargs:
            kwargs["she_reference_confirmed"] = _require_bool(
                kwargs["she_reference_confirmed"], "workflow.she_reference_confirmed"
            )
        if "charge_points_include_neutral" in kwargs:
            kwargs["charge_points_include_neutral"] = _require_bool(
                kwargs["charge_points_include_neutral"],
                "workflow.charge_points_include_neutral",
            )
        for key in ("poll_interval", "interface_count"):
            if key in kwargs:
                kwargs[key] = _require_int(kwargs[key], f"workflow.{key}")
        for key in (
            "job_root",
            "results_root",
            "vacuum_level_reference",
            "she_reference_source",
            "job_state_file",
            "summary_file",
            "analysis_file",
            "neutral_profile",
            "charge_profile",
        ):
            if key in kwargs:
                kwargs[key] = _require_string(kwargs[key], f"workflow.{key}")
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
        try:
            validate_she_reference(self.she_reference)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"workflow.she_reference is invalid: {exc}") from exc
        if self.interface_count <= 0:
            raise ValueError("workflow.interface_count must be positive")
        if self.vacuum_level_reference != "neutral":
            raise ValueError("workflow.vacuum_level_reference must be 'neutral'")
        if len(self.folders) != len(self.nelect_offsets):
            raise ValueError(
                "workflow.folders and workflow.nelect_offsets must have the same length"
            )
        if require_neutral and sum(abs(value) <= 1.0e-12 for value in self.nelect_offsets) != 1:
            raise ValueError("workflow.nelect_offsets must contain exactly one neutral point")


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
        data = _require_object(data, "scheduler")
        allowed = {field_name for field_name in cls.__dataclass_fields__}
        unknown = sorted(set(data) - allowed)
        if unknown:
            paths = ", ".join(f"scheduler.{name}" for name in unknown)
            raise ValueError(f"unknown scheduler config field(s): {paths}")
        values = dict(data)
        for key in ("submit_command", "inspect_command", "status_command", "cancel_command"):
            if key in values:
                values[key] = _string_list(values[key], f"scheduler.{key}")
        if "modules" in values:
            values["modules"] = _string_list(values["modules"], "scheduler.modules")
        for key in ("node_count", "tasks", "tasks_per_node"):
            if key in values:
                values[key] = _require_int(values[key], f"scheduler.{key}")
        if "max_inflight" in values and values["max_inflight"] is not None:
            values["max_inflight"] = _require_int(
                values["max_inflight"], "scheduler.max_inflight"
            )
        if "nodes" in values:
            values["nodes"] = _string_list(values["nodes"], "scheduler.nodes", strip=True)
        for key in (
            "kind",
            "partition",
            "memory",
            "walltime",
            "script",
            "launcher",
            "executable",
            "module_init",
            "job_id_pattern",
        ):
            if key in values:
                values[key] = _require_string(values[key], f"scheduler.{key}")
        return cls(**values)

    def validate(self) -> None:
        if self.kind not in {"slurm", "custom"}:
            raise ValueError("scheduler.kind must be slurm or custom")
        if self.node_count <= 0:
            raise ValueError("scheduler.node_count must be positive")
        if self.tasks <= 0:
            raise ValueError("scheduler.tasks must be positive")
        if self.tasks_per_node <= 0:
            raise ValueError("scheduler.tasks_per_node must be positive")
        if self.tasks > self.node_count * self.tasks_per_node:
            raise ValueError("scheduler.tasks exceed scheduler node capacity")
        if self.max_inflight is not None and self.max_inflight <= 0:
            raise ValueError("scheduler.max_inflight must be positive when configured")
        normalized_nodes = [node.strip() for node in self.nodes]
        if any(not node for node in normalized_nodes):
            raise ValueError("scheduler.nodes must be non-empty")
        if len(set(normalized_nodes)) != len(normalized_nodes):
            raise ValueError("scheduler.nodes must not contain duplicates")
        if self.nodes and len(self.nodes) != self.node_count:
            raise ValueError("scheduler.nodes must match scheduler.node_count")
        if self.kind == "custom" and not self.submit_command:
            raise ValueError("custom scheduler requires scheduler.submit_command")


@dataclass
class KitConfig:
    config_version: int = 2
    profile: str = "vaspsol-sweep"
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KitConfig":
        data = _require_object(data, "config")
        unknown = sorted(set(data) - {"config_version", "profile", "workflow", "scheduler"})
        if unknown:
            raise ValueError(f"unknown kit config field(s): {', '.join(unknown)}")
        config_version = data.get("config_version", 2)
        config_version = _require_int(config_version, "config_version")
        profile = data.get("profile", "vaspsol-sweep")
        profile = _require_string(profile, "profile")
        workflow = data.get("workflow", {})
        scheduler = data.get("scheduler", {})
        return cls(
            config_version=config_version,
            profile=profile,
            workflow=WorkflowConfig.from_dict(workflow),
            scheduler=SchedulerConfig.from_dict(scheduler),
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
    source = deepcopy(_require_object(data, "config"))
    version = _require_int(source.get("config_version", 1), "config_version")

    if version == 2:
        config = KitConfig.from_dict(source)
        config.validate()
        return config.to_dict()
    if version not in {1, 2}:
        raise ValueError("config_version must be 1 or 2")
    if "workflow" not in source or "scheduler" not in source:
        raise ValueError("legacy PBS configuration cannot be migrated; select a SLURM profile")

    unknown = sorted(set(source) - {"config_version", "profile", "workflow", "scheduler"})
    if unknown:
        raise ValueError(f"unknown kit config field(s): {', '.join(unknown)}")
    workflow_v1 = dict(_require_object(source["workflow"], "workflow"))
    scheduler_v1 = dict(_require_object(source["scheduler"], "scheduler"))
    kind = _require_string(scheduler_v1.get("kind", "pbs"), "scheduler.kind")
    if kind == "pbs":
        raise ValueError("PBS configuration cannot be migrated; select a SLURM profile")

    unknown_scheduler = sorted(set(scheduler_v1) - _V1_SCHEDULER_FIELDS)
    if unknown_scheduler:
        paths = ", ".join(f"scheduler.{name}" for name in unknown_scheduler)
        raise ValueError(f"unknown scheduler config field(s): {paths}")

    workflow = {key: value for key, value in workflow_v1.items() if key not in _PBS_WORKFLOW_FIELDS}
    nodes = deepcopy(scheduler_v1.get("nodes", []))
    if "cores" in scheduler_v1:
        cores = _require_int(scheduler_v1["cores"], "scheduler.cores")
    elif "qsub_ppn" in workflow_v1:
        cores = _require_int(workflow_v1["qsub_ppn"], "workflow.qsub_ppn")
    else:
        cores = 96
    if "partition" in scheduler_v1:
        partition = scheduler_v1["partition"]
    elif "queue" in scheduler_v1:
        partition = _require_string(scheduler_v1["queue"], "scheduler.queue")
    else:
        partition = "compute"
    scheduler = {
        key: deepcopy(value)
        for key, value in scheduler_v1.items()
        if key in SchedulerConfig.__dataclass_fields__
    }
    scheduler.update(
        {
            "kind": kind,
            "partition": partition,
            "nodes": nodes,
            "node_count": scheduler_v1.get(
                "node_count", len(nodes) if isinstance(nodes, list) and nodes else 1
            ),
            "tasks": scheduler_v1.get("tasks", cores),
            "tasks_per_node": scheduler_v1.get("tasks_per_node", cores),
        }
    )
    if "walltime" not in scheduler_v1 and "qsub_walltime" in workflow_v1:
        scheduler["walltime"] = workflow_v1["qsub_walltime"]

    migrated = {
        "config_version": 2,
        "profile": source.get("profile", "vaspsol-sweep"),
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


def write_kit_config(
    path: Path,
    config: KitConfig,
    *,
    expected_current: Any = _NO_EXPECTED_BYTES,
) -> None:
    config.validate()
    target = Path(path)
    data = json.dumps(config.to_dict(), indent=2, sort_keys=True).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if expected_current is not _NO_EXPECTED_BYTES:
            actual = target.read_bytes() if target.exists() else None
            if actual != expected_current:
                raise RuntimeError(f"configuration changed before write: {target}")
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
