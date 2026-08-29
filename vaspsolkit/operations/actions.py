"""Immutable action previews for the workbench controller."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .models import WorkbenchSnapshot


ACTION_EFFECTS = {
    "refresh": "read-only",
    "fix-inputs": "read-only",
    "monitor": "read-only",
    "check-prepared": "file-changing",
    "check": "read-only",
    "init": "file-changing",
    "save-resources": "file-changing",
    "prepare-neutral": "file-changing",
    "repair-neutral-submit": "file-changing",
    "reconcile-neutral-submit": "file-changing",
    "clear-submit-barrier": "file-changing",
    "prepare-charge": "file-changing",
    "collect": "file-changing",
    "postprocess": "file-changing",
    "submit-neutral": "external",
    "submit-selected": "external",
    "reset-queued": "external",
}
_WALLTIME = re.compile(r"^(?P<hours>\d+):(?P<minutes>\d{2}):(?P<seconds>\d{2})$")
_NODE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class ResourceRequest:
    allocation: str
    partition: str
    nodes: Tuple[str, ...]
    tasks: int
    walltime: str
    script: str
    node_count: int = 1
    tasks_per_node: int = 96
    persist: bool = False

    @classmethod
    def create(
        cls,
        *,
        allocation: str,
        partition: str,
        nodes: Tuple[str, ...],
        tasks: int,
        walltime: str,
        script: str,
        node_count: int = 1,
        tasks_per_node: int = 96,
        persist: bool = False,
    ) -> "ResourceRequest":
        return cls(
            allocation=allocation,
            partition=partition, nodes=nodes, node_count=node_count,
            tasks=tasks, tasks_per_node=tasks_per_node,
            walltime=walltime,
            script=script,
            persist=persist,
        )

    def __post_init__(self) -> None:
        self._validate_types()
        normalized_nodes = []
        for node in self.nodes:
            normalized = node.strip()
            if not normalized:
                raise ValueError("nodes must contain non-empty names")
            if normalized not in normalized_nodes:
                normalized_nodes.append(normalized)
        object.__setattr__(self, "allocation", self.allocation.strip())
        object.__setattr__(self, "nodes", tuple(normalized_nodes))
        object.__setattr__(self, "partition", self.partition.strip())
        object.__setattr__(self, "walltime", self.walltime.strip())
        object.__setattr__(self, "script", self.script.strip())
        self.validate()

    def validate(self) -> None:
        self._validate_types()
        if self.allocation not in {"auto", "specified"}:
            raise ValueError("allocation must be 'auto' or 'specified'")
        if self.allocation == "specified" and not self.nodes:
            raise ValueError("specified allocation requires nodes")
        if self.nodes and len(self.nodes) != self.node_count:
            raise ValueError("explicit nodes must match node_count")
        if self.allocation == "auto" and self.nodes:
            raise ValueError("auto allocation must not specify nodes")
        if any(_NODE_NAME.fullmatch(node) is None for node in self.nodes):
            raise ValueError("node names may contain only letters, digits, dot, underscore, and hyphen")
        if not self.partition:
            raise ValueError("partition must be non-empty")
        if min(self.node_count, self.tasks, self.tasks_per_node) <= 0:
            raise ValueError("node and task counts must be positive")
        if self.tasks > self.node_count * self.tasks_per_node:
            raise ValueError("tasks exceed requested node capacity")
        if not self.script:
            raise ValueError("script must be non-empty")
        script_path = Path(self.script)
        if script_path.is_absolute() or ".." in script_path.parts:
            raise ValueError("script must be a relative path within the Case")
        match = _WALLTIME.fullmatch(self.walltime)
        if match is None or int(match["minutes"]) >= 60 or int(match["seconds"]) >= 60:
            raise ValueError("walltime must use H+:MM:SS with minutes and seconds below 60")

    def _validate_types(self) -> None:
        if not isinstance(self.allocation, str):
            raise TypeError("allocation must be a string")
        if not isinstance(self.nodes, tuple):
            raise TypeError("nodes must be a tuple")
        if any(not isinstance(node, str) for node in self.nodes):
            raise TypeError("nodes must contain strings")
        if not isinstance(self.partition, str):
            raise TypeError("partition must be a string")
        for name in ("node_count", "tasks", "tasks_per_node"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        if not isinstance(self.walltime, str):
            raise TypeError("walltime must be a string")
        if not isinstance(self.script, str):
            raise TypeError("script must be a string")
        if not isinstance(self.persist, bool):
            raise TypeError("persist must be a bool")


@dataclass(frozen=True)
class FileDiff:
    path: Path
    before: Optional[str]
    after: Optional[str]
    change_type: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            raise TypeError("path must be a Path")
        if not self.path.is_absolute() or self.path != Path(os.path.abspath(self.path)):
            raise ValueError("path must be a normalized absolute Path")
        if self.before is not None and not isinstance(self.before, str):
            raise TypeError("before must be a string or None")
        if self.after is not None and not isinstance(self.after, str):
            raise TypeError("after must be a string or None")
        if not isinstance(self.change_type, str):
            raise TypeError("change_type must be a string")
        if not self.change_type.strip():
            raise ValueError("change_type must be non-empty")


@dataclass(frozen=True)
class ArchiveChange:
    source: Path
    destination: Path
    operation: str
    entry_type: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        for name in ("source", "destination"):
            path = getattr(self, name)
            if not isinstance(path, Path) or not path.is_absolute():
                raise ValueError(f"{name} must be an absolute Path")
        if self.operation not in {"move", "copy"}:
            raise ValueError("archive operation must be move or copy")
        if self.entry_type not in {"file", "directory", "symlink"}:
            raise ValueError("archive entry_type must be file, directory, or symlink")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ValueError("archive size must be a non-negative integer")
        if not isinstance(self.sha256, str) or len(self.sha256) != 64:
            raise ValueError("archive sha256 must be a SHA-256 digest")
        try:
            int(self.sha256, 16)
        except ValueError as exc:
            raise ValueError("archive sha256 must be a SHA-256 digest") from exc


@dataclass(frozen=True)
class ActionPlan:
    action_id: str
    effect: str
    target_case: Path
    target_jobs: Tuple[str, ...]
    title: str
    reason: str
    file_diffs: Tuple[FileDiff, ...] = ()
    archive_changes: Tuple[ArchiveChange, ...] = ()
    scheduler_request: Optional[ResourceRequest] = None
    commands_summary: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    blocked_reason: str = ""

    def __post_init__(self) -> None:
        for name in (
            "target_jobs", "file_diffs", "archive_changes", "commands_summary", "warnings"
        ):
            value = getattr(self, name)
            if not isinstance(value, (list, tuple)):
                raise TypeError(f"{name} must be a list or tuple")
            object.__setattr__(self, name, tuple(value))
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.action_id, str) or self.action_id not in ACTION_EFFECTS:
            raise ValueError("action_id is not a supported action")
        if not isinstance(self.effect, str):
            raise TypeError("effect must be a string")
        if self.effect != ACTION_EFFECTS[self.action_id]:
            raise ValueError("effect does not match action_id")
        if not isinstance(self.target_case, Path) or self.target_case != self.target_case.resolve():
            raise ValueError("target_case must be a resolved Path")
        if any(not isinstance(job, str) or not job.strip() for job in self.target_jobs):
            raise ValueError("target_jobs must contain non-empty strings")
        if any(not isinstance(diff, FileDiff) for diff in self.file_diffs):
            raise TypeError("file_diffs must contain FileDiff values")
        for diff in self.file_diffs:
            try:
                diff.path.relative_to(self.target_case)
                diff.path.resolve().relative_to(self.target_case)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError("file diff path must remain within target Case") from exc
        if any(not isinstance(change, ArchiveChange) for change in self.archive_changes):
            raise TypeError("archive_changes must contain ArchiveChange values")
        for change in self.archive_changes:
            try:
                change.source.relative_to(self.target_case)
                change.destination.relative_to(self.target_case)
            except ValueError as exc:
                raise ValueError("archive changes must remain within target Case") from exc
        if self.scheduler_request is not None and not isinstance(
            self.scheduler_request, ResourceRequest
        ):
            raise TypeError("scheduler_request must be ResourceRequest or None")
        if any(
            not isinstance(command, str) or not command.strip()
            for command in self.commands_summary
        ):
            raise ValueError("commands_summary must contain non-empty strings")
        if any(
            not isinstance(warning, str) or not warning.strip() for warning in self.warnings
        ):
            raise ValueError("warnings must contain non-empty strings")
        if not isinstance(self.title, str) or not isinstance(self.reason, str):
            raise TypeError("title and reason must be strings")
        if not self.title.strip() or not self.reason.strip():
            raise ValueError("title and reason must be non-empty")
        if not isinstance(self.blocked_reason, str):
            raise TypeError("blocked_reason must be a string")


@dataclass(frozen=True)
class ActionError:
    """Stable bilingual failure details suitable for a persistent UI panel."""

    step: str
    summary: str
    command: str
    raw: str
    suggestion: str
    case_changed: bool = False
    summary_en: str = ""
    suggestion_en: str = ""

    def __post_init__(self) -> None:
        for name in (
            "step", "summary", "command", "raw", "suggestion",
            "summary_en", "suggestion_en",
        ):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be a string")
        for name in ("step", "summary", "command", "suggestion"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must be non-empty")
        if not isinstance(self.case_changed, bool):
            raise TypeError("case_changed must be a bool")

    @property
    def summary_zh(self) -> str:
        return self.summary

    @property
    def suggestion_zh(self) -> str:
        return self.suggestion


@dataclass(frozen=True)
class ActionResult:
    action_id: str
    status: str
    snapshot: Optional["WorkbenchSnapshot"] = None
    message: str = ""
    warnings: Tuple[str, ...] = ()
    ok: bool = True
    job_ids: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    error: Optional[ActionError] = None

    def __post_init__(self) -> None:
        if not isinstance(self.warnings, (list, tuple)):
            raise TypeError("warnings must be a list or tuple")
        object.__setattr__(self, "warnings", tuple(self.warnings))
        if any(not isinstance(item, str) or not item for item in self.warnings):
            raise ValueError("warnings must contain non-empty strings")
        if not isinstance(self.ok, bool):
            raise TypeError("ok must be a bool")
        if not isinstance(self.job_ids, Mapping):
            raise TypeError("job_ids must be a mapping")
        normalized_job_ids = dict(self.job_ids)
        if any(
            not isinstance(name, str) or not name
            or not isinstance(job_id, str) or not job_id
            for name, job_id in normalized_job_ids.items()
        ):
            raise ValueError("job_ids must map non-empty strings to non-empty strings")
        object.__setattr__(self, "job_ids", MappingProxyType(normalized_job_ids))
        if self.error is not None and not isinstance(self.error, ActionError):
            raise TypeError("error must be ActionError or None")
        if self.ok and self.error is not None:
            raise ValueError("successful results must not contain an error")
        if not self.ok and self.error is None:
            raise ValueError("failed results must contain an error")
