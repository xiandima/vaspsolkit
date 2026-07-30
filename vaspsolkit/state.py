from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


VALID_JOB_STATUSES = {
    "PREPARED",
    "SUBMITTED",
    "QUEUED",
    "RUNNING",
    "CONVERGED",
    "NEEDS_REVIEW",
    "FAILED",
    "BLOCKED",
    "UNKNOWN",
}
_HELD_STATE_LOCKS = threading.local()


@dataclass
class JobRecord:
    folder: str
    status: str = "PREPARED"
    job_id: str = ""
    restart_count: int = 0
    diagnostics: List[str] = field(default_factory=list)
    metadata: Dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        if self.status not in VALID_JOB_STATUSES:
            raise ValueError(f"invalid job status: {self.status}")
        if self.restart_count < 0:
            raise ValueError("restart_count must be non-negative")


@dataclass
class WorkflowState:
    stage: str = "setup"
    jobs: Dict[str, JobRecord] = field(default_factory=dict)
    neutral: Optional[JobRecord] = None
    prepared_checked: bool = False

    def save(self, path: Path) -> None:
        path = Path(path).expanduser().absolute()
        with workflow_state_lock(path):
            self._save_locked(path)

    def _save_locked(self, path: Path) -> None:
        for job in self.jobs.values():
            job.validate()
        if self.neutral is not None:
            self.neutral.validate()
        payload = {
            "stage": self.stage,
            "jobs": {name: asdict(job) for name, job in self.jobs.items()},
            "neutral": asdict(self.neutral) if self.neutral is not None else None,
            "prepared_checked": self.prepared_checked,
        }
        _require_safe_state_path(path)
        data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                _fsync_state(handle.fileno())
            _replace_state(temporary, path)
            if path.read_bytes() != data:
                raise OSError("atomic state replace did not install the complete payload")
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                _fsync_state(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    @classmethod
    def locked_update(cls, path: Path, update):
        """Run one read-modify-write transaction under the stable state lock."""
        path = Path(path).expanduser().absolute()
        with workflow_state_lock(path):
            _require_safe_state_path(path)
            current = cls.load(path)
            replacement = update(current)
            if not isinstance(replacement, cls):
                raise TypeError("state transaction must return WorkflowState")
            replacement._save_locked(path)
            return replacement

    @classmethod
    def load(cls, path: Path) -> "WorkflowState":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        neutral_data = data.get("neutral")
        state = cls(
            stage=str(data.get("stage", "setup")),
            jobs={name: JobRecord(**values) for name, values in data.get("jobs", {}).items()},
            neutral=JobRecord(**neutral_data) if neutral_data else None,
            prepared_checked=bool(data.get("prepared_checked", False)),
        )
        for job in state.jobs.values():
            job.validate()
        if state.neutral is not None:
            state.neutral.validate()
        return state


def _require_safe_state_path(path: Path) -> None:
    current = path.parent
    while True:
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"unsafe state parent path: {current}")
        if current.parent == current:
            break
        current = current.parent
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("state target must be a regular non-symlink file")


@contextmanager
def workflow_state_lock(path: Path):
    """Hold a stable 0600 advisory lock for every state-file writer."""
    requested = Path(path).expanduser().absolute()
    _require_safe_state_path(requested)
    try:
        canonical_parent = requested.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("state parent must already exist and be safe") from exc
    path = canonical_parent / requested.name
    _require_safe_state_path(path)
    key = str(path)
    held = getattr(_HELD_STATE_LOCKS, "paths", None)
    if held is None:
        held = _HELD_STATE_LOCKS.paths = {}
    if key in held:
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return

    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        opened = os.fstat(fd)
        linked = lock_path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or stat.S_ISLNK(linked.st_mode)
            or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            raise ValueError("state lock must be a stable regular file")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        linked = lock_path.lstat()
        if (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino):
            raise ValueError("state lock changed while acquiring it")
        held[key] = 1
        try:
            yield
        finally:
            del held[key]
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _replace_state(source: str, destination: Path) -> None:
    os.replace(source, destination)


def _fsync_state(fd: int) -> None:
    os.fsync(fd)
