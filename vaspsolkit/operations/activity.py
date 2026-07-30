from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
import fcntl
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Union


DEFAULT_STATE_ROOT = Path.home() / ".local" / "state" / "vaspsolkit" / "cases"
ACTIVITY_FILENAME = "activity.jsonl"
SUBMISSION_RECEIPT_FILENAME = "neutral-submission-recovery.json"
MAX_ACTIVITY_READ_BYTES = 1024 * 1024
MAX_ACTIVITY_LINE_BYTES = 64 * 1024
_ACTIVITY_READ_CHUNK_BYTES = 16 * 1024
PathLike = Union[str, os.PathLike]


def default_state_root() -> Path:
    explicit_root = os.environ.get("VASPSOLKIT_STATE_ROOT", "").strip()
    if explicit_root:
        root = Path(explicit_root)
    else:
        xdg_state_home = os.environ.get("XDG_STATE_HOME", "").strip()
        root = (
            Path(xdg_state_home) / "vaspsolkit" / "cases"
            if xdg_state_home
            else DEFAULT_STATE_ROOT
        )
    return Path(os.path.abspath(str(root.expanduser())))


@dataclass(frozen=True)
class ActivityRecord:
    timestamp: str
    action: str
    target: str
    result: str
    old_job_id: str = ""
    new_job_id: str = ""
    message: str = ""

    def __post_init__(self) -> None:
        for name in (
            "timestamp",
            "action",
            "target",
            "result",
            "old_job_id",
            "new_job_id",
            "message",
        ):
            if type(getattr(self, name)) is not str:
                raise TypeError(f"{name} must be a string")
        for name in ("timestamp", "action", "target", "result"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True)
class SubmissionReceipt:
    case_path: str
    case_device: int
    case_inode: int
    job_id: str
    command: str
    resources: dict
    timestamp: str
    state_before: dict = field(default_factory=dict)
    plan_fingerprint: str = ""
    script_fingerprint: str = ""
    owner_token: str = ""
    raw_output: str = ""
    version: int = 0
    case_mode: int = 0
    status: str = "ACCEPTED"

    def __post_init__(self) -> None:
        for name in ("case_path", "command", "timestamp", "status"):
            if type(getattr(self, name)) is not str or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")
        if self.status not in {"SUBMITTING", "ACCEPTED", "FAILED"}:
            raise ValueError("invalid submission receipt status")
        if type(self.job_id) is not str:
            raise TypeError("job_id must be a string")
        if self.status == "ACCEPTED" and not self.job_id:
            raise ValueError("ACCEPTED receipt requires job_id")
        if self.status != "ACCEPTED" and self.job_id:
            raise ValueError("only ACCEPTED receipt may contain job_id")
        for name in ("plan_fingerprint", "script_fingerprint", "owner_token", "raw_output"):
            value = getattr(self, name)
            if type(value) is not str:
                raise TypeError(f"{name} must be a string")
        if type(self.case_device) is not int or type(self.case_inode) is not int:
            raise TypeError("case identity must use integers")
        if type(self.version) is not int or self.version < 0:
            raise ValueError("receipt version must be a non-negative integer")
        if type(self.case_mode) is not int or self.case_mode < 0:
            raise ValueError("case_mode must be a non-negative integer")
        if type(self.resources) is not dict or type(self.state_before) is not dict:
            raise TypeError("resources and state_before must be dicts")
        object.__setattr__(self, "resources", dict(self.resources))
        object.__setattr__(self, "state_before", dict(self.state_before))


def new_submission_owner_token() -> str:
    return str(uuid.uuid4())


def claim_submission_receipt(
    case: PathLike,
    receipt: SubmissionReceipt,
    state_root: Optional[PathLike] = None,
) -> Path:
    if not receipt.owner_token:
        raise ValueError("submission claim requires owner_token")
    path = submission_receipt_path(case, state_root)
    _ensure_secure_directory(path.parent)
    with _submission_lock(path):
        payload = _receipt_bytes(receipt)
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(path.parent)
        except BaseException:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise
    return path


def update_submission_receipt(
    case: PathLike,
    receipt: SubmissionReceipt,
    owner_token: str,
    state_root: Optional[PathLike] = None,
    *,
    expected_version: int,
    expected_status: str,
) -> Path:
    path = submission_receipt_path(case, state_root)
    with _submission_lock(path):
        current = read_submission_receipt(case, state_root)
        _validate_receipt_cas(current, owner_token, expected_version, expected_status)
        if receipt.owner_token != owner_token:
            raise PermissionError("updated receipt owner token mismatch")
        if receipt.version != expected_version + 1:
            raise RuntimeError("updated receipt must increment version exactly once")
        return _replace_submission_receipt(case, receipt, state_root)


def submission_receipt_path(
    case: PathLike, state_root: Optional[PathLike] = None
) -> Path:
    return _case_state_dir(case, state_root) / SUBMISSION_RECEIPT_FILENAME


def _replace_submission_receipt(
    case: PathLike,
    receipt: SubmissionReceipt,
    state_root: Optional[PathLike] = None,
) -> Path:
    if type(receipt) is not SubmissionReceipt:
        raise TypeError("receipt must be SubmissionReceipt")
    path = submission_receipt_path(case, state_root)
    _ensure_secure_directory(path.parent)
    payload = _receipt_bytes(receipt)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return path


def read_submission_receipt(
    case: PathLike, state_root: Optional[PathLike] = None
) -> Optional[SubmissionReceipt]:
    path = submission_receipt_path(case, state_root)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("submission receipt must be a regular non-symlink file")
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if type(data) is not dict:
        raise ValueError("submission receipt must contain an object")
    return SubmissionReceipt(**data)


def clear_submission_receipt(
    case: PathLike, state_root: Optional[PathLike] = None, owner_token: str = "",
    *, expected_version: int, expected_status: str,
) -> None:
    path = submission_receipt_path(case, state_root)
    with _submission_lock(path):
        current = read_submission_receipt(case, state_root)
        _validate_receipt_cas(current, owner_token, expected_version, expected_status)
        path.unlink()
        _fsync_directory(path.parent)


def _receipt_bytes(receipt: SubmissionReceipt) -> bytes:
    return (json.dumps(asdict(receipt), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _validate_receipt_cas(
    current: Optional[SubmissionReceipt], owner_token: str,
    expected_version: int, expected_status: str,
) -> None:
    if current is None:
        raise RuntimeError("submission receipt is missing")
    if not owner_token or current.owner_token != owner_token:
        raise PermissionError("submission receipt owner token mismatch")
    if current.version != expected_version or current.status != expected_status:
        raise RuntimeError("stale submission receipt version or status")


@contextmanager
def _submission_lock(receipt_path: Path):
    lock_path = receipt_path.with_name(receipt_path.name + ".lock")
    _ensure_secure_directory(lock_path.parent)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(lock_path, flags, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        os.close(fd)
        raise PermissionError("unsafe submission lock file")
    os.fchmod(fd, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _ensure_secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = path
    while True:
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise PermissionError(f"unsafe state directory component: {current}")
        if current.parent == current:
            break
        current = current.parent
    info = path.lstat()
    if info.st_uid != os.geteuid():
        raise PermissionError(f"unsafe state directory owner: {path}")
    os.chmod(path, 0o700)


def append_activity(
    case: PathLike,
    record: ActivityRecord,
    state_root: Optional[PathLike] = None,
) -> Path:
    if type(record) is not ActivityRecord:
        raise TypeError("record must be ActivityRecord")
    path = _activity_path(case, state_root)
    line = (json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if len(line) > MAX_ACTIVITY_LINE_BYTES:
        raise ValueError(
            f"activity record exceeds {MAX_ACTIVITY_LINE_BYTES} serialized bytes"
        )
    _ensure_secure_directory(path.parent)
    fd = os.open(
        path,
        os.O_WRONLY | os.O_APPEND | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        os.close(fd)
        raise PermissionError("unsafe activity log file")
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "ab") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def write_error_log(
    case: PathLike,
    text: str,
    state_root: Optional[PathLike] = None,
) -> Path:
    if not isinstance(text, str):
        raise TypeError("error log text must be a string")
    directory = _case_state_dir(case, state_root) / "logs"
    _ensure_secure_directory(directory)
    path = directory / f"error-{uuid.uuid4()}.log"
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(directory)
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    return path


def read_activities(
    case: PathLike,
    state_root: Optional[PathLike] = None,
    limit: int = 20,
) -> tuple[ActivityRecord, ...]:
    if type(limit) is not int:
        raise TypeError("limit must be an integer")
    if limit <= 0:
        raise ValueError("limit must be greater than zero")
    path = _activity_path(case, state_root)
    try:
        return _read_activity_tail(path, limit)
    except OSError:
        return ()


def _read_activity_tail(path: Path, limit: int) -> tuple[ActivityRecord, ...]:
    records = []
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        budget = min(position, MAX_ACTIVITY_READ_BYTES)
        partial = b""
        discarding_overlong = False

        while position > 0 and budget > 0 and len(records) < limit:
            read_size = min(_ACTIVITY_READ_CHUNK_BYTES, position, budget)
            position -= read_size
            budget -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)

            if discarding_overlong:
                boundary = chunk.rfind(b"\n")
                if boundary < 0:
                    continue
                chunk = chunk[: boundary + 1]
                discarding_overlong = False

            pieces = (chunk + partial).split(b"\n")
            partial = pieces[0]
            if len(partial) > MAX_ACTIVITY_LINE_BYTES:
                partial = b""
                discarding_overlong = True

            _collect_records(reversed(pieces[1:]), records, limit)

        if position == 0 and not discarding_overlong and len(records) < limit:
            _collect_records((partial,), records, limit)
    return tuple(records)


def _collect_records(
    lines: Iterable[bytes],
    records: list[ActivityRecord],
    limit: int,
) -> None:
    for line in lines:
        if len(records) == limit:
            return
        record = _parse_activity_line(line)
        if record is not None:
            records.append(record)


def _parse_activity_line(line: bytes) -> Optional[ActivityRecord]:
    if not line or len(line) > MAX_ACTIVITY_LINE_BYTES:
        return None
    try:
        data = json.loads(line.decode("utf-8"))
        if type(data) is not dict:
            return None
        return ActivityRecord(
            timestamp=data["timestamp"],
            action=data["action"],
            target=data["target"],
            result=data["result"],
            old_job_id=data.get("old_job_id", ""),
            new_job_id=data.get("new_job_id", ""),
            message=data.get("message", ""),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _activity_path(case: PathLike, state_root: Optional[PathLike]) -> Path:
    return _case_state_dir(case, state_root) / ACTIVITY_FILENAME


def _case_state_dir(case: PathLike, state_root: Optional[PathLike]) -> Path:
    root = (
        Path(os.path.abspath(str(Path(state_root).expanduser())))
        if state_root is not None
        else default_state_root()
    )
    canonical_case_path = Path(case).resolve()
    try:
        root.relative_to(canonical_case_path)
    except ValueError:
        pass
    else:
        raise ValueError("activity state root must remain outside the Case")
    canonical_case = str(canonical_case_path)
    case_id = hashlib.sha256(canonical_case.encode()).hexdigest()[:16]
    return root / case_id
