from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .reference_settings import validate_she_reference


DEFAULT_FOLDERS = ["1", "2", "3", "4", "5"]
DEFAULT_OFFSETS = [-1.0, -0.5, 0.0, 0.5, 1.0]
DEFAULT_COPY_FILES = ["INCAR", "POTCAR", "KPOINTS", "CHGCAR", "vasp.pbs"]


@dataclass
class WorkflowConfig:
    pbs_file: str = "vasp.pbs"
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
    qsub_queue: str = ""
    qsub_ppn: int = 48
    qsub_min_node: int = 0
    qsub_walltime: str = "48:00:00"
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
        if "qsub_queue" in kwargs:
            kwargs["qsub_queue"] = str(kwargs["qsub_queue"])
        if "qsub_ppn" in kwargs:
            kwargs["qsub_ppn"] = int(kwargs["qsub_ppn"])
        if "qsub_min_node" in kwargs:
            kwargs["qsub_min_node"] = int(kwargs["qsub_min_node"])
        if "qsub_walltime" in kwargs:
            kwargs["qsub_walltime"] = str(kwargs["qsub_walltime"])
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
        if self.qsub_ppn <= 0:
            raise ValueError("qsub_ppn must be positive")
        if self.qsub_min_node < 0:
            raise ValueError("qsub_min_node must be non-negative")
        if len(self.folders) != len(self.nelect_offsets):
            raise ValueError("folders and nelect_offsets must have the same length")
        if require_neutral and sum(abs(value) <= 1.0e-12 for value in self.nelect_offsets) != 1:
            raise ValueError("charge sweep must contain exactly one neutral point")


@dataclass
class SchedulerConfig:
    kind: str = "pbs"
    queue: str = ""
    cores: int = 48
    memory: str = ""
    walltime: str = "48:00:00"
    max_inflight: Optional[int] = None
    script: str = "vasp.pbs"
    nodes: List[str] = field(default_factory=list)
    submit_command: List[str] = field(default_factory=list)
    inspect_command: List[str] = field(default_factory=list)
    status_command: List[str] = field(default_factory=list)
    cancel_command: List[str] = field(default_factory=list)
    job_id_pattern: str = r"(?P<job_id>\S+)"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SchedulerConfig":
        values = dict(data)
        for key in ("submit_command", "inspect_command", "status_command", "cancel_command"):
            if key in values:
                values[key] = [str(item) for item in values[key]]
        for key in ("cores",):
            if key in values:
                values[key] = int(values[key])
        if "max_inflight" in values and values["max_inflight"] is not None:
            values["max_inflight"] = int(values["max_inflight"])
        if "nodes" in values:
            values["nodes"] = [str(item).strip() for item in values["nodes"] if str(item).strip()]
        return cls(**values)

    def validate(self) -> None:
        if self.kind not in {"pbs", "slurm", "custom"}:
            raise ValueError("scheduler kind must be pbs, slurm, or custom")
        if self.cores <= 0:
            raise ValueError("scheduler cores must be positive")
        if self.max_inflight is not None and self.max_inflight <= 0:
            raise ValueError("scheduler max_inflight must be positive when configured")
        if len(set(self.nodes)) != len(self.nodes):
            raise ValueError("scheduler nodes must not contain duplicates")
        if any(not node.strip() for node in self.nodes):
            raise ValueError("scheduler nodes must be non-empty")
        if self.kind == "custom" and not self.submit_command:
            raise ValueError("custom scheduler requires submit_command")


@dataclass
class KitConfig:
    config_version: int = 1
    profile: str = "vaspsol-sweep"
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KitConfig":
        if "workflow" not in data and "scheduler" not in data:
            workflow = WorkflowConfig.from_dict(data)
            scheduler = SchedulerConfig(
                kind="pbs",
                queue=workflow.qsub_queue,
                cores=workflow.qsub_ppn,
                walltime=workflow.qsub_walltime,
                script=workflow.pbs_file,
            )
            return cls(workflow=workflow, scheduler=scheduler)
        unknown = sorted(set(data) - {"config_version", "profile", "workflow", "scheduler"})
        if unknown:
            raise ValueError(f"unknown kit config field(s): {', '.join(unknown)}")
        return cls(
            config_version=int(data.get("config_version", 1)),
            profile=str(data.get("profile", "vaspsol-sweep")),
            workflow=WorkflowConfig.from_dict(dict(data.get("workflow", {}))),
            scheduler=SchedulerConfig.from_dict(dict(data.get("scheduler", {}))),
        )

    def validate(self) -> None:
        if self.config_version != 1:
            raise ValueError(f"unsupported config_version: {self.config_version}")
        self.workflow.validate(require_neutral=self.profile == "vaspsol-sweep")
        self.scheduler.validate()

    def to_dict(self) -> Dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)

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
        config = KitConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))
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
